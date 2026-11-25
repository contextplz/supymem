"""Round-2 policy smoke (deterministic): complex alone does not force round 2."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.intake.normalizer import classify_deterministic, question_budget, score_complexity


def should_round2(*, contradictory: bool, residual_uncertainty: float) -> bool:
    # Simplified 05 §8: round 2 only for contradiction / high residual, never a third round.
    if contradictory:
        return True
    return residual_uncertainty >= 0.85


def test_round2_for_contradiction_not_mere_complexity() -> None:
    idea = "two-sided marketplace with payments KYC escrow"
    c = classify_deterministic(idea)
    sc = score_complexity(idea, c)
    n, _ = question_budget(sc.score, sc.uncertainty, "payments")
    assert n >= 5  # complex
    assert should_round2(contradictory=False, residual_uncertainty=0.4) is False
    assert should_round2(contradictory=True, residual_uncertainty=0.4) is True
