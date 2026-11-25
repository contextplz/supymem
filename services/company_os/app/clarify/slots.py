"""Charter slot inventory + criticality."""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import QuestionKind, SlotCriticality


@dataclass(frozen=True)
class SlotDef:
    key: str
    kind: QuestionKind
    criticality: SlotCriticality
    decision_impact: float
    prompt: str


SLOT_TAXONOMY: list[SlotDef] = [
    SlotDef(
        "goal_primary",
        QuestionKind.GOAL,
        SlotCriticality.CRITICAL,
        0.95,
        "What should we prove first for this idea?",
    ),
    SlotDef(
        "mvp_scope",
        QuestionKind.CONSTRAINT,
        SlotCriticality.CRITICAL,
        0.92,
        "What is in scope for the first usable version, and what waits?",
    ),
    SlotDef(
        "audience",
        QuestionKind.INPUT,
        SlotCriticality.CRITICAL,
        0.9,
        "Who is the first real user we should design for?",
    ),
    SlotDef(
        "market_reach",
        QuestionKind.INPUT,
        SlotCriticality.CRITICAL,
        0.88,
        "How far should we cast the market net on day one?",
    ),
    SlotDef(
        "gtm_order",
        QuestionKind.CONSTRAINT,
        SlotCriticality.IMPORTANT,
        0.85,
        "Build the product first, or test demand before investing in build?",
    ),
    SlotDef(
        "monetization",
        QuestionKind.INPUT,
        SlotCriticality.IMPORTANT,
        0.7,
        "How should this make money initially?",
    ),
    SlotDef(
        "channels",
        QuestionKind.INPUT,
        SlotCriticality.IMPORTANT,
        0.65,
        "Where will you find the first 20 users?",
    ),
    SlotDef(
        "integrations",
        QuestionKind.CONSTRAINT,
        SlotCriticality.IMPORTANT,
        0.7,
        "Which integrations are must-have on day one?",
    ),
    SlotDef(
        "brand_tone",
        QuestionKind.INPUT,
        SlotCriticality.NICE,
        0.3,
        "What tone should the product speak in?",
    ),
    SlotDef(
        "non_goals",
        QuestionKind.CONSTRAINT,
        SlotCriticality.IMPORTANT,
        0.6,
        "What are you explicitly not building yet?",
    ),
    SlotDef(
        "llm_runtime",
        QuestionKind.INPUT,
        SlotCriticality.CRITICAL,
        0.86,
        "Which model should this company think with?",
    ),
]


LLM_RUNTIME_SLOT = next(s for s in SLOT_TAXONOMY if s.key == "llm_runtime")

# GStack office hours — six forcing questions, in process order.
OFFICE_HOURS_ORDER = (
    "goal_primary",
    "market_reach",
    "audience",
    "mvp_scope",
    "gtm_order",
    "non_goals",
)


def office_hours_slots() -> list[SlotDef]:
    """The entire GStack office-hours process. Not a budgeted subset."""
    by_key = {slot.key: slot for slot in SLOT_TAXONOMY}
    return [by_key[key] for key in OFFICE_HOURS_ORDER]


def slots_for_budget(n: int) -> list[SlotDef]:
    """Prefer scope / validation / market over cosmetic slots."""
    priority = {
        "goal_primary": 0,
        "mvp_scope": 1,
        "audience": 2,
        "market_reach": 3,
        "gtm_order": 4,
        "monetization": 5,
        "channels": 6,
        "non_goals": 7,
        "integrations": 8,
        "brand_tone": 9,
        "llm_runtime": 100,
    }
    ordered = sorted(
        SLOT_TAXONOMY,
        key=lambda s: (priority.get(s.key, 50), -s.decision_impact),
    )
    return ordered[: max(1, n)]
