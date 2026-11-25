"""Offline prior-art acceptance. Provider facts are explicit synthetic fixtures."""
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.security.test_connector_oauth_boundary import client as client  # noqa: F401
from tests.security.test_research_boundary import confirmed, dossier, job
from tests.security.test_durable_journey_boundary import run_queued


@pytest.fixture
def modules(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research.prior_art import entities, plan, runner
    return entities, plan, runner


def source(sid="one", *, url="https://acme.com", name="Acme", kind="yc_company", status="live", date="2021-01-01", quote="Coffee roasting inventory software.", **meta):
    return {"id": sid, "url": url, "title": name, "summary": quote,
            "evidence": {"evidence_kind": kind, "prior_art": {"company_name": name, "company_url": url,
                         "source_kind": kind, "status": status, "date": date, **meta}}}


def test_plan_uses_short_vendor_terms_and_source_seed_hints(modules):
    _, plan, _ = modules
    selected = plan.parse_plan(json.dumps({"category_terms": ["coffee roasting inventory"], "job_phrases": ["green bean stock"],
        "customer_terms": ["coffee roasters"], "seed_competitors": [{"name": "MissingCo", "domain": "missing.example"}],
        "negative_terms": [], "consumer_mobile": False}))
    assert all(len(query) <= 256 and "coffee roasting inventory" in query for _, query in plan.queries(selected))
    assert "founder" not in " ".join(query for _, query in plan.queries(selected))


@pytest.mark.parametrize("categories", [
    ["dispatch coordination software", "delivery route planning", "fleet schedule management", "warehouse logistics"],
    ["cycling coordination apps", "bicycle route planning", "commuter trip tracking", "outdoor ride navigation"],
], ids=["business", "consumer"])
def test_prior_art_queries_keep_all_categories_without_extra_searches(modules, categories):
    _, plan, _ = modules
    selected = {"category_terms": categories, "job_phrases": [categories[0]], "seed_competitors": []}
    queries = plan.queries(selected)
    discovery = " ".join(query for key, query in queries if key in {"prior_category", "prior_names"})
    assert all(category in discovery for category in categories)
    assert [key for key, query in queries] == ["prior_category", "prior_names", "prior_failures", "prior_complaints"]
    assert all(len(query) <= 256 and query.count('"') % 2 == 0 for key, query in queries)


@pytest.mark.parametrize("category,negative,description", [
    ("delivery route planning", "payment processing software",
     "Delivery route planning for dispatch teams. Export payment receipts and share route plans. Integrates with your existing processing systems."),
    ("cycling route planning", "photo editing app",
     "Cycling route planning with photo memories and controls for editing ride notes."),
    ("delivery route planning", "customer communication software",
     "Delivery route planning includes driver communications and dispatch updates."),
    ("household cash flow planning", "business cash flow software",
     "Household cash flow planning helps people manage bills between paydays."),
], ids=["business", "consumer", "business_qualifier", "consumer_qualifier"])
def test_negative_categories_do_not_combine_unrelated_page_mentions(modules, category, negative, description):
    from app.research.source_quality import relevant_company_lead

    selected = {"category_terms": [category], "job_phrases": [category], "negative_terms": [negative]}
    item = {"url": "https://vendor.example/product", "title": category, "snippet": description}
    assert relevant_company_lead(selected, item)
    assert not relevant_company_lead(selected, {**item, "snippet": f"This product is {negative} for {category}."})


def test_roundup_link_names_are_hints_only(modules):
    entities, _, runner = modules
    names = runner.roundup_names("Best coffee software: [Acme](https://acme.com) [Private](http://127.0.0.1)")
    assert names == [{"name": "Acme", "url": "https://acme.com"}]
    roundup = source(kind="name_source")
    assert entities.compile_prior_art([roundup], None)["companies"] == []
    from app.research.service import dossier_evidence_dimensions
    from app.research.verdict import qualified_count
    findings = {"source": [{"id": "names", "url": "https://roundup.example", "summary": "Plans cost $12 per month.", "evidence_kind": "name_source"}]}
    assert qualified_count(findings) == 0
    assert dossier_evidence_dimensions(findings, None)["pricing"]["status"] == "unknown"


@pytest.mark.parametrize("publisher,vendor,name", [
    ("dispatch-review.example", "parcel-router.example", "Parcel Router"),
    ("cycling-review.example", "ride-navigator.example", "Ride Navigator"),
], ids=["business", "consumer"])
def test_roundup_authors_do_not_use_up_company_discovery_slots(modules, publisher, vendor, name):
    _, _, runner = modules
    text = " ".join(f"[Author {index}](https://www.{publisher}/authors/{index})" for index in range(8))
    text += " [Social profile](https://www.linkedin.com/company/reviewer)"
    text += f" [{name}](https://{vendor})"
    assert runner.roundup_names(text, source_url=f"https://{publisher}/best-tools") == [
        {"name": name, "url": f"https://{vendor}"}]


@pytest.mark.parametrize("category,problem", [
    ("delivery route planning", "Dispatch teams plan delivery routes and schedules."),
    ("cycling route planning", "Cyclists plan cycling routes and weekend trips."),
], ids=["business", "consumer"])
@pytest.mark.parametrize("origin", ["roundup", "seed"])
def test_linked_company_followup_reads_original_url_without_rediscovery(modules, monkeypatch, category, problem, origin):
    from app.research import deep

    _, _, runner = modules
    roundup = "https://review.example/tools"
    vendor = "https://vendor.example"
    provider = SimpleNamespace(name="duckduckgo")
    reads, saved, verified = [], [], []

    def scrape(did, payload, scraper, source):
        reads.append(source["url"])
        return {"text": f"[Visit site]({vendor})" if source["url"] == roundup else problem,
            "title": "Published page title", "url": source["url"], "provider": "duckduckgo",
            "observed_at": "2026-11-15T00:00:00+00:00"}

    def search(*args, **kwargs):
        pytest.fail("An original linked URL must not depend on another search result.")

    monkeypatch.setattr(deep, "_scrape", scrape)
    monkeypatch.setattr(deep, "_search", search)
    monkeypatch.setattr(deep, "_store_sources", lambda did, payload, result, query, intent: saved.extend(result["items"]))
    monkeypatch.setattr(deep, "_store_page", lambda did, payload, sid, result: verified.append(result))
    selected = {"category_terms": [category], "job_phrases": [problem]}
    if origin == "seed":
        selected["seed_competitors"] = [{"name": "Unverified model hint", "domain": "vendor.example"}]
    gaps = runner.name_followups("dossier", {}, selected, [{"url": roundup}] if origin == "roundup" else [], provider, provider, problem)
    assert not gaps and reads == ([roundup, vendor] if origin == "roundup" else [vendor])
    assert len(saved) == 1 and saved[0]["url"] == vendor
    assert saved[0]["title"] == "Published page title" and saved[0]["snippet"] == problem
    assert len(verified) == 1 and verified[0]["url"] == vendor and verified[0]["text"] == problem


def test_missing_model_seed_never_becomes_company(modules):
    entities, _, _ = modules
    result = entities.compile_prior_art([], None, plan={"seed_competitors": [{"name": "MissingCo", "domain": "missing.example"}]})
    assert result["companies"] == []


@pytest.mark.parametrize("category,description", [
    ("delivery route planning", "Dispatch teams plan delivery routes and schedules."),
    ("cycling route planning", "Cyclists plan cycling routes and weekend trips."),
], ids=["business", "consumer"])
def test_failed_linked_candidates_do_not_use_up_verified_company_slots(modules, monkeypatch, category, description):
    from app.research import deep

    _, _, runner = modules
    roundup = "https://review.example/tools"
    urls = [f"https://vendor{index}.example" for index in range(8)]
    reads, saved = [], []

    def scrape(did, payload, scraper, source):
        url = source["url"]
        reads.append(url)
        targets = [urls[0], *["https://www.vendor0.example/product"] * 8, *urls[1:]]
        text = " ".join(f"[Product {index}]({target})" for index, target in enumerate(targets)) if url == roundup else (
            "" if url in urls[:2] else "A photo editing app for photographers." if url == urls[2] else description)
        return {"text": text, "url": url, "title": "Public product page", "provider": "duckduckgo",
            "observed_at": "2026-11-15T00:00:00+00:00"}

    monkeypatch.setattr(deep, "_scrape", scrape)
    monkeypatch.setattr(deep, "_store_sources", lambda did, payload, result, query, intent: saved.extend(result["items"]))
    monkeypatch.setattr(deep, "_store_page", lambda *args: None)
    selected = {"category_terms": [category], "job_phrases": [description], "negative_terms": ["photo editing app"]}
    provider = SimpleNamespace(name="duckduckgo")
    runner.name_followups("dossier", {}, selected, [{"url": roundup}], provider, provider, description)
    assert [item["url"] for item in saved] == urls[3:6]
    assert reads == [roundup, *urls[:6]]


@pytest.mark.parametrize("url,description", [
    ("https://dispatch.example/product", "Dispatch teams plan delivery routes."),
    ("https://cycling.example/product", "Cyclists plan weekend cycling routes."),
], ids=["business", "consumer"])
@pytest.mark.parametrize("canonical", ["public", "private"])
def test_scrape_keeps_actual_page_metadata_without_private_source_urls(modules, monkeypatch, url, description, canonical):
    from app.research import deep
    from app.signals.research import ScrapedText

    async def scrape(target):
        assert target == url
        final_url = url if canonical == "public" else "http://127.0.0.1/private"
        return ScrapedText(description, metadata={"title": "Actual published title",
            "url": final_url, "sourceURL": final_url}, usage={})

    monkeypatch.setattr(deep, "_retrieve", lambda *args, **kwargs: kwargs["callback"]())
    result = deep._scrape("dossier", {}, SimpleNamespace(name="duckduckgo", scrape=scrape), {"id": "source", "url": url})
    assert result["text"] == description and result["title"] == "Actual published title"
    assert result.get("url") == (url if canonical == "public" else None)


@pytest.mark.parametrize("description", [
    "Delivery route planning software helps dispatch teams schedule deliveries.",
    "Cycling route planning software helps cyclists plan weekend trips.",
], ids=["business", "consumer"])
def test_research_scrape_extracts_html_before_the_saved_page_limit(modules, monkeypatch, description):
    from app.research import deep
    from app.signals.research import ScrapedText

    async def scrape(url):
        return ScrapedText('<html><head><style>' + 'a{color:red}' * 1000 +
            f'</style></head><main><p>{description}</p><a href="/product">Published product</a></main></html>',
            metadata={"title": "Actual published title", "sourceURL": url}, usage={})

    monkeypatch.setattr(deep, "_retrieve", lambda *args, **kwargs: kwargs["callback"]())
    result = deep._scrape("dossier", {}, SimpleNamespace(name="duckduckgo", scrape=scrape),
        {"id": "source", "url": "https://vendor.example"})
    assert description in result["text"] and '<style>' not in result["text"]
    assert "[Published product](https://vendor.example/product)" in result["text"]
    assert len(result["text"]) <= deep.PAGE_MAX_CHARS


def test_product_description_drops_scraped_navigation_but_keeps_custody(modules):
    entities, _, _ = modules
    raw = '[Skip to main content](https://beanbox.com/#main-content) [**Free bag when you join today! ►**](https://beanbox.com/deals) 4.5 ★from 2,000+ reviews\n![Beanbox Curator](https://assets.example/image.jpg)\n[Coffee subscriptions](https://beanbox.com/coffee) from independent roasters. Fresh beans delivered monthly. Third sentence.'
    row = source(quote=raw)
    built = entities.compile_prior_art([row], None)["companies"][0]["what_they_built"]
    assert built["quote"] == entities.clean_excerpt(raw)
    assert built["description"] == "Coffee subscriptions from independent roasters. Fresh beans delivered monthly."
    assert len(entities.product_description("longword " * 100)) <= 280
    assert entities.product_description('[Home](https://example.com)\n![logo](https://example.com/a.png)') == ""


def test_product_description_prefers_saved_summary_over_scrape(modules):
    entities, _, _ = modules
    raw = "[Skip to main content](https://example.com/#main) [About us](https://example.com/about) Coffee subscriptions."
    row = source(quote="Coffee subscriptions curated from independent roasters.")
    row["evidence"]["additional_excerpts"] = [{"text": raw}]
    review = {"competitor_checks": [{"source_id": row["id"], "product_quote": raw}]}
    built = entities.compile_prior_art([row], review)["companies"][0]["what_they_built"]
    assert built["quote"] == entities.plain_text(raw)
    assert built["description"] == row["summary"]


def test_domain_merge_and_hosted_repo_identity(modules):
    entities, _, _ = modules
    assert entities.entity_key("https://www.acme.co.uk/pricing") == "acme.co.uk"
    assert entities.entity_key("https://github.com/a/x") != entities.entity_key("https://github.com/b/y")
    assert entities.entity_key("https://alpha.github.io") != entities.entity_key("https://beta.github.io")
    assert entities.entity_key("https://alpha.supymem.com") != entities.entity_key("https://beta.supymem.com")
    assert entities.entity_key("https://medium.com/@acme/post") is None
    sources = [source("one"), source("two", url="https://www.acme.com/pricing", kind="web"),
               source("three", url="https://news.ycombinator.com/item?id=1", company_url="https://acme.com", kind="hn_item"),
               source("four", url="https://github.com/a/x", kind="repository_metadata", homepage="https://acme.com")]
    result = entities.compile_prior_art(sources, None)
    assert len(result["companies"]) == 1
    assert len(result["companies"][0]["finding_ids"]) == 4


def test_publishers_and_empty_products_are_not_prior_art_but_remain_sources(modules):
    entities, _, _ = modules
    for host in ("cnn.com", "edition.cnn.com", "bbc.co.uk", "en.wikipedia.org", "blogspot.com"):
        row = source(url="https://" + host, quote="University swimming progress software.")
        assert entities.entity_key(row["url"]) is None
        assert entities.compile_prior_art([row], None)["companies"] == []
        assert row["summary"] == "University swimming progress software."
    unknown = source(kind="web")
    assert entities.compile_prior_art([unknown], None)["companies"] == []
    assert entities.compile_prior_art([unknown], None, include_unconfirmed=True)["companies"]
    empty = source(quote="![logo](https://assets.example/logo.png)")
    assert entities.compile_prior_art([empty], None)["companies"] == []
    editorial = source(kind="web", url="https://cnn.com/story", company_url="https://acme.com")
    review = {"competitor_checks": [{"source_id": "one", "product_quote": editorial["summary"]}]}
    assert entities.compile_prior_art([editorial], review)["companies"][0]["key"] == "acme.com"


def test_all_prior_art_quotes_are_plain_text_and_custody_is_checked_first(modules):
    entities, _, _ = modules
    product = "**Coffee inventory** tracks roasting."
    price = "- [go pro](https://meetings.hubspot.com/acme) - **Pro plan $29 per month**."
    positioning = "## [Built for roasters](https://acme.com)"
    row = source(quote=product)
    row["evidence"]["additional_excerpts"] = [{"text": price}, {"text": positioning}]
    complaint = source("two", kind="hn_comment", quote="- Import is **broken**. ![screenshot](https://acme.com/image.png)")
    review = {"competitor_checks": [{"source_id": "one", "product_quote": product,
               "pricing_quote": price, "positioning_quote": positioning}],
              "observations": [{"source_id": "two", "quote": complaint["summary"]}],
              "assessments": [{"assessment": "Test [imports](https://acme.com) **first**.", "source_ids": ["two"]}]}
    result = entities.compile_prior_art([row, complaint], review)
    company = result["companies"][0]
    assert company["what_they_built"]["quote"] == "Coffee inventory tracks roasting."
    assert company["pricing_now"][0]["quote"] == "go pro Pro plan $29 per month."
    assert company["positioning"]["quote"] == "Built for roasters"
    assert company["complaints"][0]["quote"] == "Import is broken."
    assert result["lessons"][0]["text"] == "Test imports first."
    assert result["lessons"][0]["citations"][0]["quote"] == "Import is broken."
    review["competitor_checks"][0]["pricing_quote"] = "Invented price: $99 per month."
    assert entities.compile_prior_art([row], review)["companies"][0]["pricing_now"] == []


def test_regional_domains_never_merge_company_facts_or_patterns(modules):
    entities, _, _ = modules
    for first, second in (("payfast.co.za", "yoco.co.za"), ("alpha.com.mx", "beta.com.mx"),
                          ("alpha.co.ke", "beta.co.ke"), ("alpha.example.com", "beta.example.com")):
        assert entities.entity_key("https://" + first) != entities.entity_key("https://" + second)
    assert entities.entity_key("https://www.payfast.co.za/pricing") == entities.entity_key("https://payfast.co.za/blog")
    sources = [source("first", name="PayFast", url="https://payfast.co.za", status="inactive", quote="PayFast order software."),
               source("second", name="Yoco", url="https://yoco.co.za", status="live", quote="Yoco inventory software.")]
    review = {"observations": [{"source_id": s["id"], "quote": s["summary"]} for s in sources],
              "assessments": [{"assessment": "Compare two public attempts.", "source_ids": ["first", "second"]}]}
    prior = entities.compile_prior_art(sources, review)
    assert {c["key"]: (c["name"], c["status"], c["finding_ids"]) for c in prior["companies"]} == {
        "payfast.co.za": ("PayFast", "inactive", ["first"]), "yoco.co.za": ("Yoco", "live", ["second"])}
    assert prior["lessons"][0]["kind"] == "pattern" and len(prior["lessons"][0]["company_keys"]) == 2


def test_status_requires_typed_current_evidence(modules):
    entities, _, _ = modules
    web = source(kind="web", status="dead")
    review = {"competitor_checks": [{"source_id": "one", "product_quote": web["summary"]}]}
    result = entities.compile_prior_art([web], review)
    assert result["companies"][0]["status"] == "unknown"
    result = entities.compile_prior_art([source(status="inactive")], None)
    assert result["companies"][0]["status"] == "inactive" and result["companies"][0]["why_failed"] is None
    assert entities.compile_prior_art([source(cache_stale=True)], None)["companies"][0]["status"] == "unknown"


def test_first_party_causal_closure_can_surface_without_yc_and_not_from_bug(modules):
    entities, _, _ = modules
    closed = source(kind="web", quote="We shut down because support costs were unsustainable.")
    closed["evidence"]["page_verified"] = True
    company = entities.compile_prior_art([closed], None)["companies"][0]
    assert company["status"] == "inactive" and company["why_failed"]["quote"] == closed["summary"]
    broken = source(kind="github_issue", status="inactive", quote="The build failed because the inventory API is broken.")
    assert entities.compile_prior_art([broken], None)["companies"][0]["why_failed"] is None


def test_praise_is_not_a_complaint(modules):
    entities, _, _ = modules
    happy = source(kind="hn_comment", quote="I love this coffee inventory app.")
    problem = source("two", kind="hn_comment", quote="Coffee inventory is broken and unreliable.")
    company = entities.compile_prior_art([happy, problem], None)["companies"][0]
    assert [entry["finding_id"] for entry in company["complaints"]] == ["two"]


def test_lessons_pattern_requires_two_cited_companies(modules):
    entities, _, _ = modules
    sources = [source(), source("two", name="Beta", url="https://beta.io")]
    review = {"observations": [{"source_id": s["id"], "quote": s["summary"]} for s in sources],
              "assessments": [{"assessment": "Compare inventory workflows.", "source_ids": ["one", "two"]},
                              {"assessment": "A single example.", "source_ids": ["one"]},
                              {"assessment": "Unsupported.", "source_ids": ["foreign"]}]}
    result = entities.compile_prior_art(sources, review)
    assert [lesson["kind"] for lesson in result["lessons"]] == ["pattern", "anecdote"]
    sources[1]["evidence"]["founder_flag"] = "exclude"
    remaining = entities.compile_prior_art(sources, review)["lessons"]
    assert [lesson["text"] for lesson in remaining] == ["A single example."]


def test_public_people_and_freshness(modules):
    entities, _, _ = modules
    result = entities.compile_prior_art([source(author="builder", author_url="https://github.com/builder")], None)
    assert result["people"][0]["role_claim_finding_id"] == "one"
    assert result["freshness"]["stale_companies"] == ["acme.com"]
    assert not entities.compile_prior_art([source(author="a@b.com", author_url="https://github.com/a")], None)["people"]
    assert not entities.compile_prior_art([source(author="builder", author_url="https://unknown.example/builder")], None)["people"]


def test_contact_filter_validates_date_spans_and_seed_domain_identity(modules):
    entities, _, _ = modules
    assert not entities._contains_contact('Observed API fields: {"source_date": "2021-01-01"}')
    assert entities._contains_contact("Call 9123-45-67")
    assert entities._contains_contact("Email builder@example.test")
    web = source(kind="web", quote="Slackline inventory integrates with Notion.")
    web["evidence"]["prior_art"].pop("company_name")
    result = entities.compile_prior_art([web], {"competitor_checks": [{"source_id": "one", "product_quote": web["summary"]}]}, plan={"seed_competitors": [
        {"name": "Slack", "domain": "acme.com"}, {"name": "Notion", "domain": "notion.so"}]})
    assert result["companies"][0]["name"] == "acme.com"


def test_public_api_logging_contains_no_query_text(modules):
    import logging
    from app.research.prior_art.adapters import _PublicAPILog
    record = logging.LogRecord("httpx", logging.INFO, "fixture", 1, 'HTTP Request: GET https://hn.algolia.com/api/v1/search?query=%s "200 OK"', ("private founder phrase",), None)
    assert _PublicAPILog().filter(record)
    assert record.getMessage() == "prior_art_public_api_request"


def test_prior_art_dimensions_are_leads_never_payment_or_market_proof(modules):
    entities, _, _ = modules
    from app.research.evidence_dimensions import evidence_dimensions
    src = source(quote="Coffee inventory is broken. Plans start at $12 per month.", points=200)
    prior = entities.compile_prior_art([src, source("dead", name="Old", url="https://old.io", status="inactive")], None)
    prior["companies"][0]["pricing_now"] = [{"finding_id": "one", "quote": "Plans start at $12 per month."}]
    dims = evidence_dimensions([{"id": s["id"], "url": s["url"], "snippet": s["summary"]} for s in [src, source("dead", url="https://old.io")]], [], prior_art=prior)
    assert all(dims[key]["status"] != "unknown" for key in ("competition", "pricing", "customer_demand", "risks"))
    assert dims["market_size"]["status"] == dims["willingness_to_pay"]["status"] == "unknown"


def test_cost_cap_stops_before_provider_call(modules):
    from app.research.execution import _validate_usage
    from app.core.jobs import FatalError
    doc = {"_runtime": {"cost_ceiling_micro": 2_000_000, "operations": {"old": {"state": "completed", "usage": {"estimate_micro": 1_900_000}}}}}
    with pytest.raises(FatalError, match="research_cost_budget_exhausted"):
        _validate_usage(None, doc, None, {"estimate_micro": 100_001})
    _validate_usage(None, doc, None, {"estimate_micro": 100_000})


def test_near_ceiling_receipt_cleanup_is_not_double_counted(modules):
    from app.research.execution import _validate_usage
    from app.core.jobs import FatalError
    saved = {"request_id": "run:review:1", "estimate_micro": 600_000, "actual_micro": 500_000, "purpose": "research.deep"}
    doc = {"_runtime": {"cost_ceiling_micro": 2_000_000, "raw_calls_reserved": 60, "operations": {
        "old": {"state": "completed", "usage": {"estimate_micro": 1_300_000}},
        "review": {"state": "responded", "usage": saved}}}}
    _validate_usage(None, doc, None, dict(saved))
    assert doc["_runtime"]["raw_calls_reserved"] == 60
    with pytest.raises(FatalError, match="research_call_budget_exhausted"):
        _validate_usage(None, doc, None, {"request_id": "run:new:1", "estimate_micro": 0, "purpose": "research.search"})


def test_raw_request_cap_counts_free_children_and_paid_retry_reservations(modules):
    from app.research.execution import _validate_usage
    from app.core.jobs import FatalError
    doc = {"_runtime": {"cost_ceiling_micro": 2_000_000, "operations": {}, "raw_calls_reserved": 53}}
    _validate_usage(None, doc, None, {"purpose": "research.prior_art.github-repos", "estimate_micro": 0})
    _validate_usage(None, doc, None, {"purpose": "research.search", "provider": "firecrawl", "estimate_micro": 10})
    assert doc["_runtime"]["raw_calls_reserved"] == 58
    _validate_usage(None, doc, None, {"purpose": "research.search", "provider": "firecrawl", "estimate_micro": 10})
    _validate_usage(None, doc, None, {"purpose": "research.search", "provider": "firecrawl", "estimate_micro": 10})
    with pytest.raises(FatalError, match="research_call_budget_exhausted"):
        _validate_usage(None, doc, None, {"purpose": "research.search", "provider": "firecrawl", "estimate_micro": 10})


def test_alternatives_and_comparison_pages_only_discover_names(modules):
    from app.research.source_quality import name_source, relevant_company_lead
    candidate = {"title": "Cropster alternatives for coffee inventory", "url": "https://aggregator.example/compare",
                 "snippet": "Compare coffee inventory and green bean management tools."}
    assert name_source(candidate)
    assert not relevant_company_lead({"category_terms": ["coffee inventory"], "job_phrases": ["green bean management"]}, candidate)


@pytest.mark.parametrize("bad", [None, {}, {"summary": {"prior_art": "bad"}, "findings": {}},
    {"summary": {"prior_art": {"lessons": [None, {"citations": [None], "text": "No citation"}]}}, "findings": {}}])
def test_completion_lesson_malformed_fallback_is_optional(modules, bad):
    from app.research.service import top_prior_art_lesson
    assert top_prior_art_lesson(bad) is None


def test_completion_message_preserves_original_actions_when_lesson_cannot_resolve(modules, monkeypatch):
    from app.chat import milestone_hooks
    from app.research import service
    from app.ventures import next_step
    from app.pagebuilder import api
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    posts = []
    body = {"summary": {"verdict": {"source_count": 1, "why": ["Existing public context."]},
            "prior_art": {"lessons": [{"text": "This is not valid.", "citations": [{"finding_id": "foreign"}]}]}}, "findings": {}}
    reads = []
    monkeypatch.setattr(service, "get_dossier", lambda did: (reads.append(did), body)[1])
    monkeypatch.setattr(next_step, "_facts", lambda vid: {"personal": False, "idea_confirmed": True})
    monkeypatch.setattr(api, "enabled", lambda: True)
    monkeypatch.setattr(milestone_hooks, "post_milestone", lambda *args, **kwargs: posts.append(kwargs))
    milestone_hooks.research_done({"_job_id": "job-fixture", "venture_id": "venture-fixture"})
    assert len(reads) == len(posts) == 1
    assert "This is not valid" not in posts[0]["body_md"]
    assert "Choose a landing page or customers first" in posts[0]["body_md"]
    assert len(posts[0]["actions"]) == 3
    assert posts[0]["key"].startswith("research_done:")


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_research_context_uses_owner_session_and_checks_scope(modules, monkeypatch, memory_enabled):
    import sys
    from types import ModuleType
    from app.research.context import capture
    from app.core.jobs import FatalError
    from app.config import settings
    # Founder memory capture is intentionally opt-in; the off path retains main's context.
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", memory_enabled)
    calls = []
    session = SimpleNamespace(get=lambda model, identity: SimpleNamespace(workspace_id="workspace"))
    assets, memory = ModuleType("app.intake.assets"), ModuleType("app.memory.context")
    def get_site(vid, *, session):
        calls.append(("site", vid, session))
        return {"founder_stage": {"stage": "page_live"}, "site": {"url": "https://acme.com", "status": "unverified"}}
    def context_for(actual_session, **kwargs):
        calls.append(("memory", kwargs["venture_id"], actual_session))
        return {"availability": "available", "effect_authority": False, "facts": []}
    assets.get_site, memory.context_for = get_site, context_for
    monkeypatch.setitem(sys.modules, "app.intake.assets", assets)
    monkeypatch.setitem(sys.modules, "app.memory.context", memory)
    result = capture(session, "venture", "workspace", {"problem": "inventory"})
    assert result["founder_stage"]["stage"] == "page_live"
    assert result["existing_site"]["status"] == "unverified"
    if memory_enabled:
        assert not result["institutional_context"]["effect_authority"]
    else:
        assert "institutional_context" not in result
    assert calls == [("site", "venture", session)] + ([("memory", "venture", session)] if memory_enabled else [])
    with pytest.raises(FatalError, match="research_scope_mismatch"):
        capture(session, "venture", "other_workspace", {})
    assert len(calls) == 1 + memory_enabled


@pytest.mark.parametrize("memory_enabled", [False, True])
def test_research_capture_never_binds_memory_scope_in_unbound_worker(modules, monkeypatch, memory_enabled):
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from types import ModuleType
    from app.config import settings
    from app.research.context import capture
    from app.tenancy import ScopeRequired, current_or_none
    assets = ModuleType("app.intake.assets")
    assets.get_site = lambda *args, **kwargs: {"founder_stage": {"stage": "concept"}, "site": None}
    monkeypatch.setitem(sys.modules, "app.intake.assets", assets)
    monkeypatch.setattr(settings, "company_os_memory_facts_enabled", memory_enabled)
    session = SimpleNamespace(get=lambda model, identity: SimpleNamespace(workspace_id="workspace"))
    def invoke():
        assert current_or_none() is None
        return capture(session, "venture", "workspace", {"problem": "inventory"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke)
        if memory_enabled:
            with pytest.raises(ScopeRequired):
                future.result()
        else:
            assert future.result() == {"founder_stage": {"stage": "concept"}, "existing_site": None}


@pytest.mark.parametrize("planner_outcome", ["valid", "invalid", "unknown"])
def test_normal_confirmed_research_exposes_prior_art_and_revoked_citations(client, monkeypatch, planner_outcome):
    """Actual signed Core/job/credit flow; synthetic provider transports only."""
    vid, _, _, jid = confirmed(client, monkeypatch)
    from tests.research_fixtures import install_research
    from tests.idea_fixtures import install_provider
    install_research(monkeypatch)
    monkeypatch.setenv("RESEARCH_PRIOR_ART_ENABLED", "1")
    from app.research.prior_art import adapters
    async def empty(*args, **kwargs):
        return {"items": [], "calls": 0, "gaps": [], "provider": "synthetic"}
    async def yc(*args, **kwargs):
        return {"items": [{"url": "https://www.ycombinator.com/companies/acme", "title": "Acme", "snippet": "Bakery order management software.",
                "provider": "yc_oss", "source_kind": "yc_company", "prior_art": {"company_name": "Acme", "company_url": "https://acme.example", "status": "inactive", "date": "2021-01-01"}}],
                "calls": 0, "gaps": ["Synthetic offline YC data."], "provider": "yc_oss"}
    for name in ("search_hn", "hn_comments", "search_github"):
        monkeypatch.setattr(adapters, name, empty)
    monkeypatch.setattr(adapters, "search_yc", yc)
    planner_calls = []
    def completion(messages, options):
        body = json.loads(messages[-1]["content"])
        planning = "sources" not in body
        if planning:
            planner_calls.append(options)
            assert options["purpose"] == "research.prior_art.plan"
            assert options["captured_config"].catalog_model == "gpt-6.1-sol"
            answer = {"category_terms": ["bakery order management"], "job_phrases": ["bakery order intake"], "customer_terms": [],
                      "seed_competitors": [], "negative_terms": [], "consumer_mobile": False}
        else:
            sid = next(s["id"] for s in body["sources"] if any("Bakery order management software." in quote for quote in s["excerpts"]))
            answer = {"overview": "An earlier attempt is publicly listed.", "observations": [{"source_id": sid, "quote_index": 0}],
                      "assessments": [{"kind": "inference", "category": "competitor_alternative", "citations": [{"source_id": sid, "quote_index": 0}], "assessment": "Compare this earlier order workflow."}],
                      "competitor_checks": [], "service_checks": [], "coverage_gaps": [], "proposed_tests": []}
        from openai.types import CompletionUsage
        return SimpleNamespace(model="gpt-6.1-sol" if planning else "DeepSeek-V4-Pro", service_tier="default",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="not JSON" if planning and planner_outcome == "invalid" else json.dumps(answer)))],
            usage=None if planning and planner_outcome == "unknown" else CompletionUsage(
                prompt_tokens=300, completion_tokens=200, total_tokens=500,
                prompt_tokens_details={"cached_tokens": 0}, completion_tokens_details={"reasoning_tokens": 0}))
    install_provider(monkeypatch, completion)
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({"research.prior_art.plan": {
        "model": "sol-deployment", "catalog_model": "gpt-6.1-sol", "reasoning_effort": "low"}}))
    run_queued("research.deep")
    from app.research import execution as ex
    receipt = ex.operation(ex.identifier(jid), "prior-art-plan")
    assert len(planner_calls) == 1 and receipt["usage"]["model_binding"]["deployment"] == "sol-deployment"
    if planner_outcome == "unknown":
        assert job(jid).status == "retry_wait" and job(jid).last_error == "provider_retryable:RetryableError"
        assert receipt["state"] == "provider_responded" and receipt["usage"]["model_receipt_error"] == "counts_unknown"
        assert not dossier(client, vid)["findings"]
        from app.db.models import Job
        from app.state import db
        with db()() as session:
            queued = session.get(Job, jid)
            queued.run_after, queued.scheduled_for = None, None
            session.commit()
        run_queued("research.deep")
        assert len(planner_calls) == 1
        return
    assert receipt["state"] == "completed" and receipt["usage"]["settled"] is True
    if planner_outcome == "invalid":
        assert receipt["response"] == {} and receipt["error_code"] == "research_invalid_output"
    assert job(jid).status == "succeeded", job(jid).last_error
    result = dossier(client, vid)
    company = next(c for c in result["summary"]["prior_art"]["companies"] if c["name"] == "Acme")
    assert company["status"] == "inactive" and result["summary"]["prior_art"]["lessons"]
    from app.research.service import set_finding_flag, get_dossier, export_markdown
    exported = export_markdown(vid, result["id"])["markdown"]
    assert "Who tried this before" in exported and "Acme" in exported
    for fid in company["finding_ids"]:
        set_finding_flag(vid, fid, "exclude")
    refreshed = get_dossier(result["id"])["summary"]["prior_art"]
    assert not any(c["name"] == "Acme" for c in refreshed["companies"])
    assert refreshed["lessons"] == []


def test_scrape_timeout_retains_protected_hold_and_stops_with_sources(client, monkeypatch):
    """Actual credit/job persistence; synthetic transport uncertainty only."""
    vid, _, _, jid = confirmed(client, monkeypatch)
    from tests.research_fixtures import install_research
    calls = install_research(monkeypatch)
    from app.research import deep
    scraped = []
    class Provider:
        name = "firecrawl"
        async def search(self, query):
            return [{"url": "https://acme.example/pricing", "title": "Bakery order software", "snippet": "Bakery order management software."}]
        async def scrape(self, url):
            scraped.append(url)
            raise TimeoutError("Synthetic request may have reached provider")
    monkeypatch.setattr(deep, "_web_provider", lambda: Provider())
    run_queued("research.deep")
    assert len(scraped) == 1 and job(jid).status == "failed_dead"
    assert job(jid).last_error == "research_outcome_uncertain"
    assert not any(kind == "review" for kind, _ in calls)
    from app.credits.accounts import get_account
    assert get_account(vid).reserved_micro > 0
    result = dossier(client, vid)
    assert result["state"] == "partial" and result["summary"]["review_kind"] == "retrieved_sources_only"
    assert result["summary"]["usage"]["has_unsettled"] is True


def test_prohibited_roundup_scrape_stops_before_reservation_or_provider(modules, monkeypatch):
    from app.research import deep
    from app.core.jobs import FatalError
    monkeypatch.setattr(deep, "perform", lambda *args, **kwargs: pytest.fail("prohibited host must not reserve or call"))
    for url in ("https://g2.com/best-software", "https://www.linkedin.com/company/acme", "http://127.0.0.1"):
        with pytest.raises(FatalError, match="research_provider_rejected"):
            deep._scrape("unused", {}, None, {"id": "unused", "url": url})
