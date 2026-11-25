"""Pre-signals journey documents and published B2B ventures remain readable."""
import asyncio
import json

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db


@pytest.fixture(params=["legacy", "published"])
def recalldesk(journey_db, monkeypatch, request):
    from app.db.models import GtmCompany, GtmContact, Venture
    from app.db.supymem_models import Deployment, FounderApp, IdeaRevision, IdeaSession, ResearchDossier, VentureWaitlist, VentureWaitlistEntry
    from app.gtm import journey
    from core.clock import utcnow

    data, now = journey_db, utcnow()
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {
        "audience": "Dental clinic owners", "problem": "Missed dental appointments", "confirmed": True}})
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).name = "RecallDesk"
        session.add(IdeaSession(id="idea_recall", workspace_id=data["wid"], venture_id=data["vid"], case="know", state="confirmed",
            current_revision_id="revision_recall", confirmed_revision_id="revision_recall", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id="revision_recall", session_id="idea_recall", workspace_id=data["wid"], venture_id=data["vid"],
            rev=1, source="founder", structured=json.dumps({"audience": "Dental clinic owners", "problem": "Missed dental appointments"}), created_at=now))
        session.add(ResearchDossier(id="research_recall", workspace_id=data["wid"], venture_id=data["vid"], revision_id="revision_recall",
            kind="deep", state="complete", summary="{}", created_at=now))
        if request.param == "legacy":
            row = journey._row(session, data["vid"], create=True)
            row.provenance_json = json.dumps({"icp": {"who": "Dental clinic owners", "confirmed": True,
                "search_queries": ["dental clinics companies"], "needs_input": False}, "discovered_for": "old-audience-key",
                "discovery_plan": ["dental clinics companies"]})
            session.add(GtmCompany(id="clinic_recall", workspace_id=data["wid"], canonical_name="Recall Dental", domain="recall.example", created_at=now))
            session.flush()
            for index, evidence in enumerate([
                {"found_at": "https://recall.example/contact", "why": "Legacy clinic result"},
                {"score": None, "signals": [{"published_at": now.isoformat()}]},
                {"score": "unknown", "signals": [None, "old signal", {"published_at": now.isoformat(), "quote": "We miss appointments."}]},
            ]):
                session.add(GtmContact(id=f"contact_recall_{index}", workspace_id=data["wid"], company_id="clinic_recall",
                    source=f"venture:{data['vid']}:public", enrichment_json=json.dumps(evidence), stage_changed_at=now, created_at=now))
            from app.db.models import VentureDocument
            session.add(VentureDocument(id="old_contact_note", venture_id=data["vid"], kind="gtm_contact_note", title="Old note",
                body_md="Saved before contact work", provenance_json="{}", created_at=now, updated_at=now))
        else:
            session.add(FounderApp(id="app_recall", workspace_id=data["wid"], venture_id=data["vid"], slug="recall-fixture",
                name="RecallDesk", kind="website", state="live", created_at=now, updated_at=now))
            session.flush()
            session.add(Deployment(id="deployment_recall", app_id="app_recall", workspace_id=data["wid"], venture_id=data["vid"],
                build_artifact_id="recall-artifact", target="supymem_static", state="live", version=1,
                url="https://recalldesk.example", started_at=now, live_at=now))
            session.add(VentureWaitlist(venture_id=data["vid"], workspace_id=data["wid"], enabled=True,
                consent_version="v1", enabled_by=data["actor"], updated_at=now))
            session.flush()
            session.add(VentureWaitlistEntry(id="signup_recall", venture_id=data["vid"], workspace_id=data["wid"], email="clinic@example.org",
                consent_version="v1", consent_text="Keep me informed", created_at=now))
        session.commit()
    return data, request.param


def test_legacy_and_live_b2b_status_and_get_route(recalldesk, monkeypatch):
    from app.api import gtm_god_ops
    from app.gtm import journey
    from fastapi.encoders import jsonable_encoder

    data, kind = recalldesk
    view = journey.status(data["wid"], data["vid"])
    assert view["icp"]["problem"] == "Missed dental appointments"
    assert view["icp"]["mode"] == "company"
    assert view["can_prepare"] and not view["discovery_started"]
    assert view["waitlist_signups"] == (1 if kind == "published" else 0)
    assert len(view["prospects"]) == (3 if kind == "legacy" else 0)
    assert all(prospect["score"] == 0 for prospect in view["prospects"])
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: data["wid"])
    assert jsonable_encoder(gtm_god_ops.journey_status(data["vid"], data["wid"], None, None)) == jsonable_encoder(view)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(gtm_god_ops.router)
    response = TestClient(app).get(f"/api/v1/ventures/{data['vid']}/gtm/journey")
    assert response.status_code == 200
    assert response.json() == jsonable_encoder(view)


def test_legacy_and_live_maintenance_does_not_start_discovery(recalldesk):
    from app.db.models import Job
    from app.gtm import journey

    data, _ = recalldesk
    assert asyncio.run(journey.maintain_all())["prepared"] == [data["vid"]]
    with data["factory"]() as session:
        assert not session.scalar(select(Job.id).where(Job.kind == "gtm.journey.prepare"))


@pytest.mark.parametrize("audience,problem", [
    ("Independent bicycle repair shops", "repair appointment scheduling"),
    ("First-time dog owners", "keeping up with puppy training"),
])
def test_status_batches_sent_and_contacted_counts(journey_db, audience, problem):
    from sqlalchemy import event
    from app.db.models import GtmCompany, GtmContact, GtmTouch
    from app.gtm import journey
    from core.clock import utcnow
    data, now = journey_db, utcnow()
    journey.update_icp(data["wid"], data["vid"], audience, problem=problem)
    with data["factory"]() as session:
        for index in range(2):
            session.add(GtmCompany(id=f"count_company{index}", workspace_id=data["wid"], canonical_name=f"Company {index}", created_at=now))
        session.flush()
        for index, company in enumerate((0, 0, 1, None)):
            session.add(GtmContact(id=f"count_contact{index}", workspace_id=data["wid"],
                company_id=f"count_company{company}" if company is not None else None, stage_changed_at=now, created_at=now))
        session.flush()
        cases = [(0, "out", "email", 0, now), (1, "out", "email", 0, now), (0, "out", "email", 0, now),
            (2, "out", "phone", 0, now), (3, "out", "email", 0, now), (2, "out", "email", 0, None),
            (2, "out", "email", 1, now), (2, "in", "email", 0, now)]
        for index, (contact, direction, channel, simulated, sent) in enumerate(cases):
            session.add(GtmTouch(id=f"count_touch{index}", workspace_id=data["wid"], venture_id=data["vid"],
                contact_id=f"count_contact{contact}", direction=direction, channel=channel, simulated=simulated, sent_at=sent, created_at=now))
        session.commit()
        expected = journey._contacted_businesses(session, data["wid"], data["vid"])
    statements = []
    def count_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT count(") and "gtm_touches.sent_at IS NOT NULL" in statement:
            statements.append(statement)
    engine = data["factory"].kw["bind"]
    event.listen(engine, "before_cursor_execute", count_sql)
    try:
        view = journey.status(data["wid"], data["vid"])
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    assert view["results"]["sent"] == 5
    assert view["auto_search"]["contacted"] == expected == 1
    assert len(statements) == 1


@pytest.mark.parametrize("audience", ["Independent bicycle repair shops", "First-time cat owners"])
def test_status_approved_drafts_offer_review_without_rechecking_send_capacity(journey_db, monkeypatch, audience):
    from app.chat import customers
    from app.gtm import journey

    def forbidden(*_args, **_kwargs):
        pytest.fail("Reading the next step must not calculate a sending offer")

    monkeypatch.setattr(customers, "_remaining_today", forbidden)
    view = {"icp": {"who": audience}, "gmail": {"connected": True},
        "drafts": [{"doc_id": "draft-read-only", "state": "draft", "review_state": "approved", "channel": "email"}],
        "batches": [{"approved_count": 1, "cap": 15}], "sends_left_today": 15}
    step = journey._next_step(journey_db["vid"], view)
    assert step["reason"] == "approved_drafts"
    assert "stay saved" in step["body_md"] and "sending limits" in step["body_md"]
    assert [action["action"] for action in step["actions"]] == ["gtm.status"]
    from app.core.local_mode import is_local
    if is_local():
        reply = customers.render(journey_db["vid"], view, show_saved=True)
        assert step["body_md"] in reply["body_md"]
        assert not any(action.get("action") == "gtm.send" or action.get("confirmation") for action in reply["actions"])


@pytest.mark.parametrize("audience", ["Independent bicycle repair shops", "First-time cat owners"])
@pytest.mark.parametrize("state", ["mail_missing", "queued", "delivery_unconfirmed"])
def test_local_status_names_your_saved_mail_account_without_sending(journey_db, monkeypatch, audience, state):
    from app.chat import customers
    from app.core.local_secrets import update_settings
    from app.gtm import journey

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("capabilities", {"send_email": True}, {})
    monkeypatch.setattr(customers, "_remaining_today", lambda *args: pytest.fail("Status must use saved sending limits"))
    draft = {"doc_id": "saved-mail", "state": "draft", "review_state": "approved", "truth_state": "queued" if state == "queued" else "approved"}
    if state == "delivery_unconfirmed":
        draft["state"] = state
    view = {"icp": {"who": audience}, "drafts": [draft], "gmail": {"connected": state != "mail_missing"}}
    step = journey._next_step(journey_db["vid"], view)
    if state == "queued":
        assert "your mail provider confirms it" in step["body_md"]
    else:
        assert "Settings > Keys and connections > Your mail account" in step["body_md"]
    assert "Gmail" not in step["body_md"]
    assert step["actions"]
    assert not any(action.get("action") == "gtm.send" or action.get("confirmation") for action in step["actions"])


@pytest.mark.parametrize("audience", ["Independent bicycle repair shops", "First-time cat owners"])
@pytest.mark.parametrize("state", ["queued", "running"])
def test_local_search_status_without_checkpoint_does_not_claim_worker_timing(journey_db, monkeypatch, audience, state):
    from app.chat import customers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(customers, "_remaining_today", lambda *args: pytest.fail("Progress must not calculate a sending offer"))
    reply = customers.render(journey_db["vid"], {"state": state, "icp": {"who": audience}})
    assert ("Your customer search is queued" if state == "queued" else "Searching") in reply["body_md"]
    assert "has not started" not in reply["body_md"] and "Starting soon" not in reply["body_md"]
    assert "%" not in reply["body_md"] and "Nothing is sent" in reply["body_md"]
    assert reply["actions"] == [{"label": "Check progress", "action": "gtm.status", "style": "primary"}]


@pytest.mark.parametrize("audience,problem", [
    ("Independent bicycle repair shops", "Schedule repair appointments"),
    ("First-time cat owners", "Plan daily pet care"),
])
def test_saved_context_refresh_keeps_explicit_founder_edits(journey_db, audience, problem):
    from app.gtm import journey

    base = {"who": audience, "problem": problem, "problem_basis": "From your saved context.", "competitors": [],
        "known_checks": {"problem": {"status": "known"}}}
    saved = {**base, "problem": "An earlier saved problem", "problem_basis": "Your saved idea", "confirmed": True}
    assert journey._journey_icp(journey_db["vid"], saved, base=base)["problem"] == problem
    for overrides in ({"problem_basis": "Founder supplied"}, {"chat_confirmed": True}):
        assert journey._journey_icp(journey_db["vid"], saved | overrides, base=base)["problem"] == saved["problem"]
    unconfirmed = base | {"known_checks": {"problem": {"status": "confirm"}}}
    assert journey._journey_icp(journey_db["vid"], saved, base=unconfirmed)["problem"] == saved["problem"]


@pytest.mark.parametrize("audience", ["Independent bicycle repair shops", "First-time cat owners"])
def test_local_unsent_booking_keeps_the_exact_api_recovery(journey_db, monkeypatch, audience):
    from app.api import gtm_god_ops
    from app.db.models import GtmContact
    from app.gtm import journey
    from core.clock import utcnow
    from fastapi import HTTPException

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    data, now = journey_db, utcnow()
    with data["factory"]() as session:
        session.add(GtmContact(id="unsent-booking", workspace_id=data["wid"], first_name=audience,
            source=f"venture:{data['vid']}:public", created_at=now, stage_changed_at=now))
        session.commit()
    with pytest.raises(HTTPException) as failure:
        gtm_god_ops.invoke(journey.contact_action, data["wid"], data["vid"], "unsent-booking", "call-booked", {})
    assert failure.value.status_code == 422
    assert failure.value.detail == {"code": "gtm_booked_requires_sent_message", "message":
        "Open Find customers, select this person's recorded outreach and mark it as sent before marking the call as booked."}


def test_status_live_legacy_50_contacts_has_no_outbound_io_and_constant_queries(recalldesk, monkeypatch, caplog):
    import logging
    import socket
    import urllib.request
    from time import perf_counter
    import httpx
    import requests
    from sqlalchemy import delete, event
    from sqlalchemy.engine import Engine
    from app.api import gtm_god_ops
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.db.models import GtmCompany, GtmContact, GtmOutreachSettings, GtmSuppression, VentureDocument
    from app.db.supymem_models import Deployment, FounderApp, ResearchDossier, ResearchFinding, VentureWaitlist, VentureWaitlistEntry
    from app.gtm import journey
    from app.gtm.god import llm
    from app.ventures import founder_memory
    from core.clock import utcnow
    from tests.unit.gtm.test_journey import _mail

    data, kind = recalldesk
    _mail(data)
    now = utcnow()
    with data["factory"]() as session:
        session.execute(delete(GtmContact).where(GtmContact.workspace_id == data["wid"]))
        row = journey._row(session, data["vid"], create=True)
        row.provenance_json = json.dumps({"icp": {"who": "Dental clinic owners", "confirmed": True}})
        if kind == "legacy":
            session.add(FounderApp(id="app_latency", workspace_id=data["wid"], venture_id=data["vid"], slug="latency",
                name="RecallDesk", kind="website", state="live", created_at=now, updated_at=now))
            session.flush()
            session.add(Deployment(id="deployment_latency", app_id="app_latency", workspace_id=data["wid"], venture_id=data["vid"],
                build_artifact_id="latency-artifact", target="supymem_static", state="live", version=1,
                url="https://recalldesk.example", started_at=now, live_at=now))
            session.add(VentureWaitlist(venture_id=data["vid"], workspace_id=data["wid"], enabled=True,
                consent_version="v1", enabled_by=data["actor"], updated_at=now))
            session.flush()
            session.add(VentureWaitlistEntry(id="signup_latency", venture_id=data["vid"], workspace_id=data["wid"],
                email="clinic@example.org", consent_version="v1", consent_text="Keep me informed", created_at=now))
        session.get(ResearchDossier, "research_recall").summary = json.dumps({"prior_art": {"companies": [
            {"name": "Excluded competitor", "website": "https://clinic0.example", "finding_ids": ["excluded_source"]}]},
            "review": {"competitor_checks": [{"name": "Competitor", "source_id": "competitor_source", "product_quote": "Appointment software"}]}})
        for source_id, excluded in [("excluded_source", True), ("competitor_source", False)]:
            session.add(ResearchFinding(id=source_id, dossier_id="research_recall", workspace_id=data["wid"], venture_id=data["vid"],
                category="competitor", title="Fixture", url="https://competitor.example", source="web",
                evidence=json.dumps({"founder_flag": "exclude" if excluded else None}), created_at=now))
        settings = session.get(GtmOutreachSettings, data["wid"])
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=data["wid"], created_at=now, updated_at=now)
            session.add(settings)
        settings.send_enabled, settings.sender_postal_address = True, "Synthetic fixture address"
        settings.aup_accepted_at = settings.dpa_accepted_at = now
        for index in range(50):
            session.add(GtmCompany(id=f"latency_company_{index}", workspace_id=data["wid"], canonical_name=f"Clinic {index}",
                domain=f"clinic{index}.example", created_at=now))
        session.flush()
        for index in range(50):
            session.add(GtmContact(id=f"latency_contact_{index}", workspace_id=data["wid"], company_id=f"latency_company_{index}",
                email=f"owner@clinic{index}.example", email_confidence="safe", recipient_geo="US", lawful_basis="legitimate_interest",
                source=f"venture:{data['vid']}:public", enrichment_json=json.dumps({"found_at": f"https://clinic{index}.example/contact"}),
                stage_changed_at=now, created_at=now))
            session.add(VentureDocument(id=f"latency_draft_{index}", venture_id=data["vid"], kind=outreach.KIND, title="Fixture draft",
                body_md="", provenance_json=json.dumps({"contact_id": f"latency_contact_{index}", "batch_id": f"batch_{index}",
                    "state": "draft", "review_state": "approved", "channel": "email", "to": f"owner@clinic{index}.example"}),
                created_at=now, updated_at=now))
        session.add(GtmSuppression(id="latency_suppression", workspace_id=data["wid"], email="OWNER@CLINIC1.EXAMPLE", reason="opt_out", created_at=now))
        session.commit()

    monkeypatch.setattr(journey, "_icp_seed", data["stored_seed"])
    outbound = []
    def forbidden(*args, **kwargs):
        outbound.append(True)
        raise AssertionError("journey GET attempted network/model/full-brief work")
    for target, name in [(httpx.Client, "send"), (httpx.AsyncClient, "send"), (httpx.HTTPTransport, "handle_request"), (httpx.AsyncHTTPTransport, "handle_async_request"),
        (requests.Session, "request"), (urllib.request, "urlopen"), (socket, "create_connection"), (outreach, "_brief"),
        (socket.socket, "connect"), (founder_memory, "memory_section_for_brief"), (llm, "json_chat"), (journey, "_discovery_model")]:
        monkeypatch.setattr(target, name, forbidden)
    monkeypatch.setattr(gtm_god_ops, "_auth_workspace", lambda *args: data["wid"])
    caplog.set_level(logging.DEBUG, logger=journey.__name__)
    statements = []
    def count_sql(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(Engine, "before_cursor_execute", count_sql)
    try:
        started = perf_counter()
        view = journey.status(data["wid"], data["vid"])
        elapsed = perf_counter() - started
        first_count = len(statements)
        print(f"status latency: {elapsed * 1000:.1f}ms; SQL statements: {first_count}")
        from collections import Counter
        if not is_local():
            assert first_count <= 50, f"{first_count} SQL statements exceeds status budget: {Counter(statements).most_common(4)}"
        assert elapsed < 3, f"status took {elapsed:.3f}s for 50 contacts"  # the N+1 bug took ~16s; CI runners vary around 1s
        assert len(view["prospects"]) == len(view["batches"]) == 50
        assert view["gmail"]["connected"]
        assert view["icp"]["problem"] == "Missed dental appointments" and view["waitlist_signups"] == 1
        assert view["icp"]["competitors"] == [{"name": "Competitor", "domain": "https://competitor.example"}]
        hidden = next(contact for contact in view["prospects"] if contact["id"] == "latency_contact_1")
        assert hidden["email"] is None and hidden["blocked_reason"] == "suppressed_or_replied"
        assert next(contact for contact in view["prospects"] if contact["id"] == "latency_contact_0")["eligible"]
        statements.clear()
        routed = gtm_god_ops.journey_status(data["vid"], data["wid"], None, None)
        assert routed == view and len(statements) <= first_count
        with data["factory"]() as session:
            session.execute(delete(GtmContact).where(GtmContact.workspace_id == data["wid"], GtmContact.id != "latency_contact_1"))
            session.commit()
        statements.clear()
        assert len(journey.status(data["wid"], data["vid"])["prospects"]) == 1
        assert first_count <= len(statements), Counter(statements).most_common(4)
    finally:
        event.remove(Engine, "before_cursor_execute", count_sql)
    assert not outbound
    records = [record for record in caplog.records if record.message == "gtm_journey_status_phase"]
    assert {record.phase for record in records} >= {"authorization", "stored_state", "icp", "prospects", "drafts", "replies", "mailbox", "settings", "outbound_policy", "batches", "customer_start", "contact_work", "total"}
    assert all(record.duration_ms >= 0 and not hasattr(record, "venture_id") for record in records)
    assert all(record.levelno == logging.DEBUG for record in records)
