"""Offline quality regression replay: reported QA excerpts plus labelled synthetic controls.

The restaurant nav/reservation and Upserve name are recorded from the bug report;
other text is a synthetic control, not a claim to have captured a live homepage.
Recorded structured verdicts exercise the production gates, not model accuracy.
"""
import asyncio

import pytest
from pydantic import ValidationError

from tests.company_os_harness import load_company_os_app
from tests.gtm_fixtures import REDDIT_BLOCK_PAGE
from tests.unit.gtm.test_journey import journey_db as journey_db

load_company_os_app()
from app.gtm import enrich, journey, quality  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_modules():
    import importlib
    load_company_os_app()
    globals().update({name: importlib.import_module("app.gtm." + name) for name in ("enrich", "journey", "quality")})


FIXTURES = [
    ("ShiftSwap QA", "buyer", False, 0.93, "We run a restaurant and struggle to cover last-minute staff shift changes.", True),
    ("ShiftSwap QA", "vendor/competitor", True, 0.99, "Upserve – Restaurant365 sells restaurant workforce scheduling software to general managers.", False),
    ("ShiftSwap QA", "directory/listing", True, 0.95, "Browse our directory of restaurants and reserve a table near you.", False),
    ("ShiftSwap QA", "buyer", False, 0.91, "We recommend making a reservation ahead of time.", False),
    ("LaunchDesk QA", "buyer", False, 0.95, "I founded a hardware startup and struggle to find my first paying customers.", True),
    ("LaunchDesk QA", "vendor/competitor", True, 0.98, "Our launch platform helps founders find their first paying customers.", False),
    ("LaunchDesk QA", "media", False, 0.91, "Read our magazine covering startup funding news and founder interviews.", False),
    ("LaunchDesk QA", "unknown", False, 0.2, "We build innovative products for a better future for everyone.", False),
    ("PupPath QA", "consumer/individual", False, 0.95, "I am a first-time dog owner and feel overwhelmed keeping up with puppy training.", True),
    ("PupPath QA", "vendor/competitor", True, 1.0, "Our obedience software subscription is sold to first-time dog owners.", False),
    ("PupPath QA", "directory/listing", True, 0.9, "Browse our listings of dog trainers and pet services near you.", False),
    ("PupPath QA", "consumer/individual", False, 0.8, "I like taking photos of my dog in the park every weekend.", False),
]
CONTEXTS = {
    "ShiftSwap QA": {"audience": "restaurant general managers", "problem": "last-minute staff shift coverage", "mode": "B2B"},
    "LaunchDesk QA": {"audience": "early-stage founders", "problem": "finding first paying customers", "mode": "B2B"},
    "PupPath QA": {"audience": "first-time dog owners", "problem": "keeping up with puppy training", "mode": "B2C"},
}
NAV = "Book Table Skip to main content (202)-555-0146 … Menu Drinks Specials"


@pytest.mark.parametrize("venture,role,anti,fit,text,keep", FIXTURES)
def test_recorded_quality_eval(monkeypatch, venture, role, anti, fit, text, keep):
    cache, calls = {}, []
    monkeypatch.setattr("app.gtm.email_learning.cached", cache.get)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: cache.update({key: value}))
    async def model(payload, stage, context):
        calls.append((stage, context))
        if stage == "marketrole":
            assert "misleading search snippet" not in context["candidates"][0]["text"]
            return {"role_in_market": role, "anti_icp": anti, "icp_fit": fit, "homepage_quote": text}
        assert stage == "passages"
        return {"items": [{"id": item["id"], "relevance": 0.92 if keep else 0.1} for item in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = CONTEXTS[venture] | {"offer": "", "competitors": []}
    home = "https://candidate.example/"
    raw = f"<nav>{NAV}</nav><main><p>{text}</p><p>{text}</p></main><footer>Privacy policy</footer>"
    extracted = quality.main_text(raw)
    assert extracted == text and NAV not in extracted
    payload = {"workspace_id": "offline-workspace"}
    verdict = asyncio.run(quality.classify_home(home, raw, context, payload))
    assert bool(verdict) == (not anti and role in {"buyer", "consumer/individual"} and fit >= 0.7)
    assert asyncio.run(quality.classify_home(home, raw, context, payload)) == verdict
    assert sum(stage == "marketrole" for stage, _ in calls) == 1
    selected = asyncio.run(quality.select_passages([{"url": home, "text": raw}], context, payload, identity=home)) if verdict else []
    assert bool(selected) == keep
    for page in selected:
        assert page["text"] in extracted
        assert quality.grounded_quote(page["text"], extracted) == page["text"]


def test_vendor_rejected_before_contact_enrichment(monkeypatch):
    async def reject(*args):
        return None
    monkeypatch.setattr(quality, "classify_home", reject)
    async def homepage(url):
        return "We sell restaurant scheduling tools to general managers."
    async def forbidden(url):
        pytest.fail("Rejected vendor must not be contact-enriched")
    assert asyncio.run(enrich.enrich_site(None, "https://vendor.example/", "workspace", "venture", scrape=forbidden,
        main_scrape=homepage, context=CONTEXTS["ShiftSwap QA"], payload={})) == {"quality_rejected": True}


@pytest.mark.parametrize("mode,relevances,expected", [
    ("B2C", [0.65], [0.65]), ("B2C", [0.6], [0.6]), ("B2C", [0.55], []),
    ("B2B", [0.65], []), ("B2C", [0.65, 0.7], [0.7]), ("B2B", [0.7], [0.7]),
])
def test_possible_passages_consumer_only_single_score(monkeypatch, mode, relevances, expected):
    calls = []
    async def model(payload, stage, context):
        calls.append(stage)
        return {"items": [{"id": row["id"], "relevance": relevances[row["id"]]} for row in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    pages = [{"url": f"https://forum.example/{index}", "text": f"I need help with puppy training exercise number {index}."}
        for index in range(len(relevances))]
    selected = asyncio.run(quality.select_passages(pages, CONTEXTS["PupPath QA"] | {"mode": mode}, {}, identity=pages[0]["url"]))
    assert [page["relevance"] for page in selected] == expected
    assert calls == ["passages"]
    assert all(bool(page.get("evidence_label") == "possible_fit") == (page["relevance"] < 0.7) for page in selected)
    assert all(any(quality.grounded_quote(page["text"], original["text"]) for original in pages) for page in selected)


@pytest.mark.parametrize("context", [CONTEXTS["ShiftSwap QA"], CONTEXTS["PupPath QA"]])
def test_possible_fit_cap_preserves_strong_signals_and_published_leads(journey_db, context):
    from app.gtm import reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    possible = [{"id": f"possible{index}", "evidence_label": "possible_fit", "fit": 100,
        "source_url": f"https://forum.example/members/possible{index}", "author": f"possible{index}",
        "routes": [{"kind": "forum", "url": f"https://forum.example/members/possible{index}", "source_url": "https://forum.example/need", "quote": "I need help with training."}]}
        for index in range(8)]
    strong = [{"id": f"strong{index}", "fit": 70, "source_url": f"https://forum.example/members/strong{index}",
        "author": f"strong{index}", "routes": [{"kind": "forum", "url": f"https://forum.example/members/strong{index}", "source_url": "https://forum.example/need", "quote": "I need help with training."}]}
        for index in range(journey.MAX_KEPT)]
    items = possible + strong
    kept = quality.limit_fits(items, journey.MAX_KEPT)
    assert kept == strong + possible[:quality.MAX_POSSIBLE_FITS]
    assert {lead["id"] for lead in sorted(items, key=reachable.rank)[:journey.MAX_KEPT]} == {lead["id"] for lead in strong}
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    query_count = journey._run_ceilings(journey._journal(payload).document(payload["journey_id"])["icp"])["signal_search"]
    journey._journal(payload).mutate(payload["journey_id"], payload, lambda row, doc, session: doc.update(
        discovery_plan=[{"query": f"query {index}", "intent": "customer_voice"} for index in range(query_count)]))
    reachable._publish(journey._journal(payload), payload, items)
    saved = journey._journal(payload).document(payload["journey_id"])["reachable_leads"]
    keep_limit = journey._keep_limit(query_count)
    assert len(saved) == keep_limit + quality.MAX_POSSIBLE_FITS
    assert {lead["id"] for lead in saved[:keep_limit]} == {lead["id"] for lead in sorted(strong, key=reachable.rank)[:keep_limit]}
    reachable._publish(journey._journal(payload), payload, items)
    assert len(journey._journal(payload).document(payload["journey_id"])["reachable_leads"]) == len(saved)


def test_structured_output_and_grounding_fail_closed(monkeypatch):
    valid = {"role_in_market": "buyer", "anti_icp": False, "icp_fit": 0.9, "homepage_quote": "We operate a restaurant with daily staff scheduling."}
    for bad in (valid | {"icp_fit": 1.1}, valid | {"icp_fit": "0.9"}, valid | {"role_in_market": "restaurant"}, valid | {"extra": True}):
        with pytest.raises(ValidationError):
            quality.MarketRole.model_validate(bad)
    assert quality.grounded_quote("staff shortages", "We serve dinner every evening.") is None
    assert quality.grounded_quote("Staff changes are hard.", "Staff\nchanges are hard.") == "Staff\nchanges are hard."
    assert quality.grounded_quote("staff changes are hard.", "Staff changes are hard.") is None
    async def incomplete(payload, stage, context):
        return {"items": [{"id": 99, "relevance": 0.99}]}
    monkeypatch.setattr(journey, "_discovery_model", incomplete)
    assert asyncio.run(quality.select_passages([{"url": "https://example.org/", "text": valid["homepage_quote"]}], {}, {}, identity="test")) == []


def test_contact_evidence_is_only_its_sentence():
    raw = "<nav>Menu Drinks Specials</nav><p>We serve dinner. Email hello@restaurant.example for staffing enquiries. Book a table tonight.</p>"
    assert enrich.contact_excerpt(raw, "hello@restaurant.example") == "Email hello@restaurant.example for staffing enquiries."
    raw = "Phone +1 202 555 0146 for staffing enquiries. We recommend reservations ahead of time."
    assert enrich.contact_excerpt(raw, "+1 202 555 0146") == "Phone +1 202 555 0146 for staffing enquiries."
    assert enrich.contact_excerpt(NAV, "(202)-555-0146") == "(202)-555-0146"


def test_homepage_cache_is_icp_scoped_and_hallucinations_are_not_cached(monkeypatch):
    cache, calls = {}, []
    monkeypatch.setattr("app.gtm.email_learning.cached", cache.get)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: cache.update({key: value}))
    text = FIXTURES[0][4]
    async def model(payload, stage, context):
        calls.append(context["audience"])
        return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False,
            "homepage_quote": "Invented evidence about their staff shortages." if context["audience"] == "invented" else text}
    monkeypatch.setattr(journey, "_discovery_model", model)
    async def run():
        for audience in ("restaurant managers", "restaurant managers", "founders", "invented", "invented"):
            result = await quality.classify_home("https://restaurant.example/", text, {"audience": audience}, {"workspace_id": "workspace"})
            assert bool(result) == (audience != "invented")
    asyncio.run(run())
    assert calls == ["restaurant managers", "founders", "invented", "invented"] and len(cache) == 2


def test_domain_and_person_deduplication():
    rows = [{"source_url": url} for url in ("https://www.restaurant.example/", "https://restaurant.example/contact",
        "https://www.linkedin.com/in/alice/", "https://linkedin.com/in/ALICE", "https://x.com/alice", "https://x.com/bob")]
    assert quality.dedupe(rows) == [rows[index] for index in (0, 2, 4, 5)]
    forum = [{"source_url": f"https://forum.example/threads/{index}", "author": "Alice",
        "routes": [{"profile": True, "url": "https://forum.example/members/alice"}]} for index in range(2)]
    assert quality.dedupe(forum) == forum[:1]


@pytest.mark.parametrize("domain,company", [
    ("greenhouse.example", "Commercial greenhouse operations"),
    ("ceramics.example", "Weekend ceramic painting classes"),
])
@pytest.mark.parametrize("published_organisation_url", [True, False])
def test_local_contacts_dedupe_by_published_organisation(monkeypatch, domain, company, published_organisation_url):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    people = [{"kind": "person", "name": name, "company": company,
        "business_url": f"https://{domain}/", "source_url": f"https://{domain}/team/{name.split()[0]}"}
        for name in ("Jordan Ellis", "Morgan Blake")]
    if not published_organisation_url:
        for person in people:
            person.pop("business_url")
    other = {"kind": "person", "name": "Taylor Avery", "company": "Another organisation",
        "business_url": "https://another.example/", "source_url": "https://another.example/team"}
    assert quality.dedupe(people + [other]) == [people[0], other]
    named = [person | {"role_match": "exact", "source_url": f"https://{domain}/team"} for person in people]
    duplicate = named[0] | {"source_url": f"https://{domain}/leadership/jordan"}
    assert quality.dedupe(named + [duplicate, other]) == named + [other]
    forum = [{"kind": "person", "author": name, "source_url": f"https://forum.example/members/{name}"}
        for name in ("potter", "gardener")]
    assert quality.dedupe(forum) == forum
    speakers = [{"kind": "person", "name": name, "company": company + str(index),
        "source_url": f"https://conference.example/speakers/{index}"}
        for index, name in enumerate(("Jordan Ellis", "Morgan Blake"))]
    assert quality.dedupe(speakers) == speakers


def test_main_content_preserves_inline_and_short_need_sentences():
    raw = "<header>Restaurant Navigation</header><main><p>We struggle with <strong>staff scheduling</strong> every single day.</p><p>Shift cover is hard.</p></main>"
    assert quality.main_text(raw) == "We struggle with staff scheduling every single day.\nShift cover is hard."
    assert [page["text"] for page in quality.passages([{"url": "https://restaurant.example/", "text": raw}])] == [
        "We struggle with staff scheduling every single day.", "Shift cover is hard."]
    assert quality.main_text("<nav>Book Table Skip to main content</nav>") == ""


@pytest.mark.parametrize("mode", ["B2B", "B2C"])
def test_search_snippets_only_join_consumer_thread_scoring(monkeypatch, mode):
    from tests.gtm_fixtures import NAPNOTES_NEED, NAPNOTES_URL
    page = "Baby naps and bedtime."
    hit = {"url": NAPNOTES_URL, "snippet": NAPNOTES_NEED}
    scored = []
    async def model(payload, stage, context):
        scored.extend(context["candidates"])
        return {"items": [{"id": row["id"], "relevance": 0.9 if row["text"] == NAPNOTES_NEED else 0.0} for row in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    pages = quality.signal_pages(hit, page, {"mode": mode})
    selected = asyncio.run(quality.select_passages(pages, {"mode": mode}, {}, identity=NAPNOTES_URL))
    if mode == "B2B":
        assert pages == [{"url": NAPNOTES_URL, "text": page}]
        assert selected == [] and scored == [{"id": 0, "url": NAPNOTES_URL, "text": page}]
    else:
        assert selected == [{"url": NAPNOTES_URL, "text": NAPNOTES_NEED, "source": "search_snippet", "relevance": 0.9}]
        assert quality.grounded_quote(NAPNOTES_NEED, page) is None
        assert quality.grounded_quote("An invented parental need", hit["snippet"]) is None
    assert quality.signal_pages(hit | {"url": "https://baby.example/blog"}, page, {"mode": mode}) == [{"url": "https://baby.example/blog", "text": page}]


def test_snippet_candidate_survives_page_sentence_cap():
    from tests.gtm_fixtures import NAPNOTES_NEED, NAPNOTES_URL
    pages = quality.signal_pages({"url": NAPNOTES_URL, "snippet": NAPNOTES_NEED},
        "\n".join(f"Unrelated page sentence number {index}." for index in range(100)), {"mode": "B2C"})
    choices = quality.passages(pages)
    assert len(choices) == 80 and choices[0]["text"] == NAPNOTES_NEED
    assert choices[0]["source"] == "search_snippet" and all(row["url"] == NAPNOTES_URL for row in choices)


@pytest.mark.parametrize("mode", ["B2B", "B2C"])
def test_long_page_prerank_keeps_late_needs_in_order_and_reserves_snippet(mode):
    from tests.gtm_fixtures import NAPNOTES_AUDIENCE, NAPNOTES_NEED, NAPNOTES_PROBLEM
    context = CONTEXTS["ShiftSwap QA"] if mode == "B2B" else {
        "mode": mode, "audience": NAPNOTES_AUDIENCE, "problem": NAPNOTES_PROBLEM}
    need = FIXTURES[0][4] if mode == "B2B" else NAPNOTES_NEED
    sentences = [f"Unrelated discussion number {index}." for index in range(130)]
    sentences += [need, "Today " + need]
    snippet = "Browse the latest community discussions."
    url = "https://community.example/thread"
    pages = [{"url": url, "text": "\n".join(sentences)},
        {"url": url, "text": snippet, "source": "search_snippet"}]
    choices = quality.passages(pages, context)
    assert [row["text"] for row in choices] == sentences[:77] + sentences[130:] + [snippet]
    assert [row["id"] for row in choices] == list(range(80))
    assert choices[-1]["source"] == "search_snippet"
    assert all(any(quality.grounded_quote(row["text"], page["text"]) for page in pages) for row in choices)


@pytest.mark.parametrize("mode", ["B2B", "B2C"])
def test_short_page_prerank_leaves_passages_unchanged(mode):
    url = "https://community.example/thread"
    text = "Unrelated discussion comes first. We struggle with last-minute staff shift changes."
    snippet = "Browse the latest community discussions."
    pages = [{"url": url, "text": text}, {"url": url, "text": snippet, "source": "search_snippet"}]
    assert quality.passages(pages, CONTEXTS["ShiftSwap QA"] | {"mode": mode}) == [
        {"id": 0, "url": url, "text": "Unrelated discussion comes first."},
        {"id": 1, "url": url, "text": "We struggle with last-minute staff shift changes."},
        {"id": 2, "url": url, "text": snippet, "source": "search_snippet"}]


@pytest.mark.parametrize("mode", ["B2B", "B2C"])
def test_first_person_prerank_bonus_is_consumer_only(mode):
    sentences = [f"Parents discuss nap facts in article number {index}." for index in range(130)]
    need = "I need help with my next nap as a parent."
    choices = quality.passages([{"url": "https://community.example/thread", "text": "\n".join(sentences + [need])}],
        {"mode": mode, "audience": "parents", "problem": "nap timing"})
    assert len(choices) == 80
    assert (need in [row["text"] for row in choices]) == (mode == "B2C")


@pytest.mark.parametrize("text", [REDDIT_BLOCK_PAGE, "Your request has been blocked due to a network policy.",
    "<html><head><title>Just a moment...</title></head><body><script>challenge()</script></body></html>",
    "Attention Required! | Cloudflare", "Checking your browser before accessing the forum.", "Access denied",
    "Enable JavaScript and cookies to continue", "Please verify you are a human", "Log in to continue", "Sign in to continue",
    "401", "HTTP/1.1 403 Forbidden", "HTTP Error 429: Too Many Requests",
    "Status code: 401 Unauthorized", "403 Forbidden. You do not have permission to access this page."])
def test_blocked_pages_never_reach_quality_models(monkeypatch, text):
    async def forbidden(*args, **kwargs):
        pytest.fail("Blocked pages must not reach the model")
    monkeypatch.setattr(journey, "_discovery_model", forbidden)
    assert quality.blocked_page(text)
    assert quality.main_text(text) == ""
    payload = {}
    url = "https://restaurant.example/"
    assert asyncio.run(quality.classify_home(url, text, CONTEXTS["ShiftSwap QA"], payload)) is None
    assert asyncio.run(quality.select_passages([{"url": url, "text": text}], {}, payload, identity=url)) == []
    assert payload["_trace_counts"]["seed:rejected:blocked_page"] == 1
    assert payload["_trace_counts"]["seed:dropped:blocked_page"] == 1


@pytest.mark.parametrize("text", ["I struggle with the login for my baby sleep tracker and need help timing naps.",
    "I tried to log in to continue tracking naps, but I still need help with bedtime.",
    "We see 403 errors when parents try to log in to continue tracking naps.",
    "We run a restaurant. Sign in to continue booking a table."])
def test_genuine_posts_with_login_mentions_are_not_blocked(text):
    assert not quality.blocked_page(text)
    assert quality.main_text(text) == text


@pytest.mark.parametrize("need,solicitation,keep", [(True, False, True), (False, False, False), (True, True, False)])
def test_enrichment_quality_and_compliance_pipeline(monkeypatch, need, solicitation, keep):
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda *args: None)
    text = FIXTURES[0][4] if need else FIXTURES[3][4]
    home = "https://restaurant.example/"
    reads = []
    async def main_scrape(url):
        reads.append(("main", url))
        return f"<nav>{NAV}</nav><p>{text}</p>"
    async def scrape(url):
        reads.append(("contact", url))
        return f"<p>{text}</p><p>Email hello@restaurant.example for staffing enquiries.</p>" + (
            "<footer>No solicitation.</footer>" if solicitation else "")
    async def model(payload, stage, context):
        if stage == "marketrole":
            assert reads == [("main", home)]
            return {"role_in_market": "buyer" if need else "unknown", "icp_fit": 0.9 if need else 0.0,
                "anti_icp": False, "homepage_quote": text}
        return {"items": [{"id": item["id"], "relevance": 0.9 if need and "shift changes" in item["text"] else 0.1}
            for item in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    facts = asyncio.run(enrich.enrich_site(None, home, "workspace", "venture", scrape=scrape, main_scrape=main_scrape,
        context=CONTEXTS["ShiftSwap QA"], payload={"workspace_id": "workspace"}, published_mailboxes=True))
    if not need:
        assert facts == {"quality_rejected": True}
    else:
        assert facts["evidence_pages"] and all(page["text"] == text for page in facts["evidence_pages"])
        assert bool(facts["contacts"]) == keep
        if solicitation:
            assert facts["no_solicitation"] and not facts["email"] and not facts["phone"]
        else:
            assert facts["contacts"][0]["display_quote"] == "Email hello@restaurant.example for staffing enquiries."


@pytest.mark.parametrize("failed_stage,failure", [(None, None), ("marketrole", "error"),
    ("marketrole", "invalid"), ("passages", "error"), ("passages", "invalid"),
    ("marketrole", "metered"), ("passages", "metered")])
def test_discovery_rejects_vendor_and_bad_evidence_before_drafting(journey_db, monkeypatch, failed_stage, failure):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_journey_signals import _leased_payload
    text = FIXTURES[0][4]
    home = "https://restaurant.example/"
    stages, reads = [], []
    async def model(payload, stage, context):
        stages.append(stage)
        if stage == failed_stage and "broken.example" in context["_domain"]:
            if failure == "metered":
                raise journey.FatalError("discovery_outcome_uncertain")
            if failure == "error":
                raise RuntimeError("synthetic model failure")
            return {"role_in_market": "buyer", "icp_fit": "invalid"} if stage == "marketrole" else {
                "items": [{"id": context["candidates"][0]["id"], "relevance": 1.1}]}
        if stage == "marketrole":
            vendor = "vendor.example" in context["candidates"][0]["url"]
            return {"role_in_market": "vendor/competitor" if vendor else "buyer", "anti_icp": vendor,
                "icp_fit": 0.0 if vendor else 0.95, "homepage_quote": context["candidates"][0]["text"]}
        if stage == "passages":
            return {"items": [{"id": item["id"], "relevance": 0.95 if item["text"] == text else 0.0} for item in context["candidates"]]}
        if stage == "reachjudge":
            row = context["candidates"][0]
            return {"items": [{"id": row["id"], "kind": "business", "quote": text, "name": "restaurant", "affiliation": "",
                "url": home, "fit": 95, "why": "Manages last-minute staff shift coverage.", "value_exchange": "Discuss staff shift coverage."}]}
        assert stage == "reachdraft" and len(context["candidates"]) == 1
        return {"items": [{"id": context["candidates"][0]["id"],
            "body": "Hi restaurant team, could we discuss shift coverage in a 15-minute call? Reply with a time.",
            "connection_note": "Hi restaurant team, open to a 15-minute call about shift coverage? Reply with a time."}]}
    class Provider:
        async def scrape(self, url, main_only=True):
            reads.append((url, main_only))
            if "vendor.example" in url:
                assert main_only
                return FIXTURES[1][4]
            if "generic.example" in url:
                return FIXTURES[3][4] + "\nEmail hello@generic.example for enquiries."
            return text if main_only else text + "\nEmail hello@restaurant.example for staffing enquiries."
    async def no_registry(*args):
        return []
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable.PublicPages, "scrape", lambda *args, **kwargs: no_registry())
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    journey.update_icp(journey_db["wid"], journey_db["vid"], CONTEXTS["ShiftSwap QA"]["audience"])
    payload = _leased_payload(journey_db)
    hits = [{"kind": "business", "url": url, "title": "restaurant", "snippet": "misleading search snippet"}
        for url in (("https://broken.example/",) if failed_stage else ()) + (home, "https://vendor.example/", "https://generic.example/")]
    leads = asyncio.run(reachable.discover(Provider(), hits, [], CONTEXTS["ShiftSwap QA"] | {"competitors": [], "offer": ""}, payload))
    assert len(leads) == 1 and leads[0]["quote"] == text and leads[0]["evidence_relevance"] == 0.95
    assert leads[0]["market_role"]["role_in_market"] == "buyer"
    assert stages.count("reachdraft") == 1 and not any("vendor.example" in url and not main for url, main in reads)
    if failed_stage:
        assert any(url == "https://broken.example/" for url, _ in reads)
        assert leads[0]["source_url"] == home


@pytest.mark.parametrize("stage", ["marketrole", "passages"])
@pytest.mark.parametrize("error", ["fatal", "retryable", "cancelled"])
def test_quality_preserves_authority_retry_and_cancellation(monkeypatch, stage, error):
    from app.core.jobs import FatalError, RetryableError
    exception = {"fatal": FatalError, "retryable": RetryableError, "cancelled": asyncio.CancelledError}[error]
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    async def model(*args):
        raise exception("gtm_prepare_authority_revoked")
    monkeypatch.setattr(journey, "_discovery_model", model)
    home, text = "https://restaurant.example/", FIXTURES[0][4]
    with pytest.raises(exception):
        asyncio.run(quality.classify_home(home, text, CONTEXTS["ShiftSwap QA"], {"workspace_id": "workspace"})
            if stage == "marketrole" else quality.select_passages([{"url": home, "text": text}],
                CONTEXTS["ShiftSwap QA"], {}, identity=home))
