"""Content-free founder journey and dead-end receipts in our analytics table."""
import json
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.analytics.events import emit_in_session
from app.analytics.feature_catalog import INGEST_FEATURES
from app.db.models import AnalyticsEvent, Venture
from app.state import db
from core.clock import utcnow

STEPS = ("idea", "clarify", "validate", "review", "find_customers", "review_leads", "sent", "call_booked")
IDEA_STEPS = {"open": "idea", "recon": "clarify", "clarifying": "clarify", "proposing": "review",
              "validating": "validate", "reviewing": "review"}
OUTCOMES = ("ok", "blocked", "timeout", "auth_lost", "model_unavailable", "needs_founder")


def stage_step(session, step, phase, *, key, venture_id, workspace_id=None):
    if step not in STEPS or phase not in {"entered", "completed"}:
        return
    if workspace_id is None and (venture := session.get(Venture, venture_id)):
        workspace_id = venture.workspace_id
    emit_in_session(session, f"journey.step_{phase}", event_key=key, venture_id=venture_id,
                    workspace_id=workspace_id, props={"step": step}, flush=False)


# The server image does not ship packages/desktop_runtime, so this mirrors cu_reliability.outcome_code
# (a test keeps the two identical). Importing it here made every Mac stop receipt fail with a 500.
_TIMEOUT = {"timeout", "timed_out", "wall_time_exceeded", "step_limit_reached", "cu_timeout", "cu_time_limit",
            "cu_wall_timeout", "cu_budget_exhausted", "ai_loop_timeout", "ai_loop_time_limit"}
_AUTH_LOST = {"auth_lost", "auth_required", "cu_auth_lost", "cu_auth_required", "cu_login_required",
              "cu_session_expired", "native_account_changed", "native_session_unavailable", "desktop_auth_required"}
_MODEL = {"model_unavailable", "cu_model_unavailable", "cu_browser_decision_unavailable", "cu_provider_unavailable",
          "ai_loop_model_unavailable", "desktop_model_unavailable"}
_BLOCKED = {"blocked", "origin_violation_stopped", "action_requires_review", "cu_blocked", "cu_policy_blocked",
            "cu_permission_denied", "cu_permission_missing", "cu_safety_blocked", "host_permissions_missing"}


def computer_outcome(*, verified, reason):
    if verified is True:
        return "ok"
    codes = {reason} if isinstance(reason, str) else set()
    for outcome, known in (("timeout", _TIMEOUT), ("auth_lost", _AUTH_LOST), ("model_unavailable", _MODEL), ("blocked", _BLOCKED)):
        if codes & known:
            return outcome
    return "needs_founder"


def stage_computer_outcome(session, body):
    outcome = computer_outcome(verified=(body.get("native_stop") or {}).get("task_verified"),
                               reason=body.get("stop_reason_code"))
    if outcome != "ok" and body.get("outcome_code") in OUTCOMES[1:]:
        outcome = body["outcome_code"]
    emit_in_session(session, "agent.computer_outcome", event_key=body["execution_id"],
                    workspace_id=body["workspace_id"], venture_id=body["venture_id"], props={"outcome": outcome})


def founder_observability(hours=24):
    now = utcnow().replace(tzinfo=None)
    since = now - timedelta(hours=hours)
    entered, completed = defaultdict(set), defaultdict(set)
    dead_ends, outcomes = Counter(), Counter()
    with db()() as session:
        rows = session.execute(select(AnalyticsEvent.event_name, AnalyticsEvent.venture_id,
            AnalyticsEvent.workspace_id, AnalyticsEvent.props_json).where(
                AnalyticsEvent.occurred_at >= since, AnalyticsEvent.occurred_at <= now,
                AnalyticsEvent.event_name.in_(("journey.step_entered", "journey.step_completed",
                                               "ui.dead_end", "agent.computer_outcome"))))
        for name, venture_id, workspace_id, raw in rows:
            try:
                props = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(props, dict):
                continue
            step = props.get("step")
            if name.startswith("journey.") and step in STEPS and venture_id:
                (entered if name.endswith("entered") else completed)[step].add((workspace_id, venture_id))
            elif name == "ui.dead_end" and props.get("feature") in INGEST_FEATURES and props.get("code") in ("ui_error_shown", "reconnect_shown", "empty_no_action", "action_failed"):
                dead_ends[(props.get("feature"), props["code"])] += 1
            elif name == "agent.computer_outcome" and props.get("outcome") in OUTCOMES:
                outcomes[props["outcome"]] += 1
    return {"hours": hours, "steps": [{"step": step, "entered": len(entered[step]),
            "completed": len(completed[step])} for step in STEPS],
            "dead_ends": [{"feature": feature, "code": code, "count": count}
                          for (feature, code), count in dead_ends.most_common(20)],
            "computer_outcomes": {outcome: outcomes[outcome] for outcome in OUTCOMES}}
