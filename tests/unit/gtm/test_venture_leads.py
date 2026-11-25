from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.test_local_mode import _owner_client, local_owner_app


DOMAINS = [
    ("ven_cargo", "Dispatch records", "Freight coordinators", "Keep delivery schedules organized", "Mara Chen", "Coordinator"),
    ("ven_garden", "Garden planner", "Home gardeners", "Track planting dates and watering", "Luis Rivera", "Gardener"),
]


@pytest.fixture
def selected_venture_scope(journey_db, request):
    from app.tenancy import bind

    fields = request.node.callspec.params
    venture_id = fields["domain"][0] if "domain" in fields else fields["venture_id"]
    with bind(workspace_id=journey_db["wid"], venture_id=venture_id, actor=journey_db["actor"]):
        yield


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("format", ["csv", "paste"])
def test_local_contact_import_preview_commit_and_list(local_owner_app, monkeypatch, domain, format):
    from app.intake import contacts

    async def check_domains(domains):
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", check_domains)
    address = f"contact@{domain[0].replace('_', '-')}.example"
    text = f"name,email\n{domain[4]},{address}" if format == "csv" else address
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        venture = client.post("/ventures", json={"raw_idea": domain[3], "start": False}).json()
        path = f"/ventures/{venture['id']}/gtm/contacts/import"
        preview = client.post(path + "/preview", json={"text": text, "format": format})
        assert preview.status_code == 200 and preview.json()["valid_rows"] == 1
        body = {"preview_id": preview.json()["preview_id"], "lawful_basis_attested": False}
        assert client.post(path, json=body).status_code == 422
        result = client.post(path, json={**body, "lawful_basis_attested": True, "lawful_basis": "consent",
            "lawful_basis_note": "You received permission at signup."})
        assert result.status_code == 200 and result.json()["imported"] == 1
        listed = client.get(f"/ventures/{venture['id']}/leads/contacts")
        assert listed.status_code == 200
        lead = listed.json()["items"][0]
        assert lead["source"] == "imported" and lead["email"] == address
        assert lead["saved"]["lawful_basis"] == "consent"


@pytest.fixture
def local_journey_db(request, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    return request.getfixturevalue("journey_db")


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_addressless_lead_can_find_a_contact_page_and_draft_without_sending(local_journey_db, monkeypatch, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api.firstlook_api import router
    from app.core.local_secrets import update_settings
    from app.db.models import GtmContact, GtmTouch
    from app.gtm import journey
    from app.research import local_providers
    from core.clock import utcnow

    data = local_journey_db
    host = f"{domain[0].replace('_', '-')}.example"
    website = f"https://{host}/"
    contact_page = website + "contact"
    with data["factory"]() as session:
        session.add(GtmContact(id="suggested", workspace_id=data["wid"], first_name=domain[1],
            email_confidence="none", source=f"venture:{data['vid']}:public", lawful_basis="none",
            enrichment_json=json.dumps({"website": website, "found_at": website}),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    monkeypatch.setattr(journey, "_icp_seed", lambda _: {"idea": {"confirmed": True, "problem": domain[3]}})
    reads = []
    allows_outreach = True

    async def read(url, *, main_only):
        reads.append(url)
        if url == website:
            return f"{domain[1]} helps {domain[2]}. [Contact us]({contact_page})"
        assert url == contact_page
        if not allows_outreach:
            return "Contact us for support. No solicitation."
        return f"Contact us to get in touch with the {domain[1]} team."

    monkeypatch.setattr(local_providers, "local_read", read)
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{data['vid']}/leads/contacts"
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        lookup = f"{path}/suggested/find-contact"
        assert client.get(path).json()["items"][0]["status"] == "suggested"
        assert client.post(lookup, headers={"X-Subject": "other_owner"}).status_code == 404
        assert client.post(lookup, json={"url": "http://127.0.0.1/"}).status_code == 422
        update_settings("capabilities", {"research": False}, {})
        blocked = client.post(lookup)
        assert blocked.status_code == 409
        assert "Settings > Capabilities" in blocked.json()["detail"]["message"]
        assert reads == []
        update_settings("capabilities", {"research": True}, {})
        response = client.post(lookup)
        assert response.status_code == 200, response.json()
        assert reads == [website, contact_page]
        assert response.json()["items"][0]["contact_page"] == contact_page
        response = client.post(f"{path}/suggested/draft")
        assert response.status_code == 200, response.json()
        lead = response.json()["items"][0]
        assert host in lead["message_draft"] and "no thanks" in lead["message_draft"]
        assert lead["status"] == "suggested" and not lead["email"] and not lead["outreach"]
        assert client.get(path).json()["items"][0]["message_draft"] == lead["message_draft"]
        assert client.get(path).json()["items"][0]["contact_page"] == contact_page
        allows_outreach = False
        assert client.post(lookup).json()["items"][0]["outreach_restricted"] is True
        assert client.post(f"{path}/suggested/draft").status_code == 409
    with data["factory"]() as session:
        assert session.scalar(select(GtmTouch.id)) is None


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_addressless_draft_requires_checked_public_contact_evidence(local_journey_db, monkeypatch, domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api.firstlook_api import router
    from app.core.local_secrets import update_settings
    from app.db.models import GtmContact, GtmTouch
    from app.research import local_providers
    from core.clock import utcnow

    data = local_journey_db
    website = f"https://{domain[0].replace('_', '-')}.example/"
    with data["factory"]() as session:
        for contact_id in ("restricted", "allowed"):
            source = website + contact_id + "/contact"
            session.add(GtmContact(id=contact_id, workspace_id=data["wid"], first_name=domain[1],
                email_confidence="none", source=f"venture:{data['vid']}:public", lawful_basis="none",
                enrichment_json=json.dumps({"website": source, "found_at": source}),
                created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()

    async def read(url, *, main_only):
        if url == website:
            return "Contact our team."
        if url == website + "restricted/contact":
            return "Contact support. No solicitation."
        assert url == website + "allowed/contact"
        return "Contact our team with questions."

    update_settings("capabilities", {"research": True}, {})
    monkeypatch.setattr(local_providers, "local_read", read)
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{data['vid']}/leads/contacts"
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        unchecked = client.post(f"{path}/restricted/draft")
        assert unchecked.status_code == 409 and "Find a contact in Leads" in unchecked.json()["detail"]["message"]
        assert client.post(f"{path}/restricted/find-contact").status_code == 200
        assert client.post(f"{path}/restricted/draft").status_code == 409
        assert client.post(f"{path}/allowed/find-contact").status_code == 200
        drafted = client.post(f"{path}/allowed/draft")
        assert drafted.status_code == 200, drafted.json()
        assert next(lead for lead in drafted.json()["items"] if lead["id"] == "allowed")["message_draft"]
        with data["factory"]() as session:
            session.get(GtmContact, "allowed").do_not_contact = True
            session.commit()
        assert client.post(f"{path}/allowed/draft").status_code == 409
        assert "message_draft" not in client.get(path).text
    with data["factory"]() as session:
        assert session.scalar(select(GtmTouch.id)) is None


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("failure,screen", [("smtp", "Your mail account"), ("model", "Your AI provider"), ("spend", "Spending")])
def test_local_draft_failure_names_the_setting_without_exposing_provider_details(local_journey_db, monkeypatch, domain, failure, screen):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.firstlook_api import router
    from app.core.local_secrets import update_settings
    from app.credits.errors import CreditsUnavailable
    from app.gtm import journey
    from app.gtm import leads
    from llm_gateway.errors import ProviderCallError

    data = local_journey_db
    if failure == "smtp":
        update_settings("capabilities", {"send_email": True}, {})
    monkeypatch.setattr(leads, "saved_leads", lambda *args: {"items": [{"id": "contact", "name": domain[4]}]})
    def fail(*args, **kwargs):
        if failure == "smtp":
            raise ValueError("private-provider-details")
        if failure == "model":
            raise ProviderCallError("bad_api_key", "private-provider-details")
        raise CreditsUnavailable("private-provider-details")
    monkeypatch.setattr(journey, "draft_for_review", fail)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        result = client.post(f"/ventures/{data['vid']}/leads/contacts/contact/draft")
        assert result.status_code in {409, 502}
        assert "Settings > Keys and connections" in result.json()["detail"]["message"]
        assert screen in result.json()["detail"]["message"]
        assert "private-provider-details" not in result.text


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("source,label", [("founder_import", "imported"), ("public", "web"), ("lead_library:licensed", "library")])
def test_local_lead_routes_list_draft_history_export(local_journey_db, monkeypatch, domain, source, label):
    import csv
    import io
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.firstlook_api import router
    from app.db.models import GtmContact
    from app.gtm import journey
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = local_journey_db
    contact_id = "local_contact"
    with data["factory"]() as session:
        session.add(GtmContact(id=contact_id, workspace_id=data["wid"], first_name=domain[4],
            role=domain[5], email="contact@public.example", email_confidence="safe", recipient_geo="US",
            source=f"venture:{data['vid']}:{source}", lawful_basis="consent", lawful_basis_note="Workshop signup",
            enrichment_json=json.dumps({"notes": "=HYPERLINK(\"https://evil.example\")"} |
                ({"founder_import": {"notes": "=HYPERLINK(\"https://evil.example\")", "status": "new"}}
                 if source == "founder_import" else {})),
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    drafted = []
    monkeypatch.setattr(journey, "draft_for_review", lambda vid, **kwargs: drafted.append((vid, kwargs)) or
        {"kind": "text", "body_md": "Your email draft is saved. Nothing was sent."})
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{data['vid']}/leads/contacts"
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["items"][0]["source"] == label
        original_notes = response.json()["items"][0]["notes"]
        assert client.post(f"{path}/{contact_id}/draft").status_code == 200
        assert drafted == [(data["vid"], {"contact_id": contact_id})]
        for status, expected in [("replied", "reply received"), ("booked", "meeting booked")]:
            response = client.patch(f"{path}/{contact_id}/history", json={"status": status})
            assert response.status_code == 200
            assert response.json()["items"][0]["status"] == expected
            assert response.json()["items"][0]["truth_basis"] == "Recorded by you"
            assert response.json()["items"][0]["notes"] == original_notes
        exported = client.get(path + "/export")
        assert exported.status_code == 200
        assert "no-store" in exported.headers["cache-control"]
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        assert rows[0]["Name"] == domain[4] and rows[0]["Source"] == label
        assert rows[0]["Notes"].startswith("'=HYPERLINK")
        assert client.get(path, headers={"X-Subject": "other_owner"}).status_code == 404
        assert client.post(f"{path}/missing/draft").status_code == 404
        assert client.patch(f"{path}/{contact_id}/history", json={"status": "sent"}).status_code == 422


@pytest.mark.parametrize("domain", DOMAINS)
def test_local_processing_does_not_require_hosted_policy_rows(journey_db, monkeypatch, domain):
    from app.db.models import GtmOutreachSettings, Workspace
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db
    with data["factory"]() as session:
        workspace = session.get(Workspace, data["wid"])
        workspace.name = domain[1]
        workspace.tenancy = "managed"
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        assert journey._processing_block(session, data["wid"]) is None
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        assert journey._processing_block(session, data["wid"]) == "aup_required"
        settings = GtmOutreachSettings(workspace_id=data["wid"], created_at=utcnow(), updated_at=utcnow())
        session.add(settings)
        settings.aup_accepted_at = utcnow()
        session.flush()
        assert journey._processing_block(session, data["wid"]) == "dpa_required"
        settings.dpa_accepted_at = utcnow()
        assert journey._processing_block(session, data["wid"]) is None


@pytest.mark.parametrize("audience,problem,name,role,company,page", [
    ("Facilities managers", "Track building inspections", "Nadia Okafor", "Facilities manager", "Cobalt Works",
     "# Building care interview\nNadia Okafor is a Facilities manager at Cobalt Works.\nTrack building inspections. Contact nadia@conversations.example."),
    ("Parents teaching children to ride bicycles", "Plan safe bicycle rides", "Tomas Silva", "Parent", "",
     "# Trail stories podcast\nTomas Silva is a Parent teaching children to ride bicycles.\nPlan safe bicycle rides. Contact tomas@conversations.example."),
])
def test_public_page_identity_survives_discovery_and_contact_saving(journey_db, monkeypatch, audience, problem, name, role, company, page):
    from app.autopilot import outreach
    from app.db.models import GtmCompany, GtmContact
    from app.gtm import journey, quality, reachable
    from app.gtm.leads import saved_leads
    from sqlalchemy import select

    from tests.unit.gtm.test_journey_signals import _leased_payload

    data = journey_db
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}, "research": {}})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in=not company)
    payload = _leased_payload(data)
    journal = journey._journal(payload)
    url = "https://conversations.example/episodes/" + name.split()[0].lower()
    text = quality.main_text(page)
    candidate = {"kind": "person", "url": url, "title": page.splitlines()[0], "text": text,
        "quality_selected": True, "evidence_label": "need_passage", "pages": [{"url": url, "text": text, "relevance": 0.95}],
        "routes": reachable.person_page_routes(url, page, text)}
    journal.mutate(payload["journey_id"], payload, lambda row, doc, session: (
        doc["_runtime"].update(reachable_candidates_quality_v1=[candidate]),
        doc.update(search_target={"target_type": "person", "requested_count": 1, "audience": audience})))

    async def judge(payload, stage, context):
        return [{"id": row["id"], "kind": "person", "name": name, "role": role, "company": company,
            "url": url, "quote": text, "affiliation": "", "fit": 90, "why": problem, "value_exchange": problem}
            for row in context["candidates"]]

    monkeypatch.setattr(journey, "_judge_batch", judge)
    leads = asyncio.run(reachable.discover(None, [], [], {"mode": "B2B" if company else "B2C", "audience": audience,
        "problem": problem, "offer": "", "competitors": [], "target_type": "person", "requested_count": 1}, payload))
    assert len(leads) == 1 and leads[0]["name"] == name and leads[0]["company"] == company
    seed = reachable.contact_seed(leads[0])
    assert seed["person_name"] == name and seed["organization"] == company
    assert reachable._publish(journal, payload, leads, final=True) == 0
    with data["factory"]() as session:
        contact = session.scalars(select(GtmContact).where(GtmContact.workspace_id == data["wid"])).one()
        assert " ".join(filter(None, (contact.first_name, contact.last_name))) == name
        assert contact.role == role
        saved_company = session.get(GtmCompany, contact.company_id) if contact.company_id else None
        assert (saved_company.canonical_name if saved_company else "") == company
        assert saved_company is None or saved_company.domain is None
    items = saved_leads(data["wid"], data["vid"])["items"]
    assert len(items) == 1 and items[0]["name"] == name and items[0]["role"] == role
    assert items[0]["company"] == (company or "Unknown")
    assert items[0]["contact_id"] and items[0]["can_draft"] and items[0]["source_url"] == url
    assert reachable.contact_seed(leads[0] | {"company": name})["organization"] != name


@pytest.mark.parametrize("audience,problem", [
    ("Facilities managers in France", "Track building inspections"),
    ("Parents teaching children to ride bicycles in France", "Plan safe bicycle rides"),
])
def test_leads_read_does_not_match_the_shared_library(journey_db, monkeypatch, audience, problem):
    from time import perf_counter

    from app.api.venture_assets import router
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.lead_library import service
    from app.lead_library.models import LeadLibraryEntry
    from core.clock import utcnow
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import event, insert
    from sqlalchemy.orm import Session

    data = journey_db
    monkeypatch.setattr(outreach, "_brief", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}, "research": {}})
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem, recruitment_opt_in="Parents" in audience)
    now = utcnow()
    with data["factory"]() as session:
        row = journey._row(session, data["vid"])
        row.provenance_json = json.dumps(journey._json(row.provenance_json) | {"state": "ready", "error_code": "no_public_candidates"})
        for index in range(2):
            session.add(GtmContact(id=f"out_of_region_{index}", workspace_id=data["wid"], first_name=f"Mika {index}",
                recipient_geo="US", source=f"venture:{data['vid']}:public", created_at=now, stage_changed_at=now))
        session.execute(insert(LeadLibraryEntry), [{"id": f"unrelated_{index}", "external_lead_id": f"unrelated-{index}",
            "person_name": f"Unrelated person {index}", "company": "Other workshop", "email": f"hello@workshop{index}.example",
            "email_status": "unverified_public", "lead_type": "operator", "function": "operations", "seniority": "manager",
            "role_title": "Facilities manager", "country_code": "FR", "company_stage": "established",
            "quality_flags": '["email_generic"]', "license_source": "synthetic owned fixture",
            "imported_at": now, "content_hash": str(index), "status": "active"} for index in range(10000)])
        session.commit()
    measurements, queries, loaded = [], [], []
    match = service.match

    def measured_match(*args, **kwargs):
        started = perf_counter()
        try:
            return match(*args, **kwargs)
        finally:
            measurements.append(perf_counter() - started)

    def sql(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    def materialized(session, instance):
        if isinstance(instance, LeadLibraryEntry):
            loaded.append(instance.id)

    monkeypatch.setattr(service, "match", measured_match)
    engine = data["factory"].kw["bind"]
    event.listen(engine, "before_cursor_execute", sql)
    event.listen(Session, "loaded_as_persistent", materialized)
    application = FastAPI()
    application.include_router(router)
    started = perf_counter()
    try:
        response = TestClient(application).get(f"/ventures/{data['vid']}/gtm/contacts", headers={"X-Subject": data["actor"]})
        elapsed = perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(Session, "loaded_as_persistent", materialized)
    library_queries = [query for query in queries if "lead_library_entries" in query]
    print(f"leads read: {elapsed:.4f}s; library: {sum(measurements):.4f}s; library queries: {len(library_queries)}; materialized entries: {len(loaded)}")
    assert response.status_code == 200 and response.json()["total"] == 2
    assert not measurements and not library_queries and not loaded


@pytest.mark.parametrize("domain,model", zip(DOMAINS, ("b2b", "consumer")))
def test_finished_people_search_is_visible_in_leads_without_email(journey_db, monkeypatch, domain, model, selected_venture_scope):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.venture_assets import router
    from app.chat import customers
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from core.clock import utcnow

    venture_id, title, audience, problem, name, role = domain
    data, now = journey_db, utcnow()
    icp = {"who": audience, "problem": problem, "business_model": model, "confirmed": True}
    monkeypatch.setattr(journey, "_icp_seed", lambda _: {"idea": icp})
    routes = [
        {"kind": "website", "url": "https://public.example/people/0"},
        {"kind": "linkedin", "url": "https://www.linkedin.com/in/public-person"},
        {"kind": "forum", "url": "https://community.example/posts/2"},
        {"kind": "phone", "value": "+1 202 555 0142"},
    ]
    people = [{"id": f"person_{index}", "kind": "person", "name": f"{name} {index}", "role": role,
        "source_url": f"https://public.example/people/{index}", "why": problem, "judging_status": "judged",
        "value_exchange": "You could compare experiences in a short conversation.", "routes": [route],
        "draft": "Could you share your experience?", "connection_note": "Could you share your experience?"}
        for index, route in enumerate(routes)]
    with data["factory"]() as session:
        session.add(Venture(id=venture_id, workspace_id=data["wid"], name=title, raw_idea=f"{problem}. Audience: {audience}",
            stage="draft", created_at=now, updated_at=now))
        session.flush()
        row = journey._row(session, venture_id, create=True)
        row.provenance_json = json.dumps({"state": "ready", "icp": icp, "reachable_leads": people,
            "search_target": {"target_type": "person", "requested_count": 4, "audience": audience}})
        session.add(GtmContact(id="saved_person", workspace_id=data["wid"], source=f"venture:{venture_id}:public",
            first_name=people[0]["name"], role=role, enrichment_json=json.dumps({"found_at": people[0]["source_url"]}),
            created_at=now, stage_changed_at=now))
        session.commit()
    view = journey.status(data["wid"], venture_id)
    body = customers.render(venture_id, view)["body_md"]
    assert all(person["name"] in body for person in people)
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    path = f"/ventures/{venture_id}/gtm/contacts"
    result = client.get(path, headers={"X-Subject": data["actor"]}).json()
    assert result["total"] == 4
    assert {item["name"] for item in result["items"]} == {person["name"] for person in people}
    for item in result["items"]:
        person = next(person for person in view["reachable_leads"] if person["name"] == item["name"])
        assert item["source_url"] == person["source_url"] and item["routes"] == person["routes"]
        assert not item["email"] and not item["can_draft"]
    assert client.get(path, headers={"X-Subject": "another_founder"}).status_code == 404
    from app.tenancy import bind
    with bind(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]):
        assert not client.get(f"/ventures/{data['vid']}/gtm/contacts", headers={"X-Subject": data["actor"]}).json()["items"]


@pytest.mark.parametrize("venture_id,title,audience,problem,name,role", DOMAINS)
def test_lead_truth_states_require_evidence_and_keep_each_outreach_item(journey_db, venture_id, title, audience, problem, name, role, selected_venture_scope):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmTouch, Job, Venture
    from app.gtm.contact_work import _put
    from app.gtm.leads import saved_leads
    from core.clock import utcnow

    data, now = journey_db, utcnow()
    states = ["suggested", "verified", "drafted", "approved", "queued", "sent", "reply received", "positive reply", "meeting booked",
        "automated", "inferred", "delivery unconfirmed", "stale queue", "old counter", "followup", "old sent timestamp"]
    with data["factory"]() as session:
        session.add(Venture(id=venture_id, workspace_id=data["wid"], name=title, raw_idea=f"{problem}. Audience: {audience}",
            stage="draft", created_at=now, updated_at=now))
        session.add_all([Job(id=venture_id + status, workspace_id=data["wid"], venture_id=venture_id, kind="experiment_run",
            status=status, payload_json="{}", created_at=now) for status in ("scheduled", "failed")])
        session.flush()
        for index, state in enumerate(states):
            email = f"person{index}@public.example"
            evidence = {"person_name": name, "found_at": "https://public.example/contact"}
            if state == "verified":
                evidence.update(email_evidence="published", contact_quote=f"Contact {name}: {email}", contact_source_url="https://public.example/contact")
            if state == "inferred":
                evidence["founder_import"] = {"status": "interested"}
            session.add(GtmContact(id=f"{venture_id}_{index}", workspace_id=data["wid"], source=f"venture:{venture_id}:public",
                first_name=name, role=role, email=email, email_confidence="risky", enrichment_json=json.dumps(evidence),
                times_emailed=3 if state == "old counter" else 0, last_emailed_at=now if state == "old sent timestamp" else None,
                created_at=now, stage_changed_at=now))
        session.flush()
        for state, direction, classification in [("reply received", "in", "human_neutral"), ("positive reply", "in", "human_positive"), ("meeting booked", "in", "human_positive"), ("automated", "in", "automated")]:
            session.add(GtmTouch(id=venture_id + state, workspace_id=data["wid"], venture_id=venture_id,
                contact_id=f"{venture_id}_{states.index(state)}", direction=direction, channel="email", classification=classification,
                body_text="Saved reply", simulated=0, sent_at=now if state == "automated" else None, created_at=now))
        session.commit()
    for state in ("drafted", "approved", "queued", "sent", "delivery unconfirmed", "stale queue", "followup"):
        index = states.index(state)
        draft = {"contact_id": f"{venture_id}_{index}", "state": "sent" if state in {"sent", "followup"} else "delivery_unconfirmed" if state == "delivery unconfirmed" else "draft",
            "review_state": "approved" if state in {"approved", "queued", "stale queue"} else "draft", "channel": "email", "day": 1,
            "to": f"person{index}@public.example", "subject": f"Your email {index}",
            "body": "Your email address is listed publicly on public.example.\nReply no thanks to opt out.\nYour sending address."}
        if state in {"queued", "stale queue"}:
            draft["scheduled_send"] = {"job_id": venture_id + ("scheduled" if state == "queued" else "failed")}
        outreach._save_draft(venture_id, draft)
        if state == "followup":
            outreach._save_draft(venture_id, draft | {"day": 3, "state": "draft", "subject": "Your saved follow-up"})
    _put(venture_id, f"{venture_id}_{states.index('meeting booked')}", "manual_outreach", json.dumps({"booked_at": now.isoformat()}))
    result = saved_leads(data["wid"], venture_id)
    by_state = {state: next(item for item in result["items"] if item["id"] == f"{venture_id}_{index}") for index, state in enumerate(states)}
    for state in states[:9] + ["delivery unconfirmed"]:
        assert by_state[state]["status"] == state
    for state in ("automated", "inferred", "old counter"):
        assert by_state[state]["status"] == "suggested"
    assert "not a received reply" in by_state["inferred"]["interest_note"]
    assert by_state["meeting booked"]["truth_basis"] == "Recorded by you"
    assert by_state["stale queue"]["status"] == "approved"
    assert by_state["old sent timestamp"]["status"] == "sent" and by_state["old sent timestamp"]["truth_basis"] == "Recorded in your saved history"
    assert by_state["followup"]["status"] == "sent"
    assert {email["truth_state"] for email in by_state["followup"]["outreach"]} == {"sent", "drafted"}
    saved = {email["contact_id"]: email for email in outreach.drafts(venture_id, audience=audience)}
    for state, expected in (("queued", "queued"), ("stale queue", "approved"), ("delivery unconfirmed", "delivery unconfirmed")):
        assert saved[f"{venture_id}_{states.index(state)}"]["truth_state"] == expected
    queued = saved[f"{venture_id}_{states.index('queued')}"]
    outreach._save_draft(venture_id, queued, queued["doc_id"])
    from app.db.models import VentureDocument
    with data["factory"]() as session:
        assert "truth_state" not in json.loads(session.get(VentureDocument, queued["doc_id"]).provenance_json)
    for item in result["items"]:
        for email in item["outreach"]:
            assert "Your email address is listed publicly" in email["body"] and "Reply no thanks" in email["body"] and "Your sending address" in email["body"]


def seed_contacts(data, venture_id, name, role):
    from app.db.models import GtmCompany, GtmContact
    from core.clock import utcnow

    with data["factory"]() as session:
        company = GtmCompany(id="company_" + venture_id, workspace_id=data["wid"], canonical_name="Community group",
            domain=venture_id + ".example", notes="Saved company notes", created_at=utcnow())
        session.add(company)
        session.flush()
        for source in ("public", "lead_library:licensed_record", "founder_import"):
            evidence = {"person_name": name, "why": "Matches the saved audience", "found_at": "https://public.example/about",
                "website": "https://public.example", "phone": "+1 202 555 0142"}
            if source == "founder_import":
                evidence["founder_import"] = {"notes": "You met at an event"}
            if source.startswith("lead_library"):
                evidence["lead_library"] = {"email_tier": "company_inbox"}
            session.add(GtmContact(id=venture_id + "_" + source.split(":")[0], workspace_id=data["wid"], company_id=company.id,
                source=f"venture:{venture_id}:{source}", first_name=name, role=role, email=f"{venture_id}.{source.split(':')[0]}@public.example",
                email_confidence="risky",
                linkedin_url="https://www.linkedin.com/in/public-person", x_handle="public_person",
                enrichment_json=json.dumps(evidence), stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()


@pytest.mark.parametrize("venture_id,title,audience,problem,name,role", DOMAINS)
def test_leads_endpoint_lists_all_sources_statuses_and_fences_scope(journey_db, venture_id, title, audience, problem, name, role, selected_venture_scope):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.venture_assets import router
    from app.authz.workspace import ensure_subject_person
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmSuppression, GtmTouch, Venture
    from app.gtm.contact_work import _put
    from core.clock import utcnow

    data = journey_db
    with data["factory"]() as session:
        session.add(Venture(id=venture_id, workspace_id=data["wid"], name=title, raw_idea=f"{problem}. Audience: {audience}",
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    seed_contacts(data, venture_id, name, role)
    seed_contacts(data, data["vid"], "Unrelated person", "Other role")
    _put(venture_id, venture_id + "_founder_import", "call_notes", "You agreed to check in next week")
    outreach._save_draft(venture_id, {"contact_id": venture_id + "_public", "state": "draft", "subject": "Your first email",
        "body": "Your email address is listed publicly on public.example.\nReply no thanks to opt out.\n42 Lake Road", "day": 1,
        "to": f"{venture_id}.public@public.example", "channel": "email"})
    with data["factory"]() as session:
        imported = session.get(GtmContact, venture_id + "_founder_import")
        imported.email = "suppressed@public.example"
        session.add(GtmSuppression(id="suppressed", workspace_id=data["wid"], email=imported.email, reason="opt_out", created_at=utcnow()))
        session.add(GtmTouch(id="reply", workspace_id=data["wid"], venture_id=venture_id, contact_id=venture_id + "_lead_library",
            direction="in", channel="email", simulated=0, body_text="Thanks", created_at=utcnow()))
        session.commit()
    application = FastAPI()
    application.include_router(router)
    client = TestClient(application)
    path = f"/ventures/{venture_id}/gtm/contacts"
    headers = {"X-Subject": data["actor"]}
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["venture_id"] == venture_id and result["workspace_id"] == data["wid"] and result["total"] == 3
    by_source = {item["source"]: item for item in result["items"]}
    assert set(by_source) == {"web", "library", "imported"}
    assert {item["name"] for item in result["items"]} == {name}
    assert by_source["web"]["status"] == "drafted" and by_source["web"]["draft"]["subject"] == "Your first email"
    assert by_source["web"]["can_draft"]
    assert "Your email address is listed publicly" in by_source["web"]["draft"]["body"]
    assert by_source["library"]["status"] == "reply received" and by_source["library"]["company_inbox"]
    assert by_source["imported"]["status"] == "opted out" and not by_source["imported"]["can_draft"]
    assert "You met at an event" in by_source["imported"]["notes"]
    assert "You agreed to check in next week" in by_source["imported"]["notes"]
    assert by_source["web"]["website"] and by_source["web"]["source_url"] and by_source["web"]["created_at"]
    saved = outreach.drafts(venture_id)[0]
    outreach._save_draft(venture_id, saved | {"channel": "manual"}, saved["doc_id"])
    assert next(item for item in client.get(path, headers=headers).json()["items"] if item["source"] == "web")["draft"]["subject"] == "Your first email"
    with data["factory"]() as session:
        public = session.get(GtmContact, venture_id + "_public")
        public.email_confidence = "guess"
        session.commit()
    assert not next(item for item in client.get(path, headers=headers).json()["items"] if item["source"] == "web")["can_draft"]
    with data["factory"]() as session:
        public = session.get(GtmContact, venture_id + "_public")
        public.email_confidence = "risky"
        public.bounced_at = utcnow()
        session.commit()
    assert next(item for item in client.get(path, headers=headers).json()["items"] if item["source"] == "web")["status"] == "bounced"
    with data["factory"]() as session:
        public = session.get(GtmContact, venture_id + "_public")
        public.bounced_at = None
        public.last_emailed_at = utcnow()
        session.commit()
    assert next(item for item in client.get(path, headers=headers).json()["items"] if item["source"] == "web")["status"] == "sent"
    ensure_subject_person("usr_unrelated")
    assert client.get(path, headers={"X-Subject": "usr_unrelated"}).status_code == 404
    assert client.get(path).status_code == 422
    assert client.get(path.replace(venture_id, "missing"), headers=headers).status_code == 404
    with data["factory"]() as session:
        session.get(Venture, venture_id).archived_at = utcnow()
        session.commit()
    assert client.get(path, headers=headers).status_code == 403


@pytest.mark.parametrize("venture_id,title,audience,problem,name,role", DOMAINS)
def test_remove_saved_lead_reuses_skip_gate_without_pending_permission(journey_db, monkeypatch, venture_id, title, audience, problem, name, role):
    from app.chat import customers
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from app.gtm.leads import saved_leads

    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = title, f"{problem}. Audience: {audience}"
        session.commit()
    seed_contacts(data, data["vid"], name, role)
    monkeypatch.setattr(customers, "_view", lambda vid: {"icp": {"who": audience, "problem": problem}, "prospects": []})
    monkeypatch.setattr(journey, "status", lambda *args, **kwargs: {"icp": {"who": audience, "problem": problem}, "prospects": []})
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: 0)
    monkeypatch.setattr(customers, "render", lambda *args, **kwargs: {"body_md": "Saved leads updated"})
    contact_id = data["vid"] + "_public"
    reply = asyncio.run(customers.execute(data["vid"], "gtm.eligibility", {"contact_id": contact_id, "skip": True, "remove": True}, data["actor"]))
    assert reply["body_md"].startswith("Removed from your leads")
    assert saved_leads(data["wid"], data["vid"])["total"] == 2
    with data["factory"]() as session:
        assert json.loads(session.get(GtmContact, contact_id).enrichment_json)["skipped"] is True
    reply = asyncio.run(customers.execute(data["vid"], "gtm.eligibility", {"contact_id": "foreign", "skip": True}, data["actor"]))
    assert reply["body_md"] == "I could not remove that person just now. Nothing changed or was sent."


@pytest.mark.parametrize("venture_id,title,audience,problem,name,role", DOMAINS)
def test_local_selected_lead_drafts_immediately_and_keeps_compliance(local_journey_db, monkeypatch, venture_id, title, audience, problem, name, role):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.firstlook_api import router
    from app.autopilot import outreach
    from app.core.local_secrets import update_settings
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from app.gtm.leads import saved_leads
    from tests.unit.gtm.test_journey import _mail

    data = local_journey_db
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}})
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    seed_contacts(data, data["vid"], name, role)
    _mail(data)
    update_settings("smtp", {"host": "smtp.public.example", "port": 587, "user": "you@public.example",
        "sender_name": "Your founder", "postal_address": "1 Founder Street, New York, NY 10001"}, {"password": "test-password"})
    contact_id = data["vid"] + "_founder_import"
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = title, f"{problem}. Audience: {audience}"
        contact = session.get(GtmContact, contact_id)
        contact.email_confidence = "risky"
        contact.contact_provenance = "founder_import"
        contact.recipient_geo = "US"
        contact.lawful_basis = "consent"
        contact.lawful_basis_note = "You received permission to contact this person."
        evidence = json.loads(contact.enrichment_json)
        evidence["founder_import"].update(lawful_basis_attested=True, email_check="mx", status="new")
        contact.enrichment_json = json.dumps(evidence)
        session.commit()
    app = FastAPI()
    app.include_router(router)
    path = f"/ventures/{data['vid']}/leads/contacts/{contact_id}/draft"
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        result = client.post(path)
        assert result.status_code == 200
        assert result.json()["receipt"]["body_md"] == "Your email draft is saved for review. Nothing was sent."
        assert all("confirmation" not in action for action in result.json()["receipt"]["actions"])
    drafts = outreach.drafts(data["vid"], "draft")
    assert len(drafts) == 1 and drafts[0]["contact_id"] == contact_id and drafts[0]["suggestion_draft"]
    assert "I have your address from my own contacts." in drafts[0]["body"]
    assert outreach.OPT_OUT in drafts[0]["body"] and "1 Founder Street" in drafts[0]["body"]
    assert next(item for item in saved_leads(data["wid"], data["vid"])["items"] if item["id"] == contact_id)["draft"]
    drafts[0]["suggestion_draft"] = False
    outreach._save_draft(data["vid"], drafts[0], drafts[0]["doc_id"])
    view = journey.status(data["wid"], data["vid"])
    assert any(draft["contact_id"] == contact_id for draft in view["drafts"])
    assert view["send_blocked_reason"] != "consumer_recruitment_only"
    with data["factory"]() as session:
        session.get(GtmContact, contact_id).unsubscribed_at = journey.utcnow()
        session.commit()
    with TestClient(app, headers={"X-Subject": data["actor"]}) as client:
        blocked = client.post(path).json()["receipt"]
    assert blocked["kind"] == "needs_you" and "Nothing was sent" in blocked["body_md"]
    assert len(outreach.drafts(data["vid"], "draft")) == 1
    for invalid in ("", [], "x" * 101):
        with pytest.raises(ValueError, match="gtm_invalid_input"):
            journey.draft_for_review(data["vid"], contact_id=invalid)
    with pytest.raises(ValueError, match="gtm_not_found"):
        journey.draft_for_review(data["vid"], contact_id="another_ventures_contact")


@pytest.mark.parametrize("domain", DOMAINS)
def test_explicit_invalid_contact_selection_refuses_at_chat_boundary(local_journey_db, monkeypatch, domain):
    from app.chat import execute
    from app.gtm import journey
    from app.db.models import Venture

    data = local_journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = domain[1], f"{domain[3]} for {domain[2]}"
        session.commit()
    monkeypatch.setattr(execute, "local_capability_reply", lambda _: None)
    drafted = []
    monkeypatch.setattr(journey, "draft_for_review", lambda *args, **kwargs: drafted.append(kwargs) or {"kind": "text"})
    for invalid in (None, "", [], "x" * 101):
        with pytest.raises(ValueError, match="gtm_invalid_input"):
            asyncio.run(execute.execute_action(data["vid"], "outreach.draft",
                payload={"run": True, "review_only": True, "contact_id": invalid}, actor_subject=data["actor"]))
    assert drafted == []
    asyncio.run(execute.execute_action(data["vid"], "outreach.draft",
        payload={"run": True, "review_only": True}, actor_subject=data["actor"]))
    assert drafted == [{}]


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("error", ["sample_mode", "gtm_approval_changed", "gtm_outcome_uncertain"])
def test_local_send_api_keeps_recovery_context(local_journey_db, monkeypatch, domain, error):
    from fastapi import HTTPException
    from app.api import gtm_god_ops
    from app.gtm import journey
    from app.db.models import Venture

    data = local_journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = domain[1], f"{domain[3]} for {domain[2]}"
        session.commit()
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: data["wid"])

    async def refuse(*args):
        raise ValueError(error)

    monkeypatch.setattr(journey, "send", refuse)
    body = gtm_god_ops.JourneySendBody(batch_id="batch", draft_ids=["draft"], approval_digest="a" * 64, cap=1)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(gtm_god_ops.journey_send(data["vid"], body))
    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert detail["code"].startswith("gtm_") and detail["message"]
    if error == "sample_mode":
        assert "sample" in detail["message"] and "Keys and connections" in detail["message"]
        assert "API key" in detail["message"] and "model" in detail["message"]
    else:
        assert "draft" in detail["message"].lower()
    if error == "gtm_outcome_uncertain":
        assert "Nothing was sent" not in detail["message"]


@pytest.mark.parametrize("venture_id,title,audience,problem,name,role", DOMAINS)
def test_linkedin_note_uses_saved_lead_playbooks_not_the_post_flow(journey_db, monkeypatch, venture_id, title, audience, problem, name, role):
    from app.chat import customers
    from app.db.models import GtmContact, Venture

    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = title, f"{problem}. Audience: {audience}"
        session.commit()
    seed_contacts(data, data["vid"], name, role)
    monkeypatch.setattr(customers, "_view", lambda vid: {"icp": {"who": audience, "problem": problem}})
    contact_id = data["vid"] + "_public"
    reply = asyncio.run(customers.execute(data["vid"], "gtm.status", {"contact_id": contact_id, "linkedin_note": True}, data["actor"]))
    assert f"LinkedIn connection note for {name}" in reply["body_md"]
    assert title in reply["body_md"] and "You open LinkedIn and send this yourself" in reply["body_md"]
    assert reply["actions"] == []
    with data["factory"]() as session:
        session.get(GtmContact, contact_id).do_not_contact = 1
        session.commit()
    blocked = asyncio.run(customers.execute(data["vid"], "gtm.status", {"contact_id": contact_id, "linkedin_note": True}, data["actor"]))
    assert blocked["body_md"] == "That lead is unavailable or has opted out. No note was written or sent."


@pytest.mark.parametrize("audience,problem", [
    ("Engineering managers at software companies", "Flaky tests delay releases"),
    ("Operators of community sports clubs", "Membership payments are hard to track"),
])
@pytest.mark.hosted_only
def test_library_matches_and_claims_keep_web_card_job_and_auto_schedule(journey_db, monkeypatch, audience, problem):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.chat import customers
    from app.db.models import Job
    from app.gtm import journey

    data = journey_db
    monkeypatch.setattr(journey, "_icp_seed", lambda vid: {"idea": {"audience": audience, "problem": problem, "confirmed": True}})
    monkeypatch.setattr("app.lead_library.service.match", lambda *args, **kwargs: {"total": 1, "items": [
        {"id": "licensed", "name": "Public person", "role": "Manager", "company": "Public organization", "why": "Audience match", "channels": ["email"]}]})
    monkeypatch.setattr(journey, "_prospects", lambda *args, **kwargs: [])
    view = journey.status(data["wid"], data["vid"])
    reply = customers.render(data["vid"], view)
    assert view["will_search"] and view["estimate"]["ceiling_micro"] > 0
    assert "lead library" in reply["body_md"]
    assert any(action.get("action") == "gtm.find" and not action.get("confirmation") for action in reply["actions"])
    seed_contacts(data, data["vid"], "Public person", "Manager")
    monkeypatch.setattr(journey, "_prospects", lambda *args, **kwargs: [{"id": data["vid"] + "_lead_library", "name": "Public person", "email": "hello@public.example"}])
    view = journey.status(data["wid"], data["vid"])
    reply = customers.render(data["vid"], view)
    assert view["will_search"] and view["estimate"]["ceiling_micro"] > 0
    assert "Your saved leads" in reply["body_md"] and "lead library" not in reply["body_md"]
    assert any(action.get("action") == "gtm.find" and not action.get("confirmation") for action in reply["actions"])
    prepared = journey.prepare(data["wid"], data["vid"])
    assert prepared["discovery_started"]
    with data["factory"]() as session:
        job = session.get(Job, prepared["job_id"])
        assert job.kind == "gtm.journey.prepare" and job.status == "scheduled"
        job.status = "succeeded"
        row = journey._row(session, data["vid"])
        saved = journey._json(row.provenance_json)
        saved["auto_search"] = {"enabled": True, "target": 100, "ceiling_micro": 1000,
            "last_run_at": (journey.utcnow() - timedelta(days=3)).isoformat()}
        monkeypatch.setattr(journey, "_contacted_businesses", lambda *args: 0)
        monkeypatch.setattr(journey.guard, "get_settings", lambda vid: {"send_max_per_day": 1})
        monkeypatch.setattr(journey.outreach, "contacts", lambda vid: [SimpleNamespace(source="venture:" + vid + ":lead_library:licensed", times_emailed=0)] * 3)
        monkeypatch.setattr(journey, "_block", lambda *args: None)
        assert journey._auto_search_due(session, row, saved, data["wid"], data["vid"]) is True
        monkeypatch.setattr(journey.outreach, "contacts", lambda vid: [SimpleNamespace(source="venture:" + vid + ":public", times_emailed=0)])
        assert journey._auto_search_due(session, row, saved, data["wid"], data["vid"]) is False
