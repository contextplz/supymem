"""Question budget stays in [3,9] with reasons."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.intake.normalizer import classify_deterministic, question_budget, score_complexity


def test_trivial_idea_yields_3() -> None:
    idea = "a notes app"
    c = classify_deterministic(idea)
    sc = score_complexity(idea, c)
    n, reason = question_budget(sc.score, sc.uncertainty, c["regulatory_surface"])
    assert n == 3
    assert reason["reasons"]


def test_regulated_marketplace_yields_8_or_9() -> None:
    idea = (
        "a two-sided marketplace for medical freelancers with payments, "
        "KYC, escrow, and insurance verification for minors"
    )
    c = classify_deterministic(idea)
    # Force regulatory for this fixture
    c["regulatory_surface"] = "health"
    c["business_model"] = "marketplace"
    sc = score_complexity(idea, c)
    n, reason = question_budget(sc.score, sc.uncertainty, c["regulatory_surface"])
    assert 8 <= n <= 9, (n, sc.score, reason)
    assert 3 <= n <= 9


def test_budget_never_outside_bounds() -> None:
    for score in [i / 10 for i in range(0, 11)]:
        n, reason = question_budget(score, score, "payments" if score > 0.5 else "none")
        assert 3 <= n <= 9
        assert "n" in reason and "reasons" in reason
