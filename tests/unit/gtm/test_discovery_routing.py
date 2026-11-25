import asyncio
import json

import pytest
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.unit.gtm.test_journey import journey_db as journey_db

load_company_os_app()


@pytest.fixture(autouse=True)
def local_discovery_mode(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")


@pytest.mark.parametrize("audience,role,mode", [
    ("commercial greenhouse operators", "operations director", "B2B"),
    ("amateur ceramic painting circles", "class coordinator", "B2C"),
])
def test_local_contact_plans_use_distinct_public_page_shapes(audience, role, mode):
    from app.gtm.discovery import Plan

    proposal = {"requested_role": role, "organisation_audience": audience,
        "queries": [{"query": f"{role} {audience} contact", "intent": "person"} for _ in range(4)]}
    plan = Plan.model_validate(proposal, context={"mode": mode, "audience": audience,
        "saved_audience": audience, "location": "Porto", "target_type": "person"})
    queries = [item.query for item in plan.queries]
    assert len(set(queries)) == 4
    assert all(audience in query and role in query and "Porto" in query for query in queries)
    assert all(any(shape in query for query in queries) for shape in ("contact", "members", "leadership", "team"))


def test_every_discovery_stage_has_a_model_route():
    from llm_gateway.routing import resolve_tier
    from app.gtm.journey import DISCOVERY_PURPOSES
    for stage, purpose in DISCOVERY_PURPOSES.items():
        assert resolve_tier("gtm.discovery." + purpose), stage


@pytest.mark.parametrize("audience", ["Freight dispatch coordinators", "Parents choosing swimming lessons"])
def test_local_discovery_keeps_provider_result_and_inline_read_limits(journey_db, monkeypatch, audience):
    from app.core.providers_local import save_settings
    from app.core.local_secrets import update_settings
    from app.gtm.discovery import local_provider
    from app.signals.research import HttpResearchProvider

    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
        models={"chat": "local-chat"}, api_key="")
    update_settings("search", {"provider": "builtin"}, {})
    calls = []

    async def search(self, query, **options):
        calls.append((query, self._limit, options))
        return [{"url": "https://sub.excluded.example/contact"}] + [
            {"url": f"https://public.example/contact/{index}"} for index in range(self._limit)]

    monkeypatch.setattr(HttpResearchProvider, "search", search)
    provider = local_provider(limit=8)
    rows = asyncio.run(provider.search(audience, include_content=True, limit=2,
        tbs="qdr:m", exclude_domains=["excluded.example"]))
    assert len(rows) == 8 and all("excluded.example" not in row["url"] for row in rows)
    assert calls == [(audience, 8, {"include_content": True, "limit": 2,
        "tbs": "qdr:m", "exclude_domains": ["excluded.example"]})]


@pytest.mark.parametrize("audience,problem,domain,consumer", [
    ("Independent freight brokers", "reconciling unpaid invoices", "freight.example", False),
    ("Apartment residents growing herbs", "growing herbs without a balcony", "herbs.example", True),
])
def test_local_discovery_saves_published_contacts(journey_db, monkeypatch, audience, problem, domain, consumer):
    from app.core import local_mode
    from app.core import providers_local
    from app.db.models import GtmContact, GtmTouch
    from app.gtm import enrich, journey
    from app.research import local_providers
    from app.signals import research
    from tests.unit.gtm.test_journey_signals import _leased_payload
    from tests.unit.gtm.test_leads_without_email import install

    data = journey_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    providers_local.save_settings(provider="openai", api_key="offline-fixture", models={"chat": "fixture-model"})
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    source = f"https://{domain}/about"
    email = f"morgan@{domain}"
    role = "Freight Broker" if not consumer else "Apartment Resident"
    text = f"Morgan Hale\n{role}\n{audience}. I struggle with {problem}.\nContact: {email}"
    searches, reads = [], []

    async def search(self, query, **options):
        assert self._limit == 10
        searches.append(query)
        return [{"url": source, "title": "Morgan Hale", "snippet": f"{audience}. {problem}."}]

    async def read(url, *, main_only=True):
        reads.append(url)
        return text

    monkeypatch.setattr(research.HttpResearchProvider, "search", search)
    monkeypatch.setattr(local_providers.LocalHttpResearchProvider, "scrape", lambda self, url, **options: read(url))
    monkeypatch.setattr(local_providers, "local_read", read, raising=False)
    monkeypatch.setattr(local_providers, "search_ready", lambda: True, raising=False)
    install(monkeypatch)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    monkeypatch.setattr(research, "_self_hosted_firecrawl", lambda: pytest.fail("Your local search must not read legacy reader settings"))
    judge = journey._discovery_model

    async def model(payload, stage, context):
        if stage in {"plan", "reformulate"}:
            return {"target_type": "person", "audience": audience, "queries": [{"query": audience + suffix, "intent": "person"}
                for suffix in (" public contacts", " published profiles")]}
        if stage == "reachjudge":
            result = await judge(payload, stage, context)
            for item in result["items"]:
                item["role"] = role
            return result
        if stage in {"judge", "draft"}:
            return {"items": []}
        return await judge(payload, stage, context)

    monkeypatch.setattr(journey, "_discovery_model", model)
    note = f"Find {audience} I can talk to"
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=consumer, note=note)
    payload = _leased_payload(data)
    asyncio.run(journey._discover(audience, problem=problem, note=note, payload=payload))
    assert searches and source in reads
    assert reads.count(source) == 1
    assert enrich.enabled()
    with data["factory"]() as session:
        contacts = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == data["wid"])))
        assert len(contacts) == 1
        assert contacts[0].email == email
        evidence = json.loads(contacts[0].enrichment_json)
        assert evidence["contact_source_url"] == source and email in evidence["contact_quote"]
        assert evidence["email_evidence"] == "published" and not evidence.get("email_route")
        assert not list(session.scalars(select(GtmTouch)))


@pytest.mark.parametrize("domain", ["freight.example", "herbs.example"])
def test_local_guessed_addresses_stay_off(journey_db, monkeypatch, domain):
    from app.config import settings
    from app.gtm import enrich

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(settings, "guessed_emails", "on")
    source = f"https://{domain}/team"
    email = f"morgan.hale@{domain}"
    facts = {"website": source, "person_name": "Morgan Hale", "person_quote": "Morgan Hale",
        "person_source_url": source, "email_route": {"route_type": "pattern", "email": email,
            "source_url": source, "pattern": "first.last", "mx_status": "mx", "confidence": 95}}
    assert not enrich.generated_route_allowed(facts, email, "US", for_send=True)
    assert enrich.guessed_routes(facts, domain, journey_db["wid"]) == []


@pytest.mark.parametrize("domain", ["freight.example", "herbs.example"])
def test_local_seed_enrichment_uses_saved_reader(journey_db, monkeypatch, domain):
    from app.core import providers_local
    from app.gtm import enrich
    from app.research import local_providers
    from app.signals import research

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    providers_local.save_settings(provider="openai", api_key="offline-fixture", models={"chat": "fixture-model"})
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "0")
    source, email = f"https://{domain}/", f"morgan@{domain}"
    reads = []

    async def read(url, *, main_only=True):
        reads.append(url)
        return f"Morgan Hale\nContact: {email}"

    monkeypatch.setattr(local_providers, "local_read", read, raising=False)
    monkeypatch.setattr(local_providers, "search_ready", lambda: True, raising=False)
    monkeypatch.setattr(research, "_self_hosted_firecrawl", lambda: pytest.fail("Your local enrichment must not read legacy reader settings"))
    facts = asyncio.run(enrich.enrich_seeds([{"website": source}], journey_db["wid"], journey_db["vid"]))
    assert reads == [source]
    assert facts[domain]["contacts"][0]["email"] == email
    assert facts[domain]["contacts"][0]["source_url"] == source
    assert facts[domain]["guessed_routes"] == []


@pytest.mark.parametrize("audience,domain", [("Freight dispatch coordinators", "freight.example"),
    ("Families growing herbs", "herbs.example")])
def test_local_homepage_is_read_once_for_role_and_contacts(journey_db, monkeypatch, audience, domain):
    from app.gtm import enrich, quality
    from app.gtm.discovery import local_provider

    source, email = f"https://{domain}/", f"morgan@{domain}"
    reads = []

    async def read(url, *, main_only=True):
        reads.append(url)
        assert not main_only
        return f'<main><h1>{audience}</h1></main><footer><a href="mailto:{email}">Contact Morgan</a></footer>'

    async def classify(url, text, context, payload):
        return {"role_in_market": "buyer", "icp_fit": .95, "anti_icp": False, "homepage_quote": audience}

    async def passages(pages, *args, **kwargs):
        return [page | {"evidence_label": "fit_only"} for page in pages]

    monkeypatch.setattr("app.research.local_providers.local_read", read)
    monkeypatch.setattr(quality, "classify_home", classify)
    monkeypatch.setattr(quality, "select_passages", passages)
    provider = local_provider()
    facts = asyncio.run(enrich.enrich_site(provider, source, journey_db["wid"], journey_db["vid"],
        published_mailboxes=True, context={"audience": audience}, payload={"workspace_id": journey_db["wid"]},
        main_scrape=provider.scrape, scrape=lambda url: provider.scrape(url, main_only=False)))
    assert reads == [source]
    assert facts["contacts"][0]["email"] == email


@pytest.mark.parametrize("domain,role", [("cedar-hospital.example", "Pharmacy Director"),
    ("orchard-cycling.example", "Ride Coordinator")])
def test_local_enrichment_reads_the_search_hit_before_stopping_at_a_generic_inbox(domain, role):
    from app.gtm import enrich

    home = f"https://{domain}/"
    source = home + "members/roster"
    reads = []

    async def scrape(url):
        reads.append(url)
        if url == home:
            return f'<a href="mailto:info@{domain}">General enquiries</a>'
        return f'<ul><li>Jaime Duran<br>{role}<br><a href="mailto:jaime@{domain}">Email Jaime</a></li></ul>'

    facts = asyncio.run(enrich.enrich_site(None, source, "workspace", "venture", scrape=scrape, published_mailboxes=True))
    assert reads == [home, source]
    assert facts["contacts"][0]["kind"] == "named_person"
    assert facts["contacts"][0]["email"] == f"jaime@{domain}"
    assert facts["contacts"][0]["source_url"] == source
