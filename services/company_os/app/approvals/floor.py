"""The seven things that always need a human (42 §4).

Independent of autonomy level: L4 does not buy past this list. The system may
*prepare* all seven fully, so approving is one click on something concrete.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FloorAction:
    key: str
    why: str
    risk: str = "high"


FLOOR_ACTIONS: dict[str, FloorAction] = {
    "merge_default_branch": FloorAction(
        "merge_default_branch", "merging to the default branch of a repo you own"
    ),
    "deploy_production": FloorAction(
        "deploy_production", "deploying to an environment you marked production"
    ),
    "external_comms": FloorAction(
        "external_comms", "sending something to a real person outside the company"
    ),
    "spend_money": FloorAction("spend_money", "spending money"),
    "delete_foreign_resource": FloorAction(
        "delete_foreign_resource", "deleting data or a resource that isn't ours"
    ),
    "widen_permissions": FloorAction(
        "widen_permissions", "widening what this system is allowed to do"
    ),
    # The catch-all: a high-risk classification the Warden is not sure about.
    "warden_high_risk": FloorAction(
        "warden_high_risk", "an action the safety check flagged and wasn't sure about"
    ),
}

# Warden confidence at or above this needs no extra human step for `high` risk;
# below it, the seventh floor action applies (42 §4.7).
WARDEN_CONFIDENCE_FLOOR = 0.9


def requires_human(
    action: str,
    *,
    autonomy_level: str = "L4",
    risk: str = "low",
    warden_confidence: float = 1.0,
) -> FloorAction | None:
    """Return the floor rule that forces a human, or None."""
    hit = FLOOR_ACTIONS.get(action)
    if hit is not None:
        return hit
    if risk == "high" and warden_confidence < WARDEN_CONFIDENCE_FLOOR:
        return FLOOR_ACTIONS["warden_high_risk"]
    return None
