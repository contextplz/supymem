import asyncio
import json

import pytest

from tests.unit.gtm.test_journey import journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload


@pytest.mark.parametrize("host,title", [
    ("stockplanning.example", "Warehouse inventory advisers"),
    ("familytrails.example", "Family walking guide"),
])
def test_markdown_contact_labels_read_opaque_published_contact_pages(journey_db, host, title):
    from app.core.local_mode import is_local
    from app.gtm import enrich, reachable

    home = f"https://{host}/"
    contact = home + "p/42"
    mailbox = f"hello@{host}"
    pages = {
        home: f"# {title}\n[Contact us](/p/42)\n[About](/p/99)\n"
            "[Contact partner](https://outside.example/p/42)\n![Contact illustration](/image/contact.png)",
        contact: f"Published enquiries: [{mailbox}](mailto:{mailbox})",
        home + "p/99": f"About {title}",
    }
    seen = []

    async def read(url):
        seen.append(url)
        return pages[url]

    facts = asyncio.run(enrich.enrich_site(None, home, journey_db["wid"], journey_db["vid"],
        scrape=read, published_mailboxes=True))
    routes = reachable.contact_routes(facts, host=host)
    route = next(route for route in routes if route["kind"] == "email")
    assert seen == ([home, contact, home + "p/99"] if is_local() else [home, contact])
    assert route["email"] == mailbox and route["route_type"] == "published"
    assert route["source_url"] == contact and mailbox in route["quote"]


@pytest.mark.parametrize("host", ["stockplanning.example", "familytrails.example"])
@pytest.mark.parametrize("error_code", ["research_provider_rejected", "research_boundary_blocked"])
def test_local_failed_reader_receipt_keeps_other_sources_not_boundary_failures(journey_db, monkeypatch, host, error_code):
    from app.core.jobs import FatalError
    from app.gtm import enrich

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    home = f"https://{host}/"
    pages = {home: "Published experience. [Contact](/contact)", home + "about": "Public about page."}

    async def read(url):
        if url == home + "contact":
            raise FatalError(error_code)
        return pages[url]

    async def run():
        return await enrich.enrich_site(None, home, journey_db["wid"], journey_db["vid"], scrape=read)

    if error_code == "research_boundary_blocked":
        with pytest.raises(FatalError, match=error_code):
            asyncio.run(run())
        return
    facts = asyncio.run(run())
    assert facts["readable_sources"] == [home]
    assert [failure["url"] for failure in facts["read_failures"]] == [home + "contact"]
    assert facts["evidence_pages"][0]["url"] == home


@pytest.mark.parametrize("host,role,path,use_callback", [
    ("stockplanning.example", "Inventory adviser", "guides/practical-advice", True),
    ("stockplanning.example", "Inventory adviser", "contact-advice", True),
    ("familytrails.example", "Walking guide", "contact-advice", True),
    ("stockplanning.example", "Inventory adviser", "", False),
])
@pytest.mark.parametrize("published_mailbox", [False, True])
def test_local_expert_reads_the_published_source_and_its_contact_links(journey_db, monkeypatch, host, role, path, use_callback, published_mailbox):
    from app.gtm import enrich, reachable
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    source = f"https://{host}/{path}"
    contact = f"https://{host}/p/42"
    mailbox = f"hello@{host}"
    pages = {source: f"{role} workshops and guided sessions." + (" [Contact](/p/42)" if published_mailbox else ""),
        contact: f"Published enquiries: {mailbox}"}
    seen = []

    async def read(url, *, main_only=True):
        seen.append(url)
        return pages[url]

    monkeypatch.setattr(local_providers, "local_read", read)
    facts = asyncio.run(enrich.enrich_site(None, source, journey_db["wid"], journey_db["vid"],
        scrape=read if use_callback else None, context={"expert_role": role}, payload={}, published_mailboxes=True))
    route = reachable.contact_routes(facts, host=host)[0]
    assert seen == ([source, contact] if published_mailbox else [source])
    assert facts["evidence_pages"][0]["url"] == source
    if published_mailbox:
        assert route["email"] == mailbox and route["source_url"] == contact
    else:
        assert route["kind"] == "website" and route["source_url"] == source and role in route["quote"]


@pytest.mark.parametrize("host,business,audience,last_page", [
    ("willowcycles.example", "Willow Cycles", "Independent bicycle repair shops", "/contact"),
    ("kestrel-audit.example", "Kestrel Audit", "Security audit consultancies", "/team"),
])
@pytest.mark.hosted_only
def test_linked_contact_pages_produce_attributed_metered_published_routes(journey_db, monkeypatch, host, business, audience, last_page):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, GtmContact
    from app.gtm import enrich, journey, quality, reachable
    from app.gtm.relationships.email_compliance import OPT_OUT, source_line

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], audience)
    payload = _leased_payload(data)
    home, seen = f"https://{host}/", []
    email = f"hello@{host}"
    pages = {home: f"<h1>{business}</h1><form>Contact form</form>Call +1 212 555 0123"
        '<a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a>'
        '<a href="/team/advisors">Advisors</a><a href="https://outsider.example/contact">Partner</a>',
        home + "contact": f"Contact {business}. Contact form. noreply@{host} hello@outsider.example",
        home + "about": f"About {business}.", home + "team": f"The {business} team."}
    pages[home.rstrip("/") + last_page] += f'<p>Business enquiries: <a href="mailto:{email}">{email}</a></p>'

    class Provider:
        async def scrape(self, url, main_only=True):
            seen.append((url, main_only))
            return pages[url]

    provider = Provider()
    async def classify(*args):
        return {"role": "customer"}
    async def select(pages, *args, **kwargs):
        return pages
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", select)

    async def scrape(url, main_only=False):
        return await journey._signal_call(payload, "contact:" + host, url + ("#quality-main-v1" if main_only else ""),
            lambda: provider.scrape(url, main_only=main_only), meter="firecrawl_scrape", cache_identity={"main_only": main_only})
    facts = asyncio.run(enrich.enrich_site(provider, home, data["wid"], data["vid"], published_mailboxes=True,
        context={}, payload=payload, scrape=scrape, main_scrape=lambda url: scrape(url, True)))
    routes = reachable.contact_routes(facts, host=host)
    route = next(route for route in routes if route["kind"] == "email")
    assert route["email"] == email and route["route_type"] == "published"
    assert route["source_url"] == home.rstrip("/") + last_page and email in route["quote"]
    assert [url for url, main in seen if not main] == [home, home + "contact"] + (
        [home + "about", home + "team"] if last_page == "/team" else [])
    assert len({url for url, _ in seen} - {home}) <= 3
    runtime = journey._journal(payload).document(payload["journey_id"])["_runtime"]
    assert runtime["ceilings"]["contact_pages"] == enrich.MAX_PAGES + 1
    assert runtime["calls_used"]["contact:" + host] == len(seen)
    view = journey.status(data["wid"], data["vid"])
    assert view["estimate"]["ceiling_micro"] == journey._search_estimate(view["icp"])
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        assert account.spent_micro == len(seen) * 5000 and account.reserved_micro == 0

    lead = {"name": business, "company": business, "source_url": "https://news.ycombinator.com/item?id=123",
        "routes": routes, "judging_status": "judged", "country": "US"}
    assert journey._ingest(data["wid"], data["vid"], [reachable.contact_seed(lead)]) == 1
    with data["factory"]() as session:
        contact = session.query(GtmContact).filter_by(email=email).one()
        assert source_line(contact) == f"Your email address is listed publicly on {host}."
        contact_id = contact.id
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "100 Market Street")
    draft_id = outreach._save_draft(data["vid"], {"contact_id": contact_id, "to": email, "channel": "email",
        "subject": "Research question", "body": "Would you share how you handle this workflow?\n\nYour email address is listed publicly on news.ycombinator.com.", "state": "draft", "source_url": lead["source_url"]})
    draft = next(draft for draft in outreach.drafts(data["vid"]) if draft["doc_id"] == draft_id)
    assert draft["source_url"] == route["source_url"]
    assert f"Your email address is listed publicly on {host}." in draft["body"]
    assert "listed publicly on news.ycombinator.com" not in draft["body"]
    assert OPT_OUT in draft["body"] and "100 Market Street" in draft["body"]


@pytest.mark.parametrize("host", ["willowcycles.example", "kestrel-audit.example"])
def test_published_routes_reject_junk_and_cross_domain_mailboxes(journey_db, host):
    from app.gtm import enrich

    rejected = [f"{local}@{host}" for local in ("noreply", "privacy", "jobs", "example", "mailer-daemon")]
    rejected += [f"hello@{host}.png", "hello@unrelated.example", f"hello@{host.split('.')[0]}.other", "unrelated@gmail.com"]
    facts = enrich.extract(host, [(f"https://{host}/contact", "\n".join(rejected)),
        ("https://unrelated.example/team", f"hello@{host}")], published_mailboxes=True)
    assert not facts["contacts"] and facts["email"] is None
    mailbox = host.split(".")[0].replace("-", "") + "@gmail.com"
    facts = enrich.extract(host, [(f"https://{host}/contact", f'Email our business: <a href="mailto:{mailbox}">{mailbox}</a>')], published_mailboxes=True)
    assert facts["contacts"][0]["email"] == mailbox


@pytest.mark.parametrize("host", ["willowcycles.example", "kestrel-audit.example"])
@pytest.mark.hosted_only
def test_generated_email_send_tier_and_missing_disclosure_keep_guesses_review_only(journey_db, monkeypatch, host):
    from app.config import settings
    from app.db.models import GtmContact
    from app.gtm import enrich
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, source_line, validate_send

    monkeypatch.setattr(settings, "guessed_emails", "on")
    monkeypatch.setenv("EMAIL_PHYSICAL_ADDRESS", "100 Market Street")
    page, email = f"https://{host}/team", f"taylor.reed@{host}"
    route = {"email": email, "route_type": "pattern", "confidence": 90, "confidence_label": "Confirmed working by other founders",
        "pattern": "first.last", "mx_status": "mx", "source_url": page}
    evidence = {"email_route": route, "website": f"https://{host}/", "found_at": page,
        "person_name": "Taylor Reed", "person_quote": "Taylor Reed, Owner", "person_source_url": page}
    assert enrich.generated_route_allowed(evidence, email, "US", for_send=True)
    assert not enrich.generated_route_allowed(evidence, email, "DE", for_send=True)
    assert not enrich.generated_route_allowed({**evidence, "person_name": "Other Person"}, email, "US", for_send=True)
    with journey_db["factory"]() as session:
        for route_type, confidence in [("guess", 40), ("pattern", 80), ("pattern", 89), ("guess", 95), ("pattern", 90), ("pattern", 95)]:
            current = evidence | {"email_route": route | {"route_type": route_type, "confidence": confidence}}
            contact = GtmContact(workspace_id=journey_db["wid"], email=email, recipient_geo="US", enrichment_json=json.dumps(current))
            assert enrich.generated_route_allowed(current, email, "US")
            tier_allowed = route_type == "pattern" and confidence >= 90
            assert enrich.generated_route_allowed(current, email, "US", for_send=True) == tier_allowed
            assert not source_line(contact)
            body = complete_body(session, journey_db["wid"], contact,
                f"Would you share your experience?\n\nYour email address is listed publicly on {host}.", "Founder")
            assert "listed publicly" not in body and OPT_OUT in body and "100 Market Street" in body
            with pytest.raises(ValueError, match="gtm_source_disclosure_required" if tier_allowed else "gtm_generated_email_review_required"):
                validate_send(session, journey_db["wid"], contact, body, "Founder")


@pytest.mark.parametrize("host,business,audience", [
    ("willowcycles.example", "Willow Cycles", "Independent bicycle repair shops"),
    ("kestrel-audit.example", "Kestrel Audit", "Security audit consultancies"),
])
@pytest.mark.hosted_only
def test_public_search_contact_discovery_uses_metered_firecrawl(journey_db, monkeypatch, host, business, audience):
    from app.db.models import CreditAccount
    from app.gtm import enrich, journey, quality, reachable
    from app.signals import research

    data, public_reads, firecrawl_reads = journey_db, [], []
    journey.update_icp(data["wid"], data["vid"], audience)
    payload = _leased_payload(data)
    home, email = f"https://{host}/", f"hello@{host}"
    quote = f"{business} struggles to keep track of customer enquiries."
    async def public_scrape(self, url, **kwargs):
        public_reads.append(url)
        assert url == home
        return (f'<main><h1>{business}</h1><p>{quote}</p></main><form>Contact form</form>'
            '<a href="/contact">Contact</a><a href="/about">About</a><a href="/team">Team</a><a href="/careers">Careers</a>')
    class Firecrawl:
        async def scrape(self, url, **kwargs):
            firecrawl_reads.append(url)
            assert url in [home + "contact", home + "about", home + "team"]
            return f"Business enquiries: {email}" if url.endswith("/team") else f"About {business}."
    async def classify(*args):
        return {"role": "customer"}
    async def passages(pages, *args, **kwargs):
        return [pages[0] | {"text": quote, "relevance": 0.9, "evidence_label": "need_passage"}]
    async def model(payload, stage, context):
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "business", "name": business, "affiliation": "",
                "url": home, "quote": quote, "fit": 90, "why": "Fits your audience.", "value_exchange": "Ask about your workflow."}
                for row in context["candidates"]]}
        return {"items": []}
    monkeypatch.setattr(reachable.PublicPages, "scrape", public_scrape)
    monkeypatch.setattr(research, "HttpResearchProvider", Firecrawl)
    monkeypatch.setattr(enrich, "enabled", lambda: True)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    monkeypatch.setattr(journey, "_discovery_model", model)
    context = {"audience": audience, "mode": "B2B", "problem": "Keeping track of customer enquiries", "offer": "Workflow research", "competitors": []}
    leads = asyncio.run(reachable.discover(reachable.PublicPages(), [{"url": home, "title": business, "kind": "business", "country": "US"}], [], context, payload, fallback=True))
    assert len(leads) == 1 and leads[0]["judging_status"] == "judged", {key: value for key, value in payload.get("_trace_counts", {}).items() if "dropped" in key or "rejected" in key}
    assert reachable.published_route(leads[0])["email"] == email
    assert public_reads == [home] and firecrawl_reads == [home + "contact", home + "about", home + "team"]
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        assert account.spent_micro == 15000 and account.reserved_micro == 0
