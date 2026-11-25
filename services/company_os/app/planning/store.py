"""Persist planned tasks + fingerprints."""

from __future__ import annotations

from contextlib import nullcontext
from itertools import islice
import json
import re

from sqlalchemy import or_, select

from app.db.models import PlannedTask, TaskFingerprint
from app.db.session import serialize_writes
from app.db.supymem_models import ExecutionPlan, PlanTask
from app.planning.dedupe import fingerprint_title
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def save_tasks(
    venture_id: str,
    tasks: list[dict],
    *,
    source: str = "planner",
    session=None,
    publish: bool = True,
) -> list[dict]:
    factory = db()
    now = utcnow()
    saved: list[dict] = []
    own_session = session is None
    if not own_session and publish:
        raise ValueError("cannot_publish_uncommitted_tasks")
    with (factory() if own_session else nullcontext(session)) as session:
        dismissed = {tag for row in session.scalars(select(PlannedTask).where(
            PlannedTask.venture_id == venture_id, PlannedTask.source == source,
            PlannedTask.status == "dismissed")) for tag in json.loads(row.tags_json or "[]")
            if isinstance(tag, str) and tag.startswith("plan_task:")}
        # Clear prior open planner tasks for a clean handoff set
        for old in session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
                or_(
                    PlannedTask.source.in_(
                        [source, "planner", "replan", "plan", "override", "handoff"]
                    ),
                    PlannedTask.source.like("approved_plan:%"),
                ),
            )
        ).all():
            old.status = "superseded"
            old.updated_at = now
        # Fingerprints are unique per venture — supersede prior rows so replan
        # can recreate the same titles without UNIQUE failures.
        for old_fp in session.scalars(
            select(TaskFingerprint).where(
                TaskFingerprint.venture_id == venture_id,
                TaskFingerprint.status == "open",
            )
        ).all():
            old_fp.status = "superseded"
        session.flush()
        for i, t in enumerate(tasks[:5]):
            if dismissed.intersection(t.get("tags") or []):
                continue
            fp = t.get("fingerprint") or fingerprint_title(t["title"])
            tid = new_id("tsk")
            tags = list(t.get("tags") or [])
            owner = t.get("owner_name") or t.get("owner")
            if not any(isinstance(tag, str) and tag.startswith("assignee:") for tag in tags):
                if not owner:
                    try:
                        from app.org.characters import encode_assignee, owner_for_work
                        from app.org.summary import resolve_org

                        agent = owner_for_work(
                            t["title"], tags, (resolve_org(venture_id).get("agents") or [])
                        )
                        encoded = encode_assignee(agent)
                        if encoded:
                            tags.append(encoded)
                            t = {
                                **t,
                                "owner_name": (agent or {}).get("name"),
                                "owner_role": (agent or {}).get("role"),
                                "owner_id": (agent or {}).get("id"),
                            }
                    except Exception:  # noqa: BLE001
                        pass
                elif owner:
                    from app.org.characters import encode_assignee

                    tags.append(
                        encode_assignee(
                            {
                                "name": t.get("owner_name") or owner,
                                "role": t.get("owner_role") or "",
                                "id": t.get("owner_id") or "",
                            }
                        )
                        or f"assignee:{owner}::"
                    )
            row = PlannedTask(
                id=tid,
                venture_id=venture_id,
                title=t["title"],
                detail=t.get("detail"),
                why_now=t["why_now"],
                tags_json=json.dumps(tags),
                est_minutes=int(t.get("est_minutes") or 30),
                runnable=1 if t.get("runnable", True) else 0,
                source=source,
                status="open",
                rank=int(t.get("rank", i)),
                fingerprint=fp,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            existing_fp = session.scalars(
                select(TaskFingerprint).where(
                    TaskFingerprint.venture_id == venture_id,
                    TaskFingerprint.fingerprint == fp,
                )
            ).first()
            if existing_fp:
                existing_fp.task_id = tid
                existing_fp.status = "open"
            else:
                session.add(
                    TaskFingerprint(
                        id=new_id("tfp"),
                        venture_id=venture_id,
                        fingerprint=fp,
                        task_id=tid,
                        status="open",
                        created_at=now,
                    )
                )
            saved.append(
                {
                    "id": tid,
                    "title": row.title,
                    "why_now": row.why_now,
                    "est_minutes": row.est_minutes,
                    "tags": [x for x in tags if not (isinstance(x, str) and x.startswith("assignee:"))],
                    "runnable": bool(row.runnable),
                    "rank": row.rank,
                    "owner_name": t.get("owner_name"),
                    "owner_role": t.get("owner_role"),
                    "owner_id": t.get("owner_id"),
                }
            )
        if own_session:
            session.commit()
    if publish:
        publish_saved_tasks(venture_id, saved)
    return saved


def publish_saved_tasks(venture_id: str, saved: list[dict]) -> None:
    """Publish committed tasks to the optional local coordination ledger."""
    try:
        from app.munder.ledger import add_task as hive_add

        for row in saved:
            hive_add(
                venture_id,
                {
                    "id": row["id"],
                    "title": row["title"],
                    "status": "todo",
                    "dependsOn": list(row.get("depends_on") or row.get("dependsOn") or []),
                    "assignee": row.get("owner_name"),
                },
            )
    except Exception:  # noqa: BLE001
        pass


def _serialize_task(row: PlannedTask) -> dict:
    tags = json.loads(row.tags_json or "[]")
    from app.org.characters import decode_assignee, strip_assignee_tags

    owner = decode_assignee(tags)
    return {
        "id": row.id,
        "title": row.detail if row.source == "startup_priorities" and row.detail else row.title,
        "detail": row.detail,
        "why_now": row.why_now,
        "est_minutes": row.est_minutes,
        "tags": strip_assignee_tags(tags),
        "runnable": bool(row.runnable),
        "status": row.status,
        "rank": row.rank,
        "source": row.source,
        "last_error": row.last_error,
        "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
        **owner,
    }


def get_task(venture_id: str, task_id: str) -> dict | None:
    factory = db()
    with factory() as session:
        row = session.get(PlannedTask, task_id)
        if not row or row.venture_id != venture_id:
            return None
        return _serialize_task(row)


_TITLE_PREFIXES = (
    "just finished ",
    "working on ",
    "finished ",
    "next up: ",
    "next up ",
)


def clean_task_title(title: str) -> str:
    text = (title or "").strip()
    low = text.lower()
    for prefix in _TITLE_PREFIXES:
        if low.startswith(prefix):
            return text[len(prefix) :].strip(" .")
    return text


def find_task_by_title(venture_id: str, title: str) -> dict | None:
    """Match a one-line / heartbeat title to a planned task."""
    needle = clean_task_title(title)
    if not needle:
        return None
    rows = list_tasks(
        venture_id,
        statuses=("running", "open", "done", "dismissed", "superseded"),
    )
    low = needle.lower()
    exact = [row for row in rows if (row.get("title") or "").strip().lower() == low]
    if exact:
        return _prefer_live(exact)
    contained = [
        row
        for row in rows
        if low in (row.get("title") or "").lower() or (row.get("title") or "").lower() in low
    ]
    if contained:
        return _prefer_live(contained)
    return None


def _prefer_live(rows: list[dict]) -> dict:
    rank = {"running": 0, "open": 1, "done": 2, "dismissed": 3, "superseded": 4}
    return sorted(rows, key=lambda row: rank.get(str(row.get("status") or ""), 9))[0]


def list_tasks(venture_id: str, *, statuses: tuple[str, ...] = ("open",), limit: int | None = None) -> list[dict]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(PlannedTask)
            .where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status.in_(statuses),
            )
            .order_by(PlannedTask.rank.asc(), PlannedTask.updated_at.desc())
            .limit(limit)
        ).all()
        return [_serialize_task(r) for r in rows]


def current_plan_rows(session, tasks):
    """Hide obsolete plan projections without changing their saved history.

    Only explicit plan/task IDs are authoritative here. A public site, title,
    or task update timestamp cannot prove that an arbitrary task is complete.
    """
    remaining = iter(tasks)
    while batch := list(islice(remaining, 100)):
        plan_ids = {task.source.removeprefix("approved_plan:") for task in batch if task.source.startswith("approved_plan:")}
        plans = {row.id: row for row in session.scalars(select(ExecutionPlan).where(ExecutionPlan.id.in_(plan_ids)))} if plan_ids else {}
        candidates, task_ids = [], set()
        for task in batch:
            plan = plans.get(task.source.removeprefix("approved_plan:")) if task.source.startswith("approved_plan:") else None
            target = None
            if plan and plan.venture_id == task.venture_id:
                if plan.superseded_by:
                    continue
                try:
                    tags = json.loads(task.tags_json or "[]")
                    ids = [tag.removeprefix("plan_task:") for tag in tags
                           if isinstance(tag, str) and tag.startswith("plan_task:")]
                except (ValueError, TypeError):
                    ids = []
                if len(ids) == 1:
                    target = ids[0]
                    task_ids.add(target)
            candidates.append((task, target))
        done = set(session.execute(select(PlanTask.id, PlanTask.venture_id).where(
            PlanTask.id.in_(task_ids), PlanTask.state == "done"))) if task_ids else set()
        yield from (task for task, target in candidates if (target, task.venture_id) not in done)


def current_plan_work(session, task: PlannedTask) -> bool:
    return next(current_plan_rows(session, (task,)), None) is not None


def list_open_tasks(venture_id: str, *, limit: int | None = None) -> list[dict]:
    return list_tasks(venture_id, statuses=("open",), limit=limit)


def list_current_tasks(venture_id: str, *, statuses: tuple[str, ...] = ("open",)) -> list[dict]:
    """Read model for founder surfaces; retain raw task history in storage."""
    with db()() as session:
        rows = session.scalars(select(PlannedTask).where(
            PlannedTask.venture_id == venture_id,
            PlannedTask.status.in_(statuses),
        ).order_by(PlannedTask.rank.asc(), PlannedTask.updated_at.desc())).yield_per(100)
        return [_serialize_task(row) for row in current_plan_rows(session, rows)]


def next_runnable_task(venture_id: str) -> dict | None:
    """First open, runnable task — used by autopilot."""
    for row in list_tasks(venture_id, statuses=("open",)):
        if row.get("runnable", True):
            return row
    return None


def has_running_task(venture_id: str) -> bool:
    return bool(list_tasks(venture_id, statuses=("running",)))


def has_any_tasks(venture_id: str) -> bool:
    """True once this venture has ever had a planned-task row."""
    return bool(
        list_tasks(
            venture_id,
            statuses=("open", "running", "done", "dismissed", "superseded"),
        )
    )


def enqueue_task(venture_id: str, task: dict, *, source: str = "chat") -> dict:
    """Insert one open task at the front. Does not wipe the existing slate."""
    factory = db()
    now = utcnow()
    with factory() as session:
        serialize_writes(session, key="planning-enqueue:" + venture_id)
        fp = task.get("fingerprint") or fingerprint_title(task["title"])
        existing = session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.fingerprint == fp,
                PlannedTask.status.in_(("open", "running")),
            )
            .order_by((PlannedTask.status == "running").desc(),
                PlannedTask.last_error.startswith("needs_you:review_draft_").desc().nulls_last())
            .with_for_update()
        ).first()
        if existing:
            if (source == "chat" and task.get("runnable", True) and existing.status == "open"
                    and existing.last_error == "incomplete:review_draft_request_unverified"):
                existing.runnable, existing.last_error, existing.updated_at = 1, None, now
                session.commit()
            return _serialize_task(existing)
        for old in session.scalars(
            select(PlannedTask).where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
            )
        ).all():
            old.rank = int(old.rank or 0) + 1
            old.updated_at = now
        tags = list(task.get("tags") or [])
        if not any(isinstance(tag, str) and tag.startswith("assignee:") for tag in tags):
            try:
                from app.org.characters import encode_assignee, owner_for_work
                from app.org.summary import resolve_org

                agent = None
                if task.get("owner_name") or task.get("owner_id"):
                    agent = {
                        "name": task.get("owner_name"),
                        "role": task.get("owner_role") or "",
                        "id": task.get("owner_id") or "",
                    }
                else:
                    agent = owner_for_work(
                        task["title"], tags, (resolve_org(venture_id).get("agents") or [])
                    )
                encoded = encode_assignee(agent)
                if encoded:
                    tags.append(encoded)
                    task = {
                        **task,
                        "owner_name": (agent or {}).get("name"),
                        "owner_role": (agent or {}).get("role") or (agent or {}).get("title"),
                        "owner_id": (agent or {}).get("id"),
                    }
            except Exception:  # noqa: BLE001
                pass
        tid = new_id("tsk")
        row = PlannedTask(
            id=tid,
            venture_id=venture_id,
            title=task["title"],
            detail=task.get("detail"),
            why_now=task.get("why_now") or f"You asked for this: “{(task.get('detail') or task['title'])[:80]}”",
            tags_json=json.dumps(tags),
            est_minutes=int(task.get("est_minutes") or 30),
            runnable=1 if task.get("runnable", True) else 0,
            source=source,
            status="open",
            rank=0,
            fingerprint=fp,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        existing_fp = session.scalars(
            select(TaskFingerprint).where(
                TaskFingerprint.venture_id == venture_id,
                TaskFingerprint.fingerprint == fp,
            )
        ).first()
        if existing_fp:
            existing_fp.task_id = tid
            existing_fp.status = "open"
        else:
            session.add(
                TaskFingerprint(
                    id=new_id("tfp"),
                    venture_id=venture_id,
                    fingerprint=fp,
                    task_id=tid,
                    status="open",
                    created_at=now,
                )
            )
        session.commit()
        serialized = _serialize_task(row)
    depends = list(task.get("depends_on") or task.get("dependsOn") or [])
    try:
        from app.munder.ledger import add_task as hive_add, sync_planned_runnable

        hive_add(
            venture_id,
            {
                "id": serialized["id"],
                "title": serialized["title"],
                "status": "todo",
                "dependsOn": depends,
                "assignee": serialized.get("owner_name"),
                "description": serialized.get("detail"),
            },
        )
        if depends:
            sync_planned_runnable(venture_id)
            refreshed = get_task(venture_id, serialized["id"])
            if refreshed:
                return refreshed
    except Exception:  # noqa: BLE001 — hive must not fail enqueue
        pass
    return serialized


def mark_task_cancelled(task_id: str, *, reason: str = "founder_cancel") -> None:
    from app.state import MEM

    MEM[f"task_cancelled:{task_id}"] = reason


def task_was_cancelled(task_id: str) -> str | None:
    from app.state import MEM

    value = MEM.get(f"task_cancelled:{task_id}")
    return str(value) if value else None


def founder_constraints(venture_id: str) -> list[str]:
    from app.state import MEM

    rows = MEM.get(f"founder_constraints:{venture_id}") or []
    return [str(item).lower() for item in rows if str(item).strip()]


def add_founder_constraints(venture_id: str, needles: list[str]) -> list[str]:
    from app.state import MEM

    existing = founder_constraints(venture_id)
    for needle in needles:
        clean = str(needle or "").strip().lower()
        if clean and clean not in existing:
            existing.append(clean)
    MEM[f"founder_constraints:{venture_id}"] = existing
    return existing


def _cancel_needles(query: str) -> list[str]:
    raw = (query or "").lower()
    needles: list[str] = []
    if re.search(r"\bsplit the work\b", raw) or "discovery" in raw:
        needles.extend(
            ["discovery", "objection", "journey", "map the", "compare ", "interview"]
        )
    if "generic" in raw:
        needles.append("generic")
    extra = re.findall(r"\b(discovery|research|outreach|prototype|objection)\b", raw)
    needles.extend(extra)
    seen: set[str] = set()
    out: list[str] = []
    for item in needles:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def complete_matching_draft_tasks(venture_id: str, to: str) -> list[dict]:
    """Mark parked private-draft tasks done once the email file exists."""
    from app.chat.steer import is_private_email_draft

    who = (to or "").strip().lower()
    if not who or who in {"founder", "the recipient"}:
        return []
    factory = db()
    now = utcnow()
    closed: list[dict] = []
    with factory() as session:
        rows = list(
            session.scalars(
                select(PlannedTask).where(
                    PlannedTask.venture_id == venture_id,
                    PlannedTask.status.in_(("open", "running")),
                )
            ).all()
        )
        for row in rows:
            if not is_private_email_draft(row.title or "", row.detail or ""):
                continue
            blob = f"{row.title} {row.detail or ''} {row.why_now or ''}".lower()
            if who not in blob:
                continue
            row.status = "done"
            row.runnable = 0
            row.last_error = None
            row.finished_at = now
            row.updated_at = now
            closed.append(_serialize_task(row))
        session.commit()
    return closed


def cancel_matching_work(venture_id: str, query: str) -> list[dict]:
    """Dismiss running/open work the founder told us to stop, and constrain refill."""
    factory = db()
    now = utcnow()
    needles = _cancel_needles(query)
    stop_that = bool(
        re.search(
            r"\b(stop that|cancel (that|this|it)|don'?t do that|park (that|this))\b",
            query or "",
            re.I,
        )
    )
    dismissed: list[dict] = []
    with factory() as session:
        rows = list(
            session.scalars(
                select(PlannedTask).where(
                    PlannedTask.venture_id == venture_id,
                    PlannedTask.status.in_(("open", "running")),
                )
            ).all()
        )
        running = [row for row in rows if row.status == "running"]
        if stop_that and running and not needles:
            targets = running
        else:
            targets = []
            for row in rows:
                blob = f"{row.title} {row.detail or ''} {row.why_now or ''}".lower()
                if needles and any(needle in blob for needle in needles):
                    targets.append(row)
            if stop_that and running:
                for row in running:
                    if row not in targets:
                        targets.append(row)
        for row in targets:
            row.status = "dismissed"
            row.runnable = 0
            row.last_error = "founder_cancel"
            row.finished_at = now
            row.updated_at = now
            mark_task_cancelled(row.id, reason="founder_cancel")
            dismissed.append(_serialize_task(row))
        session.commit()
    constraint_needles = list(needles)
    for row in dismissed:
        title = str(row.get("title") or "").lower()
        for token in ("discovery", "objection", "prototype", "interview", "journey"):
            if token in title and token not in constraint_needles:
                constraint_needles.append(token)
    if constraint_needles or dismissed:
        add_founder_constraints(venture_id, constraint_needles or ["generic discovery"])
    return dismissed


def cancel_all_work(venture_id: str) -> list[dict]:
    """Stop everything: dismiss every open and running task for the venture (queued work cannot start; running work is told to stop)."""
    now = utcnow()
    dismissed: list[dict] = []
    with db()() as session:
        for row in session.scalars(select(PlannedTask).where(
                PlannedTask.venture_id == venture_id, PlannedTask.status.in_(("open", "running")))).all():
            row.status, row.runnable, row.last_error, row.finished_at, row.updated_at = "dismissed", 0, "founder_cancel", now, now
            mark_task_cancelled(row.id, reason="founder_cancel")
            dismissed.append(_serialize_task(row))
        session.commit()
    return dismissed


def retarget_front_task(venture_id: str, title: str, *, detail: str = "") -> dict:
    """If work is running, cancel it and enqueue the new brief. Otherwise rewrite the next open item."""
    factory = db()
    now = utcnow()
    clean = (title or "").strip() or "Updated work"
    with factory() as session:
        running = session.scalars(
            select(PlannedTask)
            .where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "running",
            )
            .order_by(PlannedTask.updated_at.desc())
        ).first()
        if running is not None:
            running.status = "dismissed"
            running.runnable = 0
            running.last_error = "retargeted"
            running.finished_at = now
            running.updated_at = now
            mark_task_cancelled(running.id, reason="retargeted")
            session.commit()
            tags = ["chat", "retarget"]
            blob = f"{clean} {detail}".lower()
            if any(word in blob for word in ("landing", "headline", "hero", "waitlist")):
                tags.append("site")
            if "email" in blob or "outreach" in blob:
                tags.append("email")
            return enqueue_task(
                venture_id,
                {
                    "title": clean,
                    "detail": detail or clean,
                    "why_now": f"You retargeted in-flight work to: “{clean[:80]}”",
                    "tags": tags,
                    "runnable": True,
                },
                source="chat",
            )
        nxt = session.scalars(
            select(PlannedTask)
            .where(
                PlannedTask.venture_id == venture_id,
                PlannedTask.status == "open",
            )
            .order_by(PlannedTask.rank.asc(), PlannedTask.updated_at.desc())
        ).first()
        if nxt is None:
            session.commit()
        else:
            nxt.title = clean
            if detail:
                nxt.detail = detail
            nxt.why_now = f"You changed this work: “{clean[:80]}”"
            nxt.updated_at = now
            session.commit()
            return _serialize_task(nxt)
    return enqueue_task(
        venture_id,
        {
            "title": clean,
            "detail": detail or clean,
            "why_now": "You retargeted the front of the slate.",
            "tags": ["chat"],
            "runnable": True,
        },
    )
