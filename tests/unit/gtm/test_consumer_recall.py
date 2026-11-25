"""Public parent posts survive short search snippets; discovery stays copy-only."""
import asyncio
import logging
from collections import Counter
from datetime import timedelta

import pytest

from tests.gtm_fixtures import NAPNOTES_AUDIENCE, NAPNOTES_HITS as NAPNOTES_FIXTURE_HITS, NAPNOTES_NEED, NAPNOTES_PROBLEM, NAPNOTES_URL, REDDIT_BLOCK_PAGE, napnotes_model
from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload

NAPNOTES_HITS = [hit | {"url": hit["url"].replace("profiles.test", "www.linkedin.com").replace("short-posts.test", "x.com")}
    for hit in NAPNOTES_FIXTURE_HITS]


@pytest.mark.parametrize("audience,problem", [
    ("Procurement directors at manufacturing companies", "supplier invoice reconciliation"),
    ("First-time dog owners", "keeping up with puppy training"),
])
def test_signal_search_results_are_not_limited_by_inline_reads(journey_db, monkeypatch, audience, problem):
    from app.core.local_mode import is_local
    from app.credits.metered import price_micro
    from app.gtm import journey, reachable
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    query = f"{audience} {problem} discussion"
    searches = []
    async def search(self, planned, **options):
        if planned != query:
            return []
        searches.append((options, self._limit))
        return [{"url": f"https://forum.example/threads/entry{index}", "title": "Public discussion", "snippet": "Navigation"}
            for index in range(self._limit)]
    async def scrape(*args, **kwargs):
        return ""
    async def model(*args, **kwargs):
        return {"items": []}
    async def hn(*args, **kwargs):
        return {"items": []}
    async def discover(*args, **kwargs):
        return []
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "discover", discover)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        discovery_plan=[{"query": query, "intent": "customer_voice"}]))
    try:
        asyncio.run(journey._discover(audience, problem=problem, payload=payload))
    except RuntimeError as error:
        assert str(error) == "signal_sources_unavailable"
    assert len(searches) == 1
    options, limit = searches[0]
    inline_reads = 0 if is_local() else journey.MAX_INLINE_READS
    assert options["limit"] == (limit if is_local() else inline_reads)
    assert options["include_content"] is bool(inline_reads)
    runtime = journal.document(payload["journey_id"])["_runtime"]
    operation = runtime["operations"][journey._signal_key(runtime, "signal_search", query)]
    assert (operation.get("content_slots", 0) if is_local() else operation["content_slots"]) == inline_reads
    if is_local():
        from app.credits import local_meter
        assert operation["usage"] == {}
        with local_meter._database() as connection:
            receipts = connection.execute("SELECT status, cost_usd_micro FROM calls WHERE venture_id = ? AND purpose = ?",
                (journey_db["vid"], "gtm.discovery.search")).fetchall()
            assert receipts and all(row["status"] == "confirmed" and row["cost_usd_micro"] == 0 for row in receipts)
    else:
        assert operation["usage"]["estimate_micro"] == price_micro("firecrawl_search") + inline_reads * price_micro("firecrawl_scrape")
    assert payload["_trace_counts"]["seed:search_result"] == limit
    assert runtime["calls_used"].get("signal_scrape", 0) <= runtime["ceilings"]["signal_scrape"]


@pytest.mark.parametrize("intent", ["person", "customer_voice"])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("relevance", [0.95, 0.65, 0.55])
def test_napnotes_short_public_post_becomes_attributed_signal_and_lead(journey_db, monkeypatch, intent, inline, relevance):
    from app.gtm import journey, reachable, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, ScrapedText
    from core.clock import utcnow
    searches, scrapes = [], []
    async def model(payload, stage, context):
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = intent
        if stage == "passages":
            for item in result["items"]:
                if item["relevance"]:
                    item["relevance"] = relevance
        if stage in {"judge", "reachjudge"}:
            assert all(row["url"] == NAPNOTES_URL for row in context["candidates"])
        if stage == "passages":
            assert not any(row["url"] in {hit["url"] for hit in NAPNOTES_HITS[1:3]} for row in context["candidates"])
        return result
    async def search(self, query, **kwargs):
        searches.append((query, kwargs))
        if 'site:reddit.com baby naps overtired bedtime "I need help"' != query:
            return []
        return [hit | ({"content": "Baby naps and bedtime."} if inline and hit["url"] == NAPNOTES_URL else {})
            for hit in NAPNOTES_HITS]
    async def scrape(self, url, **kwargs):
        scrapes.append(url)
        assert url == NAPNOTES_URL
        return ScrapedText(f"Posted by [nap_parent](https://www.reddit.com/user/nap_parent/)\n\n{NAPNOTES_NEED}",
            metadata={"author": "nap_parent", "created_utc": (utcnow() - timedelta(days=2)).timestamp()}, usage={})
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    from app.core.local_mode import is_local
    if is_local():
        monkeypatch.setattr("app.research.local_providers.local_read", lambda url, **kwargs: scrape(None, url))
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    if relevance < 0.6:
        assert not doc["need_signals"] and not doc.get("reachable_leads")
        assert payload["_trace_counts"]["signal:dropped:no_relevant_need_passage"]
        return
    assert len(doc["need_signals"]) == 1
    signal = doc["need_signals"][0]
    assert signal["source_url"] == NAPNOTES_URL and signal["author"] == "nap_parent"
    assert signal["quote"] == NAPNOTES_NEED and signal["recency"] == "recent"
    assert len(doc["reachable_leads"]) == 1
    lead = doc["reachable_leads"][0]
    assert lead["source_url"] == NAPNOTES_URL and lead["quote"] == NAPNOTES_NEED
    if relevance < 0.7:
        assert signal["evidence_label"] == lead["evidence_label"] == "possible_fit"
        assert "Does this problem apply to your experience?" in signal["draft"]
        assert "Does this problem apply to your experience?" in lead["draft"]
        assert "does the problem I'm researching apply to your experience?" in lead["connection_note"]
        assert payload["_trace_counts"]["need_signal:kept:possible_fit"]
        assert payload["_trace_counts"]["reachable:kept:possible_fit"]
    else:
        assert not signal.get("evidence_label") and lead["evidence_label"] == "need_passage"
    assert all(route["kind"] == "forum" for route in lead["routes"])
    assert not journey._prospects(journey_db["vid"]) and not doc.get("industry_experts")
    assert scrapes == [NAPNOTES_URL]
    track = "reachable" if intent == "person" else "signal"
    assert payload["_trace_counts"][f"{track}:dropped:consumer_non_consumer_source"] == 2
    assert not any("consumer_profile_without_first_person_need" in reason for reason in payload["_trace_counts"])
    expert_queries = {item["query"] for item in doc.get("expert_discovery_plan") or []}
    customer_searches = [(query, options) for query, options in searches if query not in expert_queries]
    assert len(customer_searches) == 5
    assert not customer_searches[0][1]["include_content"] and "forum" in customer_searches[0][0]
    assert not customer_searches[1][1]["include_content"] and "community discussion board" in customer_searches[1][0]
    assert all("amazon.com" not in options["exclude_domains"] for query, options in customer_searches[2:])
    assert any("linkedin.com" in query for query, options in customer_searches)
    assert reachable.candidate_hits([hit | {"kind": "person"} for hit in NAPNOTES_HITS], {
        "mode": "B2C", "audience": NAPNOTES_AUDIENCE, "problem": NAPNOTES_PROBLEM}) == [
            hit | {"kind": "person"} for hit in (NAPNOTES_HITS[0], *NAPNOTES_HITS[3:])]


@pytest.mark.parametrize("intent", ["person", "customer_voice"])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("url", [NAPNOTES_URL, "https://community.whattoexpect.com/forums/first-time-parents/topic/next-nap.html"])
def test_blocked_parent_thread_scores_search_snippet(journey_db, monkeypatch, intent, inline, url):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, ScrapedText
    from core.clock import utcnow
    scrapes, scored = [], []
    async def search(self, query, **kwargs):
        if query != 'site:reddit.com baby naps overtired bedtime "I need help"':
            return []
        snippet = NAPNOTES_NEED if url == NAPNOTES_URL else f"Posted by [nap_parent](https://community.whattoexpect.com/members/nap_parent)\n\n{NAPNOTES_NEED}"
        return [{"url": url, "title": "When should the next nap be?", "snippet": snippet,
            "content": REDDIT_BLOCK_PAGE if inline else "Baby naps and bedtime.",
            "metadata": {"author": "nap_parent", "publishedTime": (utcnow() - timedelta(days=2)).isoformat()}}]
    async def scrape(self, page, **kwargs):
        scrapes.append(page)
        return ScrapedText(REDDIT_BLOCK_PAGE, metadata={}, usage={})
    async def model(payload, stage, context):
        if stage == "passages":
            scored.extend(row["text"] for row in context["candidates"])
            assert all(row["text"] in NAPNOTES_NEED or row["text"].startswith("Posted by") for row in context["candidates"])
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = intent
        return result
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda page, **kwargs: "User-agent: *\nAllow: /" if page.endswith("robots.txt") else REDDIT_BLOCK_PAGE)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    from app.core.local_mode import is_local
    if is_local():
        monkeypatch.setattr("app.research.local_providers.local_read", lambda page, **kwargs: scrape(None, page))
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    assert len(doc["need_signals"]) == len(doc["reachable_leads"]) == 1
    assert doc["need_signals"][0]["source_url"] == doc["reachable_leads"][0]["source_url"] == url
    assert doc["need_signals"][0]["quote"] == doc["reachable_leads"][0]["quote"] == NAPNOTES_NEED
    assert doc["need_signals"][0]["recency"] == "recent"
    assert NAPNOTES_NEED in scored and scrapes == ([] if inline else [url])
    assert payload["_trace_counts"]["signal:fallback:blocked_page"] == (2 if intent == "person" else 1)
    assert "signal:dropped:no_relevant_need_passage" not in payload["_trace_counts"]


def test_b2c_business_and_book_pages_drop_before_candidate_budget(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    async def model(*args, **kwargs):
        pytest.fail("Business/book pages must not reach the model")
    monkeypatch.setattr(journey, "_discovery_model", model)
    hits = [hit | {"kind": "person"} for hit in NAPNOTES_HITS[1:3]]
    context = {"mode": "B2C", "audience": NAPNOTES_AUDIENCE, "problem": NAPNOTES_PROBLEM, "offer": "NapNotes", "competitors": []}
    assert asyncio.run(reachable.discover(None, hits, [], context, payload)) == []
    assert payload["_trace_counts"]["reachable:dropped:consumer_non_consumer_source"] == 2
    assert not journey._journal(payload).document(payload["journey_id"])["_runtime"]["reachable_candidates_quality_v1"]


def test_b2c_social_published_snippet_with_own_need_is_kept_without_fetch(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    hit = NAPNOTES_HITS[3] | {"kind": "person", "title": "Pat Doe", "snippet": NAPNOTES_NEED}
    context = {"mode": "B2C", "audience": NAPNOTES_AUDIENCE, "problem": NAPNOTES_PROBLEM, "offer": "NapNotes", "competitors": []}
    class NoFetch:
        async def scrape(self, url, **kwargs):
            pytest.fail("Social pages must never be fetched")
    monkeypatch.setattr(journey, "_discovery_model", napnotes_model)
    assert journey._consumer_hit_rejection(hit, context) == ""
    leads = asyncio.run(reachable.discover(NoFetch(), [hit], [], context, _leased_payload(journey_db)))
    assert len(leads) == 1 and leads[0]["quote"] == NAPNOTES_NEED
    assert leads[0]["routes"][0]["kind"] == "linkedin"


def test_b2c_signal_drop_trace_uses_signal_track(journey_db, monkeypatch):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    async def search(self, query, **kwargs):
        return [NAPNOTES_HITS[0] | {"content": "Public discussion of baby nap routines without a first-person problem. " * 5}]
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage == "passages":
            return {"items": [{"id": row["id"], "relevance": 0.0} for row in context["candidates"]]}
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = "customer_voice"
        return result
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM)
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, payload=payload))
    assert payload["_trace_counts"]["signal:dropped:no_relevant_need_passage"] == 1
    assert "seed:dropped:no_relevant_need_passage" not in payload["_trace_counts"]


@pytest.mark.parametrize("audience,problem", [(NAPNOTES_AUDIENCE, NAPNOTES_PROBLEM),
    ("Dog owners", "My dog pulls on the leash and I cannot walk safely"),
    ("University students", "Struggling to study for exams without getting distracted")])
def test_consumer_fallback_queries_target_public_first_person_threads(audience, problem):
    from app.gtm import journey
    plan = journey._fallback_plan(audience, problem, [], context={"mode": "B2C"})
    assert len(plan) == 8 and all(item["intent"] == "community" for item in plan[:2])
    assert all(item["intent"] == "customer_voice" for item in plan[2:])
    assert sum(item["query"].startswith("site:reddit.com ") for item in plan) == 2
    assert all('"I ' in item["query"] or '"my ' in item["query"] for item in plan[2:])
    assert any('"I need help"' in item["query"] for item in plan)
    assert any('"I am struggling"' in item["query"] for item in plan)
    assert all(journey._keywords(problem, 3) in item["query"] for item in plan[2:])


@pytest.mark.parametrize("url", ["https://www.linkedin.com/in/localcoach", "https://x.com/localcoach", "https://twitter.com/localcoach"])
def test_b2c_partner_profiles_do_not_require_first_person_need(url):
    from app.gtm import journey, reachable
    hit = {"kind": "person", "url": url, "title": "Alex Doe - local coach", "snippet": "I teach swimming to local children."}
    context = {"mode": "B2C", "audience": "Local parents", "problem": "Swimming lessons"}
    assert journey._consumer_hit_rejection(hit, context) == ""
    assert reachable.candidate_hits([hit], context) == [hit | {"url": reachable.reach_url(url)}]


@pytest.mark.parametrize("title,path", [("Sleep Consultant", "/"), ("Baby sleep book", "/"),
    ("Parenting Podcast", "/"), ("Children's Hospital", "/"), ("Sleep tips", "/articles/sleep"), ("Coach directory", "/")])
def test_b2c_non_people_rejection_is_domain_independent(title, path):
    from app.gtm import journey
    assert journey._consumer_hit_rejection({"kind": "person", "url": "https://unlisted.example" + path, "title": title},
        {"mode": "B2C"}) == "consumer_non_consumer_source"


@pytest.mark.parametrize("url", [NAPNOTES_URL, "https://community.whattoexpect.com/forums/first-time-parents/topic/next-nap.html"])
@pytest.mark.parametrize("snippet_key", ["snippet", "description"])
@pytest.mark.parametrize("intent", ["person", "customer_voice"])
def test_parent_thread_chrome_scores_and_grounds_search_snippet(journey_db, monkeypatch, url, snippet_key, intent):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    scored = []
    page = "<html><head><title>Reddit - The heart of the internet</title></head><body><nav>" + "Navigation links. " * 2000 + "</nav><main><p>Posted by <a href='https://community.whattoexpect.com/members/nap_parent'>nap_parent</a></p><p>Baby naps and bedtime.</p></main><aside>Cookie settings</aside></body></html>"
    async def search(self, query, **kwargs):
        if query != 'site:reddit.com baby naps overtired bedtime "I need help"':
            return []
        return [{"url": url, "title": "When should the next nap be?", snippet_key: NAPNOTES_NEED, "content": page,
            "metadata": {"author": "nap_parent", "publishedTime": (utcnow() - timedelta(days=2)).isoformat()}}]
    async def model(payload, stage, context):
        if stage == "passages":
            scored.extend(context["candidates"])
            assert all("Navigation" not in row["text"] and "Cookie" not in row["text"] for row in context["candidates"])
        if stage == "judge":
            assert all(NAPNOTES_NEED in row["text"] for row in context["candidates"])
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = intent
        return result
    async def hn(*args, **kwargs):
        return {"items": []}
    async def no_scrape(*args, **kwargs):
        pytest.fail("Inline page should not need a scrape")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", no_scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    assert len(doc["need_signals"]) == 1
    assert doc["need_signals"][0]["source_url"] == url
    assert doc["need_signals"][0]["quote"] == NAPNOTES_NEED
    assert any(row["text"] == NAPNOTES_NEED and row["source"] == "search_snippet" and row["url"] == url for row in scored)
    assert any(row["text"] == "Baby naps and bedtime." for row in scored)
    assert "signal:dropped:no_relevant_need_passage" not in payload["_trace_counts"]
    if intent == "person":
        assert len(doc["reachable_leads"]) == 1 and doc["reachable_leads"][0]["quote"] == NAPNOTES_NEED


@pytest.mark.parametrize("relevance", [0.95, 0.65])
def test_long_parent_forum_page_scores_late_op_need_and_becomes_signal(journey_db, monkeypatch, relevance):
    from app.gtm import journey, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    url = "https://community.whattoexpect.com/forums/first-time-parents/topic/next-nap.html"
    sentences = [f"Unrelated community discussion number {index}." for index in range(150)] + [NAPNOTES_NEED]
    page = "<main>" + "".join(f"<p>{sentence}</p>" for sentence in sentences) + "</main>"
    scored = []
    async def search(self, query, **kwargs):
        if query != 'site:reddit.com baby naps overtired bedtime "I need help"':
            return []
        return [{"url": url, "title": "When should the next nap be?", "snippet": "Baby naps and bedtime.", "content": page,
            "metadata": {"author": "nap_parent", "publishedTime": (utcnow() - timedelta(days=2)).isoformat()}}]
    async def model(payload, stage, context):
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = "customer_voice"
        if stage == "passages":
            scored.append(context["candidates"])
            assert len(context["candidates"]) == 80
            assert any(row["text"] == NAPNOTES_NEED for row in context["candidates"])
            for item in result["items"]:
                if item["relevance"]:
                    item["relevance"] = relevance
        return result
    async def hn(*args, **kwargs):
        return {"items": []}
    async def no_scrape(*args, **kwargs):
        pytest.fail("Inline page should not need a scrape")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", no_scrape)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload)) == []
    assert len(scored) == 1
    signals = journey._journal(payload).document(payload["journey_id"])["need_signals"]
    assert len(signals) == 1 and signals[0]["source_url"] == url and signals[0]["quote"] == NAPNOTES_NEED
    assert (signals[0].get("evidence_label") == "possible_fit") == (relevance < 0.7)
    if relevance < 0.7:
        assert "Does this problem apply to your experience?" in signals[0]["draft"]
    assert "signal:dropped:no_relevant_need_passage" not in payload["_trace_counts"]


def test_possible_fit_discovery_caps_weak_matches_without_displacing_strong(journey_db, monkeypatch):
    from app.gtm import journey, quality
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    urls = [NAPNOTES_URL.replace("nap123", f"nap{index}") for index in range(8)]
    scored = []
    async def search(self, query, **kwargs):
        if query != 'site:reddit.com baby naps overtired bedtime "I need help"':
            return []
        return [{"url": url, "title": "When should the next nap be?", "snippet": "Baby naps and bedtime.",
            "content": NAPNOTES_NEED, "metadata": {"author": f"parent{index}", "publishedTime": (utcnow() - timedelta(days=2)).isoformat()}}
            for index, url in enumerate(urls)]
    async def model(payload, stage, context):
        result = await napnotes_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = "customer_voice"
        if stage == "passages":
            scored.append(context["_domain"])
            for item in result["items"]:
                if item["relevance"]:
                    item["relevance"] = 0.7 if context["_domain"] == urls[-1] else 0.65
        return result
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM)
    payload = _leased_payload(journey_db)
    asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    for field in ("need_signals", "reachable_leads"):
        assert len(doc[field]) == quality.MAX_POSSIBLE_FITS + 1
        assert doc[field][0]["source_url"] == urls[-1]
        assert sum(item.get("evidence_label") == "possible_fit" for item in doc[field]) == quality.MAX_POSSIBLE_FITS
    assert len(scored) == len(urls) + 1


def test_dropped_signal_passage_diagnostics_are_content_free(journey_db, monkeypatch, caplog):
    from app.gtm import journey, quality
    url = "https://community.whattoexpect.com/forums/private-topic/sensitive-parent-name.html?private=secret"
    text = "Private page text about baby naps and bedtime."
    snippet = "Sensitive search description about parents and naps."
    async def model(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": "baby naps parent", "intent": "customer_voice"}, {"query": "parent bedtime", "intent": "customer_voice"}]}
        if stage == "passages":
            return {"items": [{"id": row["id"], "relevance": 0.2} for row in context["candidates"]]}
        return {"items": []}
    async def signal_call(payload, bucket, identity, callback, **kwargs):
        return [{"url": url, "title": "Baby naps", "snippet": snippet, "content": text}] if bucket == "signal_search" else []
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(journey, "_signal_call", signal_call)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM)
    payload = _leased_payload(journey_db)
    with caplog.at_level(logging.INFO, logger=journey.__name__):
        assert asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, offer="NapNotes", payload=payload)) == []
    records = [record for record in caplog.records if record.message == "gtm_signal_passage_scores"]
    assert len(records) == 1
    record = records[0]
    assert {key: getattr(record, key) for key in ("host", "text_chars", "passage_count", "scored_count", "max_relevance", "threshold", "snippet_included")} == {
        "host": "community.whattoexpect.com", "text_chars": len(text), "passage_count": 2, "scored_count": 2,
        "max_relevance": 0.2, "threshold": quality.MIN_RELEVANCE, "snippet_included": True}
    assert record.levelno == logging.INFO and isinstance(record.max_relevance, float)
    assert all(value not in repr(record.__dict__) for value in (text, snippet, url, "private-topic", "sensitive-parent-name", "secret"))
    assert payload["_trace_counts"]["signal:dropped:no_relevant_need_passage"] == 1


@pytest.mark.parametrize("inline", [False, True])
def test_consumer_thread_extracted_before_page_limit(journey_db, monkeypatch, inline):
    from app.gtm import journey, quality
    from app.signals.research import ScrapedText
    url = "https://community.whattoexpect.com/forums/first-time-parents/topic/next-nap.html"
    raw = "<html><head><title>Reddit - The heart of the internet</title></head><body><nav>" + "Navigation. " * 2400 + "</nav><main><p>" + NAPNOTES_NEED + "</p></main><aside>Sidebar links</aside></body></html>"
    class Provider:
        async def scrape(self, page):
            assert page == url and not inline
            return ScrapedText(raw, metadata={}, usage={})
    async def call(payload, bucket, identity, callback, **kwargs):
        return await callback()
    monkeypatch.setattr(journey, "_signal_call", call)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM)
    payload = _leased_payload(journey_db)
    context = {"mode": "B2C", "audience": NAPNOTES_AUDIENCE, "problem": NAPNOTES_PROBLEM}
    hit = {"url": url, "kind": "customer_voice", "snippet": "Baby naps and bedtime.", "content": raw if inline else ""}
    _, text, _ = asyncio.run(journey._signal_page(Provider(), hit, payload, context=context))
    assert len(raw) > 24000 and raw.find(NAPNOTES_NEED) > 24000
    assert text == NAPNOTES_NEED
    assert quality.passages([{"url": url, "text": text}]) == [{"id": 0, "url": url, "text": NAPNOTES_NEED}]


def test_forum_discovery_excludes_social_articles_and_unreadable_hosts(journey_db):
    from app.gtm import journey
    context = {"mode": "B2C", "audience": NAPNOTES_AUDIENCE, "location": "Canada"}
    hits = [
        {"url": "https://forum.unreadable.example/threads/naps", "content": "Baby naps", "title": "Parents discussion"},
        {"url": "https://community.whattoexpect.com/forums/first-time-parents/topic/naps.html", "content": NAPNOTES_NEED * 5},
        {"url": "https://parents.example/talk/naps", "content": NAPNOTES_NEED * 3},
        {"url": "https://topics.family.example/naps", "content": NAPNOTES_NEED * 3},
        {"url": "https://forum.local.example/threads/naps", "content": NAPNOTES_NEED * 3},
        {"url": "https://forum.extra.example/threads/naps", "content": NAPNOTES_NEED * 3},
        {"url": NAPNOTES_URL, "content": NAPNOTES_NEED * 3},
        {"url": "https://facebook.com/groups/parents/threads/naps"},
        {"url": "https://instagram.com/community/threads/naps"},
        {"url": "https://health.example/articles/threads/naps", "content": NAPNOTES_NEED * 3},
        {"url": "https://sleep-consultant.example/", "content": NAPNOTES_NEED * 3},
        {"url": "https://community.expert.example/topic/naps", "expert_role": "sleep consultant"},
        {"url": "https://127.0.0.1/forum/naps", "content": NAPNOTES_NEED * 3},
    ]
    hosts = journey._forum_hosts(hits, context)
    assert hosts == ["community.whattoexpect.com", "parents.example", "topics.family.example", "forum.local.example"]
    plan = journey._fallback_plan(NAPNOTES_AUDIENCE, NAPNOTES_PROBLEM, [], context=context, forum_hosts=hosts)
    assert all("Canada" in item["query"] for item in plan)
    assert [item["intent"] for item in plan[:2]] == ["community"] * 2
    assert Counter(item["query"].split()[0] for item in plan[2:]) == Counter({"site:" + host: 2 if index < 2 else 1 for index, host in enumerate(hosts)})
    assert all('"I ' in item["query"] for item in plan[2:])


@pytest.mark.parametrize("forums_found", [False, True])
@pytest.mark.parametrize("plan_source", ["model", "fallback", "saved"])
def test_consumer_discovery_searches_forums_before_site_queries(journey_db, monkeypatch, forums_found, plan_source):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    hosts = ["community.parents.example", "family.example", "forum.local.example", "talk.families.example"]
    searches, stages = [], []
    model_plan = [{"query": f'site:reddit.com/r/parents{index} baby naps "I need help"',
        "intent": "customer_voice" if index % 2 else "community"} for index in range(6)]
    model_plan.append({"query": "site:linkedin.com/in parenting educator", "intent": "person"})
    async def search(self, query, **options):
        searches.append((query, options))
        if query.endswith((" forum", " community discussion board")):
            assert not options["include_content"] and options["tbs"] is None
            return [{"url": "https://forum.unreadable.example/threads/naps", "title": "Parents forum", "content": REDDIT_BLOCK_PAGE},
                *[{"url": "https://" + host + "/threads/naps", "title": "Parents forum", "content": NAPNOTES_NEED * 3} for host in hosts]] if forums_found else []
        if query.startswith("site:"):
            assert searches[0][0].endswith(" forum") and searches[1][0].endswith(" community discussion board")
        return []
    async def model(payload, stage, context):
        stages.append(stage)
        if stage == "plan":
            assert plan_source != "saved"
            if plan_source == "fallback":
                raise ValueError("synthetic plan failure")
            return {"queries": model_plan}
        return {"items": []}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    if plan_source == "saved":
        journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=model_plan))
    asyncio.run(journey._discover(NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, payload=payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    assert [query for query, options in searches[:2]] == [item["query"] for item in doc["discovery_plan"][:2]]
    threads = [item for item in doc["discovery_plan"][2:] if item["intent"] in {"customer_voice", "community"}]
    if forums_found:
        assert [item["query"].split()[0] for item in threads] == ["site:" + host for host in hosts[:len(threads)]]
        assert "forum.unreadable.example" not in " ".join(query for query, options in searches)
    else:
        assert sum("site:reddit.com" in query for query, options in searches) == 2
    if plan_source != "fallback":
        assert model_plan[-1] in doc["discovery_plan"]
    assert len(searches) == journey.CALL_CEILINGS["signal_search"]
    assert sum("site:reddit.com" in query for query, options in searches) <= 2
    assert stages.count("plan") == (0 if plan_source == "saved" else 1)


@pytest.mark.parametrize("audience,person", [("Solo technical founders in the US", True),
    ("Freelance translators in the US", True), ("Independent bakery owners", False)])
def test_forum_policy_preserves_business_plans_and_person_routes(journey_db, monkeypatch, audience, person):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    plan = [{"query": f'site:reddit.com/r/startups{index} manual notes "I need help"', "intent": "customer_voice"}
        for index in range(6)] + [{"query": "business manual notes contact", "intent": "business"},
        {"query": "site:linkedin.com/in founder manual notes", "intent": "person"}]
    searches = []
    async def search(self, query, **options):
        searches.append(query)
        if query.endswith((" forum", " community discussion board")):
            return [{"url": "https://forum.founders.example/threads/notes", "title": "Founders forum"}]
        return []
    async def scrape(self, url, **options):
        return REDDIT_BLOCK_PAGE
    async def model(payload, stage, context):
        assert stage != "plan"
        return {"items": []}
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(adapters, "search_hn", hn)
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem="manual notes")
    payload = _leased_payload(journey_db)
    journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(discovery_plan=plan))
    if person:
        with pytest.raises(RuntimeError, match="^signal_sources_unavailable$"):
            asyncio.run(journey._discover(audience, problem="manual notes", payload=payload))
    else:
        asyncio.run(journey._discover(audience, problem="manual notes", payload=payload))
    doc = journey._journal(payload).document(payload["journey_id"])
    assert not doc["expert_discovery_plan"]
    if person:
        assert searches[0].endswith(" forum") and searches[1].endswith(" community discussion board")
        assert all(item in doc["discovery_plan"] for item in plan[-2:])
        assert all(item["query"].startswith("site:forum.founders.example") for item in doc["discovery_plan"][2:]
            if item["intent"] == "customer_voice")
        assert len(plan) <= len(doc["discovery_plan"]) <= journey._run_ceilings(doc["icp"])["signal_search"]
        assert sum("site:reddit.com" in query for query in searches) <= 2
    else:
        assert doc["discovery_plan"] == plan
        assert sum("site:reddit.com" in query for query in searches) == 6


@pytest.mark.parametrize("body", ["Baby naps and bedtime.", REDDIT_BLOCK_PAGE])
@pytest.mark.parametrize("mode,audience", [("B2C", NAPNOTES_AUDIENCE), ("B2B", "solo technical founders in the US")])
def test_unreadable_thread_host_stops_after_two_reads_and_keeps_other_hosts(journey_db, monkeypatch, body, mode, audience):
    from app.gtm import journey
    from app.signals.research import ScrapedText
    reads = []
    class Provider:
        async def scrape(self, url):
            reads.append(url)
            await asyncio.sleep(0)
            return ScrapedText(body, metadata={}, usage={})
    async def call(payload, bucket, identity, callback, **kwargs):
        return await callback()
    monkeypatch.setattr(journey, "_signal_call", call)
    journey.update_icp(journey_db["wid"], journey_db["vid"], NAPNOTES_AUDIENCE, problem=NAPNOTES_PROBLEM, recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    context = {"mode": mode, "audience": audience, "problem": "baby naps bedtime"}
    def hit(host, index):
        return {"url": f"https://{host}/threads/naps-{index}", "kind": "customer_voice", "title": "Baby naps", "snippet": "Baby naps and bedtime."}
    async def read():
        return await asyncio.gather(*(journey._signal_page(Provider(), hit("forum.blocked.example", index), payload, context=context) for index in range(5)))
    results = asyncio.run(read())
    assert len(reads) == 2 and all(not result[1] for result in results[2:])
    assert payload["_trace_counts"]["signal:dropped:host_unreadable"] == 3
    inline = hit("forum.blocked.example", 6) | {"content": NAPNOTES_NEED}
    assert asyncio.run(journey._signal_page(Provider(), inline, payload, context=context))[1] == NAPNOTES_NEED
    assert len(reads) == 2
    asyncio.run(journey._signal_page(Provider(), hit("forum.other.example", 1), payload, context=context))
    assert len(reads) == 3
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert runtime["thread_readability"]["forum.blocked.example"]["failures"] == 2


def test_b2b_short_reads_do_not_activate_consumer_host_cutoff(journey_db, monkeypatch):
    from app.gtm import journey
    reads = []
    class Provider:
        async def scrape(self, url):
            reads.append(url)
            return "Business orders and appointments."
    async def call(payload, bucket, identity, callback, **kwargs):
        return await callback()
    monkeypatch.setattr(journey, "_signal_call", call)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "Bakery owners", problem="business orders appointments")
    payload = _leased_payload(journey_db)
    context = {"mode": "B2B", "audience": "Bakery owners", "problem": "business orders appointments"}
    for index in range(3):
        asyncio.run(journey._signal_page(Provider(), {"url": f"https://forum.business.example/threads/orders-{index}", "title": "Business orders appointments"}, payload, context=context))
    assert len(reads) == 3
    assert not journey._journal(payload).document(payload["journey_id"])["_runtime"].get("thread_readability")
