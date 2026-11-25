"""Database schedules share the jobs outbox with founder standing tasks."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from core.clock import as_utc, utcnow
from app.core.jobs import enqueue, publish_wake
from app.core.local_mode import is_local
from app.state import db
from app.worker.catalog import load_trigger_policy, stage2_kinds, tick_kinds

# The policy continues to choose runnable kinds, including the empty 30m tick.
INTERVALS = {"maintenance": 60, "5m": 300, "15m": 900, "30m": 1800,
             "hourly": 3600, "6h": 21600, "daily": 86400, "weekly": 604800}


def _window(tick: str, now: datetime) -> str:
    if tick == "5m":
        bucket = (now.minute // 5) * 5
        return now.strftime("%Y%m%dT%H") + f"{bucket:02d}"
    if tick == "15m":
        bucket = (now.minute // 15) * 15
        return now.strftime("%Y%m%dT%H") + f"{bucket:02d}"
    if tick == "30m":
        bucket = 0 if now.minute < 30 else 30
        return now.strftime("%Y%m%dT%H") + f"{bucket:02d}"
    if tick == "hourly":
        return now.strftime("%Y%m%dT%H")
    if tick == "6h":
        return now.strftime("%Y%m%dT") + f"{(now.hour // 6) * 6:02d}"
    if tick == "weekly":
        return now.strftime("%Y-W%W")
    return now.strftime("%Y%m%d")


def dispatch_tick(tick: str, *, session=None, now: datetime | None = None, all_workers: bool = False) -> dict:
    """Enqueue in the caller's transaction, or commit a standalone signed tick."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    window = _window(tick, moment)
    if all_workers:
        # A specialist can lead the shared scheduler. Its lease filter must not
        # consume a global interval without enqueueing the general worker's jobs.
        policy = (load_trigger_policy().get("ticks") or {}).get(tick) or {}
        supported = stage2_kinds()
        kinds = [kind for kind in policy.get("stage2_kinds", []) if kind in supported]
    else:
        kinds = tick_kinds(tick)
    local = is_local()
    if local:
        from app.core.local_worker import tick_kinds as local_tick_kinds

        kinds = local_tick_kinds(tick)
    if not local and "inbox.briefing.tick" in kinds:
        from app.inbox.store import enabled as inbox_enabled
        if not inbox_enabled(session):
            kinds = [kind for kind in kinds if kind != "inbox.briefing.tick"]
    if not local and "inbox.cos_brief.tick" in kinds:
        from app.inbox.weekly import enabled as cos_brief_enabled
        if not cos_brief_enabled(session):
            kinds = [kind for kind in kinds if kind != "inbox.cos_brief.tick"]
    ids: list[str] = []
    def stage(session):
        for kind in kinds:
            ids.append(
                enqueue(
                    session,
                    kind,
                    {"tick": tick, "window": window},
                    dedupe_key=f"{kind}:{window}",
                    priority=10,
                )
            )
    if session is not None:
        stage(session)
    else:
        with db()() as own_session:
            stage(own_session)
            own_session.commit()
        publish_wake()
    return {"tick": tick, "window": window, "enqueued": kinds, "ids": ids}


def _boundary(moment: datetime, seconds: int) -> datetime:
    # Weekly windows start Monday, matching internal_tick._window's %W.
    epoch = datetime(1970, 1, 5) if seconds == 604800 else datetime(1970, 1, 1)
    return epoch + timedelta(seconds=((moment - epoch).total_seconds() // seconds) * seconds)


def run_due(session, *, now: datetime | None = None) -> dict:
    """Claim and advance due schedules atomically with their outbox jobs.

    The caller commits before publishing wake or running the safety sweep.
    Safety maintenance must stay outside execution slots and nested DB sessions.
    """
    from app.db.session import serialize_writes
    from app.db.task_models import MaintenanceSchedule

    moment = as_utc(now or utcnow()).replace(tzinfo=None)
    result = {"ticks": [], "ids": [], "maintenance_due": False, "tasks": []}
    if session.get_bind().dialect.name == "postgresql":
        leader = session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                                {"key": "task-maintenance-scheduler"})
        if not leader:
            return result
    else:
        serialize_writes(session, key="task-maintenance-scheduler")
    for tick, seconds in INTERVALS.items():
        if session.get(MaintenanceSchedule, tick) is None:
            session.add(MaintenanceSchedule(tick=tick, interval_seconds=seconds,
                        next_fire_at=_boundary(moment, seconds), updated_at=moment))
    session.flush()
    rows = session.scalars(select(MaintenanceSchedule).where(
        MaintenanceSchedule.next_fire_at <= moment,
        MaintenanceSchedule.tick.in_(INTERVALS),
    ).order_by(MaintenanceSchedule.next_fire_at, MaintenanceSchedule.tick)
      .with_for_update(skip_locked=True)).all()
    for row in rows:
        seconds = row.interval_seconds
        if seconds <= 0:
            raise ValueError("maintenance_interval_invalid")
        due = as_utc(row.next_fire_at).replace(tzinfo=None)
        # ponytail: coalesce missed maintenance windows, avoiding a provider storm after downtime.
        latest = due + timedelta(seconds=((moment - due).total_seconds() // seconds) * seconds)
        if row.tick == "maintenance":
            result["maintenance_due"] = True
        else:
            tick = dispatch_tick(row.tick, session=session, now=latest, all_workers=True)
            result["ticks"].append({"tick": row.tick, "window": tick["window"], "enqueued": tick["enqueued"]})
            result["ids"].extend(tick["ids"])
        row.next_fire_at = latest + timedelta(seconds=seconds)
        row.updated_at = moment
    return result
