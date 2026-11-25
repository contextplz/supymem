"""Proposal generation — falsifiable, evidenced, metered, rollback declared."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.ids import new_id


REQUIRED = ("hypothesis", "evidence", "predicted_metric", "rollback")


@dataclass
class Proposal:
    id: str
    tier: str  # config|org|code|strategy
    hypothesis: str
    evidence: list[str]
    predicted_metric: str
    predicted_delta: float
    rollback: dict[str, Any]
    authored_by_agent_id: str
    trust_of_evidence: str = "internal"
    status: str = "proposed"
    soft_metrics_only: bool = False
    # Filled by proposal generation when the mutation surface is known. Optional
    # for compatibility with the original evolution loop.
    target_kind: str | None = None
    target_id: str | None = None
    venture_id: str | None = None
    parent_archive_ids: list[str] = field(default_factory=list)


def validate_proposal_shape(p: Proposal | dict[str, Any]) -> bool:
    if isinstance(p, Proposal):
        d = p.__dict__
    else:
        d = p
    for k in REQUIRED:
        if not d.get(k):
            return False
    return True


def generate_proposal(
    hyp: dict[str, Any],
    *,
    authored_by: str,
    evidence: list[str],
    trust: str = "internal",
) -> Proposal:
    tier = "org" if hyp.get("kind") == "org_replace" else "config"
    p = Proposal(
        id=new_id("prop"),
        tier=tier,
        hypothesis=str(hyp),
        evidence=list(evidence) or ["detector"],
        predicted_metric="engagement_rate",
        predicted_delta=0.05,
        rollback={"kind": tier, "action": "restore_previous"},
        authored_by_agent_id=authored_by,
        trust_of_evidence=trust,
    )
    if not validate_proposal_shape(p):
        raise ValueError("invalid proposal shape")
    return p
