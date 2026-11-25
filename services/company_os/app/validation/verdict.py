"""Deterministic validation verdicts derived from research structure only."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.research.evidence_dimensions import evidence_dimensions, explicit_demand_leads


class ValidationVerdict(str, Enum):
    PRELIMINARY = "Preliminary"
    STRONG = "Strong"
    PROMISING = "Promising"
    MIXED = "Mixed"
    WEAK = "Weak"
    DONT = "Don't"
    UNAVAILABLE = "Unavailable"


class VerdictResult(BaseModel):
    verdict: ValidationVerdict
    confidence: float | None
    competitor_count: int
    demand_signal_count: int
    differentiation_score: float | None
    recommendation: str
    evidence_dimensions: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple, set, dict)) else int(value or 0)


def compute_verdict(research: dict[str, Any] | None) -> VerdictResult:
    """Give a bounded research disposition, never a viability vote from missing data."""
    research = research or {}
    mode = str(research.get("mode") or "").lower()
    if mode and mode != "live":
        reason = str(research.get("reason") or "research_did_not_run")
        return VerdictResult(
            verdict=ValidationVerdict.UNAVAILABLE,
            confidence=0.0,
            competitor_count=0,
            demand_signal_count=0,
            differentiation_score=None,
            recommendation=(
                "Research did not run on the live web "
                f"({reason}). Retry the scan; if it persists, report the research failure. "
                "This is not a market verdict."
            ),
            evidence_dimensions=evidence_dimensions([], []),
        )
    competitors = _count(research.get("competitors"))
    signals = len(explicit_demand_leads(research.get("demand_signals")))
    sources = research.get("sources") or []
    if not sources:
        return VerdictResult(
            verdict=ValidationVerdict.UNAVAILABLE,
            confidence=0.0,
            competitor_count=competitors,
            demand_signal_count=0,
            differentiation_score=None,
            recommendation="No source evidence is available. Research gaps remain unknown; retry or broaden the scan.",
            evidence_dimensions=evidence_dimensions([], []),
        )
    raw_gap = research.get("differentiation_score", research.get("differentiation"))
    if isinstance(raw_gap, bool):
        differentiation = 1.0 if raw_gap else 0.0
    elif isinstance(raw_gap, (int, float)):
        differentiation = max(0.0, min(1.0, float(raw_gap)))
    else:
        differentiation = None

    # Search excerpts can prioritize tests, but cannot yield a viability verdict
    # or a meaningful numeric confidence about demand or willingness to pay.
    dimensions = research.get("evidence_dimensions")
    if not isinstance(dimensions, dict):
        dimensions = evidence_dimensions(sources, research.get("competitors") or [])
    confidence = None
    verdict = ValidationVerdict.PRELIMINARY
    # Prioritize one useful move from the actual coverage. The cited lead is
    # shown separately by callers; this is an experiment proposal, not a
    # positive market verdict or an assertion that anyone will pay.
    if dimensions.get("customer_demand", {}).get("support"):
        next_test = "Ask the person behind the customer-request lead whether the founder's narrow offer solves their specific problem, then request a paid pilot."
    elif dimensions.get("problem_validation", {}).get("support"):
        next_test = "Interview people who report this problem, compare their current workaround with the founder's offer, and request a paid pilot."
    elif dimensions.get("competition", {}).get("support"):
        next_test = "Compare the closest alternative with the founder's proposed workflow, then ask five target customers which difference matters enough to try or pay for."
    else:
        next_test = "Ask five target customers about their current workaround and offer a small paid pilot; record refusals and reasons as well as interest."
    recommendation = (
        "The founder's idea is still worth a bounded test. This scan cannot establish demand or reject the thesis. "
        + next_test
    )
    return VerdictResult(
        verdict=verdict,
        confidence=confidence,
        competitor_count=competitors,
        demand_signal_count=signals,
        differentiation_score=round(differentiation, 2) if differentiation is not None else None,
        recommendation=recommendation,
        evidence_dimensions=dimensions,
    )
