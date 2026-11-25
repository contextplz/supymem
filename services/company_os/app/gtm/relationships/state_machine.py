"""Relationship state machine (doc 115 §5)."""

from __future__ import annotations

from dataclasses import dataclass

from core.clock import utcnow

RELATIONSHIP_STAGES = frozenset(
    {
        "new",
        "researched",
        "congratulated",
        "nudged",
        "conversing",
        "friend",
        "offered",
        "converted",
        "cooling",
        "nurturing",
        "dormant",
        "customer_care",
        "disqualified",
        "unsubscribed",
        "bounced",
        "do_not_contact",
    }
)

TERMINAL = frozenset(
    {"disqualified", "unsubscribed", "bounced", "do_not_contact", "customer_care"}
)

_EDGES: dict[str, frozenset[str]] = {
    "new": frozenset({"researched", "disqualified", "do_not_contact"}),
    "researched": frozenset({"congratulated", "dormant", "disqualified"}),
    "congratulated": frozenset({"nudged", "conversing", "dormant", "cooling"}),
    "nudged": frozenset({"conversing", "dormant", "cooling"}),
    "conversing": frozenset({"friend", "cooling", "offered"}),
    "friend": frozenset({"offered", "nurturing", "cooling"}),
    "offered": frozenset({"converted", "nurturing", "cooling"}),
    "converted": frozenset({"customer_care"}),
    "cooling": frozenset({"nurturing", "dormant", "conversing"}),
    "nurturing": frozenset({"friend", "offered", "dormant"}),
    "dormant": frozenset({"researched", "nurturing"}),
}


@dataclass(frozen=True)
class TransitionResult:
    ok: bool
    from_stage: str
    to_stage: str
    reason: str = ""


def can_transition(from_stage: str, to_stage: str) -> TransitionResult:
    if from_stage not in RELATIONSHIP_STAGES:
        return TransitionResult(False, from_stage, to_stage, "unknown_from")
    if to_stage not in RELATIONSHIP_STAGES:
        return TransitionResult(False, from_stage, to_stage, "unknown_to")
    if from_stage in TERMINAL and to_stage != from_stage:
        return TransitionResult(False, from_stage, to_stage, "terminal")
    if to_stage in {"unsubscribed", "bounced", "do_not_contact", "disqualified"}:
        return TransitionResult(True, from_stage, to_stage)
    allowed = _EDGES.get(from_stage, frozenset())
    if to_stage not in allowed:
        return TransitionResult(False, from_stage, to_stage, "edge_missing")
    return TransitionResult(True, from_stage, to_stage)


def transition(contact, to_stage: str) -> TransitionResult:
    result = can_transition(contact.relationship_stage, to_stage)
    if not result.ok:
        return result
    contact.relationship_stage = to_stage
    contact.stage_changed_at = utcnow()
    return result
