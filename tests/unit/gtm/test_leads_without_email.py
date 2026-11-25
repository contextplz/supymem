"""Judged leads stay when no email is published, each with an honest reach route. Fake search and models only."""
import asyncio

import pytest
from sqlalchemy import func, select

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.gtm.test_journey_signals import _leased_payload

B2B = {"mode": "B2B", "audience": "independent bicycle repair shops in Porto", "problem": "repair jobs are tracked on paper",
    "offer": "repair job tracker", "competitors": []}
B2B_SITES = {
    "https://rodaviva.example/": "<main><h1>Roda Viva Bicycles</h1><p>We are an independent bicycle repair shop in Porto.</p></main>",
    "https://pedalfino.example/": "<main><h1>Pedal Fino</h1><p>Pedal Fino is an independent bicycle workshop in Porto.</p>"
        "<p>Phone: +351 22 123 4567</p></main>",
}
B2B_PROFILE = "https://www.linkedin.com/in/ana-sousa-bikes"
CONSUMER = {"mode": "B2C", "audience": "first-time kayak owners in Oslo", "problem": "storing a kayak in a small flat",
    "offer": "wall storage kit", "competitors": []}
CONSUMER_PROFILE = "https://www.linkedin.com/in/ola-kayak-oslo"


def install(monkeypatch):
    from app.gtm import enrich, journey
    stages = []
    async def model(payload, stage, context):
        stages.append(stage)
        if stage == "marketrole":
            text = context["candidates"][0]["text"]
            quote = next(line for line in text.splitlines() if "independent" in line)
            return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False, "homepage_quote": quote}
        if stage == "passages":
            return {"items": [{"id": row["id"], "relevance": 0.05} for row in context["candidates"]]}
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": row["kind"], "name": row["title"], "affiliation": "", "url": row["evidence_pages"][0]["url"],
                "quote": row["evidence_pages"][0]["text"], "fit": 88, "why": "They belong to this audience.",
                "value_exchange": "Could compare notes in a short research call."} for row in context["candidates"]]}
        assert stage == "reachdraft"
        return {"items": [{"id": row["id"], "body": f"Hi {row['recipient_name']}, could we talk for 15 minutes? Reply with a time.",
            "connection_note": f"Hi {row['recipient_name']}, could we talk for 15 minutes? Reply with a time."} for row in context["candidates"]]}
    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr("app.gtm.email_learning.cached", lambda key: None)
    monkeypatch.setattr("app.gtm.email_learning.cache_save", lambda key, value: None)
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "synthetic")
    monkeypatch.setattr(enrich, "guessed_routes", lambda *args, **kwargs: [])
    return stages


class Provider:
    async def scrape(self, url, *, main_only=True):
        return B2B_SITES.get(url, "")


def _routes(lead):
    return {route["kind"] for route in lead["routes"]}


def _assert_no_email_contact(data):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.autopilot import outreach
    with data["factory"]() as session:
        assert all(not contact.email for contact in session.scalars(select(GtmContact)))
        assert session.scalar(select(func.count()).select_from(GtmTouch)) == 0
        assert session.scalar(select(func.count()).select_from(VentureDocument).where(VentureDocument.kind == outreach.KIND)) == 0


@pytest.mark.parametrize("context,host,company,role,place", [
    (B2B, "workshopadvice.example", "Workshop Advice", "Bicycle mechanic", "Porto"),
    (CONSUMER, "paddleschool.example", "Paddle School", "Kayak instructor", "Oslo"),
], ids=["business", "consumer"])
@pytest.mark.parametrize("specific_person", [False, True])
def test_generic_expert_sources_read_own_contacts_without_inventing_people(
        journey_db, monkeypatch, caplog, context, host, company, role, place, specific_person):
    from app.gtm import journey, reachable
    from app.gtm.relationships.email_compliance import source_line
    from app.db.models import GtmContact
    from tests.company_os_harness import configure_local_gtm

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_gtm(monkeypatch)
    install(monkeypatch)
    caplog.set_level("INFO", logger=journey.log.name)
    home = f"https://{host}/"
    article = home + "guides/advice"
    email = f"hello@{host}"
    quote = f"{company}. {role} services in {place}."
    pages = {home: f"<main><p>{quote}</p></main><a href='/contact'>Contact</a>",
        home + "contact": f"Business enquiries: {email}", article: quote + "<a href='/contact'>Contact</a>"}
    reads = []

    class ExpertPages:
        async def scrape(self, url, **options):
            reads.append(url)
            return pages[url]

    async def model(payload, stage, targeting):
        if stage == "reachjudge":
            assert targeting.get("target_type") == ("person" if specific_person else None)
            return {"items": [{"id": row["id"], "kind": row["kind"], "name": company,
                "company": company, "location": place, "affiliation": "", "url": row["evidence_pages"][0]["url"], "quote": quote,
                "fit": 88, "why": "Published relevant professional experience.",
                "value_exchange": "Compare practical experience in a short research call."}
                for row in targeting["candidates"]]}
        return {"items": []}

    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "record_judged_live_leads", lambda *args: None)
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], problem=context["problem"], recruitment_opt_in=True)
    targeting = context | {"target_type": "person", "requested_role": role if specific_person else ""}
    payload = _leased_payload(data)
    journey._journal(payload).mutate(payload["journey_id"], payload,
        lambda row, doc, session: doc.update(search_target=targeting))
    leads = asyncio.run(reachable.discover(ExpertPages(), [{"url": article, "title": company,
        "kind": "person", "expert_role": role}], [], targeting, payload))
    if specific_person:
        assert leads == [] and reads == [article]
        return
    assert len(leads) == 1 and leads[0]["kind"] == "business" and leads[0]["lead_type"] == "insight"
    assert reads == [article, home + "contact"]
    route = reachable.published_route(leads[0])
    assert route["email"] == email and route["source_url"] == home + "contact"
    with data["factory"]() as session:
        contact = session.query(GtmContact).filter_by(email=email).one()
        assert source_line(contact) == f"Your email address is listed publicly on {host}."
    assert any(record.message == "gtm_lead_trace" and record.lead_trace["stage"] == "kept"
        and record.lead_trace["url"] == article for record in caplog.records)


@pytest.mark.parametrize("context,host,name,role", [
    (B2B, "workshopadvice.example", "Avery McKay", "Bicycle mechanic"),
    (CONSUMER, "paddleschool.example", "Rowan DeSilva", "Kayak instructor"),
], ids=["business", "consumer"])
@pytest.mark.parametrize("uppercase", [False, True])
def test_published_expert_name_keeps_its_original_case(journey_db, monkeypatch, context, host, name, role, uppercase):
    from app.gtm import journey, playbooks, reachable
    from app.gtm.relationships.email_compliance import source_line
    from app.db.models import GtmContact
    from tests.company_os_harness import configure_local_gtm

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_gtm(monkeypatch)
    install(monkeypatch)
    name = name.upper() if uppercase else name
    home = f"https://{host}/"
    email = f"hello@{host}"
    quote = f"{name}. {role}."

    class ExpertPages:
        async def scrape(self, url, **options):
            return f"<main><p>{quote}</p><a href='mailto:{email}'>Contact</a></main>"

    async def model(payload, stage, targeting):
        if stage == "reachjudge":
            return {"items": [{"id": row["id"], "kind": "person", "name": name,
                "role": role, "affiliation": "", "url": home, "quote": quote, "fit": 88,
                "why": "Published relevant professional experience.",
                "value_exchange": "Compare practical experience in a short research call."}
                for row in targeting["candidates"]]}
        return {"items": []}

    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "record_judged_live_leads", lambda *args: None)
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], problem=context["problem"], recruitment_opt_in=True)
    targeting = context | {"target_type": None}
    payload = _leased_payload(data)
    journey._journal(payload).mutate(payload["journey_id"], payload,
        lambda row, doc, session: doc.update(search_target=targeting))
    leads = asyncio.run(reachable.discover(ExpertPages(), [{"url": home, "title": name,
        "kind": "business", "expert_role": role}], [], targeting, payload))
    assert len(leads) == 1 and leads[0]["name"] == name and leads[0]["lead_type"] == "insight"
    assert playbooks.real_person(leads[0]) == name
    assert not playbooks.real_person(leads[0] | {"company": name})
    assert not playbooks.real_person(leads[0] | {"name": "WORKSHOP", "company": ""})
    with data["factory"]() as session:
        contact = session.query(GtmContact).filter_by(email=email).one()
        assert source_line(contact) == f"Your email address is listed publicly on {host}."


@pytest.mark.parametrize("context,host,place,country", [
    (B2B, "workshopadvice.example", "Porto, Portugal", "PT"),
    (CONSUMER, "paddleschool.example", "Oslo, Norway", "NO"),
], ids=["business", "consumer"])
def test_publication_logs_committed_contacts_and_exact_drop_gates(journey_db, caplog, context, host, place, country):
    from app.gtm import journey, reachable
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"] + ", " + place, recruitment_opt_in=True)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    first = _lead("a", "Published professional", host) | {"location": place, "country": country}
    duplicate = first | {"id": "b", "source_url": f"https://{host}/about"}
    overseas = _lead("c", "Other professional", "other." + host) | {"location": "Austin", "country": "US"}
    caplog.set_level("INFO", logger=journey.log.name)
    reachable._publish(journal, payload, [lead | {"judging_status": "pending"} for lead in (first, duplicate, overseas)])
    assert not [record for record in caplog.records if record.message == "gtm_lead_trace"]
    reachable._publish(journal, payload, [first, duplicate, overseas], final=True)
    reachable._publish(journal, payload, [first], final=True)
    decisions = [record.lead_trace for record in caplog.records if record.message == "gtm_lead_trace"]
    assert decisions == [
        {"url": first["source_url"], "stage": "kept", "reason": "qualified_public_source"},
        {"url": duplicate["source_url"], "stage": "dropped", "reason": "duplicate_source"},
        {"url": overseas["source_url"], "stage": "dropped", "reason": "geography_mismatch"},
    ]


@pytest.mark.parametrize("context,host,role", [
    (B2B, "workshopadvice.example", "Bicycle mechanic"),
    (CONSUMER, "paddleschool.example", "Kayak instructor"),
], ids=["business", "consumer"])
def test_unreadable_expert_sources_are_not_logged_as_failed_qualification(journey_db, monkeypatch, caplog, context, host, role):
    from app.core.jobs import FatalError
    from app.gtm import journey, reachable
    from tests.company_os_harness import configure_local_gtm

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_gtm(monkeypatch)
    install(monkeypatch)
    source = f"https://{host}/guides/advice"

    class UnreadablePages:
        async def scrape(self, url, **options):
            raise FatalError("research_provider_rejected")

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], problem=context["problem"], recruitment_opt_in=True)
    caplog.set_level("INFO", logger=journey.log.name)
    leads = asyncio.run(reachable.discover(UnreadablePages(), [{"url": source, "kind": "business", "expert_role": role}], [],
        context | {"target_type": "person"}, _leased_payload(data)))
    assert not leads
    decisions = [record.lead_trace for record in caplog.records if record.message == "gtm_lead_trace"]
    assert {"url": source, "stage": "dropped", "reason": "source_unreadable"} in decisions
    assert all(record["reason"] for record in decisions if record["stage"] == "fetch_failed")


@pytest.mark.parametrize("context,url,title,snippet", [
    (B2B, "https://rodaviva.example/", "Roda Viva Bicycles", "Bicycle repair in Porto"),
    (CONSUMER, CONSUMER_PROFILE, "Ola Berg - Kayak owner - Oslo", "First-time kayak owner in Oslo, living in a small flat."),
], ids=["business", "consumer"])
@pytest.mark.parametrize("change", ["normalized", "unpublished_quote", "unpublished_url"])
def test_judged_quote_uses_original_public_span_after_reader_normalization(journey_db, monkeypatch, context, url, title, snippet, change):
    from app.gtm import journey, reachable

    install(monkeypatch)
    original = journey._discovery_model

    async def model(payload, stage, context):
        answer = await original(payload, stage, context)
        if stage == "reachjudge":
            for item in answer["items"]:
                if change == "unpublished_quote":
                    item["quote"] = "This person has agreed to pay for a product and receive marketing."
                elif change == "unpublished_url":
                    item["url"] = "https://unpublished.example/profile"
                else:
                    item["quote"] = item["quote"].casefold()
        return answer

    monkeypatch.setattr(journey, "_discovery_model", model)
    monkeypatch.setattr(reachable, "record_judged_live_leads", lambda *args: None)
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], problem=context["problem"])
    hits = [{"kind": "business" if context is B2B else "person", "url": url, "title": title, "snippet": snippet}]
    leads = asyncio.run(reachable.discover(Provider(), hits, [], context, _leased_payload(data)))
    if change != "normalized":
        assert leads == []
        return
    assert len(leads) == 1 and leads[0]["source_url"] == url
    assert leads[0]["quote"] != leads[0]["quote"].casefold()
    assert not any(route.get("email") for route in leads[0]["routes"])


def test_b2b_judged_businesses_and_profiles_stay_with_honest_routes(journey_db, monkeypatch):
    from app.chat import customers
    from app.gtm import journey, reachable
    data = journey_db
    install(monkeypatch)
    recorded = []
    monkeypatch.setattr(reachable, "record_judged_live_leads", lambda wid, vid, leads: recorded.extend(leads))
    journey.update_icp(data["wid"], data["vid"], B2B["audience"], problem=B2B["problem"])
    hits = [{"kind": "business", "url": url, "title": title, "snippet": "Bicycle repair in Porto"}
        for url, title in zip(B2B_SITES, ("Roda Viva Bicycles", "Pedal Fino"))]
    hits.append({"kind": "person", "url": B2B_PROFILE, "title": "Ana Sousa - Owner at Oficina Ana - Porto",
        "snippet": "Owner of an independent bicycle repair shop in Porto."})
    leads = asyncio.run(reachable.discover(Provider(), hits, [], B2B, _leased_payload(data)))
    assert {lead["source_url"] for lead in leads} >= set(B2B_SITES) | {B2B_PROFILE}
    by_url = {lead["source_url"]: lead for lead in leads}
    site_only = by_url["https://rodaviva.example/"]
    assert _routes(site_only) == {"website"} and site_only["routes"][0]["url"].startswith("https://rodaviva.example/")
    assert _routes(by_url["https://pedalfino.example/"]) == {"phone"}
    assert _routes(by_url[B2B_PROFILE]) == {"linkedin"}
    assert all(lead["evidence_label"] == "fit_only" for lead in leads)
    assert not any(route.get("email") for lead in leads for route in lead["routes"])
    _assert_no_email_contact(data)
    assert {lead["id"] for lead in recorded} == {lead["id"] for lead in leads}

    view = journey.status(data["wid"], data["vid"])
    assert len(view["reachable_leads"]) == 3 and not view.get("error_code")
    body = customers.render(data["vid"], view | {"state": "ready"})["body_md"]
    assert "found nothing usable" not in body
    assert "+351 22 123 4567" in body and "https://rodaviva.example/" in body and B2B_PROFILE in body
    assert "—" not in body


def test_no_solicitation_site_never_gets_a_website_route(journey_db, monkeypatch):
    from app.gtm import journey, reachable
    data = journey_db
    install(monkeypatch)
    journey.update_icp(data["wid"], data["vid"], B2B["audience"], problem=B2B["problem"])
    class Blocked(Provider):
        async def scrape(self, url, *, main_only=True):
            return B2B_SITES["https://rodaviva.example/"] + "<footer>No solicitation.</footer>"
    hit = {"kind": "business", "url": "https://rodaviva.example/", "title": "Roda Viva Bicycles", "snippet": "Porto"}
    assert asyncio.run(reachable.discover(Blocked(), [hit], [], B2B, _leased_payload(data))) == []


@pytest.mark.parametrize("context,url,role", [
    (B2B, "https://rodaviva.example/", "Bicycle mechanic"),
    (CONSUMER, "https://instructor.example/about", "Kayak instructor"),
], ids=["business", "consumer"])
def test_public_person_reads_keep_the_requested_expert_role_for_judging(journey_db, monkeypatch, context, url, role):
    from app.gtm import journey, reachable

    install(monkeypatch)
    judged = []

    async def judge(payload, stage, context):
        assert stage == "reachjudge"
        judged.extend(context["candidates"])
        return []

    class PublicPerson(Provider):
        async def scrape(self, url, *, main_only=True):
            return f"<main><p>Published practitioner. {role}. {context['audience']}.</p></main>"

    monkeypatch.setattr(journey, "_judge_batch", judge)
    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], problem=context["problem"])
    hit = {"kind": "person", "url": url, "title": "Published practitioner", "snippet": role, "expert_role": role}
    targeting = context | {"target_type": "person"}
    asyncio.run(reachable.discover(PublicPerson(), [hit], [], targeting, _leased_payload(data)))
    assert len(judged) == 1 and judged[0]["expert_role"] == role


def test_consumer_public_profile_stays_without_any_email(journey_db, monkeypatch):
    from app.chat import customers
    from app.gtm import journey, reachable
    data = journey_db
    install(monkeypatch)
    journey.update_icp(data["wid"], data["vid"], CONSUMER["audience"], problem=CONSUMER["problem"])
    hit = {"kind": "person", "url": CONSUMER_PROFILE, "title": "Ola Berg - Kayak owner - Oslo",
        "snippet": "First-time kayak owner in Oslo, living in a small flat."}
    leads = asyncio.run(reachable.discover(Provider(), [hit], [], CONSUMER, _leased_payload(data)))
    assert [lead["source_url"] for lead in leads] == [CONSUMER_PROFILE]
    assert _routes(leads[0]) == {"linkedin"} and leads[0]["evidence_label"] == "fit_only"
    _assert_no_email_contact(data)
    view = journey.status(data["wid"], data["vid"])
    assert len(view["reachable_leads"]) == 1
    assert CONSUMER_PROFILE in customers.render(data["vid"], view | {"state": "ready"})["body_md"]


@pytest.mark.parametrize("who,request_text,place", [
    ("independent bicycle repair shops who take cash", "find me independent bicycle repair shops in Porto I can talk to", "Porto"),
    ("first-time kayak owners who live in flats", "find me first-time kayak owners in Oslo I can talk to", "Oslo"),
])
def test_followup_labels_use_audience_and_place_only(journey_db, who, request_text, place):
    from app.chat import customers
    reply = customers.empty_search_reply({"state": "ready", "discovery_note": request_text, "icp": {"who": who}})
    target = f"{who} in {place}"
    first, second, _ = reply["actions"]
    assert first["label"] == f"Find communities for {target}"
    assert second["label"] == f"Search public LinkedIn for {target}"
    assert first["payload"]["query"] == f"{target} public subreddits forums groups"
    assert second["payload"]["query"] == f"site:linkedin.com/in/ {target}"
    assert "I can talk to" not in first["label"] + first["payload"]["query"] + second["payload"]["query"]
    amended = customers.empty_search_reply({"state": "ready", "discovery_note": request_text,
        "search_request": f"{request_text}\nLatest request: Find {who} in {place} with public contact information.",
        "icp": {"who": "different audience"}})
    for result in (reply, amended):
        assert "Your saved request:" not in str(result)
        assert "Latest request:" not in str(result)
    assert "with public contact information" in amended["actions"][0]["payload"]["query"]
    assert reply["body_md"].startswith(f"I searched public sources for {target},")


@pytest.mark.parametrize("audience,hypothesis", [
    ("dental laboratory purchasing coordinators", "If dental laboratory purchasing coordinators review a sample kit, four teams will reserve a demonstration."),
    ("urban gardeners with shaded balconies", "After urban gardeners with shaded balconies compare seedlings, half will choose a monthly planting pack."),
])
def test_empty_search_omits_experiment_hypotheses_from_copy_and_followups(journey_db, audience, hypothesis):
    from app.chat import customers

    for notes in ({"discovery_note": hypothesis}, {"search_request": hypothesis},
                  {"search_request": hypothesis, "discovery_note": hypothesis}):
        reply = customers.empty_search_reply({"state": "ready", "icp": {"who": audience}, **notes})
        assert hypothesis not in str(reply)
        assert "Your saved request:" not in str(reply)
        assert audience in reply["body_md"] and "Broaden your audience" in reply["body_md"]
        communities, profiles, manual = reply["actions"]
        assert communities["payload"]["query"] == f"{audience} public subreddits forums groups"
        assert profiles["payload"]["query"] == f"site:linkedin.com/in/ {audience}"
        assert manual["action"] == "contacts.preview"
        assert "\u2014" not in str(reply)


@pytest.mark.parametrize("audience", ["independent bicycle repair shops", "neighborhood walking groups"])
def test_plain_request_uses_local_search_or_library_with_requested_place(journey_db, monkeypatch, audience):
    from app.chat import customers
    from app.core.local_mode import is_local
    data = journey_db
    seen = []
    def match(venture_id, *, limit=5, offset=0, icp=None):
        assert not is_local(), "Your local search must not query a hosted lead catalog."
        seen.append(icp)
        return {"total": 1, "items": [{"id": "entry_1", "name": "Rui Lima", "role": "Owner", "company": "Oficina Rui",
            "why": "Runs a repair shop", "channels": ["LinkedIn"]}]}
    monkeypatch.setattr("app.lead_library.service.match", match)
    view = {"state": "idle", "can_prepare": True, "icp": {"who": audience},
        "search_request": f"find me {audience} in Porto I can talk to"}
    reply = customers.render(data["vid"], view)
    if is_local():
        assert seen == [] and "Rui Lima" not in reply["body_md"]
        assert audience in reply["body_md"] and "Porto" in reply["body_md"]
        assert not any(action.get("action", "").startswith("gtm.library_") for action in reply["actions"])
    else:
        assert "Rui Lima" in reply["body_md"] and seen[0]["location"] == "Porto"
        assert reply["actions"][0]["action"] == "gtm.library_claim"
    assert any(action["action"] == "gtm.find" for action in reply["actions"])
    constrained = customers.render(data["vid"], view | {"search_request": "find me 8 shops in Porto, only ones open on Sundays"})
    assert "Rui Lima" not in constrained["body_md"]


@pytest.mark.parametrize("context,host", [
    (B2B, "repairteachers.example"),
    (CONSUMER, "paddlecoaches.example"),
], ids=["business", "consumer"])
def test_distinct_published_people_at_one_organisation_are_saved_once(journey_db, context, host):
    from app.db.models import GtmContact
    from app.gtm import journey

    data = journey_db
    journey.update_icp(data["wid"], data["vid"], context["audience"], recruitment_opt_in=True)
    rows = []
    for first, last in (("Sana", "Wright"), ("Mira", "Reed"), ("Theo", "Mason")):
        source = f"https://{host}/people/{first.lower()}"
        rows.append({"website": f"https://{host}/", "source_url": source, "organization": "Published specialists",
            "first_name": first, "last_name": last, "person_name": first + " " + last,
            "enriched": True, "score": 88, "contact_source_url": source,
            "contact_quote": first + " " + last + " offers professional advice.", "email_confidence": "published"})
    rows[-1].pop("person_name")
    assert journey._ingest(data["wid"], data["vid"], rows) == 3
    assert journey._ingest(data["wid"], data["vid"], [rows[0] | {"source_url": f"https://{host}/team"}]) == 0
    assert journey._ingest(data["wid"], data["vid"], [rows[-1] | {"source_url": f"https://{host}/profiles/theo"}]) == 0
    with data["factory"]() as session:
        contacts = session.query(GtmContact).filter_by(workspace_id=data["wid"]).all()
        assert len(contacts) == 3 and all(contact.email is None for contact in contacts)
