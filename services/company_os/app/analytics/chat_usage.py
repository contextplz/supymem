"""Closed server-side usage labels for authenticated chat action dispatch."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from app.analytics.events import emit

# These are implemented action paths (including the guarded Idea answer bridge).
# A model suggestion, confirmation card, or arbitrary action string cannot mint usage.
ACTION_FEATURE = {
    "inbox.briefing.consent": "settings", "inbox.briefing.settings": "settings", "inbox.item.feedback": "settings",
    "status": "ventures", "why": "chat", "money": "credits", "leads": "ventures",
    "research": "research", "research.start": "research", "research.update_and_rerun": "research", "agents": "execution",
    "whats_next": "planning", "refine_site": "website",
    "openworker_search": "research", "agent_reach": "research",
    "pause": "execution", "cancel": "planning", "approve": "planning",
    "slate_override": "planning", "office_hours": "planning",
    "ceo_review": "planning", "brain_brief": "chat", "social_post": "content",
    "social_guide": "content", "social_status": "content", "social_connect": "settings",
    "gtm.diagnosis": "gtm", "credits.balance": "credits",
    "connections.status": "settings", "connections.connect": "settings", "connections.disconnect": "settings", "content.drafts": "content",
    "idea.answer": "idea",
    "site.preview": "website", "site.rewrite": "website", "site.publish": "website", "site.unpublish": "website",
    "waitlist.enable": "website", "waitlist.test": "website",
    "plan.resources_none": "planning", "plan.quote": "planning", "plan.generate": "planning",
    "plan.approve": "planning",
    "build.prepare": "builds", "build.app.create": "builds", "build.start": "builds",
    "work.start": "execution", "resume": "execution", "blocker.explain": "execution", "chat.model_retry": "chat",
    "jobs.status": "execution", "job.retry": "execution", "outreach.draft": "gtm", "mac.task.prepare": "computer_use",
    "gtm.draft_rewrite": "gtm",
    # Experiments are the idea-validation loop; the closed feature list has no separate label.
    "experiment.list": "idea", "experiment.start": "idea", "experiment.record": "idea",
    "experiment.revise": "idea", "experiment.run_review": "idea",
}

# Router handlers that actually read a feature's saved state and return it to
# the founder, without going through execute_action. Navigation offers and
# classified intents alone are deliberately absent: neither uses the feature.
READ_INTENT_FEATURE = {
    "price_band.calculate": "idea", "unit_economics.calculate": "planning",
    "gtm_review": "gtm",
    "open_screen.analytics_read": "analytics",
    "open_screen.builds_read": "builds",
}

# Reviewed, route-specific chat actions use their own endpoint rather than the
# generic chat action registry. Keep these closed labels separate from dispatch.
REVIEWED_CHAT_FEATURE = {"gtm.diagnosis.start": "gtm"}

_ACTOR: ContextVar[tuple[str, str | None, str] | None] = ContextVar("chat_usage_actor", default=None)


@contextmanager
def attributed_chat_action(person_id: str | None, workspace_id: str | None,
                           venture_id: str) -> Iterator[None]:
    """Attach the verified request actor only for this async request context."""
    token = _ACTOR.set((person_id, workspace_id, venture_id) if person_id else None)
    try:
        yield
    finally:
        _ACTOR.reset(token)


def _registered(action_name: str) -> bool:
    from app.chat.actions import REGISTRY

    return action_name in REGISTRY


def record_chat_action(venture_id: str, action_name: str, outcome: str) -> None:
    actor = _ACTOR.get()
    feature = ACTION_FEATURE.get(action_name)
    # A registered action outside the closed feature map is still counted as a
    # tool call (feature ``chat``), so nothing dispatched is silently dropped;
    # the report views resolve its fine label through the feature catalog.
    counted_as_feature = feature is not None
    if feature is None and _registered(action_name):
        feature = "chat"
    if actor is None or feature is None or actor[2] != venture_id:
        return
    person_id, workspace_id, _ = actor
    props = {"source": "chat", "tool": action_name, "feature": feature, "outcome": outcome}
    emit("chat.tool_invoked", person_id=person_id, workspace_id=workspace_id,
         venture_id=venture_id, props=props)
    if outcome == "returned" and counted_as_feature:
        # A returned handler is feature use, including handled failure replies;
        # it is not a successful downstream job or useful answer.
        emit("feature.used", person_id=person_id, workspace_id=workspace_id,
             venture_id=venture_id, props={"source": "chat", "feature": feature, "tool": action_name})


def record_served_chat_read(venture_id: str, intent: str) -> None:
    """Count a saved, founder-visible feature read with closed dimensions only."""
    actor = _ACTOR.get()
    feature = READ_INTENT_FEATURE.get(intent)
    if actor is None or feature is None or actor[2] != venture_id:
        return
    person_id, workspace_id, _ = actor
    emit("chat.tool_invoked", person_id=person_id, workspace_id=workspace_id,
         venture_id=venture_id, props={"source": "chat", "tool": intent,
                                        "feature": feature, "outcome": "returned"})
    emit("feature.used", person_id=person_id, workspace_id=workspace_id,
         venture_id=venture_id, props={"source": "chat", "feature": feature,
                                        "tool": intent})


def record_provider_error_reply(person_id: str | None, workspace_id: str | None,
                                venture_id: str) -> None:
    """Record a saved, founder-visible provider failure without its message or prompt."""
    if not person_id:
        return
    emit("chat.reply_error", person_id=person_id, workspace_id=workspace_id,
         venture_id=venture_id, props={"source": "chat", "failure_kind": "provider_call"})


def _actor_for(venture_id: str) -> tuple[str, str | None] | None:
    actor = _ACTOR.get()
    return (actor[0], actor[1]) if actor is not None and actor[2] == venture_id else None


def record_confirm_failure(venture_id: str, action_name: str, reason: str, status: int) -> None:
    """A confirm card that did not execute (rejected, stale, short of credits, handler error).

    Closed labels only: the tool name is recorded only when it is a registered action.
    """
    who = _actor_for(venture_id)
    if who is None:
        return
    from app.analytics.error_events import log_error_event
    from app.analytics.feature_catalog import ACTION_SLUG

    tool = action_name if _registered(action_name) else "unknown"
    feature = ACTION_SLUG.get(tool, "chat")
    emit("chat.reply_error", person_id=who[0], workspace_id=who[1], venture_id=venture_id,
         props={"source": "chat", "failure_kind": "confirm_failed", "reason": reason, "tool": tool,
                "feature": feature, "status": status})
    log_error_event("chat", feature=feature, code=f"confirm_{reason}", venture_id=venture_id,
                    person_id=who[0], status=status)


def record_guard(venture_id: str, kind: str) -> None:
    """The deterministic chat guard (or the claims guard) replaced or refused a reply."""
    who = _actor_for(venture_id)
    if who is None:
        return
    emit("guard.flag", person_id=who[0], workspace_id=who[1], venture_id=venture_id,
         props={"source": "chat", "feature": "guards", "kind": kind})
