from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.validation.verdict import ValidationVerdict, compute_verdict


def test_verdict_is_deterministic():
    research = {
        "competitors": [{"name": "A"}, {"name": "B"}],
        "claims": [{"claim": "signal"}] * 4,
        "sources": [{"url": "https://example.com"}] * 4,
        "differentiation_score": 0.7,
    }
    results = [compute_verdict(research) for _ in range(5)]
    assert all(result == results[0] for result in results)


def test_blocked_research_is_unavailable_not_weak():
    result = compute_verdict(
        {
            "mode": "blocked",
            "reason": "no_research_provider",
            "competitors": [],
            "claims": [],
            "sources": [],
        }
    )
    assert result.verdict == ValidationVerdict.UNAVAILABLE
    assert result.confidence == 0.0
    assert "not a market" in result.recommendation.lower() or "did not run" in result.recommendation.lower()
