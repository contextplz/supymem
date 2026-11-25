"""Website enrichment, the article filter and the country-confirmation flow. No network."""
from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_journey import _assert_credit_usage, journey_db  # noqa: F401  (fixture)
from tests.unit.gtm.test_reviewed_ports import safety_runtime as safety_runtime

load_company_os_app()
from app.gtm import enrich  # noqa: E402

HOME = """<html><body><h1>Acme Bakery</h1>
<a href="/about">About</a> <a href="/contact">Contact</a> <a href="https://other.example/contact">Partner</a>
<footer>&copy; 2026 Acme Bakery Ltd. All rights reserved.</footer></body></html>"""
CONTACT = """<h2>Contact us</h2>
<p>Acme Bakery Ltd, 120 Market Street, San Francisco, CA 94103</p>
<p>Write to <a href="mailto:hello@acmebakery.example?subject=Hi">us</a> or jane@personal-mail.example</p>
<p>Jane Doe, Founder</p><p>Sam Roe</p><p>Head of Sales</p>"""


def _use_metered_local_reader(monkeypatch):
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from app.research import local_providers
        update_settings("search", {"provider": "firecrawl", "firecrawl_url": "https://api.firecrawl.dev"},
            {"firecrawl_key": "synthetic"})
        monkeypatch.setattr(local_providers, "local_read", local_providers.fetch_page)
        monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
            (0, 0, 0, "", ("93.184.216.34", 0))])


@pytest.mark.parametrize("domain,description", [
    ("greenhouse.example", "Commercial greenhouse operations"),
    ("ceramics.example", "Weekend ceramic painting classes"),
])
def test_local_reader_follows_team_links_from_result_pages(monkeypatch, domain, description):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    home = f"https://{domain}/"
    pages = {home: f'<h1>{description}</h1><a href="/about">About</a>',
        home + "about": '<a href="/team">Our team</a>',
        home + "team": f'<p>{description}</p><a href="/contact">Contact our team</a>',
        home + "contact": f'<p>Contact: hello@{domain}</p>'}
    reads = []

    async def scrape(url):
        reads.append(url)
        return pages[url]

    for website in (home + "about", home.rstrip("/")):
        reads.clear()
        facts = asyncio.run(enrich.enrich_site(None, website, "workspace", "venture", scrape=scrape))
        assert reads == list(pages)
        assert facts["email"] == f"hello@{domain}"
        assert facts["email_page"] == home + "contact"
        assert not facts.get("guessed_routes")


def test_extract_publishes_only_what_pages_say():
    facts = enrich.extract("acmebakery.example", [("https://acmebakery.example/", HOME), ("https://acmebakery.example/contact", CONTACT)])
    assert facts["name"] == "Acme Bakery Ltd"
    assert facts["email"] == "hello@acmebakery.example" and facts["email_page"] == "https://acmebakery.example/contact"
    assert (facts["country"], facts["country_source"], facts["country_page"]) == ("US", "postal_address", "https://acmebakery.example/contact")
    assert [(p["name"], p["role"]) for p in facts["people"]] == [("Jane Doe", "Founder"), ("Sam Roe", "Head of Sales")]


def test_no_evidence_means_no_email_and_no_country():
    page = "<p>We serve Canada and the UK. Call +1 555 0100. Mail me at owner@personal-mail.example or press@other.example or noreply@acme.example</p>"
    facts = enrich.extract("acme.example", [("https://acme.example/", page)])
    assert facts["email"] is None and facts["country"] is None and facts["people"] == []


@pytest.mark.parametrize("text,expected", [
    ("Call +49 30 1234567", ("DE", "phone_prefix")),
    ("Unit 4, 10 High St\nLondon, United Kingdom 12", ("GB", "postal_address")),
    ("\n".join(["Products"] * 9 + ["Based in Ireland"]), ("IE", "footer")),
    ("We ship to customers in Canada and 40 other places every week.", None),
    ("123 Main St, Toronto M5V 2T6", ("CA", "postal_address")),
])
def test_country_records_which_evidence(text, expected):
    assert enrich._country(text) == expected


@pytest.mark.parametrize("url,title,kind", [
    ("https://acme.example/", "Acme - ordering", "company"),
    ("https://www.g2.com/categories/bakery", "Best bakery software", "directory"),
    ("https://acme.example/blog/10-tips", "Tips", "article"),
    ("https://acme.example/", "10 best bakery apps in 2026", "article"),
    ("https://acme.example/", "Acme alternatives and reviews", "article"),
    ("https://medium.com/@x/post", "Post", "directory"),
])
def test_article_and_directory_filter(url, title, kind):
    assert enrich.classify(url, title) == kind


def test_merge_sets_geo_only_from_supported_evidence_and_never_invents_email():
    seed = {"website": "https://acmebakery.example/", "organization": "acmebakery.example"}
    facts = enrich.extract("acmebakery.example", [("https://acmebakery.example/contact", "Acme Bakery Ltd\n+49 30 1234567\nJane Doe, CEO")])
    row = enrich.merge(seed, facts)
    assert row["country"] == "DE" and row["country_source"] == "phone_prefix" and "email" not in row and row["people"][0]["name"] == "Jane Doe"
    bare = enrich.merge(seed, enrich.extract("acmebakery.example", [("https://acmebakery.example/", "nothing here")]))
    assert "country" not in bare and "email" not in bare
    au = enrich.merge(seed, enrich.extract("acmebakery.example", [("https://acmebakery.example/", "1 Main St, Sydney 2000, Australia +61 2 0000")]))
    assert "country" not in au


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_enrich_site_is_page_capped_and_metered(journey_db, monkeypatch, domain):  # noqa: F811
    _use_metered_local_reader(monkeypatch)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    d, seen, charges = journey_db, [], []
    from app.credits import metered
    from app.core.local_mode import is_local
    real = metered.call

    async def spy(callback, **kw):
        charges.append(kw)
        return await real(callback, **kw)
    monkeypatch.setattr(metered, "call", spy)

    class Provider:
        name = "duckduckgo"
        async def scrape(self, url, main_only=True):
            seen.append((url, main_only))
            return {f"https://{domain}/": HOME}.get(url, "<p>Plain page</p>")

    if is_local():
        from app.research import local_providers
        monkeypatch.setattr(local_providers, "scrape_provider", lambda: Provider())
    facts = asyncio.run(enrich.enrich_site(Provider(), f"https://{domain}/products/x", d["wid"], d["vid"]))
    assert seen[0] == (f"https://{domain}/", False)
    assert len(seen) <= enrich.MAX_PAGES and all(not main for _, main in seen)
    assert "https://other.example/contact" not in [u for u, _ in seen]
    assert facts["email"] is None
    assert charges and all(c["provider"] == ("duckduckgo" if is_local() else "firecrawl")
        and c["estimate_micro"] == (0 if is_local() else 5000) and c["workspace_id"] == d["wid"]
        and c["venture_id"] == d["vid"] for c in charges) and len(charges) == len(seen)


def test_enrichment_is_flagged_and_failures_leave_candidates_alone(journey_db, monkeypatch):  # noqa: F811
    d = journey_db
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    assert asyncio.run(enrich.enrich_seeds([{"website": "https://a.example/"}], d["wid"], d["vid"])) == {}


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_unspent_slot_retries_resume_paid_pages_without_spending_twice(journey_db, monkeypatch, safety_runtime, domain):
    _use_metered_local_reader(monkeypatch)
    from app.signals import research
    from app.core.jobs import RetryableError
    from app.core.local_mode import is_local
    d, calls, progress, checkpoints = journey_db, [], {}, []
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    async def scrape(self, url, main_only=True):
        calls.append(url)
        if len(calls) == 2:
            raise research.FirecrawlLimiterUnavailable()
        return HOME if url.endswith("/") else CONTACT.replace("acmebakery.example", domain)
    monkeypatch.setattr(research.HttpResearchProvider, "scrape", scrape)
    seeds = [{"website": f"https://{domain}/"}]
    with pytest.raises(RetryableError, match="gtm_enrichment_slot_unavailable"):
        asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress, checkpoint=lambda: checkpoints.append(1)))
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0 if is_local() else 5000, reserved_micro=0)
    if is_local():
        from app.credits import local_meter
        spend = local_meter.snapshot(workspace_id=d["wid"], venture_id=d["vid"])
        assert spend["month_cost_usd_micro"] == 5000
        assert sum(task["unconfirmed_calls"] for task in spend["tasks"]) == 0
    assert progress["pages"][domain] == [(f"https://{domain}/", HOME)]
    result = asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress))
    assert result[domain]["email"] == f"hello@{domain}"
    assert calls.count(f"https://{domain}/") == 1 and calls[1] == calls[2]
    assert checkpoints
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0 if is_local() else 15000, reserved_micro=0)
    if is_local():
        spend = local_meter.snapshot(workspace_id=d["wid"], venture_id=d["vid"])
        assert spend["month_cost_usd_micro"] == 15000
        assert sum(task["unconfirmed_calls"] for task in spend["tasks"]) == 0


@pytest.mark.parametrize("domain", ["audit", "music"])
def test_enrichment_total_budget_defers_remaining_work_and_never_queues_a_batch_of_slots(journey_db, monkeypatch, domain):
    from app.core.jobs import RetryableError
    from app.core.local_mode import is_local
    d, started, progress = journey_db, [], {}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 0.03)
    monkeypatch.setattr(enrich, "COMPANY_TIMEOUT", 1)
    async def slow(provider, website, wid, vid, **kwargs):
        started.append(website)
        await asyncio.sleep(0.1)
        return {"people": [], "contacts": []}
    monkeypatch.setattr(enrich, "enrich_site", slow)
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    seeds = [{"website": f"https://{domain}{index}.example/"} for index in range(enrich.CONCURRENCY + 2)]
    if is_local():
        assert asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress)) == progress["done"]
    else:
        with pytest.raises(RetryableError, match="gtm_enrichment_deferred"):
            asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress))
    assert started == [seed["website"] for seed in seeds[:enrich.CONCURRENCY]]
    assert not progress["failed"]
    assert list(progress["done"]) == [enrich._host(seed["website"]) for seed in seeds[:enrich.CONCURRENCY]]
    assert not any(enrich._host(seed["website"]) in progress["pages"] for seed in seeds[enrich.CONCURRENCY:])


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_pacing_deadline_is_durable_retryable_unspent_and_never_starts_http(journey_db, monkeypatch, domain, safety_runtime):
    import httpx
    from app.core.jobs import RetryableError
    from app.signals import research
    from app.core.local_mode import is_local
    d, requests = journey_db, []
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(enrich, "COMPANY_TIMEOUT", 5)
    real_to_thread = asyncio.to_thread
    async def wait_for_slot(callback, *args, **kwargs):
        if callback is research._pace_firecrawl:
            await asyncio.Event().wait()
        return await real_to_thread(callback, *args, **kwargs)
    monkeypatch.setattr(research.asyncio, "to_thread", wait_for_slot)
    if is_local():
        from app.research import local_providers
        async def waiting_reader(url, *, main_only=True):
            await asyncio.Event().wait()
        monkeypatch.setattr(local_providers, "local_read", waiting_reader)
        monkeypatch.setattr(enrich, "TOTAL_TIMEOUT", 0.03)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: requests.append(request)), **kwargs))
    progress = {}
    if is_local():
        monkeypatch.setattr(enrich, "COMPANY_TIMEOUT", 0.03)
        assert asyncio.run(enrich.enrich_seeds([{"website": f"https://{domain}/"}], d["wid"], d["vid"], progress=progress)) == {}
        assert progress["failed"] == [domain]
        failure, = progress["read_failures"]
        assert (failure["url"], failure["reason"]) == (f"https://{domain}/", "timed out")
    else:
        with pytest.raises(RetryableError, match="gtm_enrichment_slot_unavailable"):
            asyncio.run(enrich.enrich_seeds([{"website": f"https://{domain}/"}], d["wid"], d["vid"]))
    assert requests == []
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=0)


@pytest.mark.parametrize("domain", ["wholesale.example", "gardening.example"])
def test_unknown_provider_timeout_keeps_hold_and_is_not_replayed(journey_db, monkeypatch, safety_runtime, domain):
    _use_metered_local_reader(monkeypatch)
    from app.core.local_mode import is_local
    from app.signals import research
    d, requests, progress = journey_db, [], {}
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    async def timeout(self, url, main_only=True):
        requests.append(url)
        raise TimeoutError("Provider outcome unknown")
    monkeypatch.setattr(research.HttpResearchProvider, "scrape", timeout)
    seeds = [{"website": f"https://{domain}/"}]
    assert asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress)) == {}
    assert asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"], progress=progress)) == {}
    assert requests == [f"https://{domain}/"]
    with d["factory"]() as session:
        _assert_credit_usage(d, session, spent_micro=0, reserved_micro=0 if is_local() else 5000)
    if is_local():
        from app.credits import local_meter
        spend = local_meter.snapshot(workspace_id=d["wid"], venture_id=d["vid"])
        assert spend["month_cost_usd_micro"] == 5000
        assert sum(task["unconfirmed_calls"] for task in spend["tasks"]) == 1
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")

    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(enrich, "enrich_site", boom)
    assert asyncio.run(enrich.enrich_seeds(seeds, d["wid"], d["vid"])) == {}


def _public_row(**extra):
    return {"website": "https://acmebakery.example/", "source_url": "https://acmebakery.example/", "organization": "acmebakery.example", "why": "Public search candidate."} | extra


def test_ingest_records_published_email_and_geo_only_when_enriched(journey_db):  # noqa: F811
    from app.gtm import journey, enrich as e
    d = journey_db
    facts = e.extract("acmebakery.example", [("https://acmebakery.example/", HOME), ("https://acmebakery.example/contact", CONTACT)])
    rows = e.merge_rows([_public_row()], {"acmebakery.example": facts})
    assert journey._ingest(d["wid"], d["vid"], rows) == 1
    prospect = journey._prospects(d["vid"])[0]
    assert prospect["email"] == "hello@acmebakery.example" and prospect["source_url"].endswith("/contact")
    assert prospect["country"] == "US" and prospect["country_source"] == "postal_address" and prospect["company"] == "Acme Bakery Ltd"
    from app.db.models import GtmContact
    with d["factory"]() as session:
        contact = session.query(GtmContact).one()
        # The column's CHECK allows safe/risky/guess/none; publication is recorded as evidence beside the exact page.
        assert contact.email_confidence == "risky" and '"email_evidence": "published"' in contact.enrichment_json


def test_unenriched_ingest_leaves_geo_unknown(journey_db):  # noqa: F811
    from app.gtm import journey
    d = journey_db
    assert journey._ingest(d["wid"], d["vid"], [_public_row(country="US")]) == 1
    assert journey._prospects(d["vid"])[0]["country"] is None


def _public_contact(d, email="info@acmebakery.example", geo=None):
    from app.gtm import journey
    row = _public_row(email=email, email_confidence="published", source_url="https://acmebakery.example/contact", enriched=True, people=[])
    if geo:
        row["country"] = geo
    journey._ingest(d["wid"], d["vid"], [row])
    return journey._prospects(d["vid"])[0]


def test_patch_prospect_rejects_unsupported_country_and_missing_note(journey_db):  # noqa: F811
    from app.gtm import journey
    d = journey_db
    p = _public_contact(d)
    with pytest.raises(ValueError, match="gtm_country_unsupported"):
        journey.patch_prospect(d["wid"], d["vid"], p["id"], {"country": "AU", "lawful_basis": "legitimate_interest"})
    with pytest.raises(ValueError, match="gtm_lawful_basis_note_required"):
        journey.patch_prospect(d["wid"], d["vid"], p["id"], {"country": "DE", "lawful_basis": "consent"})
    with pytest.raises(ValueError, match="gtm_invalid_input"):
        journey.patch_prospect(d["wid"], d["vid"], p["id"], {})
    assert journey._prospects(d["vid"])[0]["country"] is None


def test_us_needs_no_note_and_skip_marks_and_restores(journey_db):  # noqa: F811
    from app.gtm import journey
    d = journey_db
    p = _public_contact(d)
    state = journey.patch_prospect(d["wid"], d["vid"], p["id"], {"country": "us", "lawful_basis": "legitimate_interest"})
    assert state["prospects"][0]["country"] == "US" and state["prospects"][0]["blocked_reason"] == "us_postal_address_required"
    state = journey.patch_prospect(d["wid"], d["vid"], p["id"], {"skip": True})
    assert state["prospects"][0]["skipped"] and state["prospects"][0]["blocked_reason"] == "skipped" and state["drafts"] == []
    assert not journey.patch_prospect(d["wid"], d["vid"], p["id"], {"skip": False})["prospects"][0]["skipped"]


def test_bulk_confirm_is_all_or_nothing_and_exposes_policy(journey_db):  # noqa: F811
    from app.gtm import journey
    d = journey_db
    first = _public_contact(d)
    journey._ingest(d["wid"], d["vid"], [_public_row(website="https://zed.example/", source_url="https://zed.example/contact", email="info@zed.example", email_confidence="published", organization="zed.example")])
    ids = [p["id"] for p in journey._prospects(d["vid"])]
    with pytest.raises(ValueError, match="gtm_lawful_basis_note_required"):
        journey.confirm_prospects(d["wid"], d["vid"], [{"contact_id": ids[0], "country": "US", "lawful_basis": "legitimate_interest"}, {"contact_id": ids[1], "country": "CA", "lawful_basis": "consent"}])
    assert all(p["country"] is None for p in journey._prospects(d["vid"]))
    state = journey.confirm_prospects(d["wid"], d["vid"], [{"contact_id": ids[0], "country": "US", "lawful_basis": "legitimate_interest"},
        {"contact_id": ids[1], "country": "CA", "lawful_basis": "consent", "lawful_basis_note": "They replied yes on 3 May."}])
    assert {p["country"] for p in state["prospects"]} == {"US", "CA"}
    options = {o["code"]: o for o in state["geo_options"]}
    assert options["US"] == {"code": "US", "note_required": False, "default_basis": "legitimate_interest"}
    assert options["DE"]["note_required"] and options["FR"]["default_basis"] == "legitimate_interest" and "AU" not in options
    assert first["id"] == ids[0]


def test_search_again_fetches_more_and_preserves_previous_prospects(journey_db, monkeypatch):
    from app.gtm import journey
    from tests.unit.gtm.test_journey import _work
    d, calls = journey_db, []

    async def discover(who, search_queries=None, **kwargs):
        calls.append(who)
        return [{"email": f"info@{len(calls)}.example", "email_confidence": "published", "organization": f"Co {len(calls)}",
                 "market_role": {"role_in_market": "buyer", "anti_icp": False, "icp_fit": 0.9, "homepage_quote": "Independent bakery handling customer orders by phone."},
                 "source_url": f"https://{len(calls)}.example/contact", "website": f"https://{len(calls)}.example/"}]
    monkeypatch.setattr(journey, "_discover", discover)
    view = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners handling orders by phone")
    _work(d, view)
    state = journey.status(d["wid"], d["vid"])
    assert len(calls) == 1 and state["will_search"] is True
    assert state["state"] == "ready" and "Confirm each country" in state["message"] and "1 company" in state["message"]
    again = journey.prepare(d["wid"], d["vid"], retry=True)
    _work(d, again)
    assert len(calls) == 2 and len(journey.status(d["wid"], d["vid"])["prospects"]) == 2
    changed = journey.update_icp(d["wid"], d["vid"], "Independent bakery owners handling orders by phone", search_queries=["bakery owners orders phone"])
    _work(d, changed)
    assert len(calls) == 3 and len(journey.status(d["wid"], d["vid"])["prospects"]) == 3
