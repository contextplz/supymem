import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.unit.gtm.test_journey_signals import _leased_payload, quality_response
from tests.unit.gtm.test_journey_signals import discovery_models as discovery_models
from tests.unit.gtm.test_journey_signals import journey_db as journey_db

def _replay(filename):
    text = (Path(__file__).parent / "fixtures" / filename).read_text()
    return json.loads(text.replace("https://threads.test/", "https://www.reddit.com/")
        .replace("https://photo-posts.test/", "https://www.instagram.com/"))


REPLAY = _replay("discovery_public_replay.json")
AUDIENCES = {"recall": "Independent dental clinics in the US",
    "splash": "Working parents of children in gated apartment communities in Bangalore",
    "brew": "Home espresso enthusiasts US/Canada"}


@pytest.mark.parametrize("venture,count", [("recall", 8), ("splash", 8), ("brew", 8)])
def test_recorded_plans_are_usable(venture, count):
    from app.gtm.discovery import Plan, parse_response
    plan = parse_response(Plan, REPLAY[venture]["plan"])
    assert len(plan["queries"]) == count
    assert all(len(query["query"].split()) <= 16 for query in plan["queries"])


@pytest.mark.parametrize("venture,count", [("recall", 1), ("splash", 2), ("brew", 7)])
def test_recorded_channels_survive_unavailable_judge(journey_db, monkeypatch, venture, count):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider, _sources_from_response
    from core.clock import utcnow
    rows = _sources_from_response("firecrawl", {"success": True, "data": {"web": REPLAY[venture]["web"]}})
    remaining = list(rows)
    async def search(*args, **kwargs):
        excluded = kwargs.get("exclude_domains", ())
        found = [row for row in remaining if not any(journey.enrich._host(row["url"]) == domain
            or journey.enrich._host(row["url"]).endswith("." + domain) for domain in excluded)][:5]
        remaining[:] = [row for row in remaining if row not in found]
        return found
    async def judge(payload, stage, context):
        if stage == "plan":
            return {"queries": [{"query": "audience community", "intent": "community"}, {"query": "audience needs", "intent": "customer_voice"}]}
        raise RuntimeError("judge unavailable")
    async def hn(*args, **kwargs):
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("extra page read"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", judge)
    journey.update_icp(journey_db["wid"], journey_db["vid"], AUDIENCES[venture])
    journey.prepare(journey_db["wid"], journey_db["vid"], recruitment_opt_in=True)
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover(AUDIENCES[venture], problem="relevant problem", competitors=["Trade Coffee", "Bean Box", "Atlas"], payload=payload)) == []
    doc = journey._journal(payload).document(payload["journey_id"])
    channels = doc["channels"]
    assert len(channels) == count and not doc["need_signals"]
    assert len({channel["url"] for channel in channels}) == count
    for channel in channels:
        source = next(row for row in rows if row["url"] == channel["source_url"] and journey._verbatim(channel["quote"], row.get("content") or row["snippet"]))
        assert source and journey._community_url(source["url"], channel["url"], source.get("content") or source["snippet"])
        assert "permission" in channel["draft"]
    if venture == "brew":
        assert "Be civil" in channels[0]["participation"]
        assert journey._customer_drafts("BrewBox", AUDIENCES[venture])["lookalikes_note"].endswith("nothing is sent.")
    if venture == "splash":
        assert all("unverified" in channel["why_fits"] for channel in channels)
        assert not any("Autistic" in channel["name"] for channel in channels)
    for row in rows:
        assert journey._qualify_signal(row | {"kind": "customer_voice"}, row.get("content") or row["snippet"], {}, "relevant problem", AUDIENCES[venture], utcnow()) is None


def test_tracking_tokens_are_data_not_credentials(journey_db):
    from app.boundary.filter import inspect_payload
    from app.gtm.discovery import judge_context
    from llm_gateway.boundary import BoundaryViolation
    source = "Discuss espresso. [Forum](https://forum.example/?sid=abcdef0123456789abcdef0123456789)"
    context = judge_context({"candidates": [{"id": 0, "text": source}]})
    assert "sid=" not in context["candidates"][0]["text"]
    inspect_payload([{"role": "user", "content": json.dumps(context)}], purpose="gtm.discovery.judge", workspace_id=journey_db["wid"], provider="test")
    unsafe = judge_context({"candidates": [{"id": 0, "text": "password=LiveCredentialValueLongEnough"}]})
    with pytest.raises(BoundaryViolation):
        inspect_payload([{"role": "user", "content": json.dumps(unsafe)}], purpose="gtm.discovery.judge", workspace_id=journey_db["wid"], provider="test")


def test_pii_redaction_cannot_break_json_escapes(monkeypatch):
    from app.boundary.detectors import pii
    from app.gtm.discovery import judge_context
    monkeypatch.setattr(pii, "_RESERVED_TLDS", tuple(tld for tld in pii._RESERVED_TLDS if tld != ".example"))
    context = {"candidates": [{"id": 0, "text": r'Contact \person@redaction-mail.example and "quoted" text.'}]}
    cleaned = judge_context(context)
    assert "redaction-mail.example" not in cleaned["candidates"][0]["text"]
    assert json.loads(json.dumps(cleaned)) == cleaned


def test_year_old_needs_are_ranked_and_labelled_not_invented():
    from app.gtm import journey
    from core.clock import utcnow
    now = utcnow()
    hit = {"url": "https://forum.example/post", "kind": "customer_voice", "title": "Dental recall"}
    text = "We need dental recall help; no-shows leave our front desk overwhelmed."
    judged = {"quote": text}
    def need(age):
        return journey._qualify_signal(hit, text, {"publishedTime": (now - timedelta(days=age)).isoformat()}, "dental recall", "dental clinic owners", now, judged=judged)
    recent, older = need(30), need(180)
    assert recent["recency"] == "recent" and older["recency"] == "older" and "(older)" in older["why_now"]
    assert journey._signal_score(recent, "dental clinic owners", now) > journey._signal_score(older, "dental clinic owners", now)
    assert need(366) is None and need(-1) is None


def test_judge_cannot_override_same_offer_vendor_exclusion():
    from app.gtm import journey
    text = "Our product is a coffee subscription for espresso enthusiasts. Join our coffee community."
    hit = {"url": "https://seller.example/", "title": "Coffee Community", "content": text}
    assert journey._channel(hit, "home espresso enthusiasts", "espresso beans", [], "coffee subscription",
        judged={"quote": text, "name": "Coffee Community", "url": hit["url"]}) is None


@pytest.mark.parametrize("metadata", [{"publishedTime": "2026-10-01"}, {"article:published_time": "2026-10-01"}, {"og:published_time": "2026-10-01"}])
def test_need_dates_use_all_published_metadata(metadata):
    from app.gtm import journey
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    quote = "We need dental recall help; no-shows overwhelm our front desk."
    hit = {"url": "https://forum.example/need", "kind": "customer_voice"}
    signal = journey._qualify_signal(hit, quote, metadata, "dental recall", "dental staff", now, judged={"quote": quote})
    assert signal["published_at"].startswith("2026-10-01") and signal["recency"] == "recent"


@pytest.mark.parametrize("stamp,status", [("Posted 6d ago", "approximate"), ("4d ago ·", "approximate"), ("Posted 2 years ago", "approximate"),
    ('<time datetime="2026-10-01T12:00:00Z">', "published"), ('<meta property="article:published_time" content="2026-10-01">', "published"),
    ("2026-10-01", "published")])
def test_publication_dates_are_scoped_and_relative_to_fetch(stamp, status):
    from app.gtm import journey
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    quote = "We need help rebooking dental no-shows."
    date, actual, evidence = journey._publication({"observed_at": "2026-10-08T00:00:00Z"}, stamp + "\n" + quote, {}, quote, now)
    assert date and actual == status and evidence
    if stamp == "Posted 6d ago":
        assert date.date().isoformat() == "2026-10-02"
    assert journey._publication({}, quote + "\n\nRecent posts\nPosted 1d ago", {}, quote, now)[0] is None
    assert journey._publication({}, "Posted 1d ago\nWe work at another practice.\n\n" + quote, {}, quote, now)[0] is None
    assert journey._publication({}, "> Posted 1d ago\n\n" + quote, {}, quote, now)[0] is None


def test_recorded_undated_reddit_need_survives_but_vendor_pitch_does_not():
    from app.gtm import journey
    from core.clock import utcnow
    replay = _replay("discovery_public_replay2.json")
    need, pitch = replay["recall"]
    who = "Independent dental clinics in the US with front-desk staff"
    problem = "Dental clinics lose revenue from no-show and cancelled appointments because front-desk staff cannot follow up with every patient to rebook."
    signal = journey._qualify_signal(need | {"kind": "customer_voice"}, need["description"], {}, problem, who, utcnow())
    assert signal and signal["quote"] == need["description"] and signal["published_at"] is None
    assert signal["recency"] == "unknown" and "Date unknown" in signal["why_now"]
    assert journey._signal_score(signal, who, utcnow()) == 0
    assert journey._qualify_signal(need | {"kind": "community"}, need["description"], {}, problem, who, utcnow())["kind"] == "customer_voice"
    assert journey._qualify_signal(pitch | {"kind": "customer_voice"}, pitch["description"], {}, problem, who, utcnow()) is None


def test_recorded_physical_swim_venue_is_partner_not_direct_substitute():
    from app.gtm import journey
    replay = _replay("discovery_public_replay2.json")
    row = replay["splash"][0]
    hit = row | {"content": row["markdown"]}
    channels = journey._public_channels(hit, "Bangalore parents", "child quit swimming lessons", ["Paper Otter"], "swimming lessons")
    assert len(channels) == 1 and channels[0]["url"] == "https://www.paper-otter.example/"
    assert channels[0]["channel_role"] == "partner/channel to approach"
    assert journey._verbatim(channels[0]["quote"], row["markdown"])
    assert journey._same_offer(hit["url"], "Paper Otter", row["markdown"], ["Paper Otter"], "swimming lessons", "")
    directory = hit | {"url": "https://www.justdial.com/Bangalore/Swimming", "title": "Popular Swimming Classes"}
    assert not journey._public_channels(directory, "Bangalore parents", "swimming", [], "swimming lessons")


@pytest.mark.parametrize("venture,count", [("recall", 2), ("splash", 3), ("brew", 4)])
def test_new_recorded_channels_keep_exact_public_evidence(venture, count):
    from app.gtm import journey
    from app.signals.research import _sources_from_response
    replay = _replay("discovery_public_replay2.json")
    rows = _sources_from_response("firecrawl", {"success": True, "data": {"web": replay[venture]}})
    channels = {channel["url"]: channel for row in rows for channel in journey._public_channels(
        row, AUDIENCES[venture], "espresso beans" if venture == "brew" else "swimming lessons" if venture == "splash" else "dental no-shows",
        ["Trade Coffee", "Bean Box", "Atlas"], "coffee subscription" if venture == "brew" else "swimming lessons")}
    assert len(channels) == count
    for channel in channels.values():
        source = next(row for row in rows if row["url"] == channel["source_url"])
        text = source.get("content") or source["snippet"]
        assert journey._verbatim(channel["quote"], text)
        assert journey._community_url(source["url"], channel["url"], text)
        assert "permission" in channel["draft"]
        if "instagram.com" in channel["url"]:
            assert channel["url"] == source["url"] and channel["channel_role"] == "partner/channel to approach"
            assert "unverified" in channel["why_fits"]
            assert not journey._community_url(source["url"], "https://www.instagram.com/", text)
    if venture == "brew":
        venue = next(row for row in rows if row["url"] == "https://steam-mitten.example/")
        channel = journey._channel(venue, AUDIENCES[venture], "espresso beans", [], "coffee subscription", judged={
            "name": "Steam Mitten", "url": venue["url"], "quote": "Our New York workshop teaches beginner and advanced coffee classes."})
        assert channel and channel["channel_role"] == "partner/channel to approach"


def test_forum_discussing_subscriptions_is_not_the_vendor():
    from app.gtm import journey
    text = "[Back to Discussion List](https://forum.example/)\nWe buy a coffee subscription and discuss espresso beans."
    hit = {"url": "https://forum.example/topic", "title": "Espresso community", "content": text}
    assert journey._same_offer(hit["url"], hit["title"], text, [], "coffee subscription", "espresso")
    assert journey._channel(hit, "Home espresso enthusiasts", "espresso beans", [], "coffee subscription")
    assert not journey._channel(hit, "Home espresso enthusiasts", "espresso beans", [{"name": "Vendor", "domain": "forum.example"}], "coffee subscription")


@pytest.mark.parametrize("name,domain", [("Trade Coffee", "drinktrade.com"), ("Bean Box", "beanbox.com"), ("Atlas", "atlascoffeeclub.com")])
def test_online_coffee_substitutes_never_become_channels(name, domain):
    from app.gtm import journey
    hit = {"url": "https://" + domain, "title": name, "content": "Our coffee subscription community helps home espresso enthusiasts."}
    assert not journey._public_channels(hit, "Home espresso enthusiasts US/Canada", "espresso beans", [name], "coffee subscription")
    assert not journey._channel(hit, "Home espresso enthusiasts US/Canada", "espresso beans", [name], "coffee subscription",
        judged={"name": name, "url": hit["url"], "quote": hit["content"]})


def test_undated_needs_are_not_recent_or_company_contacts(journey_db, monkeypatch):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    from core.clock import utcnow
    quote = "We need help rebooking dental no-shows."
    rows = [{"url": "https://forum.example/undated", "title": "Dental recall", "content": "I work at Dental Practice. " + quote},
        {"url": "https://forum.example/recent", "title": "Dental recall", "content": quote, "metadata": {"publishedTime": utcnow().isoformat()}}]
    async def search(*args, **kwargs):
        return rows
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": "dental no-shows", "intent": "customer_voice"}, {"query": "dental recall", "intent": "customer_voice"}]}
        return {"items": [{"id": index, "kind": "customer_voice", "quote": quote, "affiliation": "Dental Practice" if index == 0 else "", "name": "", "url": ""} for index in range(2)]}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("unexpected scrape"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "dental clinic owners")
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover("dental clinic owners", problem="dental rebooking no-shows", payload=payload)) == []
    signals = journey._journal(payload).document(payload["journey_id"])["need_signals"]
    assert [signal["recency"] for signal in signals] == ["recent", "unknown"]
    assert not signals[-1]["published_at"] and not signals[-1].get("company_evidence_url")
    assert not journey._journal(payload).document(payload["journey_id"])["_runtime"]["calls_used"].get("domain_search")


def test_restricted_hosts_and_inline_markdown_never_trigger_scrapes(journey_db, monkeypatch):
    from app.gtm import journey
    from app.research.prior_art import adapters
    from app.signals.research import HttpResearchProvider
    async def search(*args, **kwargs):
        return [{"url": "https://www.instagram.com/reel/example/", "title": "Swimming", "snippet": "Child swimming classes in Bangalore"},
            {"url": "https://www.reddit.com/r/Dentistry/comments/post/", "title": "Dental no-shows", "snippet": "We need help rebooking dental no-shows."},
            {"url": "https://forum.example/need", "title": "Dental recall", "content": "We need help rebooking dental no-shows."}]
    async def hn(*args, **kwargs):
        return {"items": []}
    async def model(payload, stage, context):
        if stage in {"marketrole", "passages"}:
            return quality_response(stage, context)
        if stage == "plan":
            return {"queries": [{"query": "dental no-shows", "intent": "customer_voice"}, {"query": "dental recall", "intent": "customer_voice"}]}
        return {"items": []}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(HttpResearchProvider, "search", search)
    monkeypatch.setattr(HttpResearchProvider, "scrape", lambda *args, **kwargs: pytest.fail("restricted or already fetched page read"))
    monkeypatch.setattr(adapters, "search_hn", hn)
    monkeypatch.setattr(journey, "_discovery_model", model)
    journey.update_icp(journey_db["wid"], journey_db["vid"], "dental clinic owners")
    payload = _leased_payload(journey_db)
    assert asyncio.run(journey._discover("dental clinic owners", problem="dental rebooking no-shows", payload=payload)) == []
    signals = journey._journal(payload).document(payload["journey_id"])["need_signals"]
    assert signals and all(signal["published_at"] is None for signal in signals)
