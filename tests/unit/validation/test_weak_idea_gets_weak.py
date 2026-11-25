from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.validation.verdict import ValidationVerdict, compute_verdict  # noqa: E402


def test_competitor_count_without_evidence_cannot_reject_idea():
    result = compute_verdict(
        {
            "competitors": [{"name": f"incumbent-{index}"} for index in range(8)],
            "claims": [],
            "sources": [],
            "differentiation_score": 0.0,
        }
    )
    assert result.verdict == ValidationVerdict.UNAVAILABLE
    assert "unknown" in result.recommendation.lower()
