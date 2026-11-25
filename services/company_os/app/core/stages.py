"""Venture stage machine — table-driven legal transitions only."""

from __future__ import annotations

from core.enums import VentureStage
from core.errors import IllegalTransition

# Directed edges: from → frozenset(to)
TRANSITIONS: dict[VentureStage, frozenset[VentureStage]] = {
    VentureStage.DRAFT: frozenset({VentureStage.CLASSIFYING, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.CLASSIFYING: frozenset({VentureStage.CLARIFYING, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.CLARIFYING: frozenset({VentureStage.CHARTER_REVIEW, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.CHARTER_REVIEW: frozenset({VentureStage.ORG_PLANNING, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.ORG_PLANNING: frozenset({VentureStage.FORK_CHOICE, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.FORK_CHOICE: frozenset(
        {
            VentureStage.BUILDING,
            VentureStage.DEMAND_TESTING,
            VentureStage.PAUSED,
            VentureStage.ARCHIVED,
        }
    ),
    VentureStage.BUILDING: frozenset(
        {VentureStage.READINESS_REVIEW, VentureStage.PAUSED, VentureStage.ARCHIVED}
    ),
    VentureStage.DEMAND_TESTING: frozenset(
        {
            VentureStage.BUILDING,
            VentureStage.PIVOT,
            VentureStage.PAUSED,
            VentureStage.ARCHIVED,
        }
    ),
    VentureStage.READINESS_REVIEW: frozenset(
        {VentureStage.HOSTING, VentureStage.PAUSED, VentureStage.ARCHIVED}
    ),
    VentureStage.HOSTING: frozenset({VentureStage.LIVE, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.LIVE: frozenset({VentureStage.PAUSED, VentureStage.ARCHIVED, VentureStage.PIVOT}),
    VentureStage.PIVOT: frozenset({VentureStage.CLARIFYING, VentureStage.PAUSED, VentureStage.ARCHIVED}),
    VentureStage.PAUSED: frozenset(
        {
            VentureStage.DRAFT,
            VentureStage.CLASSIFYING,
            VentureStage.CLARIFYING,
            VentureStage.CHARTER_REVIEW,
            VentureStage.ORG_PLANNING,
            VentureStage.FORK_CHOICE,
            VentureStage.BUILDING,
            VentureStage.DEMAND_TESTING,
            VentureStage.READINESS_REVIEW,
            VentureStage.HOSTING,
            VentureStage.LIVE,
            VentureStage.ARCHIVED,
        }
    ),
    VentureStage.ARCHIVED: frozenset(),
}

_STRAIGHT_PATH = (
    VentureStage.DRAFT,
    VentureStage.CLASSIFYING,
    VentureStage.CLARIFYING,
    VentureStage.CHARTER_REVIEW,
    VentureStage.ORG_PLANNING,
    VentureStage.FORK_CHOICE,
    VentureStage.DEMAND_TESTING,
    VentureStage.BUILDING,
    VentureStage.READINESS_REVIEW,
    VentureStage.HOSTING,
    VentureStage.LIVE,
)


def can_transition(current: VentureStage | str, nxt: VentureStage | str) -> bool:
    cur = VentureStage(current)
    nxt_s = VentureStage(nxt)
    return nxt_s in TRANSITIONS.get(cur, frozenset())


def transition(current: VentureStage | str, nxt: VentureStage | str) -> VentureStage:
    cur = VentureStage(current)
    nxt_s = VentureStage(nxt)
    allowed = TRANSITIONS.get(cur, frozenset())
    if nxt_s not in allowed:
        raise IllegalTransition(
            f"illegal stage transition {cur.value} → {nxt_s.value}",
            detail={"from": cur.value, "to": nxt_s.value},
        )
    return nxt_s


def progress(current: VentureStage | str, target: VentureStage | str) -> VentureStage:
    """Advance across a valid multi-step journey without permitting regression."""
    start = VentureStage(current)
    goal = VentureStage(target)
    if start == goal:
        return goal
    if start is VentureStage.PAUSED and can_transition(start, goal):
        return goal
    if start in _STRAIGHT_PATH and goal in _STRAIGHT_PATH and _STRAIGHT_PATH.index(goal) > _STRAIGHT_PATH.index(start):
        return goal
    raise IllegalTransition(
        f"illegal stage progression {start.value} → {goal.value}",
        detail={"from": start.value, "to": goal.value},
    )


def reach_at_least(current: VentureStage | str, target: VentureStage | str) -> VentureStage:
    """Reach a straight-path stage while preserving an already later stage."""
    start = VentureStage(current)
    goal = VentureStage(target)
    if start in _STRAIGHT_PATH and goal in _STRAIGHT_PATH:
        if _STRAIGHT_PATH.index(start) >= _STRAIGHT_PATH.index(goal):
            return start
    return progress(start, goal)
