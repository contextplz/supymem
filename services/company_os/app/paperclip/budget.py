"""Budget status reporting. Ported from Paperclip ``budgets.ts``.

``budgetStatusFromObserved`` (warn at ``warnPercent`` of the cap, hard_stop at
the cap, amount<=0 is unlimited). Venture work has no spend-cap hard stop.

MIT: Paperclip AI.
"""

from __future__ import annotations

from typing import Literal

BudgetStatus = Literal["ok", "warning", "hard_stop"]


def budget_status_from_observed(
    observed_amount: float,
    amount: float,
    warn_percent: float,
) -> BudgetStatus:
    """Copy of Paperclip ``budgetStatusFromObserved``."""
    if amount <= 0:
        return "ok"
    if observed_amount >= amount:
        return "hard_stop"
    if observed_amount >= (amount * warn_percent) / 100:
        return "warning"
    return "ok"


def hard_stop_for_venture(venture_id: str) -> dict | None:
    """Legacy scheduler hook: spend caps never block venture work."""
    return None
