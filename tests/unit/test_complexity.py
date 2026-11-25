"""Complexity scoring bands for fixture ideas."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.intake.normalizer import classify_deterministic, score_complexity

FIXTURES = [
    ("a todo list app", (0.2, 0.55)),
    ("an app that helps home bakers take custom cake orders on WhatsApp", (0.35, 0.75)),
    ("a two-sided marketplace for freelancers with escrow payments and KYC", (0.55, 0.95)),
    ("HIPAA-compliant patient portal with insurance billing", (0.55, 0.95)),
]


def test_complexity_bands_and_factors() -> None:
    for idea, (lo, hi) in FIXTURES:
        classification = classify_deterministic(idea)
        result = score_complexity(idea, classification)
        assert lo <= result.score <= hi, (idea, result.score, lo, hi)
        assert result.factors, "must carry contributing factors"
        assert all("factor" in f and "why" in f for f in result.factors)
