"""Validation chain: schema → static → tests → rl_smoke → shadow → peer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evolution.peer_review import review
from app.evolution.propose import Proposal, validate_proposal_shape


STAGES = ["schema", "static", "tests", "rl_smoke", "shadow", "peer"]
FORBIDDEN_META_TARGETS = frozenset(
    {
        "warden",
        "fitness_registry",
        "fitness_registry definitions",
        "gate constants",
        "memory port",
        "rollback machinery",
    }
)
_FORBIDDEN_PATHS = (
    "app/warden",
    "config/policy.baseline",
    "config/fitness_registry",
    "app/memory/port.py",
    "app/evolution/rollback",
    "gate constants",
)


@dataclass
class ValidationResult:
    passed: bool
    failed_stage: str | None = None
    summary: str = ""
    reviewed_by: str | None = None


def forbidden_meta_target(proposal: Proposal | dict[str, Any]) -> str | None:
    """Return the blocked target label when a proposal attempts evaluator edits."""
    values = proposal if isinstance(proposal, dict) else vars(proposal)
    target = " ".join(
        str(values.get(key, ""))
        for key in ("target", "target_id", "target_kind", "path", "hypothesis", "spec_json")
    ).lower()
    for forbidden in FORBIDDEN_META_TARGETS:
        if forbidden in target:
            return forbidden
    return next((path for path in _FORBIDDEN_PATHS if path in target), None)


def _log_forbidden(proposal: Proposal, target: str) -> None:
    # The audit writer is intentionally best-effort, so validation remains
    # deterministic if a local test has no configured database.
    from app.audit.writer import audit

    audit(
        "evolution.forbidden_meta_target",
        venture_id=getattr(proposal, "venture_id", None),
        target_ref=target,
        outcome="denied",
        denied_reason="forbidden_meta_target",
        payload={"proposal_id": proposal.id, "target": target},
    )


async def validate(
    proposal: Proposal,
    *,
    reviewer_id: str,
    fail_at: str | None = None,
    shadow_cycles: int = 3,
) -> ValidationResult:
    target = forbidden_meta_target(proposal)
    if target:
        _log_forbidden(proposal, target)
        return ValidationResult(False, "schema", "forbidden_meta_target")
    if not validate_proposal_shape(proposal):
        return ValidationResult(False, "schema", "shape invalid")
    for stage in STAGES:
        if fail_at == stage:
            return ValidationResult(False, stage, f"failed at {stage}")
        if stage == "shadow" and shadow_cycles < 3:
            return ValidationResult(False, "shadow", "need 3 shadow cycles")
        if stage == "peer":
            try:
                r = review(proposal.id, author_id=proposal.authored_by_agent_id, reviewer_id=reviewer_id)
            except PermissionError as e:
                return ValidationResult(False, "peer", str(e))
            return ValidationResult(True, summary="ok", reviewed_by=r.reviewed_by_agent_id)
    return ValidationResult(True, summary="ok")
