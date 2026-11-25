import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db, _work


@pytest.mark.parametrize("seed", [
    {"organization": "Test Bakery", "website": "https://bakery.example/", "source_url": "https://forum.example/bakery-orders",
        "email": "owner@bakery.example", "email_confidence": "published", "enriched": True,
        "contact_source_url": "https://bakery.example/contact", "contact_quote": "Contact Test Bakery at owner@bakery.example",
        "signals": [{"kind": "customer_voice", "quote": "We bakery owners lose phone orders in manual notes.",
            "source_url": "https://forum.example/bakery-orders"}]},
    {"organization": "Source Only Fixture", "source_url": "https://source-only.example/contact",
        "why": "Synthetic browser fixture: a public source with no contact address."},
    {"organization": "Retry Source Fixture", "source_url": "https://retry-source.example/contact",
        "why": "Synthetic source candidate after retry; permission remains unverified."},
], ids=["postgres", "browser", "browser-retry"])
def test_offline_fixture_buyer_survives_discovery_worker(journey_db, monkeypatch, seed):
    from app.gtm import journey
    from app.db.models import GtmTouch
    from sqlalchemy import select
    from tests.gtm_fixtures import BAKERY_NEED, qualified_bakery, quality_model
    from core.clock import utcnow
    calls = []
    async def model(payload, stage, context):
        calls.append((stage, context))
        return await quality_model(payload, stage, context)
    async def discover(who, search_queries=None, **kwargs):
        candidate = seed | {"signals": [signal | {"published_at": utcnow().isoformat(), "observed_at": utcnow().isoformat(),
            "confidence": "observed", "company_evidence_url": signal["source_url"],
            "why_now": "The owner reports losing phone orders."} for signal in seed.get("signals", [])]}
        return [await qualified_bakery(candidate, who, **kwargs)]
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(journey, "_discover", discover)
    _work(journey_db, journey.update_icp(journey_db["wid"], journey_db["vid"], "Independent bakery owners handling phone orders"))
    state = journey.status(journey_db["wid"], journey_db["vid"])
    assert len(state["prospects"]) == 1
    assert state["prospects"][0]["email"] == seed.get("email")
    assert state["prospects"][0]["blocked_reason"] == ("unknown_jurisdiction" if seed.get("email") else "published_email_missing")
    assert state["drafts"] == []
    assert [stage for stage, context in calls] == ["marketrole", "passages"]
    assert BAKERY_NEED in calls[0][1]["candidates"][0]["text"]
    with journey_db["factory"]() as session:
        assert not session.scalar(select(GtmTouch.id))


def test_need_signal_rows_survive_without_market_role():
    from pathlib import Path
    src = (Path(__file__).parents[3] / "services/company_os/app/gtm/journey.py").read_text()
    # Need-signal leads (people describing the pain) skip the company classifier and must not be dropped.
    assert 'rows = [row for row in rows if row.get("market_role") or row.get("signals")]' in src
