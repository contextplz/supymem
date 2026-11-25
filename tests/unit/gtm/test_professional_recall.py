"""Synthetic public founder posts, not production venture data."""
import asyncio
import json
from datetime import timedelta

import pytest

from tests.gtm_fixtures import REDDIT_BLOCK_PAGE
from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload
from tests.unit.gtm.test_leads_without_email import _assert_no_email_contact


AUDIENCE = "solo technical founders in the US"
PROBLEM = "manual customer interviews and launch outreach"
OFFER = "software to automate customer interviews and launch outreach"
NEED = "I am a solo technical founder in the US and need help with manual customer interviews and launch outreach."
HN = "https://news.ycombinator.com/item?id=123456"
REDDIT = "https://www.reddit.com/r/startups/comments/founder123/customer_interviews/"
FORUM = "https://community.founders.example/threads/customer-interviews/"
CONTEXT = {"audience": AUDIENCE, "problem": PROBLEM, "offer": OFFER, "competitors": [], "mode": "B2B"}


async def founder_model(payload, stage, context):
    assert context["mode"] == "B2B"
    if stage == "plan":
        return {"queries": [{"query": "founders manual customer interviews launch outreach", "intent": "customer_voice"},
            {"query": "founders customer interviews first person pain", "intent": "customer_voice"}]}
    if stage == "passages":
        return {"items": [{"id": row["id"], "relevance": 0.95 if row["text"] == NEED else 0.0}
            for row in context["candidates"]]}
    if stage in {"judge", "reachjudge"}:
        return {"items": [{"id": row["id"], "kind": "person" if stage == "reachjudge" else "customer_voice",
            "quote": NEED, "affiliation": "", "name": "", "url": row["url"], "fit": 95,
            "why": "Published first-person founder need.",
            "value_exchange": "Compare customer interview and launch outreach workflows in a research call."}
            for row in context["candidates"]]}
    return {"items": []}


@pytest.mark.parametrize("url", [HN, REDDIT, FORUM])
@pytest.mark.parametrize("content", ["page", "blocked", "short"])
@pytest.mark.parametrize("intent", ["business", "person", "customer_voice"])
def test_b2b_founder_threads_produce_signals_and_published_routes(journey_db, monkeypatch, url, content, intent):
    from app.core.local_mode import is_local
    from app.gtm import journey, reachable, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, ScrapedText
    from core.clock import utcnow
    author_url = ("https://news.ycombinator.com/user?id=founder_qa" if url == HN else
        "https://www.reddit.com/user/founder_qa/" if url == REDDIT else "https://community.founders.example/members/founder_qa/")
    metadata = {"author": "founder_qa", "created_utc": (utcnow() - timedelta(days=2)).timestamp()}
    page = f"Posted by [founder_qa]({author_url})\n\n{NEED}"
    inline = {"page": page, "blocked": REDDIT_BLOCK_PAGE, "short": "Customer interviews and launch outreach."}[content]
    hit = {"url": url, "title": "Founder customer interviews", "snippet": NEED, "content": inline, "metadata": metadata}
    scrapes = []
    async def model(payload, stage, context):
        result = await founder_model(payload, stage, context)
        if stage == "plan":
            result["queries"][0]["intent"] = intent
        return result
    async def search(self, query, **kwargs):
        return [hit, {"url": "https://news.ycombinator.com/", "title": "Hacker News", "snippet": "Startup news"}]
    async def scrape(self, source, **kwargs):
        scrapes.append(source)
        assert source == url
        return ScrapedText(page, metadata=metadata, usage={})
    async def hn(*args, **kwargs):
        return {"items": []}
    def fetch(source, **kwargs):
        if url != HN or content == "blocked":
            return None
        assert source == "https://hn.algolia.com/api/v1/items/123456"
        return json.dumps({"id": 123456, "type": "story", "author": "founder_qa",
            "created_at_i": int(metadata["created_utc"]), "text": NEED, "children": []})
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", scrape)
    if is_local():
        monkeypatch.setattr("app.research.local_providers.local_read", lambda source, **kwargs: scrape(None, source))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    hits = reachable.candidate_hits([hit | {"kind": intent}], CONTEXT, payload)
    assert len(hits) == 1 and hits[0]["kind"] == "person"
    assert asyncio.run(journey._discover(AUDIENCE, problem=PROBLEM, offer=OFFER, payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    assert len(doc["need_signals"]) == 1 and doc["need_signals"][0]["quote"] == NEED
    assert doc["need_signals"][0]["source_url"] == url
    assert doc["icp"]["mode"] == "company" and not doc["icp"].get("recruitment_opt_in")
    if content != "blocked" or url == REDDIT:
        assert len(doc["reachable_leads"]) == 1, (str(payload.get("_trace_counts")), scrapes)
        lead = doc["reachable_leads"][0]
        assert lead["kind"] == "person" and lead["quote"] == NEED
        assert lead["author"] == "founder_qa" and lead["source_url"] == url
        assert {route["url"] for route in lead["routes"]} == {url, author_url}
        assert all(route["kind"] == "forum" and not route.get("email") for route in lead["routes"])
    else:
        assert not doc["reachable_leads"]
    _assert_no_email_contact(journey_db)


def test_algolia_author_and_date_use_existing_thread_route(journey_db, monkeypatch):
    from app.gtm import journey, reachable, scrapy_leads
    from core.clock import utcnow
    author_url = "https://news.ycombinator.com/user?id=founder_qa"
    hit = {"url": HN, "title": "Founder customer interviews", "kind": "customer_voice", "snippet": NEED,
        "content": NEED, "provider": "hn_algolia", "prior_art": {"author": "founder_qa", "author_url": author_url,
            "date": (utcnow() - timedelta(days=2)).isoformat()}}
    monkeypatch.setattr(journey, "_discovery_model", founder_model)
    monkeypatch.setattr(scrapy_leads, "_fetch", lambda *args, **kwargs: None)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    leads = asyncio.run(reachable.discover(None, [hit], [], CONTEXT, payload))
    assert len(leads) == 1, payload.get("_trace_counts")
    assert leads[0]["signals"][0]["published_at"] == hit["prior_art"]["date"]
    assert {route["url"] for route in leads[0]["routes"]} == {HN, author_url}


@pytest.mark.parametrize("multiple_hits", [False, True])
@pytest.mark.parametrize("prior_failures", [0, 2])
def test_founder_comment_becomes_signal_and_commenter_lead(journey_db, monkeypatch, multiple_hits, prior_failures):
    from app.core.local_mode import is_local
    from app.gtm import email_learning, journey, quality, reachable, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, _sources_from_response
    from core.clock import utcnow
    need = "I spend hours on manual customer interviews and launch outreach"
    created = int((utcnow() - timedelta(days=2)).timestamp())
    tree = {"id": 123456, "type": "story", "author": "story_author", "created_at_i": created - 86400,
        "text": "<p>How do founders launch their products?</p>", "children": [
            {"id": 123457, "type": "comment", "author": "other_founder", "created_at_i": created,
                "text": "<p>I use a spreadsheet.</p>", "children": [
                    {"id": 123458, "type": "comment", "author": "interview_founder", "created_at_i": created,
                        "text": f"<p>{need}</p>", "children": []}]}]}
    fetched, cached, inline_searches = [], [], []
    def fetch(url, **kwargs):
        assert kwargs == {"allow_json": True}
        fetched.append(url)
        return json.dumps(tree | {"id": int(url.rsplit("/", 1)[-1])})
    async def hn(result, cap, url, **kwargs):
        assert url == adapters.HN_SEARCH and cap == 1 and "num_comments>4" in kwargs["params"]["numericFilters"] and kwargs["params"]["tags"] == "ask_hn"
        return {"hits": [{"objectID": "123456", "title": "Founder launch workflows",
            "story_text": tree["text"], "author": "story_author", "created_at_i": created - 86400,
            "_tags": ["story", "ask_hn"], "num_comments": 2, "points": 5}]}
    async def search(*args, **kwargs):
        inline_searches.append(kwargs.get("include_content", False))
        return _sources_from_response("firecrawl", {"success": True, "data": {"web": [
            {"url": f"https://news.ycombinator.com/item?id={123456 + index}",
                "title": "Founder customer interviews and launch outreach", "description": tree["text"],
                **({"markdown": "# Founder launch workflows\n\nHow do founders launch their products?"} if index < 7 else {})}
            for index in range(8 if multiple_hits else 1)] if multiple_hits or is_local() else []}})
    async def forbidden_scrape(*args, **kwargs):
        raise AssertionError("HN items must use the comment API, not Firecrawl")
    async def model(payload, stage, context):
        if stage == "passages":
            assert all(need not in row["text"] or row["text"] == need for row in context["candidates"])
            return {"items": [{"id": row["id"], "relevance": 0.95 if row["text"] == need else 0.5}
                for row in context["candidates"]]}
        result = await founder_model(payload, stage, context)
        if stage in {"judge", "reachjudge"}:
            for row in result["items"]:
                row["quote"] = need
        return result
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(adapters, "_request", hn)
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", forbidden_scrape)
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda key, value: cached.append((key, value)))
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    if prior_failures:
        for identifier in (123456, 123455):
            journey._thread_read_observed(payload, f"https://news.ycombinator.com/item?id={identifier}", False)
    assert asyncio.run(journey._discover(AUDIENCE, problem=PROBLEM, offer=OFFER, payload=payload)) == []
    document = journey._journal(payload).document(payload["journey_id"])
    expected = [f"https://hn.algolia.com/api/v1/items/{123456 + index}" for index in range(4 if multiple_hits else 1)]
    assert fetched == expected
    assert document["_runtime"]["calls_used"]["hn_items"] == len(expected)
    assert document["_runtime"]["thread_readability"]["news.ycombinator.com"]["failures"] == 0
    assert not any("host_unreadable" in key for key in payload.get("_trace_counts", {}))
    assert inline_searches and any(inline_searches) == (not is_local())
    signal = document["need_signals"][0]
    assert signal["quote"] == need and signal["author"] == "interview_founder"
    assert signal["author_url"] == "https://news.ycombinator.com/user?id=interview_founder"
    assert signal["date_evidence"] == journey.datetime.fromtimestamp(created, journey.timezone.utc).isoformat()
    lead = document["reachable_leads"][0]
    assert lead["author"] == lead["name"] == "interview_founder" and lead["quote"] == need
    assert lead["source_url"] in [f"https://news.ycombinator.com/item?id={123456 + index}" for index in range(len(expected))]
    assert {route["url"] for route in lead["routes"]} == {lead["source_url"], signal["author_url"]}
    item = document["_runtime"]["judge_candidates"][0]
    assert len(item["text"]) > len(tree["text"])
    assert len(item["hit"]["content"]) > len("# Founder launch workflows\n\nHow do founders launch their products?")
    pages = quality.signal_pages(item["hit"], item["text"], CONTEXT)
    assert len(pages) == 3 and all(page["source"] != "search_snippet" for page in pages)
    assert reachable.author_routes(HN, item["text"], need, "story_author")["author"] == "interview_founder"
    assert any(value.get("signal_pages") for key, value in cached if isinstance(value, dict))


@pytest.mark.parametrize("provider", ["hn_algolia", "firecrawl"])
@pytest.mark.parametrize("context", [CONTEXT, CONTEXT | {"mode": "B2C", "audience": "parents"}])
@pytest.mark.parametrize("prior_failures", [0, 2])
def test_production_inline_hn_hits_fetch_comments(journey_db, monkeypatch, provider, context, prior_failures):
    from app.gtm import email_learning, journey, quality, scrapy_leads
    from app.research.prior_art import adapters
    from app.signals.research import _sources_from_response
    story = "<p>How do founders launch their products?</p>"
    async def request(*args, **kwargs):
        return {"hits": [{"objectID": "123456", "title": "Ask HN: Founder workflows", "story_text": story,
            "author": "story_author", "_tags": ["story", "ask_hn"], "num_comments": 1, "points": 5}]}
    monkeypatch.setattr(adapters, "_request", request)
    if provider == "hn_algolia":
        hit = asyncio.run(adapters.search_hn("founder workflows", tags="(story,comment,ask_hn)", by_date=True))["items"][0]
    else:
        hit = _sources_from_response("firecrawl", {"success": True, "data": {"web": [{"url": HN,
            "title": "Founder workflows", "description": "Founder workflows",
            "markdown": "# Founder workflows\n\n" + "How do founders launch their products? " * 12,
            "metadata": {"statusCode": 403}}]}})[0]
    fetched = []
    def fetch(url, **kwargs):
        assert kwargs == {"allow_json": True}
        fetched.append(url)
        return json.dumps({"id": 123456, "type": "story", "author": "story_author", "text": story,
            "children": [{"id": 123457, "type": "comment", "author": "commenter", "text": NEED * 8}]})
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    if prior_failures:
        for identifier in (123456, 123455):
            journey._thread_read_observed(payload, f"https://news.ycombinator.com/item?id={identifier}", False)
    enriched, text, metadata = asyncio.run(journey._signal_page(None, hit, payload, context=context))
    assert fetched == ["https://hn.algolia.com/api/v1/items/123456"]
    assert len(text) > len(hit["content"])
    assert len(enriched["content"]) == len(text)
    assert metadata.get("statusCode", 200) < 400
    passages = quality.passages(quality.signal_pages(enriched, text, context), context)
    assert any(page.get("source") == "hn_comment" and page.get("author") == "commenter" and NEED in page["text"] for page in passages)
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert runtime["calls_used"]["hn_items"] == 1
    assert runtime["thread_readability"]["news.ycombinator.com"]["failures"] == 0
    assert not any("host_unreadable" in key for key in payload.get("_trace_counts", {}))


@pytest.mark.parametrize("context", [CONTEXT, CONTEXT | {"mode": "B2C", "audience": "parents"}])
def test_hn_items_ceiling_and_replay(journey_db, monkeypatch, context):
    from app.gtm import email_learning, journey, scrapy_leads
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        return json.dumps({"id": int(url.rsplit("/", 1)[-1]), "type": "story", "author": "founder_qa",
            "text": "Founder workflows", "children": [{"id": 1, "type": "comment", "author": "commenter",
                "text": NEED}]})
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    for index, identifier in enumerate([123456, 123456, 123457, 123458, 123459, 123460, 123461, 123462, 123456]):
        hit = {"url": f"https://news.ycombinator.com/item?id={identifier}",
            "kind": "customer_voice", "title": "Founder workflows", "snippet": "Founder workflows",
            **({"content": "Founder workflows"} if identifier < 123462 else {}),
            **({"provider": "hn_algolia"} if index % 2 == 0 else {}), "metadata": {"statusCode": 403}}
        result = asyncio.run(journey._signal_page(None, hit, payload, context=context))
        if identifier < 123460:
            assert result[0]["signal_pages"][1]["source"] == "hn_comment" and NEED in result[1]
            assert "statusCode" not in result[2]
        runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
        assert runtime["thread_readability"]["news.ycombinator.com"]["failures"] == 0
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert len(fetched) == runtime["calls_used"]["hn_items"] == journey.CALL_CEILINGS["hn_items"] == 4
    assert "signal_scrape" not in runtime["calls_used"] and "public_page" not in runtime["calls_used"]
    assert not any("host_unreadable" in key for key in payload.get("_trace_counts", {}))


@pytest.mark.parametrize("response", [None, "not json", json.dumps({"id": 1, "type": "story"}), 403])
def test_failed_hn_items_use_item_ceiling_not_host_cutoff(journey_db, monkeypatch, response):
    from app.gtm import email_learning, journey, scrapy_leads
    fetched = []
    def fetch(url, **kwargs):
        fetched.append(url)
        if response == 403:
            import httpx
            blocked = httpx.Response(403, request=httpx.Request("GET", url))
            blocked.raise_for_status()
        return response
    monkeypatch.setattr(scrapy_leads, "_fetch", fetch)
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    for identifier in range(123456, 123458):
        asyncio.run(journey._signal_page(None, {"url": f"https://news.ycombinator.com/item?id={identifier}",
            "content": NEED * 10, "provider": "hn_algolia"}, payload, context=CONTEXT))
    for identifier in range(123458, 123463):
        assert not asyncio.run(journey._signal_page(None, {"url": f"https://news.ycombinator.com/item?id={identifier}",
            "kind": "customer_voice" if identifier % 2 else "person"}, payload, context=CONTEXT))[1]
    assert len(fetched) == journey.CALL_CEILINGS["hn_items"] == 4
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert runtime["thread_readability"]["news.ycombinator.com"]["failures"] == 4
    assert not any("host_unreadable" in key for key in payload.get("_trace_counts", {}))


def test_hn_tree_is_bounded_and_excludes_deleted_unattributed_and_deep_comments():
    from app.gtm import journey, quality
    from core.clock import utcnow
    comments = [{"id": index + 2, "type": "comment", "author": f"founder_{index}", "text": f"<p>{NEED}</p>"}
        for index in range(45)]
    comments[0]["children"] = [{"id": 200, "type": "comment", "author": "child", "text": NEED,
        "children": [{"id": 201, "type": "comment", "author": "deep", "text": NEED}]}]
    tree = {"id": 1, "type": "story", "author": "story", "text": "Story about founder workflows", "children": comments}
    result = journey._hn_thread(json.dumps(tree), 1, HN)
    assert len(result["signal_pages"]) == 41 and len(result["content"]) <= 24000
    assert "founder_40" not in result["content"]
    tree["children"] = comments[:1] + [comments[1] | {"deleted": True}, comments[2] | {"author": "bad](https://evil.example)"}]
    result = journey._hn_thread(json.dumps(tree), 1, HN)
    assert [page["author"] for page in result["signal_pages"]] == ["story", "founder_0", "child"]
    assert "<p>" not in result["content"] and "deep" not in result["content"]
    tree["children"] = [comment | {"text": NEED * 200} for comment in comments]
    assert len(journey._hn_thread(json.dumps(tree), 1, HN)["content"]) <= 24000
    assert journey._hn_thread(json.dumps(tree), 2, HN) == {}
    assert journey._hn_thread("not json", 1, HN) == {}
    pages = journey._hn_thread(json.dumps(tree), 1, HN)["signal_pages"]
    page = quality.passages(pages, CONTEXT)[1]
    assert page["author"] == "founder_0" and page["source"] == "hn_comment"
    hit = {"url": HN, "kind": "customer_voice", "signal_pages": pages,
        "created_utc": utcnow().timestamp(), "prior_art": {"date": utcnow().isoformat()}}
    signal = journey._qualify_signal(hit, "", {"created_utc": utcnow().timestamp()}, PROBLEM, AUDIENCE,
        utcnow(), judged={"quote": NEED, "author": "founder_0"}, context=CONTEXT)
    assert signal["author"] == "founder_0" and signal["published_at"] is None


def test_business_audience_does_not_read_hn_items(journey_db, monkeypatch):
    from app.gtm import journey, scrapy_leads
    def forbidden(*args, **kwargs):
        raise AssertionError("business audiences must not call HN")
    monkeypatch.setattr(scrapy_leads, "_fetch", forbidden)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "independent dental practices", problem=PROBLEM)
    payload = _leased_payload(journey_db)
    hit = {"url": HN, "kind": "customer_voice", "provider": "hn_algolia", "content": NEED}
    assert asyncio.run(journey._signal_page(None, hit, payload, context=CONTEXT | {
        "audience": "independent dental practices"}))[1] == NEED
    assert "hn_items" not in journey._journal(payload).document(payload["journey_id"])["_runtime"]["calls_used"]


@pytest.mark.parametrize("audience", ["startup founders", "freelancers", "consultants", "developers", "designers", "coaches", "solo accountants"])
def test_professional_audience_is_not_consumer_mode(audience):
    from app.gtm import journey
    context = CONTEXT | {"audience": audience, "business_model": "B2B"}
    assert journey._person_audience(context)
    assert not journey._consumer(context)
    assert not journey._person_audience(CONTEXT | {"audience": "independent dental practices"})


@pytest.mark.parametrize("url", ["https://news.ycombinator.com/", "https://news.ycombinator.com/news",
    "https://news.ycombinator.com/newest", "https://news.ycombinator.com/item", "https://news.ycombinator.com/item?id=invalid"])
def test_hn_lists_are_not_person_threads(url):
    from app.gtm import enrich, journey, reachable
    assert enrich.classify(url) == "directory"
    assert not journey._consumer_thread_url(url)
    assert reachable.candidate_hits([{"url": url, "kind": "customer_voice"}], CONTEXT) == []
    assert enrich.classify(HN, "Top startup articles") == "thread"
    assert reachable.candidate_hits([{"url": HN, "kind": "customer_voice"}], CONTEXT | {"audience": "dental practices"}) == []


@pytest.mark.parametrize("text,competitor", [
    ("I am a solo technical founder in the US. Our software monitors factory sensors.", False),
    ("I am a startup founder in the US. Our agency provides branding and design services for startups.", False),
    ("I am a solo technical founder. Our software automates customer interviews and launch outreach.", True),
])
def test_founder_homepages_require_buyer_proof_without_same_offer_bypass(journey_db, monkeypatch, text, competitor):
    from app.gtm import email_learning, journey, quality
    calls = []
    async def model(payload, stage, context):
        calls.append(stage)
        assert context["target_type"] == "person" and context["mode"] == "B2B"
        return {"role_in_market": "buyer", "icp_fit": 0.95, "anti_icp": False, "homepage_quote": text}
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda *args: None)
    monkeypatch.setattr(journey, "_discovery_model", model)
    verdict = asyncio.run(quality.classify_home("https://startup.example/", text, CONTEXT, {"workspace_id": journey_db["wid"]}))
    assert bool(verdict) is not competitor
    assert calls == ([] if competitor else ["marketrole"])


def test_marketrole_prompt_distinguishes_unrelated_founder_business(journey_db, monkeypatch):
    from app.gtm import journey
    import app.research.metered as metered
    captured = []
    def synthesize(*args, **kwargs):
        captured.append(kwargs["messages"])
        return {"role_in_market": "buyer", "icp_fit": 0.95, "anti_icp": False, "homepage_quote": NEED}
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    monkeypatch.setattr(metered, "synthesize", synthesize)
    asyncio.run(journey._discovery_model(_leased_payload(journey_db), "marketrole", CONTEXT | {
        "target_type": "person", "candidates": [{"id": 0, "url": "https://startup.example/", "text": NEED}]}))
    prompt = captured[0][0]["content"]
    assert "SAME offer or a substitute" in prompt and "B2B describes the purchase" in prompt
    assert "Selling unrelated tools or services" in prompt
    assert "Companies selling tools/services INTO the target audience" not in prompt


def test_empty_public_read_is_not_cached(journey_db, monkeypatch):
    from app.gtm import email_learning, journey
    saved = []
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda key, value: saved.append(value))
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCE, problem=PROBLEM)
    payload = _leased_payload(journey_db)
    async def empty():
        return {}
    async def full():
        return {"content": "thread"}
    assert asyncio.run(journey._signal_call(payload, "hn_items", "https://hn.algolia.com/api/v1/items/1", empty)) == {}
    assert saved == []
    assert asyncio.run(journey._signal_call(payload, "hn_items", "https://hn.algolia.com/api/v1/items/2", full)) == {"content": "thread"}
    assert saved == [{"content": "thread"}]
