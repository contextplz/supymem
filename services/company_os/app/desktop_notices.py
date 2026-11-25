"""Durable founder-facing notices delivered to the signed-in Mac app."""

from __future__ import annotations

import json
import re

from core.clock import as_utc, utcnow
from core.ids import new_id
from sqlalchemy import or_, select

from app.db.models import HumanTask, Venture, Workspace
from app.db.supymem_models import NeedsYouRequest, Notice, Suggestion
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
from app.state import db
from app.workspaces.resolve import ensure_desk


def _workspace_id(venture_id: str | None) -> str:
    if venture_id:
        factory = db()
        with factory() as session:
            venture = session.get(Venture, venture_id)
            if venture is not None and venture.workspace_id:
                return venture.workspace_id
    # Local machine-wide notices belong to the local Desk. Authenticated hosted
    # founders cannot see this workspace, so an unattributed notice never leaks.
    return ensure_desk().id


def _target_path(venture_id: str | None, target_path: str | None) -> str | None:
    """Return a same-app route; native code independently validates it before navigation."""
    if target_path is not None:
        candidate = target_path.strip()
    elif venture_id:
        candidate = f"/v/{venture_id}/now"
    else:
        return None
    if "?" in candidate or ".." in candidate:
        raise ValueError("desktop_notice_target_invalid")
    routed = r"/v/ven_[A-Za-z0-9_-]+/(?:now|work|outreach|connections|analytics|settings|suggestions)"
    work_order = r"/v/ven_[A-Za-z0-9_-]+/device-work-orders/dwo_[A-Za-z0-9_-]+"
    attention = r"/v/ven_[A-Za-z0-9_-]+#attention-ht_[A-Za-z0-9_-]+"
    if not re.fullmatch(f"(?:{routed}|{work_order}|{attention})", candidate):
        raise ValueError("desktop_notice_target_invalid")
    return candidate


def enqueue(*, title: str, body: str, venture_id: str | None = None, kind: str = "watch",
            target_path: str | None = None) -> str:
    notice_id = new_id("not")
    factory = db()
    with factory() as session:
        session.add(
            Notice(
                id=notice_id,
                workspace_id=_workspace_id(venture_id),
                person_id=None,
                kind=kind[:80],
                title=title[:120],
                body=body[:400],
                target_path=_target_path(venture_id, target_path),
                read_at=None,
                created_at=utcnow(),
            )
        )
        session.commit()
    if kind == "human_task":
        from app.email.triggers import on_needs_you
        on_needs_you(venture_id, title, body, notice_id)  # never raises
    return notice_id


def _recipient_filter(person_id: str | None):
    # Machine-wide loopback delivery receives broadcast notices only.
    return or_(Notice.person_id.is_(None), Notice.person_id == person_id) if person_id else Notice.person_id.is_(None)


def _task_notice(session, row, venture_id):
    match = re.fullmatch(r"task:(tsk_[A-Za-z0-9_-]+)", row.kind)
    task = session.get(Task, match.group(1)) if match else None
    workspace = session.get(Workspace, row.workspace_id)
    if (not task or not workspace or task.workspace_id != row.workspace_id or task.venture_id != venture_id
            or task.owner_person_id != workspace.owner_id or row.person_id != task.owner_person_id):
        return None
    run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state != "queued")
                         .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
    # A notice from a previous standing run cannot certify this run's outcome.
    if not run or as_utc(run.started_at) > as_utc(row.created_at):
        return None
    if task.state in {"input_required", "auth_required", "approval_required", "failed", "rejected"} and run.state == task.state:
        if as_utc(task.updated_at) > as_utc(row.created_at):
            return None
        try:
            blocker = json.loads(task.blocker_json or "{}")
        except (ValueError, TypeError):
            return None
        if not isinstance(blocker, dict) or not all(isinstance(blocker.get(key), str) and blocker[key] for key in ("reason", "unblock_action")):
            return None
        return "Task needs your attention", f"{task.title}: {blocker['reason']} {blocker['unblock_action']}"
    if (run.state == "completed" and run.finished_at is not None and task.state in {"completed", "submitted"}
            and as_utc(run.finished_at) <= as_utc(row.created_at)):
        from app.ui_config import ui_config
        if session.scalar(select(TaskStep.id).where(TaskStep.run_id == run.id,
                TaskStep.skill == "gtm.journey.prepare").limit(1)) and ui_config(task.owner_person_id)["founder_ui"] != "classic":
            return None
        return f"Task finished: {task.title}", "The task finished and its result checks passed. Open the saved result in Supymem."
    if (task.kind != "one_time" and task.state == "submitted" and run.state in {"failed", "rejected"}
            and run.finished_at and as_utc(run.finished_at) <= as_utc(row.created_at)):
        stopped = session.scalar(select(TaskEvent).where(TaskEvent.task_id == task.id, TaskEvent.run_id == run.id,
            TaskEvent.kind == "blocker", TaskEvent.created_at <= row.created_at)
            .order_by(TaskEvent.created_at.desc()).limit(1))
        try:
            reason = json.loads(stopped.data_json) if stopped else {}
        except (TypeError, ValueError):
            return None
        if reason.get("run_only") is True and all(isinstance(reason.get(key), str) and reason[key] for key in ("reason", "unblock_action")):
            return "Task run stopped", f"{task.title}: {reason['reason']} {reason['unblock_action']}"
    if task.state == run.state == "working" and as_utc(run.deadline_at) < utcnow():
        eta = session.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task.id, TaskEvent.run_id == run.id,
            TaskEvent.kind == "eta_slip", TaskEvent.created_at <= row.created_at).limit(1))
        if eta:
            return "Task estimate changed", f"{task.title}: this run is taking longer than its estimate. Open its saved progress in Supymem."
    return None


def _current_notice(session, row: Notice) -> dict | None:
    from app.needs_you.service import _quiet, _requirements_copy, _row
    from app.needs_you.templates import blocker_message

    target = row.target_path
    if target is not None:
        try:
            _target_path(None, target)
        except ValueError:
            return None
    found = re.match(r"/v/(ven_[A-Za-z0-9_-]+)(?:/|#)", target or "")
    venture_id = found.group(1) if found else None
    venture = session.get(Venture, venture_id) if venture_id else None
    if venture_id and (venture is None or venture.workspace_id != row.workspace_id
                       or venture.archived_at is not None or venture.stage == "purged"):
        return None
    title, body, verified = row.title, _requirements_copy(row.body), False
    if row.kind.startswith("task:"):
        projected = _task_notice(session, row, venture_id)
        if projected is None:
            return None
        title, body = projected
        body = _requirements_copy(body)
        verified = True
    elif row.kind.startswith("approval:"):
        from app.approvals.service import _can_approve
        from app.db.models import Approval
        match = re.fullmatch(r"approval:(apr_[A-Za-z0-9_-]+)", row.kind)
        approval = session.get(Approval, match.group(1)) if match else None
        if (not approval or approval.workspace_id != row.workspace_id or not row.person_id
                or approval.assigned_to != row.person_id or approval.status != "pending"
                or as_utc(approval.expires_at) <= utcnow()
                or not _can_approve(session, row.workspace_id, row.person_id)):
            return None
        title, body = "An action needs your approval", "Review the exact saved action in Supymem before deciding."
        verified = True
    elif row.kind.startswith("reminder:"):
        verified = True  # the founder's own reminder, delivered at the time they asked for
    elif row.kind == "human_task":
        task_match = re.fullmatch(r"/v/ven_[A-Za-z0-9_-]+#attention-(ht_[A-Za-z0-9_-]+)", target or "")
        task = session.get(HumanTask, task_match.group(1)) if task_match else None
        if task is None or task.venture_id != venture_id or task.status not in {"open", "pending", "in_progress"}:
            return None
        title = f"Supymem needs you: {task.title}"
        body = _requirements_copy(task.why) or "Review this saved requirement in Supymem."
        verified = True
    elif row.kind == "needs_you":
        if venture is None:
            return None
        requests = list(session.scalars(select(NeedsYouRequest).where(
            NeedsYouRequest.venture_id == venture_id, NeedsYouRequest.workspace_id == row.workspace_id,
            NeedsYouRequest.status == "open").order_by(NeedsYouRequest.created_at, NeedsYouRequest.id)))
        if _quiet(utcnow()):
            requests = [request for request in requests if request.severity == "blocking"]
        if not requests:
            return None
        title = "Supymem has a blocker" if len(requests) == 1 else f"Supymem has {len(requests)} blockers"
        body = blocker_message([_row(request) for request in requests], flat=True)
        verified = True
    elif row.kind == "suggestions":
        if venture is None:
            return None
        cards = list(session.scalars(select(Suggestion).where(Suggestion.venture_id == venture_id,
            Suggestion.status.in_(["new", "saved"])).order_by(Suggestion.created_at, Suggestion.id)))
        if not cards:
            return None
        title = f"{len(cards)} saved suggestion{'s' if len(cards) != 1 else ''} await your review"
        body = "; ".join(card.title for card in cards)
        verified = True
    elif row.kind.startswith("nudge:"):
        pass  # Charlie's proactive nudge: its own plain title and body (app/chat/proactive.py)
    else:
        # A stored historical event proves neither current blockers nor active
        # execution. Keep its receipt explicitly qualified as saved copy.
        title = f"Saved notice: {title}"
        body = f"Saved {row.created_at.date().isoformat()}: {body}"
    if venture is not None and not row.kind.startswith("nudge:"):
        title = f"{venture.name}: {title}"
    return {"id": row.id, "title": title[:120], "body": body[:400], "workspace_id": row.workspace_id,
            "kind": row.kind, "target_path": target, "ts": as_utc(row.created_at).timestamp(), "acked": False,
            "venture_id": venture_id, "venture_name": venture.name if venture is not None else None,
            "current_state_verified": verified}


def pending(*, limit: int = 20, workspace_ids: set[str] | None = None, person_id: str | None = None) -> list[dict]:
    if limit <= 0:
        return []
    factory = db()
    with factory() as session:
        query = select(Notice).where(Notice.read_at.is_(None), _recipient_filter(person_id)).order_by(Notice.created_at.desc(), Notice.id.desc())
        if workspace_ids is not None:
            if not workspace_ids:
                return []
            query = query.where(Notice.workspace_id.in_(workspace_ids))
        projected, seen = [], set()
        for row in session.scalars(query).yield_per(100):
            item = _current_notice(session, row)
            if item is None:
                continue
            key = (row.kind, row.workspace_id, row.target_path)
            if (row.kind in {"human_task", "needs_you", "suggestions"} or row.kind.startswith(("task:", "approval:"))) and key in seen:
                continue
            seen.add(key)
            projected.append(item)
            if len(projected) == limit:
                break
        return projected


def ack(ids: list[str], *, workspace_ids: set[str] | None = None, person_id: str | None = None) -> int:
    wanted = {value for value in ids if value}
    if not wanted:
        return 0
    factory = db()
    with factory() as session:
        query = select(Notice).where(Notice.id.in_(wanted), Notice.read_at.is_(None), _recipient_filter(person_id))
        if workspace_ids is not None:
            if not workspace_ids:
                return 0
            query = query.where(Notice.workspace_id.in_(workspace_ids))
        rows = session.scalars(query).all()
        now = utcnow()
        for row in rows:
            row.read_at = now
        session.commit()
        return len(rows)
