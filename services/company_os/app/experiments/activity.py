"""Per-option receipts written by executing tools, never by generated narration."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from enum import Enum
import json
import math

from sqlalchemy import select

from app.chat.recovery import log_founder_exception
from app.db.models import ActivityLine, Venture
from app.db.supymem_models import Experiment
from app.state import db
from app.tenancy import current_or_none
from core.clock import as_utc, utcnow
from core.redact import redact, redact_string

def _scoped_venture(session, venture_id):
    venture = session.get(Venture, venture_id)
    scope = current_or_none()
    if venture and scope and (scope.workspace_id not in {"none", venture.workspace_id}
            or scope.venture_id and scope.venture_id != venture_id):
        raise PermissionError("experiment_activity_scope_invalid")
    return venture


class EvidenceLevel(str, Enum):
    HYPOTHESIS = "hypothesis"
    DESK_RESEARCH = "desk_research"
    PROSPECT_DISCOVERY = "prospect_discovery"
    DRAFTED_OUTREACH = "drafted_outreach"
    APPROVED_OUTREACH = "approved_outreach"
    SENT_OUTREACH = "sent_outreach"
    MARKET_RESPONSE = "market_response"
    CONCLUSION = "conclusion"


def run_context(context: dict, *, venture_id: str | None = None) -> dict:
    spec = context.get("spec")
    result = {**(spec if isinstance(spec, dict) else {}), **context}
    if venture_id is not None:
        result["venture_id"] = venture_id
    return result


def experiment_context(session, venture_id: str, experiment_id: str | None) -> dict:
    if not experiment_id:
        return {}
    row = session.get(Experiment, experiment_id)
    if not row or row.venture_id != venture_id:
        return {}
    from app.gtm.journey import _json
    return run_context({**_json(row.context), "venture_id": venture_id, "workspace_id": row.workspace_id})


def record(context: dict | str, item: dict | None = None, *, action: str = "", tool: str = "",
           evidence_level: EvidenceLevel | str = EvidenceLevel.HYPOTHESIS,
           started_at: datetime | None = None, ended_at: datetime | None = None, source_url: str | None = None,
           evidence_summary: str = "", outcome: str | dict = "succeeded", session=None) -> int | dict | None:
    supplied = item is not None
    if supplied:
        from app.experiments.exploration import _load

        venture_id = context
        if not isinstance(venture_id, str) or not isinstance(item, dict):
            raise ValueError("experiment_activity_scope_invalid")
        if not any(option["id"] == item.get("option_id") for round_ in _load(venture_id).get("rounds", [])
                   for option in round_["options"]):
            raise ValueError("experiment_activity_scope_invalid")
        context = {"venture_id": venture_id, "option_id": item["option_id"]}
        action, tool = item.get("action", ""), item.get("tool", "")
        evidence_level = item.get("evidence_level", EvidenceLevel.HYPOTHESIS)
        source_url, evidence_summary = item.get("source_url"), item.get("evidence_summary", "")
        outcome = item.get("outcome", {})
        try:
            started_at = datetime.fromisoformat(item["started_at"])
            ended_at = datetime.fromisoformat(item["ended_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("experiment_activity_interval_invalid") from exc
    if not isinstance(context, dict):
        raise ValueError("experiment_activity_scope_invalid")
    context = run_context(context)
    if not context.get("option_id"):
        return None
    status = outcome.get("status", "succeeded") if isinstance(outcome, dict) else outcome
    for value in (context["option_id"], context.get("venture_id"), action, tool, status):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("experiment_activity_scope_invalid")
    if isinstance(outcome, dict):
        for key in ("message_id", "event_id"):
            if key in outcome and (not isinstance(outcome[key], str) or not outcome[key].strip()):
                raise ValueError("experiment_activity_scope_invalid")
        for key in ("count", "events", "sample_size", "positive_reply_rate", "positive_replies", "pain", "need", "buying_intent"):
            if key in outcome and (type(outcome[key]) not in (int, float)
                    or not math.isfinite(outcome[key]) or outcome[key] < 0
                    or key == "positive_reply_rate" and outcome[key] > 1
                    or key in {"count", "events", "sample_size", "positive_replies"} and not float(outcome[key]).is_integer()):
                raise ValueError("experiment_activity_count_invalid")
        if "positive_replies" in outcome and outcome["positive_replies"] > outcome.get("count", outcome.get("events", 1)):
            raise ValueError("experiment_activity_count_invalid")
    level = EvidenceLevel(evidence_level).value
    if not isinstance(started_at, datetime) or not isinstance(ended_at, datetime):
        raise ValueError("experiment_activity_interval_invalid")
    started_at, ended_at = as_utc(started_at), as_utc(ended_at)
    if ended_at < started_at:
        raise ValueError("experiment_activity_interval_invalid")
    if not isinstance(evidence_summary, str):
        raise ValueError("experiment_activity_summary_invalid")
    if outcome == "succeeded" and action in {"draft", "email", "reply", "discover"}:
        if not evidence_summary.isdecimal():
            raise ValueError("experiment_activity_count_invalid")
    if source_url is not None:
        from app.research.deep import safe_source_url
        source_url = safe_source_url(source_url) if isinstance(source_url, str) else None
        if source_url is None:
            raise ValueError("experiment_activity_source_invalid")
    details = {"option_id": context["option_id"], "venture_id": context["venture_id"],
        "action": action, "tool": tool, "source_url": source_url,
        "evidence_summary": redact_string(evidence_summary), "evidence_level": level,
        "started_at": started_at.isoformat(), "ended_at": ended_at.isoformat(), "outcome": redact(outcome)}
    row = ActivityLine(venture_id=context["venture_id"], phase="experiments", kind="option_activity",
        status=redact_string(status), text=render(details), detail_json=json.dumps(details, allow_nan=False), evidence_url=source_url,
        query=details["evidence_summary"] if action == "search" else None, ts=ended_at)

    def save(target):
        venture = _scoped_venture(target, row.venture_id)
        if not venture or (context.get("workspace_id") and venture.workspace_id != context["workspace_id"]):
            raise ValueError("experiment_activity_scope_invalid")
        row.workspace_id = venture.workspace_id
        target.add(row)
        target.flush()
        return row.seq

    if session is not None:
        identifier = save(session)
    else:
        with db()() as target:
            identifier = save(target)
            target.commit()
    return {**details, "id": identifier} if supplied else identifier


def record_safely(context, *, session=None, **fields) -> int | None:
    venture_id = None
    try:
        scope = current_or_none()
        venture_id = scope.venture_id if scope else None
        if isinstance(context, dict):
            venture_id = run_context(context).get("venture_id") or venture_id
        if not callable(context) and not run_context(context).get("option_id"):
            return None
        with (session.begin_nested() if session is not None else db()()) as target:
            target = session if session is not None else target
            resolved = context(target) if callable(context) else context
            venture_id = run_context(resolved).get("venture_id") or venture_id
            identifier = record(resolved, session=target, **fields)
            if session is None:
                target.commit()
                if identifier:
                    from app.experiments import exploration
                    exploration.refresh_messages(run_context(resolved)["venture_id"])
            return identifier
    except BaseException as exc:
        # shortcut: telemetry outages leave logged gaps, add receipt-backed backfill when gap-free recovery is required.
        log_founder_exception(exc, venture_id, "experiment.activity.write")
        return None


@contextmanager
def track(context: dict, *, action: str, tool: str, evidence_level: EvidenceLevel | str,
          source_url: str | None = None, evidence_summary: str = "", session=None):
    """Keep telemetry failures from changing metered results or provider errors."""
    context = run_context(context)
    details = {"evidence_summary": evidence_summary, "outcome": "succeeded"}
    if not context.get("option_id"):
        yield details
        return
    started_at = utcnow()
    try:
        yield details
    except BaseException as exc:
        log_founder_exception(exc, context.get("venture_id"), "experiment.activity." + action)
        details["outcome"] = "failed"
        raise
    finally:
        record_safely(context, action=action, tool=tool, evidence_level=evidence_level,
            started_at=started_at, ended_at=utcnow(), source_url=source_url, session=session, **details)


def render(item: dict) -> str:
    summary = " ".join(item.get("evidence_summary", "").replace("\u2014", "-").split())
    action, outcome = item["action"], item["outcome"]
    if isinstance(outcome, dict):
        count = outcome.get("count", outcome.get("events", 1))
        status = outcome.get("status", "succeeded")
        if status != "succeeded":
            return "Your email delivery was not confirmed" if action in {"sent", "email"} else "Your action did not finish"
        labels = {"drafted": "Prepared", "approved": "Approved", "queued": "Queued", "sent": "Sent"}
        if action in labels:
            return f"{labels[action]} {count} email {'draft' if count == 1 else 'drafts'}" if action == "drafted" else f"{labels[action]} {count} {'email' if count == 1 else 'emails'}"
        if action in {"reply_received", "positive_reply"}:
            return f"{count} {'person replied' if count == 1 else 'people replied'}"
        if action == "meeting_booked":
            return f"{count} {'meeting' if count == 1 else 'meetings'} booked"
        outcome = status
    source = (item.get("source_url") or "the source").replace("\u2014", "-")
    if action == "email" and outcome != "succeeded":
        return "Your email delivery was not confirmed"
    if outcome == "failed":
        return {"search": f"Could not search {summary}", "fetch": f"Could not read {source}",
                "email": "Your email delivery was not confirmed"}.get(action, "Your action did not finish")
    if action == "search":
        return f"Searched {summary}"
    if action == "fetch":
        return f"Read {source}" if outcome != "empty" else f"No readable content at {source}"
    if action in {"draft", "email", "reply", "discover"}:
        count = int(summary)
        return {"draft": f"Prepared {count} email {'draft' if count == 1 else 'drafts'}",
                "email": f"Emailed {count} {'person' if count == 1 else 'people'}",
                "reply": f"{count} replied", "discover": f"Found {count} potential {'contact' if count == 1 else 'contacts'}"}[action]
    if action == "compare":
        return "Checked your results"
    return "Recorded your action"


def list_activity(venture_id: str, option_id: str | None = None) -> list[dict]:
    query = select(ActivityLine).where(ActivityLine.venture_id == venture_id,
        ActivityLine.phase == "experiments", ActivityLine.kind == "option_activity")
    with db()() as session:
        if _scoped_venture(session, venture_id) is None:
            return []
        rows = session.scalars(query.order_by(ActivityLine.seq))
        result = []
        for row in rows:
            item = {**json.loads(row.detail_json), "id": row.seq}
            if option_id is not None and item["option_id"] != option_id:
                continue
            item["text"] = render(item)
            result.append(item)
        result.sort(key=lambda item: (item["started_at"], item["ended_at"], item["id"]))
        return result


def list_for_option(venture_id: str, option_id: str) -> list[dict]:
    return list_activity(venture_id, option_id)
