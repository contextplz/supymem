"""The engagement status machine (36 §3).

Pure functions over strings: no ORM, no I/O. Keeping the legal moves in one
table is what makes "an errand never silently becomes a project" and "delivered
is unreachable while an outcome is unmet" checkable rather than hoped for.
"""

from __future__ import annotations

STATUSES: tuple[str, ...] = (
    "triaging",
    "awaiting_goahead",
    "planned",
    "working",
    "blocked",
    "in_review",
    "delivered",
    "abandoned",
)

TERMINAL: frozenset[str] = frozenset({"delivered", "abandoned"})

# Statuses that count as an open engagement for the tile and for observe-level
# descent (mirrors app/workspaces/observe.py::IN_FLIGHT).
IN_FLIGHT: tuple[str, ...] = (
    "triaging",
    "awaiting_goahead",
    "planned",
    "working",
    "in_review",
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "triaging": frozenset({"awaiting_goahead", "planned", "working", "blocked", "abandoned"}),
    "awaiting_goahead": frozenset({"planned", "blocked", "abandoned"}),
    "planned": frozenset({"working", "blocked", "abandoned"}),
    # An errand has no review phase, so it may hand over straight from work.
    "working": frozenset({"in_review", "delivered", "blocked", "abandoned"}),
    "blocked": frozenset({"planned", "working", "in_review", "abandoned"}),
    "in_review": frozenset({"working", "delivered", "blocked", "abandoned"}),
    # A revert un-does the work, so it lands as abandoned with the reason given.
    "delivered": frozenset({"abandoned"}),
    "abandoned": frozenset(),
}

# The phase sequence each size actually walks (36 §5). `plan` and `review` are
# skipped for errands, and only a project pauses for a go-ahead.
PHASES_BY_SIZE: dict[str, tuple[str, ...]] = {
    "errand": ("orient", "triage", "work", "deliver"),
    "task": ("orient", "triage", "plan", "work", "review", "deliver"),
    "project": ("orient", "triage", "plan", "work", "review", "deliver"),
}


class LifecycleError(ValueError):
    """An illegal status move. Carries a founder-readable message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    if target not in STATUSES:
        raise LifecycleError("unknown_status", f"There is no '{target}' state.")
    if not can_transition(current, target):
        raise LifecycleError(
            "illegal_transition",
            f"An engagement can't go from {current.replace('_', ' ')} "
            f"to {target.replace('_', ' ')}.",
        )


def is_open(status: str) -> bool:
    return status in IN_FLIGHT or status == "blocked"


def first_status(size: str, *, needs_clarification: bool = False) -> str:
    """Where triage leaves an engagement.

    A project always stops for a go-ahead before spending anything past triage,
    and so does anything still waiting on its one clarification answer.
    """
    if size == "project" or needs_clarification:
        return "awaiting_goahead"
    if size == "errand":
        return "working"
    return "planned"


def next_status_for(size: str, current: str) -> str | None:
    """The next status on the happy path, or ``None`` at the end of it."""
    if current in TERMINAL:
        return None
    order: tuple[str, ...]
    if size == "errand":
        order = ("triaging", "working", "delivered")
    elif size == "project":
        order = ("triaging", "awaiting_goahead", "planned", "working", "in_review", "delivered")
    else:
        order = ("triaging", "planned", "working", "in_review", "delivered")
    if current == "blocked":
        return None
    try:
        index = order.index(current)
    except ValueError:
        return None
    if index + 1 >= len(order):
        return None
    return order[index + 1]


def phases_for_size(size: str) -> tuple[str, ...]:
    return PHASES_BY_SIZE.get(size, PHASES_BY_SIZE["task"])
