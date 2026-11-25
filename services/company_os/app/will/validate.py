"""Deterministic Slate composition rules (doc 79 §1.1)."""

from __future__ import annotations

from collections.abc import Callable

from app.will.model import Slate


class SlateValidationError(ValueError):
    """A slate violated an invariant and therefore must not be shown or saved."""


EvidenceResolver = Callable[[str], bool]


def validate_slate(
    slate: Slate,
    *,
    behavioural_signals: int = 0,
    evidence_resolver: EvidenceResolver | None = None,
) -> Slate:
    """Validate all six rules and return *slate* when it is safe to use."""
    items = slate.items
    violations: list[str] = []
    if len(items) != 5:
        violations.append("a slate must contain exactly five items")
    if sorted(item.rank for item in items) != [1, 2, 3, 4, 5]:
        violations.append("item ranks must be exactly 1 through 5")
    for item in items:
        if not item.evidence or not all(e.strip() for e in item.evidence):
            violations.append(f"rank {item.rank} has no resolvable evidence")
        elif evidence_resolver and not all(evidence_resolver(e) for e in item.evidence):
            violations.append(f"rank {item.rank} cites unresolvable evidence")
    if behavioural_signals < 30 and not any(item.origin == "probe" for item in items):
        violations.append("signal-starved ventures need at least one probe")
    if sum(item.autonomy == "needs_founder_action" for item in items) > 2:
        violations.append("at most two items may need founder action")
    if sum(item.autonomy == "auto" for item in items) < 3:
        violations.append("at least three items must be autonomous")
    directions: set[tuple[str, int]] = set()
    for item in items:
        direction = 1 if item.expected_delta >= 0 else -1
        key = (item.expected_metric, direction)
        if key in directions:
            violations.append(
                f"multiple items target {item.expected_metric} in the same direction"
            )
        directions.add(key)
    first = next((item for item in items if item.rank == 1), None)
    if first is None or first.blocked_by:
        violations.append("rank 1 must be executable now")
    if violations:
        raise SlateValidationError("; ".join(violations))
    return slate
