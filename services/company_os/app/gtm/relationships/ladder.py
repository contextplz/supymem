"""Ask ladder (OT-16). Sibling of ``state_machine.py`` — do not copy that file.

Phase 0 subset: sequential rungs only. Illegal skips are refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

RUNGS: Final[tuple[str, ...]] = (
    "none",
    "connect",
    "question",
    "value_return",
    "call_15",
    "session",
    "paid",
    "dropped",
)

_ORDER: Final[dict[str, int]] = {
    "none": 0,
    "connect": 1,
    "question": 2,
    "value_return": 3,
    "call_15": 4,
    "session": 5,
    "paid": 6,
}


@dataclass(frozen=True)
class TransitionResult:
    ok: bool
    from_rung: str
    to_rung: str
    reason: str = ""


def can_transition(from_rung: str, to_rung: str) -> TransitionResult:
    """Allow same rung, drop from anywhere, or a single step forward. No skips."""

    if from_rung not in RUNGS:
        return TransitionResult(False, from_rung, to_rung, "unknown_from")
    if to_rung not in RUNGS:
        return TransitionResult(False, from_rung, to_rung, "unknown_to")
    if from_rung == to_rung:
        return TransitionResult(True, from_rung, to_rung, "same")
    if to_rung == "dropped":
        return TransitionResult(True, from_rung, to_rung)
    if from_rung == "dropped":
        return TransitionResult(False, from_rung, to_rung, "dropped")
    if from_rung not in _ORDER or to_rung not in _ORDER:
        return TransitionResult(False, from_rung, to_rung, "unknown")
    if _ORDER[to_rung] == _ORDER[from_rung] + 1:
        return TransitionResult(True, from_rung, to_rung)
    return TransitionResult(False, from_rung, to_rung, "illegal_skip")


def call_15_allowed(
    *,
    from_rung: str,
    has_value_return: bool,
    sku_approved: bool,
) -> TransitionResult:
    """OT-43: meeting ask needs sequential value_return plus an approved SKU."""

    base = can_transition(from_rung, "call_15")
    if not base.ok:
        return base
    if not has_value_return:
        return TransitionResult(False, from_rung, "call_15", "need_value_return")
    if not sku_approved:
        return TransitionResult(False, from_rung, "call_15", "need_approved_sku")
    return TransitionResult(True, from_rung, "call_15")
