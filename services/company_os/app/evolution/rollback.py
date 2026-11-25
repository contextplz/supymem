"""Rollback for every target kind."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.evolution.archive import archive


@dataclass
class RollbackResult:
    ok: bool
    reason: str
    kind: str


def rollback(promotion: dict[str, Any], *, reason: str) -> RollbackResult:
    kind = promotion.get("target_kind") or promotion.get("kind") or "config"
    # All kinds supported
    if kind in {"agent_spec", "workflow", "tool", "org", "product_code", "strategy", "config"}:
        archive(
            {
                "hypothesis": promotion.get("hypothesis") or promotion.get("proposal_id"),
                "predicted_metric": (promotion.get("baseline") or {}).get("metric"),
                "tier": kind,
                "outcome": "rolled_back",
                "reason": reason,
            }
        )
        promotion["status"] = "rolled_back"
        return RollbackResult(True, reason, kind)
    return RollbackResult(False, f"unknown_kind:{kind}", kind or "?")
