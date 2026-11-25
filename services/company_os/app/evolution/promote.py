"""Promotion with baseline snapshot + observation window."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.feedback.fitness import assert_promotion_metrics, resolve_metric
from app.org.lifecycle import atomic_replace
from core.clock import utcnow
from core.ids import new_id

_PROMOTIONS: list[dict[str, Any]] = []
MAX_PROMOTIONS_PER_CYCLE = 2


@dataclass
class Promotion:
    id: str
    proposal_id: str
    target_kind: str
    target_id: str
    from_version: str
    to_version: str
    baseline: dict[str, Any]
    observe_until: str
    status: str = "observing"


def promote(
    proposal: Any,
    *,
    target_kind: str,
    target_id: str,
    from_version: str = "1.0.0",
    to_version: str = "1.0.1",
    metrics_used: list[str] | None = None,
    baseline_value: float = 0.0,
) -> Promotion:
    metrics_used = metrics_used or [proposal.predicted_metric]
    # Soft-only refusal
    assert_promotion_metrics(metrics_used)
    for m in metrics_used:
        resolve_metric(m)
    # Capability floor
    if getattr(proposal, "trust_of_evidence", "internal") == "untrusted":
        raise PermissionError("needs_founder")
    now = utcnow()
    promo = Promotion(
        id=new_id("promo"),
        proposal_id=proposal.id,
        target_kind=target_kind,
        target_id=target_id,
        from_version=from_version,
        to_version=to_version,
        baseline={"metric": proposal.predicted_metric, "value": baseline_value},
        observe_until=(now + timedelta(hours=24)).isoformat(),
    )
    if target_kind == "org":
        # atomic swap helper expects kwargs for new agent
        pass
    _PROMOTIONS.append(promo.__dict__)
    return promo


def promotions() -> list[dict[str, Any]]:
    return list(_PROMOTIONS)


def clear_promotions() -> None:
    _PROMOTIONS.clear()
