"""Evolution cycle orchestration with budgets (24 §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evolution.archive import has_similar_failure
from app.evolution.diagnose import run_deterministic_detectors, synthesize_diagnosis
from app.evolution.propose import generate_proposal
from app.evolution.validate import validate
from app.evolution.promote import MAX_PROMOTIONS_PER_CYCLE, promote
from app.feedback.fitness import soft_only_promotion_allowed
from app.worldmodel.actions import Action
from app.worldmodel.simulate import simulate


@dataclass
class TokenBudget:
    limit: int
    used: int = 0

    def charge(self, n: int) -> bool:
        if self.used + n > self.limit:
            return False
        self.used += n
        return True

    def exhausted(self) -> bool:
        return self.used >= self.limit


@dataclass
class CycleResult:
    status: str
    note: str = ""
    findings: int = 0
    promotions: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    pro_calls: int = 0
    reason: str = ""


async def run_cycle(
    venture: dict[str, Any],
    *,
    budget_tokens: int = 10_000,
    kill_switch: bool = False,
    drift_frozen: bool = False,
    author_id: str = "ag_author",
    reviewer_id: str = "ag_reviewer",
) -> CycleResult:
    if kill_switch:
        return CycleResult(status="aborted", reason="kill_switch")
    if drift_frozen:
        return CycleResult(status="aborted", reason="constraint_drift_freeze")

    ledger = TokenBudget(budget_tokens)
    findings = run_deterministic_detectors(venture)
    if not findings:
        return CycleResult(status="finished", note="healthy; no Pro call spent", cost_usd=0.0, pro_calls=0)

    if not ledger.charge(500):
        return CycleResult(status="aborted", reason="token_budget", tokens_used=ledger.used)

    diagnosis = synthesize_diagnosis(findings)
    promoted = 0
    for hyp in diagnosis["hypotheses"]:
        if ledger.exhausted():
            return CycleResult(
                status="finished",
                reason="token_budget",
                findings=len(findings),
                promotions=promoted,
                tokens_used=ledger.used,
                pro_calls=diagnosis["pro_calls"],
                cost_usd=diagnosis["cost_usd"],
            )
        if has_similar_failure(hyp):
            continue
        if not ledger.charge(800):
            return CycleResult(
                status="finished",
                reason="token_budget",
                findings=len(findings),
                promotions=promoted,
                tokens_used=ledger.used,
                pro_calls=1,
                cost_usd=0.01,
            )
        prop = generate_proposal(hyp, authored_by=author_id, evidence=findings[0].evidence)
        sim = await simulate(venture.get("id") or "ven_x", Action(kind=hyp.get("kind", "config_change")))
        if sim.verdict == "unfavourable":
            continue
        # insufficient_data does NOT block
        v = await validate(prop, reviewer_id=reviewer_id)
        if not v.passed:
            continue
        if promoted >= MAX_PROMOTIONS_PER_CYCLE:
            break
        try:
            promote(
                prop,
                target_kind="org" if prop.tier == "org" else "config",
                target_id=hyp.get("subject") or "target",
                metrics_used=[prop.predicted_metric] if soft_only_promotion_allowed([prop.predicted_metric]) else ["stage_velocity_days"],
            )
            promoted += 1
        except PermissionError:
            continue

    return CycleResult(
        status="finished",
        findings=len(findings),
        promotions=promoted,
        tokens_used=ledger.used,
        pro_calls=diagnosis["pro_calls"],
        cost_usd=diagnosis["cost_usd"],
    )
