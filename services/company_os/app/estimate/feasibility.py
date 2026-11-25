"""Credit feasibility gate (docs/supymem/07 §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.credits.accounts import balance, get_account
from app.credits.packs import credit_packs
from app.credits.units import credits_to_micro, micro_to_credits
from app.estimate.model import ResourceEstimate


@dataclass
class Feasibility:
    ok: bool
    shortfall: int
    explanation: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)


def explain_shortfall(estimate: ResourceEstimate, *, available_micro: int, need: int) -> str:
    ranked = sorted(estimate.lines, key=lambda l: l.micro, reverse=True)
    drivers = []
    for line in ranked[:3]:
        if line.micro <= 0:
            continue
        drivers.append(
            f"{line.meter}: {micro_to_credits(line.micro)} credits ({line.basis})"
        )
    gap = micro_to_credits(need - available_micro)
    have = micro_to_credits(available_micro)
    head = f"Balance is {have} credits; this plan needs {micro_to_credits(need)}."
    if drivers:
        return head + " Top drivers: " + "; ".join(drivers) + f". Shortfall {gap} credits."
    return head + f" Shortfall {gap} credits."


def feasibility(workspace_id: str, estimate: ResourceEstimate) -> Feasibility:
    from app.credits.accounts import account_for_workspace

    account = account_for_workspace(workspace_id) or account_for_workspace(workspace_id)
    if account is None:
        available = 0
    else:
        available = int(balance(account)["available_micro"])
    need = estimate.total_upfront_micro + estimate.first_month_run_micro
    if available >= need:
        return Feasibility(ok=True, shortfall=0)
    shortfall = need - available
    packs = [p for p in credit_packs() if credits_to_micro(p.credits + p.bonus_credits) >= shortfall]
    pack = packs[0] if packs else (credit_packs()[-1] if credit_packs() else None)
    options = [
        {
            "kind": "TopUp",
            "pack": pack.sku if pack else None,
            "credits": (pack.credits + pack.bonus_credits) if pack else None,
        },
        {
            "kind": "ReduceScope",
            "candidates": [
                l.meter for l in sorted(estimate.lines, key=lambda x: x.micro, reverse=True) if l.micro
            ][:5],
        },
        {"kind": "Phase", "defer": "milestones after first paid user"},
        {
            "kind": "UseOwnResources",
            "candidates": [o.get("asset") for o in estimate.offsets] or ["llm_key", "cursor", "aws_credits"],
        },
    ]
    return Feasibility(
        ok=False,
        shortfall=shortfall,
        explanation=explain_shortfall(estimate, available_micro=available, need=need),
        options=options,
    )
