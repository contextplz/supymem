"""Durable live status for roster agents."""
from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import select

from app.db.models import AgentInstance, AgentLiveStatus
from app.state import db
from core.clock import utcnow

HEARTBEAT_TIMEOUT_SECONDS = 90


def _ensure_instance(session, agent_id: str, venture_id: str) -> None:
    """Pack agents are named people before compose hires them. Heartbeat still needs a row."""
    if session.get(AgentInstance, agent_id) is not None:
        return
    session.add(
        AgentInstance(
            id=agent_id,
            venture_id=venture_id,
            spec_id=agent_id,
            spec_version="pack",
            status="ready",
            created_at=utcnow(),
        )
    )


def _upsert(agent_id: str, venture_id: str, state: str, task: str | None = None) -> dict[str, object]:
    now = utcnow()
    factory = db()
    with factory() as session:
        _ensure_instance(session, agent_id, venture_id)
        row = session.get(AgentLiveStatus, agent_id)
        if row is None:
            row = AgentLiveStatus(
                agent_id=agent_id,
                venture_id=venture_id,
                state=state,
                current_task_desc=task,
                last_heartbeat=now,
                updated_at=now,
            )
            session.add(row)
        else:
            if row.venture_id != venture_id:
                raise ValueError("agent does not belong to venture")
            row.state = state
            row.current_task_desc = task
            row.last_heartbeat = now
            row.updated_at = now
        session.commit()
        return _serialize(row)


def _serialize(row: AgentLiveStatus) -> dict[str, object]:
    now = utcnow()
    last_heartbeat = row.last_heartbeat
    if last_heartbeat is not None and last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
    is_dead = (
        row.state == "working"
        and (
            last_heartbeat is None
            or now - last_heartbeat > timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
        )
    )
    return {
        "agent_id": row.agent_id,
        "venture_id": row.venture_id,
        "state": "blocked" if is_dead else row.state,
        "current_task_desc": row.current_task_desc,
        "last_heartbeat": last_heartbeat.isoformat() if last_heartbeat else None,
        "blocked_reason": "no heartbeat" if is_dead else None,
    }


def mark_working(agent_id: str, venture_id: str, current_task_desc: str) -> dict[str, object]:
    if not current_task_desc.strip():
        raise ValueError("current_task_desc is required while working")
    from app.ventures.lifecycle import is_paused

    if is_paused(venture_id):
        return mark_idle(agent_id, venture_id)
    return _upsert(agent_id, venture_id, "working", current_task_desc)


def mark_idle(agent_id: str, venture_id: str) -> dict[str, object]:
    return _upsert(agent_id, venture_id, "idle")


def mark_all_idle(
    venture_id: str, extra_agent_ids: list[str] | None = None
) -> int:
    """Set every live agent idle. Returns how many were actually working."""
    from app.db.models import AgentInstance

    live = list_live_status(venture_id)
    halted = 0
    seen: set[str] = set()
    for row in live:
        aid = str(row.get("agent_id") or "")
        if not aid:
            continue
        seen.add(aid)
        if row.get("state") == "working":
            halted += 1
        mark_idle(aid, venture_id)

    extras = [str(a).strip() for a in (extra_agent_ids or []) if str(a).strip()]
    if not extras:
        return halted

    factory = db()
    with factory() as session:
        known = {
            row.id
            for row in session.scalars(
                select(AgentInstance).where(AgentInstance.venture_id == venture_id)
            ).all()
        }
        live_ids = {
            row.agent_id
            for row in session.scalars(
                select(AgentLiveStatus).where(AgentLiveStatus.venture_id == venture_id)
            ).all()
        }
    for aid in extras:
        if aid in seen:
            continue
        if aid not in known and aid not in live_ids:
            # AgentLiveStatus.agent_id FKs to agent_instances — skip ghosts.
            continue
        mark_idle(aid, venture_id)
        seen.add(aid)
    return halted


def heartbeat(agent_id: str, venture_id: str) -> dict[str, object]:
    factory = db()
    with factory() as session:
        row = session.get(AgentLiveStatus, agent_id)
        if row is None:
            raise KeyError(f"live status does not exist for {agent_id}")
        if row.venture_id != venture_id:
            raise ValueError("agent does not belong to venture")
        row.last_heartbeat = utcnow()
        row.updated_at = row.last_heartbeat
        session.commit()
        return _serialize(row)


def list_live_status(venture_id: str) -> list[dict[str, object]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(AgentLiveStatus).where(AgentLiveStatus.venture_id == venture_id)
        ).all()
        return [_serialize(row) for row in rows]
