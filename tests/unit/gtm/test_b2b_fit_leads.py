"""Synthetic own-site B2B fixtures; models and page providers are offline."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload

FIXTURE = json.loads((Path(__file__).parent / "fixtures/b2b_physio.json").read_text())


def install_model(monkeypatch, *, need=False, draft_failure=None):
    from app.gtm import journey
    calls = []
    async def model(payload, stage, context):
        calls.append((stage, context))
        if stage == "marketrole":
            text = context["candidates"][0]["text"]
            site = next(site for site in FIXTURE["sites"] if site["name"] in text)
            assert site["role"]["homepage_quote"] in text
            return site["role"]
        if stage == "passages":
            return {"items": [{"id": row["id"], "relevance": 0.95 if need and row["text"] == FIXTURE["need"] else 0.1}
                for row in context["candidates"]]}
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": row["kind"], "name": row["title"], "affiliation": "",
                "url": row["evidence_pages"][0]["url"], "quote": row["evidence_pages"][0]["text"], "fit": 95,
                "why": "This clinic struggles with missed appointments.", "value_exchange": "Help with your missed appointments."}
                for row in context["candidates"]]}
        assert stage == "reachdraft"
        assert len(context["candidates"]) <= 8
        assert all(row["evidence_label"] == ("need_passage" if need else "fit_only") for row in context["candidates"])
        assert all(row["text"] == (FIXTURE["need"] if need else FIXTURE["sites"][0]["role"]["homepage_quote"]) for row in context["candidates"])
        if draft_failure == "gtm_call_budget_exhausted":
            from app.core.jobs import FatalError
            raise FatalError(draft_failure)
        if draft_failure == "model_failed":
            raise RuntimeError("Model unavailable")
        if draft_failure == "credits_unavailable":
            from app.credits.errors import CreditsUnavailable
            raise CreditsUnavailable("private provider policy")
        if draft_failure == "insufficient_credits":
            from app.credits.errors import InsufficientCredits
            raise InsufficientCredits("fixture-account", 0, 1)
        if draft_failure == "provider_failed":
            from llm_gateway.errors import ProviderCallError
            raise ProviderCallError("model_not_configured", "private provider details")
        if not need:
            return {"items": [{"id": row["id"],
                "body": f"Hi {row['recipient_name'] if draft_failure != 'wrong_recipient' else 'Someone Else'}, your homepage says you are an independent physiotherapy clinic in Bristol. "
                    "Does the problem I'm researching affect your work? Open to a 15-minute call? Reply with a time.",
                "connection_note": f"Hi {row['recipient_name']}, I saw your Bristol clinic homepage. "
                    "Does the problem I'm researching affect your work? Open to a 15-minute call? Reply with a time."}
                for row in context["candidates"]]}
        return {"items": [{"id": row["id"], "body": "Hi, I saw your public note about unfilled cancellations. Open to a 15-minute call? Reply with a time.",
            "connection_note": "Hi, could we discuss your published cancellation note in a 15-minute call? Reply with a time."}
            for row in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)
    return calls


@pytest.mark.parametrize("name, host, quote", [
    ("Stonebridge Rail", "rail.example", "We maintain regional freight locomotives across three depots."),
    ("Garden Circle", "garden.example", "Our members swap surplus seedlings every spring."),
])
def test_fit_only_reason_uses_the_saved_passage_and_its_source(journey_db, name, host, quote):
    from app.chat import customers
    from app.gtm import enrich

    source = f"https://{host}/about"
    seed = {"name": name, "website": f"https://{host}/", "source_url": source}
    facts = {"market_role": {"role_in_market": "buyer"}, "evidence_label": "fit_only", "people": [],
        "name": name, "email": None, "country": None, "evidence_pages": [{"url": source, "text": quote}]}
    lead = enrich.merge(seed, facts)
    assert lead["why"] == lead["why_now"] == f'"{quote}" (from {host})'
    assert f"Why: {lead['why']}" in customers._lead_block(1, lead)
    fallback = enrich.merge(seed, facts | {"evidence_pages": []})["why"]
    assert host in fallback and "does not show" in fallback and quote not in fallback


@pytest.mark.parametrize("site", FIXTURE["sites"], ids=lambda site: site["name"])
def test_physio_homepage_market_role_gate(journey_db, monkeypatch, site):
    from app.gtm import enrich
    calls = install_model(monkeypatch)
    fetched = []
    async def scrape(url):
        fetched.append(url)
        return site["html"]
    facts = asyncio.run(enrich.enrich_site(None, site["url"], journey_db["wid"], journey_db["vid"],
        scrape=scrape, main_scrape=scrape, published_mailboxes=True,
        context=FIXTURE["context"], payload={"workspace_id": journey_db["wid"]}))
    assert bool(facts.get("quality_rejected")) is not site["keep"], calls
    if site["keep"]:
        assert facts["market_role"] == site["role"]
        assert facts["evidence_label"] == "fit_only"
        assert facts["contacts"][0]["email"] == "hello@willowphysio.example"
        assert facts["phone"] and "Book an appointment" in facts["company_text"]
        merged = enrich.merge({"organization": site["name"], "website": site["url"]}, facts)
        assert merged["evidence_label"] == "fit_only"
        assert merged["why"] == merged["why_now"] == enrich.fit_reason(site["role"]["homepage_quote"], site["url"])
    else:
        assert fetched == [site["url"]]
    assert all(stage in {"marketrole", "passages"} for stage, _ in calls)
    assert any(stage == "passages" for stage, _ in calls) == site["keep"]


@pytest.mark.parametrize("need,draft_failure", [(False, None), (True, None), (False, "model_failed"),
    (False, "credits_unavailable"), (False, "insufficient_credits"), (False, "provider_failed"),
    (False, "gtm_call_budget_exhausted"), (False, "wrong_recipient")],
    ids=["fit_only", "need_passage", "unexpected_failure", "policy_unavailable", "insufficient_credits",
         "provider_failure", "call_ceiling", "wrong_recipient"])
def test_physio_discovery_keeps_buyer_with_contact_and_safe_draft(journey_db, monkeypatch, need, draft_failure):
    from app.autopilot import claims
    from app.gtm import enrich, journey, reachable
    site = FIXTURE["sites"][0]
    context = FIXTURE["context"]
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    calls = install_model(monkeypatch, need=need, draft_failure=draft_failure)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    class Provider:
        async def scrape(self, url, *, main_only=True):
            page = next(row for row in FIXTURE["sites"] if row["url"] == url)
            return page["html"] + (f"<p>{FIXTURE['need']}</p>" if need and page is site else "")
    hits = [{"kind": "business", "url": row["url"], "title": row["name"], "snippet": "UK physiotherapy clinic"}
        for row in (FIXTURE["sites"][:1] if need else FIXTURE["sites"])]
    provider = Provider()
    leads = asyncio.run(reachable.discover(provider, hits, [], context, payload))
    assert len(leads) == 1 and leads[0]["name"] == site["name"], (payload.get("_trace_counts"), calls)
    lead = leads[0]
    assert lead["market_role"] == site["role"]
    assert lead["evidence_label"] == ("need_passage" if need else "fit_only")
    assert lead["routes"][0]["email"] == "hello@willowphysio.example"
    assert lead["draft_source"] == ("template" if draft_failure else "model")
    assert lead["draft_status"] == ("output_rejected" if draft_failure == "wrong_recipient" else
        "credits_required" if draft_failure == "insufficient_credits" else
        "model_failed" if draft_failure == "provider_failed" else "draft_failed" if draft_failure else "personalised")
    assert sum(stage == "reachdraft" for stage, _ in calls) == 1
    if need:
        assert lead["quote"] == FIXTURE["need"] and lead["evidence_relevance"] == 0.95
    else:
        assert lead["why"] == lead["value_exchange"] == enrich.fit_reason(site["role"]["homepage_quote"], site["url"])
        assert lead["quote"] == site["role"]["homepage_quote"] and lead["evidence_relevance"] == 0.0
        for field in ("draft", "connection_note", "x_note"):
            assert ("whether" if draft_failure else "Does the problem I'm researching affect your work?") in lead[field]
            assert "15-minute call" in lead[field]
            assert lead[field].startswith(f"Hi {site['name']} team,\n\n")
            assert "missed appointments" not in lead[field] and "your experience" not in lead[field]
            assert not claims.violations(lead[field])
        assert len(lead["draft"].split()) <= 90 and len(lead["x_note"]) <= 280
        if not draft_failure:
            assert "Bristol" in lead["draft"] and "Bristol" in lead["connection_note"]
        visible = journey.status(journey_db["wid"], journey_db["vid"])["reachable_leads"]
        assert len(visible) == 1 and visible[0]["value_exchange"] == lead["why"]
        assert visible[0]["evidence_label"] == "fit_only"
        assert visible[0]["draft_source"] == lead["draft_source"] and visible[0]["draft_status"] == lead["draft_status"]


@pytest.mark.parametrize("failure", ["person", "consumer", "missing_role", "vendor", "directory", "anti_icp", "low_fit", "no_contact", "guessed_only", "foreign_contact", "ungrounded_quote"])
def test_missing_need_never_bypasses_non_buyer_or_contact_gate(journey_db, monkeypatch, failure):
    from app.gtm import journey, quality, reachable
    site = FIXTURE["sites"][0]
    context = FIXTURE["context"] | ({"mode": "B2C"} if failure == "consumer" else {})
    journey.update_icp(journey_db["wid"], journey_db["vid"], context["audience"], problem=context["problem"])
    payload = _leased_payload(journey_db)
    calls = install_model(monkeypatch)
    role = dict(site["role"])
    routes = [{"kind": "email", "email": "hello@willowphysio.example", "route_type": "published",
        "source_url": site["url"], "quote": "hello@willowphysio.example"}]
    if failure in {"vendor", "directory"}:
        role["role_in_market"] = {"vendor": "vendor/competitor", "directory": "directory/listing"}[failure]
    if failure == "anti_icp":
        role["anti_icp"] = True
    if failure == "low_fit":
        role["icp_fit"] = 0.69
    if failure == "no_contact":
        routes = []
    if failure == "guessed_only":
        routes[0]["route_type"] = "guess"
    if failure == "foreign_contact":
        routes[0]["source_url"] = "https://directory.example/"
    if failure == "ungrounded_quote":
        role["homepage_quote"] = "An invented independent clinic membership quote."
    candidate = {"kind": "person" if failure == "person" else "business", "url": site["url"], "title": site["name"],
        "text": quality.main_text(site["html"]), "pages": [{"url": site["url"], "text": quality.main_text(site["html"])}],
        "market_role": None if failure == "missing_role" else role, "routes": routes, "people": [], "country": "GB",
        "expert_role": None}
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
        doc["_runtime"].update(reachable_candidates_quality_v1=[candidate]))
    assert asyncio.run(reachable.discover(None, [], [], context, payload)) == []
    assert [stage for stage, _ in calls] == ["passages"]


@pytest.mark.parametrize("audience,problem,role,domain,mode", [
    ("Freight dispatch teams", "reconciling invoices", "Freight adviser", "dispatch.example", "B2B"),
    ("Families growing herbs", "choosing indoor plants", "Gardening adviser", "gardening.example", "B2C"),
])
def test_expert_public_source_is_insight_not_an_inferred_buyer(journey_db, monkeypatch, audience, problem, role, domain, mode):
    from app.db.models import GtmTouch
    from app.gtm import journey, reachable

    context = {"audience": audience, "problem": problem, "offer": "", "competitors": [], "mode": mode}
    journey.update_icp(journey_db["wid"], journey_db["vid"], audience, problem=problem)
    payload = _leased_payload(journey_db)
    source, email = f"https://{domain}/", f"hello@{domain}"
    text = f"Morgan Hale is a {role}. Contact: {email}"
    candidate = {"kind": "business", "url": source, "title": "Morgan Hale", "text": text,
        "pages": [{"url": source, "text": text}], "expert_role": role, "people": [], "country": "",
        "routes": [{"kind": "email", "email": email, "route_type": "published", "source_url": source, "quote": email}]}
    stages = []

    async def model(payload, stage, context):
        stages.append(stage)
        if stage == "reachjudge":
            return {"items": [{"id": 0, "kind": "business", "name": "Morgan Hale", "affiliation": "",
                "url": source, "quote": text, "fit": 95, "why": "Published professional experience.",
                "value_exchange": "Discuss their published work."}]}
        assert stage == "reachdraft"
        return {"items": []}

    monkeypatch.setattr(journey, "_discovery_model", model)
    journal = journey._journal(payload)
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session:
        doc["_runtime"].update(reachable_candidates_quality_v1=[candidate]))
    leads = asyncio.run(reachable.discover(None, [], [], context, payload))
    assert len(leads) == 1 and leads[0]["lead_type"] == "insight"
    assert leads[0]["evidence_label"] == "Public source" and leads[0]["evidence_relevance"] == 0
    assert problem not in leads[0]["why"] and problem not in leads[0]["value_exchange"]
    doc = journal.document(payload["journey_id"])
    assert doc["reachable_leads"] == [] and len(doc["industry_experts"]) == 1
    assert stages == ["reachjudge", "reachdraft"]
    with journey_db["factory"]() as session:
        assert session.query(GtmTouch).count() == 0


def test_fit_only_enrichment_keeps_no_solicitation_block(journey_db, monkeypatch):
    from app.gtm import enrich
    site = FIXTURE["sites"][0]
    install_model(monkeypatch)
    async def scrape(url):
        return site["html"] + "<footer>No solicitation.</footer>"
    facts = asyncio.run(enrich.enrich_site(None, site["url"], journey_db["wid"], journey_db["vid"],
        scrape=scrape, main_scrape=scrape, published_mailboxes=True,
        context=FIXTURE["context"], payload={"workspace_id": journey_db["wid"]}))
    assert facts == {"quality_rejected": True}


def test_fit_only_label_survives_ingest_and_review_draft(journey_db):
    from app.autopilot import outreach
    from app.gtm import journey
    data = journey_db
    site = FIXTURE["sites"][0]
    journey.update_icp(data["wid"], data["vid"], FIXTURE["context"]["audience"], problem=FIXTURE["context"]["problem"])
    assert journey._ingest(data["wid"], data["vid"], [{"organization": site["name"], "website": site["url"],
        "source_url": site["url"], "email": "hello@willowphysio.example", "enriched": True,
        "email_confidence": "published", "contact_source_url": site["url"], "contact_quote": "hello@willowphysio.example",
        "evidence_label": "fit_only", "why": "Fits your audience; no public sign of this problem yet"}]) == 1
    prospects = journey._prospects(data["vid"], wid=data["wid"])
    assert len(prospects) == 1 and prospects[0]["evidence_label"] == "fit_only"
    assert prospects[0]["why"] == "Fits your audience; no public sign of this problem yet"
    assert journey._draft_ready_admitted(data["wid"], data["vid"], review_only=True) == 1
    drafts = outreach.drafts(data["vid"])
    assert len(drafts) == 1 and "Does the problem I'm researching affect your work?" in drafts[0]["body"]
    assert "how you handle this today" not in drafts[0]["body"]


def test_customer_sector_words_do_not_make_a_practice_a_competitor():
    from app.gtm import journey
    offer = "Online booking and automated recall reminders for independent dental practices in the UK"
    audience = "independent dental practices in the UK"
    practice = ("Old Square Dental Practice. We offer a monthly dental plan subscription for our patients. "
                "Book your check-up online or call reception. Independent practice in Leeds.")
    vendor = ("DentBook. Our software gives dental practices online booking and automated recall reminders. "
              "Book a demo of our platform.")
    assert not journey._same_offer("https://oldsquaredental.example/", "", practice, [], offer, "", audience=audience)
    assert journey._same_offer("https://dentbook.example/", "", vendor, [], offer, "", audience=audience)
