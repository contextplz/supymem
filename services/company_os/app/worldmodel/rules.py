"""Layer-1 hard rules — never skip to estimators/LLM when rules cover a quantity."""

from __future__ import annotations

from typing import Any

# quantity -> fixed value when rule applies
RULES: dict[str, float] = {
    "rate_limit_ok": 1.0,
    "landing_conversion_floor": 0.01,
}


def rules_rate(quantity: str, state: Any, action: Any) -> float | None:
    if quantity == "rate_limit_ok":
        return 1.0
    if quantity == "budget_ok":
        cost = float(getattr(action, "cost_usd", 0) or (action.get("cost_usd") if isinstance(action, dict) else 0) or 0)
        rem = float(getattr(state, "budget_remaining_usd", 0))
        return 1.0 if cost <= rem else 0.0
    if quantity in RULES and quantity.startswith("rule_"):
        return RULES[quantity]
    # Covered quantities from RULES keys
    if quantity in RULES:
        return RULES[quantity]
    return None
