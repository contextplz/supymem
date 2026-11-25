import asyncio
import socket
import re
from datetime import timedelta

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db, _work, _expire_search_replay


def test_offline_eval_uses_real_gate_and_never_network(journey_db, monkeypatch):
    from app.core.local_mode import is_local
    from scripts.gtm.lead_eval import run, metrics
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("offline network"))
    result = asyncio.run(run())
    assert result["mode"] == "offline"
    assert set(result["ventures"]) == {"ShiftSwap QA", "LaunchDesk QA", "PupPath QA"}
    for case in result["ventures"].values():
        assert case["retrieved"] == 20 and 15 <= case["candidates"] <= 30
        assert 5 <= case["kept"] <= 10 and case["drafted"] == case["kept"]
        assert case["precision_at_5"] == 1.0 and case["junk_rate"] == 0.0
    queries = " ".join(row["query"] for row in result["ventures"]["ShiftSwap QA"]["queries"])
    assert not re.search(r"\d|-jobs|-ShiftVendor", queries)
    cities = ("New York", "Los Angeles", "Chicago")
    if is_local():
        assert "US" in queries and not any(city in queries for city in cities)
    else:
        assert all(city in queries for city in cities)
    assert result["restaurant_recall"]["seeds"] == (5 if is_local() else 4)
    assert metrics([{"judging_status": "judged", "source_url": "https://unlabelled.example"}], {})["precision_at_5"] is None


def test_confirmation_automates_consumer_discovery_and_repeat_excludes_seen(journey_db, monkeypatch):
    from app.gtm import journey
    from app.db.models import GtmTouch
    from sqlalchemy import select
    calls = []
    async def discover(who, search_queries=None, **kwargs):
        doc = journey._journal(kwargs["payload"]).document(kwargs["payload"]["journey_id"])
        calls.append(doc.get("seen_lead_urls") or [])
        lead = {"id": "owner-one", "name": "Owner One", "source_url": "https://www.reddit.com/user/owner-one/",
            "fit": 90, "quote": "I need help with puppy training.", "routes": [], "judging_status": "judged"}
        journey._journal(kwargs["payload"]).mutate(kwargs["payload"]["journey_id"], kwargs["payload"],
            lambda row, data, session: data.update(reachable_leads=[lead]))
        journey._journal(kwargs["payload"]).mutate(kwargs["payload"]["journey_id"], kwargs["payload"],
            lambda row, data, session: data["_runtime"].update(
                reachable_candidates_quality_v1=[{"url": "https://checked.example/"}],
                public_reachable_candidates_quality_v1=[{"url": "https://public-checked.example/"}]))
        return []
    monkeypatch.setattr(journey, "_discover", discover)
    state = journey.update_icp(journey_db["wid"], journey_db["vid"], "first-time US dog owners", problem="puppy training frustration")
    assert state["state"] == "queued" and state["recruitment_opt_in"]
    _work(journey_db, state)
    _expire_search_replay(journey_db, state["job_id"])
    _work(journey_db, journey.prepare(journey_db["wid"], journey_db["vid"], retry=True))
    assert len(calls) == 2 and calls[0] == []
    assert "https://www.reddit.com/user/owner-one/" in calls[1]
    assert not {"https://checked.example/", "https://public-checked.example/"} & set(calls[1])
    with journey_db["factory"]() as session:
        assert not list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))


def test_discovery_key_includes_quality_and_founder_targeting():
    import hashlib
    import json
    from app.gtm import journey
    icp = {"who": "independent restaurant managers", "problem": "staff shift coverage", "location": "Austin",
        "geography": "US", "company_size": "10-50 employees"}
    key = journey._discovery_key(icp)
    for field in ("location", "geography", "company_size"):
        assert journey._discovery_key(icp | {field: "different"}) != key
    old_key = hashlib.sha256(json.dumps([icp.get("who"), icp.get("problem"), None, None, [], "", journey._consumer(icp),
        icp["location"], icp["geography"], icp["company_size"]], sort_keys=True).encode()).hexdigest()[:16]
    assert key != old_key


def test_streamed_leads_are_deduplicated_before_top_ten():
    from app.gtm import journey, reachable
    from scripts.qa.eval_reachable_leads import LocalRun
    local = LocalRun(journey)
    leads = [{"id": str(index), "source_url": f"https://buyer{index}.example/", "fit": 95 - index,
        "quote": "We struggle with staff shift coverage.", "routes": [], "judging_status": "judged"} for index in range(12)]
    duplicate = leads[0] | {"id": "duplicate", "source_url": "https://www.buyer0.example/contact", "fit": 96}
    reachable._publish(local, {"journey_id": "local-eval"}, [*leads, duplicate])
    kept = local.doc["reachable_leads"]
    assert len(kept) == 10 and kept[0]["id"] == "duplicate"
    assert "0" not in {lead["id"] for lead in kept}
    assert all(lead["judging_status"] == "judged" for lead in kept)


def test_queries_use_confirmed_geography_and_need_not_size_or_exclusions(journey_db):
    from app.gtm import journey
    from app.gtm.discovery import Plan
    who = "general managers of independent restaurants in Austin, TX with 10-50 employees"
    plan = Plan.model_validate({"queries": journey._fallback_plan(who, "manual shift scheduling", [{"name": "ShiftVendor"}])})
    assert any('"general manager"' in row.query and "independent restaurant" in row.query and "Austin" in row.query
        for row in plan.queries if row.intent == "business")
    assert not re.search(r"\d|-jobs|-ShiftVendor", " ".join(row.query for row in plan.queries))
    assert any(row.intent == "need_hiring" and "shift" in row.query for row in plan.queries)
    assert "-ShiftVendor" not in " ".join(journey._search_queries(who, "manual shift scheduling", ["ShiftVendor"]))
    assert any("association" in row["query"] for row in journey._expert_queries("first-time US dog owners", "puppy training"))


def test_discovery_sends_competitor_and_seen_exclusions_and_preserves_edited_searches(journey_db, monkeypatch):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from scripts.qa.eval_reachable_leads import LocalRun
    local, requests = LocalRun(journey), []
    local.doc["seen_lead_urls"] = ["https://seen.example/about", "https://www.reddit.com/r/kitchen/comments/seen123/question/"]
    async def call(*args, cache_identity=None, **kwargs):
        return await local.call(*args, **kwargs)
    async def model(payload, stage, context):
        return {"queries": [{"query": "restaurants Austin contact", "intent": "business"},
            {"query": "restaurants Austin forum need", "intent": "customer_voice"}]}
    async def search(self, query, **kwargs):
        requests.append({"query": query, **kwargs})
        return []
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-fixture")
    monkeypatch.setattr(journey, "_journal", lambda payload: local)
    monkeypatch.setattr(journey, "_signal_call", call)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("unexpected scrape"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    payload = {"workspace_id": journey_db["wid"], "venture_id": journey_db["vid"], "journey_id": "local-eval", "_job_attempt": 1}
    asyncio.run(journey._discover("restaurant general managers Austin", ["restaurants Austin contact", "restaurants Austin forum need"],
        problem="manual shift scheduling", competitors=[{"name": "ShiftVendor", "domain": "vendor.example"}], payload=payload))
    from app.core.local_mode import is_local
    assert len(requests) == (2 if is_local() else 4)
    assert all("seen.example" in request["exclude_domains"] for request in requests)
    assert all("vendor.example" not in request["exclude_domains"] for request in requests)
    assert all("reddit.com" not in request["exclude_domains"] for request in requests)
    assert all("-inurl:" not in request["query"] for request in requests)
    assert {request["query"] for request in requests} == {query["query"] for query in local.doc["discovery_plan"] + local.doc.get("broadened_discovery_plan", [])}
    assert requests[0]["tbs"] is None and requests[1]["tbs"]
    from app.gtm import reachable
    assert reachable.seen_business_domains(local.doc["seen_lead_urls"]) == {"seen.example"}


def test_restaurant_seed_routing_recall_floor_and_seen_boundaries(journey_db, monkeypatch, caplog):
    from app.core.local_mode import is_local
    from scripts.gtm.lead_eval import load_fixture
    from scripts.qa.eval_reachable_leads import LocalRun
    from app.gtm import journey, reachable
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, DuckDuckGoProvider
    fixture = load_fixture()
    case = fixture["ventures"][0]
    context = case | {"seen_lead_urls": ["https://tinparsnip.example/", "https://shiftvendor.example/"]}
    seeds = reachable.candidate_hits(fixture["restaurant_search_results"], context)
    businesses = {"https://paperturnip.example/about", "https://tinparsnip.example.evil.example/"}
    if is_local():
        businesses.add("https://yelp.com/biz/fixture-cantina")
    assert {row["url"] for row in seeds} == businesses | {"https://www.linkedin.com/in/fixture-restaurant-gm"}
    assert next(row for row in seeds if "paperturnip.example" in row["url"])["kind"] == "business"
    local, requests, routed = LocalRun(journey), [], []
    local.doc.update(icp=case, seen_lead_urls=context["seen_lead_urls"])
    async def call(*args, cache_identity=None, **kwargs):
        return await local.call(*args, **kwargs)
    async def model(payload, stage, context):
        return {"queries": [{"query": '"general manager" independent restaurant 1-5 locations "New York" contact -jobs', "intent": "business"},
            {"query": "restaurant hourly staff 25-60 employees complaints", "intent": "customer_voice"}]} if stage == "plan" else {"items": []}
    async def search(self, query, **kwargs):
        requests.append(query)
        return fixture["restaurant_search_results"]
    async def discover(provider, hits, signals, context, payload, **kwargs):
        routed.extend(hits)
        return [{"source_url": hit["url"]} for hit in hits]
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "offline-fixture")
    monkeypatch.setattr(journey, "_journal", lambda payload: local)
    monkeypatch.setattr(journey, "_signal_call", call)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(reachable, "discover", discover)
    monkeypatch.setattr(DuckDuckGoProvider, "search", lambda *args: pytest.fail("unexpected fallback"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    caplog.set_level("INFO", logger=journey.__name__)
    payload = {"workspace_id": journey_db["wid"], "venture_id": journey_db["vid"], "journey_id": "local-eval", "_job_attempt": 1}
    asyncio.run(journey._discover(case["audience"], problem=case["problem"], competitors=case["competitors"], payload=payload))
    assert len(requests) == 10
    assert local.doc["_runtime"]["calls_used"]["broaden_search"] == 2
    assert len(local.doc["broadened_discovery_plan"]) == 2
    assert not re.search(r"\d|-jobs|-inurl:", " ".join(requests))
    if is_local():
        assert not any(city in " ".join(requests) for city in ("Los Angeles", "Chicago"))
        businesses = businesses - {"https://yelp.com/biz/fixture-cantina"} | {"https://localnews.example/news/restaurant-owners"}
    else:
        assert all(city in query for city, query in zip(("New York", "Los Angeles", "Chicago"), requests[:3]))
    assert {row["url"] for row in routed if row["kind"] == "business"} == businesses
    assert len(routed) == local.doc["candidate_seed_count"] == len(seeds)
    records = [record for record in caplog.records if record.message == "gtm_discovery_query_seeds"]
    assert len(records) == len(requests) and all(isinstance(record.seeds, int) and not hasattr(record, "query") for record in records)


def test_public_pages_fail_closed_on_robots_and_never_social(journey_db, monkeypatch):
    from app.gtm import reachable, email_learning, scrapy_leads
    fetched = []
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda *args: None)
    def fetch(url, **kwargs):
        fetched.append(url)
        return "User-agent: *\nDisallow: /" if url.endswith("/robots.txt") else pytest.fail("disallowed page fetched")
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    pages = reachable.PublicPages()
    assert asyncio.run(pages.scrape("https://clinic.example/contact")) == ""
    assert fetched == ["https://clinic.example/robots.txt"]
    assert asyncio.run(pages.scrape("https://www.linkedin.com/in/person")) == ""
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    assert not asyncio.run(pages.allowed("https://clinic.example/contact"))


@pytest.mark.parametrize("mode,age,kept", [("B2C", 89, True), ("B2C", 91, False), ("B2B", 200, True), ("B2B", 366, False)])
def test_author_candidate_freshness_and_seen_exclusion(journey_db, monkeypatch, mode, age, kept):
    from app.gtm import journey, reachable
    from scripts.qa.eval_reachable_leads import LocalRun
    local = LocalRun(journey)
    monkeypatch.setattr(journey, "_journal", lambda payload: local)
    async def model(payload, stage, context):
        return {"items": []}
    monkeypatch.setattr(journey, "_discovery_model", model)
    url = "https://www.reddit.com/r/puppy101/comments/test123/training/"
    signal = {"source_url": url, "author_url": "https://www.reddit.com/user/fixture_owner/", "author": "fixture_owner",
        "published_at": (journey.utcnow() - timedelta(days=age)).isoformat(), "quote": "I need help with puppy training."}
    context = {"mode": mode, "audience": "first-time US dog owners", "problem": "puppy training", "competitors": [], "offer": ""}
    payload = {"journey_id": "local-eval", "_job_attempt": 1}
    asyncio.run(reachable.discover(None, [], [signal], context, payload))
    assert bool(local.doc["_runtime"]["reachable_candidates_quality_v1"]) is kept
    local.doc["_runtime"].pop("reachable_candidates_quality_v1")
    asyncio.run(reachable.discover(None, [], [signal], context | {"seen_lead_urls": [url]}, payload))
    assert local.doc["_runtime"]["reachable_candidates_quality_v1"] == []
