"""Failed background jobs in chat: plain copy from the real error code, and a Retry that is a button.

Copy comes from the job's stored ``last_error`` code (one vocabulary: chat/blockers.py), never from a
guess. Explicit retry requests use the existing guarded execution path without a usage confirmation.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.chat.actions import get_action
from app.chat.blockers import error_copy, humanize_error, with_explain
from app.chat.confirm import ConfirmationCard
from app.db.models import Job
from app.core.job_status import job_label, list_jobs
from app.core.jobs import ACTIVE_JOB_STATUSES, IDEMPOTENT_RETRY_KINDS, publish_wake, retry_failed_job, retry_is_safe
from app.db.session import serialize_writes
from app.state import db

FAILED = ("failed", "failed_dead", "poison")
# kind -> (what the founder calls it, retry button label, page)
KINDS: dict[str, tuple[str, str, str]] = {
    "idea.validate": ("Validation", "Retry validation", "idea"),
    "research.deep": ("Research", "Retry research", "research"),
    "plan.compile": ("Planning", "Retry planning", "plan"),
    "idea.clarify_turn": ("Clarification", "Try again", "idea"),
}
_WORDS = {"validation": "idea.validate", "research": "research.deep", "planning": "plan.compile",
          "plan": "plan.compile", "clarification": "idea.clarify_turn"}
_CODE = re.compile(r"[a-z][a-z0-9_]{2,80}")
_KIND_WORD = re.compile(r"\b(validation|research|planning|plan|clarification)\b", re.I)
RETRY_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:help me (?:safely )?)?(?:retry|restart|try again)\b", re.I)
STATUS_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:what|which|show|list)\s+(?:(?:me|my|our|is|has|are|the|tasks?|work)\s+)*(failed|running)(?:\s+(?:tasks?|work|now|right now))?\s*[?.!]*\s*$", re.I)


def clean_code(code: object) -> str:
    """Only a plain snake_case error code is kept; a long message never rides into chat or a button."""
    text = str(code or "").strip().partition(":")[0]
    return text if _CODE.fullmatch(text) else ""


def kind_in(text: str) -> str | None:
    m = _KIND_WORD.search(text or "")
    return _WORDS[m.group(1).lower()] if m else None


def _failed(row: Job) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except ValueError:
        payload = {}
    return {"kind": row.kind, "job_id": row.id, "payload": payload if isinstance(payload, dict) else {},
            "code": clean_code(row.last_error), "reason": humanize_error(row.last_error)}


def latest_failed(venture_id: str, workspace_id: str | None = None, kind: str | None = None) -> dict[str, Any] | None:
    """The newest failed task with no newer nonfailed replacement in the same scope."""
    with db()() as session:
        newer = aliased(Job)
        query = select(Job).where(Job.venture_id == venture_id, Job.status.in_(FAILED),
            ~select(newer.id).where(newer.venture_id == Job.venture_id,
                newer.workspace_id == Job.workspace_id, newer.kind == Job.kind,
                newer.created_at > Job.created_at, newer.status.notin_(FAILED)).exists())
        if kind:
            query = query.where(Job.kind == kind)
        if workspace_id:
            query = query.where(Job.workspace_id == workspace_id)
        job = session.scalars(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
        if job is None:
            return None
        return _failed(job)


def body(kind: str, code: str) -> str:
    """One plain paragraph naming what failed and why, from the stored code."""
    label, _, page = KINDS.get(kind, (job_label(kind), "", "task"))
    code = clean_code(code)
    reason = error_copy(code) or ("It stopped with an error I do not have a specific explanation for."
                                  if code else "No reason was saved for it.")
    saved = "your saved work is kept, and nothing went public" if kind in KINDS else "your saved work is kept"
    if code == "handler_failed":
        return f"I could not finish this step for you, but {saved}. Open your {page} to continue."
    return f"{label} did not finish. {reason} {saved.capitalize()}."


def retry_button(kind: str) -> dict[str, Any]:
    _, label, page = KINDS[kind]
    return {"label": label, "action": "job.retry", "payload": {"kind": kind}, "style": "primary"}


def retryable(kind: str, code: str) -> bool:
    """These clarification failures may offer a founder-confirmed NEW attempt."""
    return kind != "idea.clarify_turn" or clean_code(code) in {"clarification_invalid_output", "clarification_insufficient_credits", "clarification_outcome_uncertain", "job_outcome_uncertain"}


def failure_actions(venture_id: str, kind: str, code: str) -> list[dict[str, Any]]:
    """Retry button, a link to the page, and Explain in detail carrying the real code."""
    label, _, page = KINDS[kind]
    actions = [retry_button(kind)] if retryable(kind, code) else []
    actions.append({"label": f"Open {page}", "href": f"/v/{venture_id}/{page}",
                    "style": "secondary" if actions else "primary"})
    return with_explain({"actions": actions}, "job_failed", job_kind=kind, job_code=clean_code(code))["actions"]


def failure_reply(venture_id: str, failed: dict[str, Any]) -> dict[str, Any]:
    if failed["kind"] not in KINDS:
        return {"kind": "needs_you", "body_md": f"{job_label(failed['kind'])} did not finish. {failed.get('reason') or humanize_error(failed['code'])}",
                "actions": [{"label": "Review saved work", "action": "jobs.status", "payload": {"state": "failed"}}], "citations": []}
    return {"kind": "needs_you", "body_md": body(failed["kind"], failed["code"]),
            "actions": failure_actions(venture_id, failed["kind"], failed["code"]), "citations": []}


def explain_failure(venture_id: str, kind: str, code: str) -> dict[str, Any]:
    """'Explain in detail' for a failed job: the real reason, what is safe, and the retry button."""
    if kind not in KINDS:
        return {"kind": "text", "citations": [], "actions": [],
                "body_md": "I do not have a step-by-step guide for that yet. Ask me what is blocking us."}
    label, _, page = KINDS[kind]
    code = clean_code(code)
    reason = error_copy(code) or ("No specific reason was saved for this failure." if not code else
                                  "It stopped with an error I do not have a plain explanation for yet.")
    steps = ["Your answers and earlier results are saved; nothing was lost.",
             f"Tap **{KINDS[kind][1]}** below to run it again. I will show what it costs first.",
             "If it fails again with the same reason, open Settings and check your AI model."]
    if kind == "idea.clarify_turn":
        steps = steps[:1] + [f"Open the {page} page to answer or retry the question."]
    text = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    actions = [retry_button(kind)] if retryable(kind, code) else []
    actions += [{"label": f"Open {page}", "href": f"/v/{venture_id}/{page}", "style": "secondary"},
                {"label": "Open Settings", "href": f"/v/{venture_id}/settings", "style": "secondary"}]
    return {"kind": "text", "citations": [], "actions": actions,
            "body_md": f"**{label} did not finish.** {reason}\n\n{text}"}


# ---------------------------------------------------------------- retry (card first)

def _nothing(venture_id: str, kind: str | None) -> dict[str, Any]:
    what = job_label(kind).lower() if kind else "task"
    from app.chat.handlers.general import _next_step_actions

    return {"kind": "text", "citations": [], "actions": _next_step_actions(venture_id),
            "body_md": f"I have no failed {what} to retry, so I did not start anything."}


def prepare(venture_id: str, kind: str | None, workspace_id: str | None = None, *, failed: dict[str, Any] | None = None) -> dict[str, Any]:
    """The retry confirmation card for the latest failed job (or the named kind)."""
    failed = failed or latest_failed(venture_id, workspace_id, kind)
    if failed is None:
        return _nothing(venture_id, kind)
    kind = failed["kind"]
    if kind not in KINDS:
        return retry_exact(venture_id, failed["job_id"], workspace_id)
    lead = body(kind, failed["code"]) + " "
    if kind == "research.deep":
        from app.chat.build_path import research_prerequisites
        blocked = research_prerequisites(venture_id)
        if blocked:
            return blocked
        card = replace(ConfirmationCard.for_action(
            get_action("research.start"),
            interpretation="Run research again using your saved idea.",
            consequences=["Starts research again from your saved idea.",
                          "Nothing is published or sent."]), confirm_label="Retry research")
        return {"kind": "confirmation", "citations": [], "actions": [card.as_action()],
                "body_md": lead + "Confirm to run it again."}
    if kind == "plan.compile":
        from app.chat import plan_chain

        reply = plan_chain.generate_prepare(venture_id)
        return {**reply, "body_md": lead + reply["body_md"]}
    if kind == "idea.validate":
        payload = failed["payload"]
        sid, rev = str(payload.get("session_id") or ""), str(payload.get("revision_id") or "")
        if not sid:
            return failure_reply(venture_id, failed)
        card = replace(ConfirmationCard.for_action(
            get_action("job.retry"),
            interpretation="Run idea validation again using your saved answers.",
            consequences=["Starts a new validation review; your answers are kept as they are.",
                          "Nothing is built, published or sent."],
            resulting_state={"kind": kind, "session_id": sid, "revision_id": rev, "run": True}),
            confirm_label="Retry validation")
        return {"kind": "confirmation", "citations": [], "actions": [card.as_action()],
                "body_md": lead + "Confirm to run it again on your saved answers."}
    if kind == "idea.clarify_turn" and retryable(kind, failed["code"]) and failed["payload"].get("session_id"):
        card = replace(ConfirmationCard.for_action(
            get_action("job.retry"),
            interpretation="Ask the clarification question again using your saved answers.",
            consequences=["Starts a fresh model call for this request; nothing you wrote is changed.",
                          "An earlier request without a confirmed result is never repeated.",
                          "Nothing is built, published or sent."],
            resulting_state={"kind": kind, "session_id": str(failed["payload"]["session_id"]),
                             "failed_job_id": failed["job_id"], "run": True}),
            confirm_label="Try again")
        return {"kind": "confirmation", "citations": [], "actions": [card.as_action()],
                "body_md": lead + "Confirm to try the question again."}
    return failure_reply(venture_id, failed)


def run(venture_id: str, payload: dict[str, Any], workspace_id: str | None = None, *, allow_uncertain: bool = False) -> dict[str, Any]:
    """Executor for a confirmed validation retry (research and planning confirm through their own actions)."""
    if payload.get("job_id"):
        return retry_exact(venture_id, str(payload["job_id"]), workspace_id)
    if not payload.get("run"):
        kind = str(payload.get("kind") or "")
        return prepare(venture_id, kind if kind in KINDS else None, workspace_id)
    if payload.get("kind") == "idea.clarify_turn":
        from app.idea.service import retry_failed_clarification

        try:
            started = retry_failed_clarification(str(payload.get("session_id") or ""), venture_id=venture_id,
                                      failed_job_id=payload.get("failed_job_id"), allow_uncertain=allow_uncertain)
        except (RuntimeError, ValueError, KeyError) as exc:
            from app.chat.recovery import founder_failure
            return founder_failure(exc, venture_id, "your next idea question", {"action": "idea.review", "payload": {}},
                                   safety="Check your saved idea before starting another attempt.")
        return {"kind": "text", "job_id": started.get("job_id"), "citations": [], "actions": [{"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "primary"}],
                "body_md": "I am asking the question again on your saved answers. I will post here when it is ready."}
    from app.idea.validation import start_validation

    link = {"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "primary"}
    try:
        started = start_validation(str(payload.get("session_id") or ""), revision_id=str(payload.get("revision_id") or "") or None,
                         venture_id=venture_id, workspace_id=workspace_id)
    except (RuntimeError, ValueError, KeyError) as exc:
        from app.chat.recovery import founder_failure
        return founder_failure(exc, venture_id, "your idea review", {"action": "idea.review", "payload": {}},
                               safety="Check your saved idea before starting another attempt.")
    return {"kind": "text", "job_id": started.get("job_id"), "citations": [], "actions": [link],
            "body_md": "Validation is running again on your saved answers. I will post here when it is back."}


def retry_exact(venture_id: str, job_id: str, workspace_id: str | None) -> dict[str, Any]:
    reply = {"kind": "text", "citations": [], "actions": []}
    with db()() as session:
        serialize_writes(session, key="job-retry:" + job_id)
        row = session.scalar(select(Job).where(Job.id == job_id, Job.venture_id == venture_id,
            Job.workspace_id == workspace_id).with_for_update()) if workspace_id else None
        if row is None:
            return {**reply, "body_md": "You can only retry work saved in this venture. Nothing was started."}
        label = job_label(row.kind)
        if row.status in ACTIVE_JOB_STATUSES:
            return {**reply, "body_md": f"{label} is already queued or running. You do not need to restart it."}
        newer = session.scalar(select(Job.status).where(Job.venture_id == venture_id,
            Job.workspace_id == workspace_id, Job.kind == row.kind, Job.created_at > row.created_at,
            Job.status.in_(ACTIVE_JOB_STATUSES | {"succeeded", "done"})).order_by(Job.created_at.desc()).limit(1))
        if newer in ACTIVE_JOB_STATUSES:
            return {**reply, "body_md": f"A newer {label.lower()} is already queued or running. You do not need to restart it."}
        if row.status in {"succeeded", "done"} or newer:
            return {**reply, "body_md": f"{label} has already finished successfully. You do not need to retry it."}
        if row.status not in FAILED:
            return {**reply, "body_md": f"{label} was stopped. Review your saved work before starting a new task."}
        failed = _failed(row)
        if not retry_is_safe(session, row):
            return {**reply, "kind": "needs_you",
                "body_md": f"{label} did not finish. {failed['reason']} I did not restart it because an earlier action may have completed or this task needs a fresh review. Check your saved result first, then start a new round if you still need it.",
                "actions": [{"label": "Review saved work", "action": "jobs.status", "payload": {"state": "failed"}},
                            {"label": "Start a new round", "action": "chat.prefill", "text": "Review my saved result and help me start a new round safely."}]}
        if row.kind in IDEMPOTENT_RETRY_KINDS:
            retry_failed_job(session, row)
            session.commit()
    if failed["kind"] not in IDEMPOTENT_RETRY_KINDS:
        return prepare(venture_id, failed["kind"], workspace_id, failed=failed)
    publish_wake()
    return {**reply, "body_md": f"{label} was restarted from your saved work. I will post here when it is ready.",
            "run_id": job_id, "action_started": True}


def status(venture_id: str, workspace_id: str | None, state: str = "running") -> dict[str, Any]:
    visible = list_jobs(workspace_id, limit=200, venture_id=venture_id,
        active_only=state != "failed", failed_only=state == "failed")["items"] if workspace_id else []
    lines = [f"- {item['label']}: {item['failure_reason'] or 'No reason was saved.'}" if state == "failed"
             else f"- {item['label']}: {(item.get('wait') or {}).get('label') or 'Running.'}" for item in visible]
    actions = [{"label": f"Retry {item['label'].lower()}", "action": "job.retry", "payload": {"job_id": item["id"]}}
               for item in visible] if state == "failed" else []
    return {"kind": "text", "citations": [], "actions": actions,
            "body_md": "\n".join(lines) or ("You have no failed tasks to review." if state == "failed" else "You have no tasks running right now.")}
