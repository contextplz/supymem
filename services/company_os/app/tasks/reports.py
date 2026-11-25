"""Deterministic, private standing reports from scoped saved business records."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.credits._txn import naive_utc
from app.db.models import GtmContact, GtmTouch, Venture, VentureDocument, Workspace
from app.db.supymem_models import VentureWaitlistEntry
from app.db.task_models import Task, TaskRun

KIND = "task_report"
SKILL = "tasks.waitlist_summary"
MAX_WINDOW = timedelta(days=7)
HUMAN_REPLIES = ("human_positive", "human_neutral", "human_negative")


def payload_digest(snapshot: dict) -> str:
    """Compare outcomes, excluding run identifiers and an advancing empty window."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json(raw):
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("task_report_invalid")
    return value


def _scope(session, task_id, run_id, workspace_id, venture_id):
    task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
    workspace, venture = session.get(Workspace, workspace_id), session.get(Venture, venture_id)
    if (not task or not run or run.task_id != task.id or not workspace or not venture
            or (task.workspace_id, task.venture_id) != (workspace_id, venture_id)
            or venture.workspace_id != workspace_id or workspace.owner_id != task.owner_person_id
            or workspace.archived_at or venture.archived_at):
        raise PermissionError("task_report_scope_mismatch")
    return task, run


def _identifier(run_id):
    return "task-report:" + run_id


def _previous(session, task, run, end):
    prior = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.id != run.id,
        TaskRun.state == "completed", TaskRun.started_at < run.started_at)
        .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
    row = session.get(VentureDocument, _identifier(prior.id)) if prior else None
    if row is None:
        return None
    data = _json(row.provenance_json)
    if ((row.kind, row.venture_id) != (KIND, task.venture_id)
            or type(data.get("schema_version")) is not int
            or any(data.get(key) != value for key, value in {
                "schema_version": 1, "skill": SKILL, "task_id": task.id, "run_id": prior.id,
                "workspace_id": task.workspace_id, "venture_id": task.venture_id,
            }.items())):
        raise ValueError("task_report_previous_invalid")
    previous_end = _time(data["window"]["end_at"])
    if previous_end > end:
        raise ValueError("task_report_window_invalid")
    return data


def _iso(moment):
    return naive_utc(moment).replace(tzinfo=timezone.utc).isoformat()


def _time(value):
    if not isinstance(value, str):
        raise ValueError("task_report_window_invalid")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("task_report_window_invalid")
    return naive_utc(moment)


def _snapshot(session, workspace_id, venture_id, start, end):
    entry_ids = list(session.scalars(select(VentureWaitlistEntry.id).where(
        VentureWaitlistEntry.workspace_id == workspace_id, VentureWaitlistEntry.venture_id == venture_id,
        VentureWaitlistEntry.is_test.is_(False), VentureWaitlistEntry.created_at >= start,
        VentureWaitlistEntry.created_at < end).order_by(VentureWaitlistEntry.id)))
    sent = aliased(GtmTouch)
    # An attributed human response needs the same scoped, actually sent thread
    # receipt used by apply_reply. A sender match alone is insufficient.
    sent_thread = select(sent.id).where(sent.workspace_id == workspace_id, sent.venture_id == venture_id,
        sent.contact_id == GtmTouch.contact_id, sent.provider_thread_id == GtmTouch.provider_thread_id,
        sent.direction == "out", sent.simulated == 0, sent.sent_at.is_not(None)).exists()
    replies = session.execute(select(GtmTouch.id, GtmTouch.classification).join(
        GtmContact, GtmContact.id == GtmTouch.contact_id).where(
        GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id,
        GtmContact.workspace_id == workspace_id, GtmTouch.direction == "in", GtmTouch.simulated == 0,
        GtmTouch.channel == "email", GtmTouch.classification.in_(HUMAN_REPLIES),
        GtmTouch.message_id.is_not(None), GtmTouch.message_id != "",
        GtmTouch.provider_thread_id.is_not(None), GtmTouch.provider_thread_id != "",
        GtmTouch.created_at >= start, GtmTouch.created_at < end, sent_thread).order_by(GtmTouch.id)).all()
    classified = Counter(row.classification for row in replies)
    return {"counts": {"signup_count": len(entry_ids), "reply_count": len(replies),
        "interested_reply_count": classified["human_positive"], "negative_reply_count": classified["human_negative"],
        "neutral_reply_count": classified["human_neutral"]},
        "source_refs": {"waitlist_entry_ids": entry_ids, "reply_touch_ids": [row.id for row in replies]}}


def _body(window, snapshot):
    counts = snapshot["counts"]
    start, end = (_time(window[key]).strftime("%d %b %Y %H:%M UTC") for key in ("start_at", "end_at"))
    return ("# Waitlist and outreach summary\n\n"
        f"Reporting window: {start} to {end}.\n\n"
        f"- Waitlist sign-ups: {counts['signup_count']}\n"
        f"- Human replies: {counts['reply_count']}\n"
        f"- Interested replies: {counts['interested_reply_count']}\n"
        f"- Other human replies: {counts['negative_reply_count'] + counts['neutral_reply_count']}\n\n"
        "Test sign-ups, simulated messages and automated replies are excluded.\n")


def _verified(row, data):
    return {"verified": True, "pending": False, "blocker": None, "artifact_id": row.id,
        "quiet": data["quiet"], "payload_digest": data["payload_digest"],
        "evidence": [{"ref": "venture_document:" + row.id, "state": "verified", **data["snapshot"]["counts"]}]}


def write_report(session, *, task_id, run_id, workspace_id, venture_id, now=None):
    """Persist one immutable report in the caller's checkpoint transaction."""
    from app.db.session import serialize_writes

    serialize_writes(session, key="task-report:" + run_id)
    task, run = _scope(session, task_id, run_id, workspace_id, venture_id)
    row = session.get(VentureDocument, _identifier(run_id))
    if row is not None:
        result = verify_report(session, task_id=task_id, run_id=run_id,
            workspace_id=workspace_id, venture_id=venture_id)
        if not result["verified"]:
            raise ValueError("task_report_changed")
        return result
    end = naive_utc(now)
    if end < naive_utc(run.started_at):
        raise ValueError("task_report_window_invalid")
    prior = _previous(session, task, run, end)
    start = max(end - MAX_WINDOW, _time(prior["window"]["end_at"])) if prior else end - MAX_WINDOW
    snapshot = _snapshot(session, workspace_id, venture_id, start, end)
    digest = payload_digest(snapshot)
    quiet = task.kind != "one_time" and (
        snapshot["counts"]["signup_count"] + snapshot["counts"]["reply_count"] == 0
        or bool(prior and prior.get("payload_digest") == digest))
    window = {"start_at": _iso(start), "end_at": _iso(end)}
    data = {"schema_version": 1, "skill": SKILL, "task_id": task_id, "run_id": run_id,
        "workspace_id": workspace_id, "venture_id": venture_id, "window": window,
        "snapshot": snapshot, "payload_digest": digest, "quiet": quiet}
    row = VentureDocument(id=_identifier(run_id), venture_id=venture_id, kind=KIND,
        title="Waitlist and outreach summary", body_md=_body(window, snapshot),
        provenance_json=json.dumps(data, sort_keys=True), created_at=end, updated_at=end)
    session.add(row)
    session.flush()
    return _verified(row, data)


def verify_report(session, *, task_id, run_id, workspace_id, venture_id):
    """Recompute counts in the exact saved window; no provider or contact text."""
    try:
        task, run = _scope(session, task_id, run_id, workspace_id, venture_id)
        row = session.get(VentureDocument, _identifier(run_id))
        if row is None:
            raise ValueError("task_report_missing")
        data = _json(row.provenance_json)
        if ((row.kind, row.venture_id) != (KIND, venture_id)
                or type(data.get("schema_version")) is not int or data["schema_version"] != 1
                or any(data.get(key) != value for key, value in {"skill": SKILL, "task_id": task_id,
                    "run_id": run_id, "workspace_id": workspace_id, "venture_id": venture_id}.items())
                or type(data.get("quiet")) is not bool):
            raise ValueError("task_report_invalid")
        start, end = (_time(data["window"][key]) for key in ("start_at", "end_at"))
        if (start > end or end - start > MAX_WINDOW or end > naive_utc(row.created_at)
                or end < naive_utc(run.started_at)):
            raise ValueError("task_report_window_invalid")
        snapshot = _snapshot(session, workspace_id, venture_id, start, end)
        if (data.get("snapshot") != snapshot or data.get("payload_digest") != payload_digest(snapshot)
                or data["payload_digest"] != payload_digest(data["snapshot"])
                or row.body_md != _body(data["window"], snapshot)):
            raise ValueError("task_report_changed")
        prior = _previous(session, task, run, end)
        expected_start = max(end - MAX_WINDOW, _time(prior["window"]["end_at"])) if prior else end - MAX_WINDOW
        expected_quiet = task.kind != "one_time" and (snapshot["counts"]["signup_count"]
            + snapshot["counts"]["reply_count"] == 0 or bool(prior and prior.get("payload_digest") == data["payload_digest"]))
        if start != expected_start or data["quiet"] != expected_quiet:
            raise ValueError("task_report_invalid")
        return _verified(row, data)
    except (ValueError, TypeError, KeyError, PermissionError) as exc:
        return {"verified": False, "pending": False, "quiet": True, "evidence": [],
            "blocker": {"class": "policy" if isinstance(exc, PermissionError) else "impossible",
                "reason": "The saved summary could not be verified against this venture's records.",
                "unblock_action": "Review the report and run a fresh summary."}}
