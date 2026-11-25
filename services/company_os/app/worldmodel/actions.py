"""Action shapes for simulation arms."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    kind: str
    cost_usd: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)


NO_OP = Action(kind="noop", cost_usd=0.0)

QUANTITIES_FOR = {
    "noop": ["rate_limit_ok"],
    "config_change": ["engagement_rate", "rate_limit_ok", "budget_ok"],
    "org_replace": ["engagement_rate", "stage_velocity_days"],
    "campaign": ["qualified_signups_per_usd", "budget_ok"],
}
