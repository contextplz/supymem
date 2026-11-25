"""Larger searches retain the same evidence, fit and credit boundaries offline."""
import asyncio
import json
import re

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload


@pytest.mark.parametrize("audience,problem,country", [
    ("independent bicycle repair shops in Canada", "repair appointment scheduling", "CA"),
    ("procurement directors at manufacturing companies in the UK", "supplier invoice reconciliation", "GB"),
])
def test_larger_search_keeps_judged_unique_businesses_with_bounded_cost(journey_db, monkeypatch, audience, problem, country):
    from app.core.local_mode import is_local
    from app.credits.metered import price_micro
    from app.gtm import journey, reachable
    from app.gtm.discovery import Judgement, ReachDraft, Rejection
    from app.signals.research import HttpResearchProvider

    searches, reads, judge_ids = [], [], []
    active, peak = 0, 0
    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-search")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)

    async def search(self, query, **options):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        searches.append((query, options))
        offset = len(searches) * 5
        await asyncio.sleep(0)
        active -= 1
        if options["limit"] != journey.BUSINESS_SEARCH_RESULTS:
            return []
        indices = [0] + [(offset + index) % 60 + 1 for index in range(29)]
        return [{"url": f"https://firm{index}.example/" + ("about" if index == 0 and offset > 5 else ""),
            "title": f"Firm {index}", "snippet": audience, "country": country} for index in indices]

    async def scrape(self, url, **options):
        reads.append((url, options.get("main_only", True)))
        index = re.search(r"firm(\d+)", url)[1]
        return f"<h1>Firm {index}</h1><p>We are {audience}. We handle {problem} for our customers.</p><p>Email hello@firm{index}.example</p>"

    async def model(payload, stage, context):
        if stage == "plan":
            return {"queries": journey._fallback_plan(audience, problem, [], context=context)}
        if stage == "marketrole":
            text = context["candidates"][0]["text"]
            return {"role_in_market": "buyer", "icp_fit": 0.95, "anti_icp": False, "homepage_quote": text[:1000]}
        if stage == "passages":
            return {"items": [{"id": row["id"], "relevance": 0.95} for row in context["candidates"]]}
        if stage == "reachjudge":
            judge_ids.extend(row["id"] for row in context["candidates"])
            return {"items": [{"id": row["id"], "kind": row["kind"], "name": row["title"], "affiliation": "",
                "url": row["evidence_pages"][0]["url"], "quote": row["evidence_pages"][0]["text"],
                "fit": 69 if "firm0.example" in row["url"] else 95,
                "why": "Handles the confirmed workflow.", "value_exchange": "Discuss your workflow."}
                for row in context["candidates"]]}
        return {"items": []}

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    if is_local():
        monkeypatch.setattr("app.research.local_providers.local_read", lambda url, **kwargs: scrape(None, url))
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    confirmation = journey.status(journey_db["wid"], journey_db["vid"])
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(audience, confirmation["icp"]["search_queries"], problem=problem, payload=payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    plan, runtime = doc["discovery_plan"], doc["_runtime"]
    assert 12 <= len(plan) <= journey.BUSINESS_SEARCH_QUERIES
    assert 1 < peak <= journey.enrich.CONCURRENCY
    assert len(searches) == len(plan) == runtime["calls_used"]["signal_search"]
    assert all(count <= runtime["ceilings"].get(bucket, count) for bucket, count in runtime["calls_used"].items())
    business = [item for item in plan if item["intent"] == "business"]
    if journey._local_business_category(audience) and not is_local():
        cities = journey._business_cities(audience)
        assert len(cities) >= 3
        assert all(sum(city in item["query"] for item in business) >= 3 for city, state in cities)
    else:
        assert len({item["query"] for item in business}) >= 6
        assert not any(city in item["query"] for city in ("London", "Toronto") for item in business)
    leads = doc["reachable_leads"]
    assert len(leads) == journey._keep_limit(len(plan)) == journey.MAX_KEPT == 30
    assert all(lead["judging_status"] == "judged" and lead["fit"] >= 70 for lead in leads)
    assert not any("firm0.example" in lead["source_url"] for lead in leads)
    assert len({journey.enrich._host(lead["source_url"]) for lead in leads}) == len(leads)
    assert len({journey.enrich._host(url) for url, main_only in reads}) > 30
    assert len(reads) == len(set(reads))
    assert max(judge_ids) >= 40
    assert Rejection(id=journey.MAX_CANDIDATES - 1, reject_reason="Outside your audience").id == 79
    assert Judgement.model_validate({"id": 79, "kind": "business", "name": "Firm", "affiliation": "",
        "url": "https://firm.example/", "quote": "A published business workflow", "fit": 90, "why": "Fits"}).id == 79
    last_id = ((journey.MAX_CANDIDATES + journey.MAX_POSSIBLE_FITS + 7) // 8) * 8 - 1
    assert Rejection(id=last_id, reject_reason="Outside your audience").id == last_id
    assert ReachDraft(id=last_id, body="Hi, could we discuss your workflow?", connection_note="Hi, could we discuss your workflow?").id == last_id
    with pytest.raises(ValueError):
        Rejection(id=last_id + 1, reject_reason="Outside your audience")
    assert journey._keep_limit(6) == 12 and journey._keep_limit(100) == 30
    expected = (((journey.BUSINESS_SEARCH_QUERIES + journey.CALL_CEILINGS["broaden_search"]) * 3 + 5 + 6) * price_micro("firecrawl_search")
        + (15 + journey.MAX_PROSPECTS * (journey.enrich.MAX_PAGES + 1)) * price_micro("firecrawl_scrape"))
    assert confirmation["estimate"]["ceiling_micro"] == journey._search_estimate(doc["icp"]) == expected
    search_operations = [operation for key, operation in runtime["operations"].items() if key.startswith("signal_search-")]
    if is_local():
        from app.credits import local_meter
        assert expected == 0 and all(operation["usage"] == {} for operation in search_operations)
        with local_meter._database() as connection:
            receipts = connection.execute("SELECT status, cost_usd_micro FROM calls WHERE venture_id = ? AND purpose = ?",
                (journey_db["vid"], "gtm.discovery.search")).fetchall()
            assert len(receipts) == len(plan)
            assert all(row["status"] == "confirmed" and row["cost_usd_micro"] == 0 for row in receipts)
    else:
        assert expected > 430000
        assert any(operation["usage"]["estimate_micro"] == 3 * price_micro("firecrawl_search") for operation in search_operations)
    assert sum(operation.get("usage", {}).get("estimate_micro", 0) for operation in runtime["operations"].values()) <= expected
    context = {"audience": audience, "mode": "B2B", "problem": problem}
    duplicates = [{"url": "https://firm.example/", "title": audience, "kind": "business", "country": country},
        {"url": "https://www.firm.example/about", "title": audience, "kind": "business", "country": country}]
    assert len(reachable.candidate_hits(duplicates, context)) == 1


@pytest.mark.parametrize("audience", ["independent bicycle repair shops in Canada",
    "procurement directors at manufacturing companies in the UK"])
def test_repeat_search_and_missing_input_cards_quote_the_larger_run(journey_db, audience):
    from app.api.gtm_god_ops import JourneyIcpBody
    from app.chat import customers
    from app.gtm import journey

    icp = {"who": audience, "problem": "", "basis": "Founder confirmed", "needs_input": True, "mode": "company"}
    view = {"icp": icp, "estimate": {"ceiling_micro": 0}, "state": "idle", "prospects": [], "drafts": []}
    card = json.dumps(customers._search_card(view, retry=True))
    assert "credit" not in card.lower() and "confirmation" not in card
    assert json.loads(card)["payload"]["retry"] is True
    assert "credit" not in customers.render(journey_db["vid"], view)["body_md"].lower()
    queries = [f"{audience} contact {index}" for index in range(journey.BUSINESS_SEARCH_QUERIES)]
    assert JourneyIcpBody(who=audience, search_queries=queries).search_queries == journey._checked_queries(queries)
    with pytest.raises(ValueError):
        JourneyIcpBody(who=audience, search_queries=queries + ["beyond the query ceiling"])
    with pytest.raises(ValueError):
        journey._checked_queries(queries + ["beyond the query ceiling"])


@pytest.mark.parametrize("audience", ["independent bicycle repair shops in Canada",
    "procurement directors at manufacturing companies in the UK"])
def test_expanded_failed_queries_preserve_signal_page_budget(journey_db, monkeypatch, audience):
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider

    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-search")
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)

    search_calls = []

    async def search(self, query, **options):
        search_calls.append(query)
        if is_local() and len(search_calls) > 1 or options.get("include_content"):
            raise TimeoutError("Unknown search outcome")
        return []

    async def model(payload, stage, context):
        return {"queries": journey._fallback_plan(audience, context["problem"], [], context=context)} if stage == "plan" else {"items": []}

    async def no_hn(*args, **kwargs):
        return {"items": [], "calls": 0}

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr("app.research.prior_art.adapters.search_hn", no_hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem="appointment and supplier coordination")
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(audience, problem="appointment and supplier coordination", payload=payload)) == []
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    if is_local():
        assert runtime["calls_used"].get("signal_scrape", 0) == 0
    else:
        assert 0 < runtime["calls_used"]["signal_scrape"] <= runtime["ceilings"]["signal_scrape"] - 5
    assert any(operation.get("state") == "uncertain" for operation in runtime["operations"].values())

    async def page():
        return "An earlier public page with attributable workflow evidence."

    async def read_earlier_pages():
        for index in range(5):
            assert await journey._signal_call(payload, "signal_scrape", f"earlier-page-{index}", page, meter="firecrawl_scrape")

    asyncio.run(read_earlier_pages())
