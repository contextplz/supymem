"""Work orders and the per-order component budget (doc 09 / LD-41)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Warn at 15, hard-fail at 20 components per work order (doc 21 P3.4).
BUDGET_WARN = 15
BUDGET_HARD = 20


@dataclass
class BudgetResult:
    ok: bool
    decomposition: list[str] = field(default_factory=list)
    warn: bool = False


@dataclass
class WorkOrder:
    id: str
    venture_id: str
    title: str
    components: list[str]
    files_expected: list[str]
    status: str = "planned"
    kind: str = "code"


_ORDERS: dict[str, WorkOrder] = {}
_SEQ = 0


def _next_id() -> str:
    global _SEQ
    _SEQ += 1
    return f"wo_{_SEQ}"


def check_component_budget(components: list[str] | None) -> BudgetResult:
    comps = list(components or [])
    n = len(comps)
    if n <= BUDGET_HARD:
        return BudgetResult(ok=True, warn=n >= BUDGET_WARN)
    chunks = [comps[i : i + 10] for i in range(0, n, 10)]
    decomposition = [
        f"Split into {len(chunks)} work orders of ≤10 components "
        f"(e.g. {', '.join(c[0] for c in chunks if c)})."
    ]
    return BudgetResult(ok=False, decomposition=decomposition, warn=True)


def create_workorder(
    venture_id: str,
    title: str = "",
    components: list[str] | None = None,
    files_expected: list[str] | None = None,
    **_: Any,
) -> WorkOrder:
    comps = list(components or [])
    budget = check_component_budget(comps)
    if not budget.ok:
        raise ValueError("component budget exceeded: " + "; ".join(budget.decomposition))
    wo = WorkOrder(
        id=_next_id(),
        venture_id=venture_id,
        title=title or "work",
        components=comps,
        files_expected=list(files_expected or []),
    )
    _ORDERS[wo.id] = wo
    return wo


def orders_for(venture_id: str) -> list[WorkOrder]:
    return [wo for wo in _ORDERS.values() if wo.venture_id == venture_id]


def clear_workorders() -> None:
    _ORDERS.clear()
