"""National local-business discovery keeps clean briefs and bounded city coverage."""
import asyncio
import json
import re

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db, _work


RAW = "something for gym owners idk, members keep leaving"
AUDIENCE = "independent gym owners in the US with 1 to 3 locations"


@pytest.fixture()
def hosted_journey_db(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    yield from journey_db.__wrapped__(tmp_path, monkeypatch, request)


@pytest.mark.parametrize("raw,pitch", [
    ("something for shop owners idk, orders keep changing", "Shop owners need help keeping orders organized"),
    ("something for families idk, meals keep getting skipped", "Families need help keeping meals organized"),
])
def test_local_known_problem_fallback_preserves_the_clean_saved_pitch(journey_db, monkeypatch, raw, pitch):
    from app.db.models import Venture
    from app.gtm import journey

    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"raw_idea": raw, "problem": raw}, "research": {}})
    with journey_db["factory"]() as session:
        session.get(Venture, journey_db["vid"]).raw_idea = raw
        journey._store(session, journey._row(session, journey_db["vid"], create=True), {"pitch": pitch})
        session.commit()
    assert journey._icp(journey_db["vid"])["problem"] == pitch


@pytest.mark.parametrize("audience,cities", [
    ("Dental practice owners", ["New York", "Los Angeles", "Chicago"]),
    ("Families planning weekend outings", []),
])
def test_local_national_search_uses_requested_country_without_inventing_consumer_locations(journey_db, audience, cities):
    from app.core.local_mode import is_local
    from app.gtm import journey

    assert [city for city, region in journey._business_cities(audience, context={"location": "United States"})] == ([] if is_local() else cities)
    assert journey._business_cities(audience) == []


@pytest.mark.parametrize("audience", ["Railway maintenance coordinators", "Families learning to skate"])
def test_local_discovery_preserves_requested_search_limits(journey_db, monkeypatch, audience):
    from app.gtm.discovery import local_provider
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from tests.company_os_harness import configure_local_gtm

    configure_local_gtm(monkeypatch)
    calls = []
    async def search(provider, query, **kwargs):
        calls.append((query, provider._limit))
        return [{"url": f"https://person{index}.example/"} for index in range(provider._limit)]

    monkeypatch.setattr(local_providers, "search", search)
    provider = local_provider(limit=10)
    assert len(asyncio.run(provider.search(audience))) == 10
    provider = local_provider(limit=20)
    assert len(asyncio.run(provider.search(audience))) == 20
    assert calls == [(audience, 10), (audience, 20)]


@pytest.mark.parametrize("source", ["revision", "raw_revision", "pitch", "raw"])
def test_search_problem_prefers_current_revision_then_pitch_then_clean_raw(journey_db, monkeypatch, source):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db
    pitch = "a member retention tool for independent gyms"
    clarified = "Gym members leave because owners miss early cancellation signals"
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {
        "audience": AUDIENCE, "problem": RAW, "confirmed": True}})
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = RAW
        row = journey._row(session, data["vid"], create=True)
        journey._store(session, row, {"pitch": pitch if source != "raw" else ""})
        if source in {"revision", "raw_revision"}:
            now = utcnow()
            session.add(IdeaSession(id="gym_session", workspace_id=data["wid"], venture_id=data["vid"],
                case="know", state="open", current_revision_id="gym_revision", created_at=now, updated_at=now))
            session.flush()
            session.add(IdeaRevision(id="gym_revision", session_id="gym_session", workspace_id=data["wid"],
                venture_id=data["vid"], rev=1, source="clarifier", structured=json.dumps({
                    "customer": AUDIENCE, "problem": RAW if source == "raw_revision" else clarified}), created_at=now))
        session.commit()

    base = journey._icp(data["vid"])
    expected = clarified if source == "revision" else "Independent gyms need help with member retention." if source != "raw" else "gym owners members keep leaving"
    assert base["problem"] == expected
    icp = journey._journey_icp(data["vid"], base | {"problem": RAW, "confirmed": True, "chat_confirmed": True})
    assert icp["problem"] == expected
    queries = icp["search_queries"] + journey._search_queries(AUDIENCE, "I think maybe " + RAW)
    queries += [item["query"] for item in journey._fallback_plan(AUDIENCE, icp["problem"], [])]
    assert RAW not in " ".join(queries)
    assert not re.search(r"\b(?:something|idk|i think|maybe)\b", " ".join(queries), re.I)
    if source == "raw":
        changed = journey._journey_icp(data["vid"], base | {"problem": RAW, "location": "US"}, base=base | {"location": "United States"})
        assert changed["search_queries"] == journey._search_queries(changed["who"], changed["problem"], [], context=changed)
        custom = [query + " Austin" for query in icp["search_queries"]]
        assert journey._journey_icp(data["vid"], icp | {"search_queries": custom}, base=base)["search_queries"] == custom
    if source == "revision":
        revised = "Gym owners lack warning when members stop visiting"
        with data["factory"]() as session:
            session.add(IdeaRevision(id="gym_revision_v2", session_id="gym_session", workspace_id=data["wid"],
                venture_id=data["vid"], rev=2, source="clarifier", structured=json.dumps({
                    "customer": AUDIENCE, "problem": revised}), created_at=utcnow()))
            session.get(IdeaSession, "gym_session").current_revision_id = "gym_revision_v2"
            session.commit()
        assert journey._journey_icp(data["vid"], icp)["problem"] == revised
        for entered in ("Owners need onboarding help", "gym owners members keep leaving"):
            explicit = icp | {"problem": entered, "problem_basis": "From the problem you entered."}
            assert journey._journey_icp(data["vid"], explicit)["problem"] == entered


@pytest.mark.parametrize("audience,location,cities", [
    (AUDIENCE, "", ["New York", "Los Angeles", "Chicago"]),
    ("independent gym owners", "United States", ["New York", "Los Angeles", "Chicago"]),
    ("bakery owners in the UK", "", ["London", "Manchester", "Birmingham"]),
    ("salon owners in India", "", ["Mumbai", "Delhi", "Bangalore"]),
    ("dental clinics in Canada", "", ["Toronto", "Vancouver", "Montreal"]),
    ("restaurants in Australia", "", ["Sydney", "Melbourne", "Brisbane"]),
])
def test_country_local_business_queries_use_cities(journey_db, monkeypatch, audience, location, cities):
    from app.gtm import journey
    from app.gtm.discovery import Plan

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    context = {"location": location}
    plan = Plan.model_validate({"queries": journey._fallback_plan(audience, "missed appointments", [], context=context)})
    assert len(plan.queries) <= journey.CALL_CEILINGS["signal_search"]
    assert [item.intent for item in plan.queries[:3]] == ["business"] * 3
    assert all(city in item.query for city, item in zip(cities, plan.queries[:3]))
    assert all("contact" in item.query for item in plan.queries[:3])
    assert cities[0] in journey._decision_maker_queries(audience, context=context)[0]["query"]


@pytest.mark.parametrize("audience", ["gym owners in Austin, TX", "salon owners in London", "dental clinics in Raleigh, NC in the US"])
def test_explicit_city_audiences_do_not_rotate(journey_db, audience):
    from app.gtm import journey

    assert journey._fallback_plan(audience, "missed appointments", []) == journey._fallback_plan(
        audience, "missed appointments", [], context={"city_offset": 3})
    assert journey._business_cities("UK freelance graphic designers") == []
    assert journey._business_cities("independent gym owners") == []
    assert journey._business_cities("independent gym owners who work with us") == []
    assert journey._business_cities("gym owners in Texas, US") == [("Texas", "")]
    assert journey._business_cities("gym owners in Dallas and Houston in the US") == [("Dallas", "TX"), ("Houston", "TX")]
    assert journey._business_cities("gym owners in the US", context={"location": "Nashville"}) == []
    assert "New York" not in journey._discovery_summary(AUDIENCE, [{"query": "independent gym US contact", "intent": "business"}])


@pytest.mark.parametrize("audience,category", [
    ("plumbers who handle emergency repairs", "plumbers"),
    ("electricians that install lighting", "electricians"),
    ("plumbers who help homeowners with repairs", "plumbers"),
    ("personal trainers who work with parents", "personal trainers"),
    ("owners of laundromats with several machines", "laundromats"),
    ("pottery studios that host workshops", "pottery studios"),
    ("bicycle repair shops with appointment calendars", "bicycle repair shops"),
    ("accounting practices who manage tax filings", "accounting practices"),
    ("dental clinics who schedule follow-up visits", "independent dental practice"),
    ("software developers who sell subscriptions", ""),
    ("owners of bicycles who need repairs", ""),
    ("renters who need plumbers", ""),
    ("home coffee enthusiasts who place orders online", ""),
])
def test_local_categories_derive_short_audience_phrases(journey_db, audience, category):
    from app.core.local_mode import is_local
    from app.gtm import journey

    assert journey._local_business_category(audience) == category
    if category:
        assert not journey._consumer(audience)
    else:
        assert not journey._home_seller(audience)
    cities = journey._business_cities(audience, context={"location": "United States"})
    assert [city for city, state in cities] == (["New York", "Los Angeles", "Chicago"] if category and not is_local() else [])


@pytest.mark.parametrize("audience,phrase,country", [
    ("home-based textile sellers in India who accept custom orders through community pages", "home-based textile sellers", "India"),
    ("home bakers in the UK who take pastry orders through social media", "home bakers", "UK"),
    ("home bakers in Canada", "home bakers", "Canada"),
    ("home-based ceramic artists in the UK", "home-based ceramic artists", "UK"),
    ("people who sell handmade ceramics from home in Canada through marketplace listings", "handmade ceramics", "Canada"),
    ("collectible sellers on auction marketplaces in Australia", "collectible sellers", "Australia"),
])
def test_home_and_social_sellers_use_community_marketplace_and_profile_discovery(journey_db, audience, phrase, country):
    from app.gtm import journey
    from app.gtm.discovery import Plan

    context = {"who": audience, "customer_type": "B2B"}
    assert journey._consumer(audience, context) and journey._consumer(context)
    assert journey._local_business_category(audience) == ""
    assert journey._business_cities(audience) == []
    assert journey._decision_maker_queries(audience, context=context) == []
    for problem in ("", "lost order details"):
        queries = journey._search_queries(audience, problem, context=context)
        plan = Plan.model_validate({"queries": journey._fallback_plan(audience, problem, [], context=context)})
        assert len(queries) == len(plan.queries) == journey.CALL_CEILINGS["signal_search"]
        assert all(item.intent != "business" and "contact" not in item.query for item in plan.queries)
        assert all(phrase in query and country in query for query in queries)
        assert [item.intent for item in plan.queries[:4]] == ["community", "community", "person", "person"]
        assert "community" in queries[0] and "marketplace" in queries[2] and "social media profile" in queries[3]
        assert not any(city in " ".join(queries) for city in ("Mumbai", "London", "Toronto", "Sydney"))
        assert not any(clause in " ".join(queries) for clause in ("who accept", "take pastry orders", "through", "auction marketplaces"))
    targeted = journey._search_queries(audience, "lost order details", forum_hosts=["forum.example"])
    assert all("site:forum.example" in query for query in targeted[4:])
    voices = [item for item in journey._fallback_plan(audience, "lost order details", [], forum_hosts=["forum.example"])
        if item["intent"] == "customer_voice"]
    assert len(voices) == 4 and all("site:forum.example" in item["query"] for item in voices)


@pytest.mark.parametrize("audience,phrase,local", [
    ("plumbers in the US who take bookings through email and phone", "plumbers", True),
    ("electricians in the US that accept bookings through community pages", "electricians", True),
    ("plumbers in the US who service restaurants through maintenance contracts", "plumbers", True),
    ("pottery studios in the US who supply dental clinics through wholesale orders", "pottery studios", True),
    ("dental clinics in the US who track bookings through email and phone", "independent dental practice", True),
    ("software developers in the US who sell subscriptions through email and phone", "software developers", False),
    ("owners of bicycles in the US who need help with bookings", "owners bicycles", False),
])
def test_fallback_queries_clean_clauses_and_rotate_only_local_business_cities(journey_db, monkeypatch, audience, phrase, local):
    from app.gtm import journey

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    batches = []
    for offset, expected in ((0, ["New York", "Los Angeles", "Chicago"]), (3, ["Houston", "Austin", "Denver"])):
        context = {"city_offset": offset}
        plan = journey._fallback_plan(audience, "missed reservations", [], context=context)
        business = [item["query"] for item in plan if item["intent"] == "business"]
        cities = [city for city, state in journey._business_cities(audience, context=context)]
        assert cities == (expected if local else [])
        batches.append(cities)
        assert all(phrase in query and "US" in query for query in business)
        people = [item["query"] for item in journey._decision_maker_queries(audience, context=context)]
        assert all(journey._audience_phrase(audience) in query and (expected[0] if local else "US") in query for query in people)
        for query in business + people:
            assert not re.search(r"\b(?:who|that|through|bookings|email|phone|subscriptions)\b", query, re.I)
        if local:
            assert len(business) == 3
            assert all(city in query for city, query in zip(expected, business))
        else:
            assert not any(city in " ".join(business) for city in expected)
    assert not set(batches[0]) & set(batches[1])


@pytest.mark.hosted
def test_national_gym_discovery_rotates_and_reads_city_business_sites_within_ceilings(hosted_journey_db, monkeypatch):
    from app.db.models import Venture
    from app.gtm import journey
    from app.signals.research import HttpResearchProvider, ScrapedText
    from tests.unit.gtm.test_journey_signals import signal_provider
    from tests.unit.gtm.test_reachable_leads import fixture_models

    data = hosted_journey_db
    searches, scrapes, _ = signal_provider(monkeypatch)
    fixture_models(monkeypatch)
    model = journey._discovery_model
    async def gym_model(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": "independent gym United States owner contact", "intent": "business"},
                {"query": "locally owned fitness center United States contact", "intent": "business"},
                {"query": "independent strength training gym United States contact", "intent": "business"},
                {"query": RAW + " hiring", "intent": "need_hiring"},
                {"query": "gym owners US something gym owners switched tools", "intent": "competitor_switch"}]}
        return await model(payload, stage, context)
    monkeypatch.setattr(journey, "_discovery_model", gym_model)
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": AUDIENCE, "problem": RAW, "confirmed": True}})
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = RAW
        session.commit()
    cities = ["New York", "Los Angeles", "Chicago", "Houston", "Austin", "Denver", "Atlanta", "Seattle"]
    async def search(provider, query, **kwargs):
        searches.append((query, kwargs))
        city = next((city for city in cities if city in query), None)
        if not city or "contact" not in query or "site:" in query:
            return []
        return [{"url": f"https://{city.lower().replace(' ', '')}.example/", "title": f"{city} Gym",
            "snippet": f"Independent gym in {city}, US", "provider": "firecrawl"}]
    async def scrape(provider, url, **kwargs):
        scrapes.append(url)
        host = journey.enrich._host(url)
        city = next(city for city in cities if city.lower().replace(" ", "") == host.split(".")[0])
        text = f"{city} Gym is an independent gym in {city}, US serving members at one location. Members keep leaving between visits.\n[Contact](/contact)\n120 Main Street\nUnited States"
        if url.endswith("/contact"):
            text = f"Contact our gym owner\nhello@{host}\nPhone: +1 202 555 0101"
        return ScrapedText(text, metadata={}, usage={})
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)

    batches = []
    for retry in (False, True):
        view = journey.prepare(data["wid"], data["vid"], retry=True, authorize=True) if retry else journey.update_icp(data["wid"], data["vid"], AUDIENCE)
        _work(data, view)
        with data["factory"]() as session:
            document = journey._json(journey._row(session, data["vid"]).provenance_json)
        plan = document["discovery_plan"]
        business = [item["query"] for item in plan if item["intent"] == "business"]
        selected = {city for city in cities if any(city in query for query in business)}
        assert len(selected) == 3
        assert all(city in journey._discovery_summary(AUDIENCE, plan) for city in selected)
        batches.append(selected)
        assert len(plan) <= document["_runtime"]["ceilings"]["signal_search"]
        counts = document["_runtime"]["calls_used"]
        assert all(counts.get(bucket, 0) <= ceiling for bucket, ceiling in document["_runtime"]["ceilings"].items())
        assert all(count <= 3 for bucket, count in counts.items() if bucket.startswith("contact:"))
        assert sum(bucket.startswith("contact:") for bucket in counts) <= journey.MAX_PROSPECTS
        assert len(document["reachable_leads"]) >= 3
        assert not re.search(r"\b(?:something|idk|i think|maybe)\b", " ".join(item["query"] for item in plan), re.I)
    assert batches == [{"New York", "Los Angeles", "Chicago"}, {"Houston", "Austin", "Denver"}]
    assert all(f"https://{city.lower().replace(' ', '')}.example/contact" in scrapes for city in set.union(*batches))
