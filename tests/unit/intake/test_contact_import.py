"""Offline contact-import parser, persisted preview and journey boundaries."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.gtm.test_journey import journey_db as journey_db
from tests.unit.test_local_mode import local_owner_app as local_owner_app


@pytest.mark.parametrize("idea,address", [
    ("Equipment maintenance scheduling for factories", "buyer@factory.example.invalid"),
    ("Shared garden planning for families", "gardener@garden.example.invalid"),
])
def test_live_local_contact_import_saves_one_consented_reserved_address(local_owner_app, monkeypatch, idea, address):
    from app.core.local_owner import ensure_owner
    from app.core import local_mode
    from app.db.models import GtmContact, Venture
    from app.intake import contacts
    from app.state import db
    from core.local_security import _proxy_secret
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    requests = []
    real_client = httpx.AsyncClient
    def dns(request):
        requests.append(request)
        return httpx.Response(200, json={"Status": 3})
    monkeypatch.setattr(contacts.httpx, "AsyncClient", lambda **kwargs: real_client(**(kwargs | {"transport": httpx.MockTransport(dns)})))
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        client.headers.pop("X-Local-Proxy-Secret", None)
        assert client.post("/ventures", json={"raw_idea": idea, "start": False}).status_code == 403
        client.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode()
        data = seed(db(), actor=ensure_owner().id)
        with db()() as session:
            session.get(Venture, data["vid"]).raw_idea = idea
            session.commit()
        path = f'/ventures/{data["vid"]}/gtm/contacts/import'
        preview = client.post(path + "/preview", json={"text": f"email,country\n{address},US", "format": "csv"})
        assert preview.status_code == 200
        body = {"preview_id": preview.json()["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"}
        refused = client.post(path, json={**body, "lawful_basis_attested": False})
        assert refused.status_code == 422
        saved = client.post(path, json=body)
        assert saved.status_code == 200
        assert len(saved.json()["contact_ids"]) == 1
        assert client.post(path, json=body).json()["contact_ids"] == saved.json()["contact_ids"]
        assert requests == []
        with db()() as session:
            contact = session.get(GtmContact, saved.json()["contact_ids"][0])
            assert contact.email == address and contact.contact_provenance == "founder_import"
            assert contact.lawful_basis == "consent" and contact.email_confidence == "guess"
        assert any(item["contact_id"] == saved.json()["contact_ids"][0]
            for item in client.get(f'/ventures/{data["vid"]}/gtm/contacts').json()["items"])


@pytest.mark.parametrize("address", ["buyer@warehouse.example.invalid", "gardener@garden.example.invalid"])
def test_reserved_test_contacts_never_need_dns_or_get_verified(journey_db, monkeypatch, address):
    from app.intake import contacts

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    domain = address.split("@", 1)[1]
    def no_network(**kwargs):
        pytest.fail("Your reserved test contact must not make a DNS request")
    monkeypatch.setattr(contacts.httpx, "AsyncClient", no_network)
    assert asyncio.run(contacts.check_domains({domain})) == {domain: "unknown"}


@pytest.mark.parametrize("audience,address", [
    ("Industrial procurement teams", "lee@factory.example"),
    ("Family cycling groups", "jo@cycling.example"),
])
def test_conn_regression_import_does_not_start_or_claim_drafts(journey_db, monkeypatch, audience, address):
    from app.chat.assets_in_chat import commit
    from app.gtm import journey
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        session.get(Venture, journey_db["vid"]).raw_idea = audience
        session.commit()
    preview = _preview(journey_db, monkeypatch, f"name,email\nLee Rowan,{address}")
    calls = []
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: calls.append(args))
    reply = commit(journey_db["vid"], {"preview_id": preview["preview_id"],
        "lawful_basis_attested": True, "lawful_basis": "consent"}, journey_db["actor"])
    assert calls == []
    assert "ready for your review" not in reply["body_md"] and "prepare follow-up" not in reply["body_md"]
    assert "Imported **1** contact" in reply["body_md"] and reply["actions"]


def _module():
    from app.intake import contacts
    return contacts


def _preview(d, monkeypatch, text, *, format="csv", mapping=None):
    contacts = _module()
    async def mx(domains):
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", mx)
    return asyncio.run(contacts.preview(d["wid"], d["vid"], text, format=format, mapping=mapping, actor=d["actor"]))


def _commit(d, view, **kwargs):
    return _module().commit(d["wid"], d["vid"], view["preview_id"], actor=d["actor"],
        lawful_basis_attested=True, lawful_basis="consent", **kwargs)


def _commit_and_draft(d, view, **kwargs):
    """Import, then explicitly request drafts for the drafting and eligibility fixtures."""
    from app.gtm import journey

    result = _commit(d, view, **kwargs)
    if not result["journey"].get("drafts"):
        journey._draft_ready(d["wid"], d["vid"])
    return result | {"journey": journey.status(d["wid"], d["vid"], authorize=False),
                     "drafts_status": _module()._prepare_drafts(d["wid"], d["vid"])}


def test_mapped_csv_preserves_history_and_previews_bad_rows(journey_db):
    contacts = _module()
    text = ('Person,Address,Business,State,Date,Country\n'
            'Ada,ADA@bakery.example,Ada Bakery,contacted,2026-01-01,ca\n'
            'Duplicate,ada@bakery.example,Bakery,new,,CA\n'
            'Bad,ada..x@bakery.example,Bakery,replied,not-a-date,Canada\n'
            'Short,bob@bakery.example\n')
    parsed = contacts.parse_contacts(text, mapping={"name": "Person", "email": "Address", "company": "Business", "status": "State", "last_contacted": "Date"})
    rows = parsed["rows"]
    assert rows[0]["email"] == "ada@bakery.example" and rows[0]["country"] == "CA"
    assert rows[0]["last_contacted"] == "2026-01-01T00:00:00+00:00" and rows[1]["duplicate"]
    assert len(rows[2]["errors"]) == 3 and rows[3]["errors"] == ["This row has a different number of columns."]


@pytest.mark.parametrize("text,mapping,error", [
    ("", None, "non-empty"),
    ("email,email\na@one.example,b@two.example", None, "unique"),
    ("name\nAda", None, "Map an email"),
    ("email\na@one.example", {"email": "wrong"}, "Choose a column"),
    ("email\n" + "\n".join(f"a{i}@one.example" for i in range(2001)), None, "2,000"),
    ("email\n" + "x" * (2 * 1024 * 1024), None, "2 MB"),
], ids=["empty", "duplicate-columns", "missing-identity", "unknown-mapping", "row-limit", "byte-limit"])
def test_import_input_bounds_and_mapping_errors(journey_db, text, mapping, error):
    contacts = _module()
    with pytest.raises(contacts.ContactImportError, match=error):
        contacts.parse_contacts(text, mapping=mapping)


def test_paste_handles_names_linkedin_and_dedupes(journey_db):
    rows = _module().parse_contacts('Ada Baker <ada@bakery.example>\nada@bakery.example\nhttps://www.linkedin.com/in/ada/\nbad email', format="paste")["rows"]
    assert rows[0]["name"] == "Ada Baker" and rows[1]["duplicate"]
    assert rows[2]["email_check"] == "not_applicable" and rows[2]["linkedin_url"].endswith("/ada/")
    assert rows[3]["errors"]
    assert len(_module().parse_contacts('Ada <ada@bakery.example>, Bob <bob@bakery.example>', format="paste")["rows"]) == 2
    assert _module().parse_contacts('email,notes\nada@bakery.example,unsupported\x00text')["rows"][0]["errors"]
    with pytest.raises(_module().ContactImportError, match="UTF-8"):
        _module().parse_contacts('email\nada@bakery.example\ud800')


def test_mx_queries_are_domain_only_and_null_or_unavailable_is_not_verified(journey_db, monkeypatch):
    contacts, requests, real_client = _module(), [], httpx.AsyncClient
    def reply(request):
        requests.append(request)
        name = request.url.params["name"]
        if name == "working.example":
            data = {"Status": 0, "Answer": [{"type": 15, "data": "10 mail.working.example."}]}
        elif name == "null.example":
            data = {"Status": 0, "Answer": [{"type": 15, "data": "0 ."}]}
        elif name == "unavailable.example":
            return httpx.Response(503)
        else:
            data = {"Status": 3}
        return httpx.Response(200, json=data)
    monkeypatch.setattr(contacts.httpx, "AsyncClient", lambda **kwargs: real_client(**(kwargs | {"transport": httpx.MockTransport(reply)})))
    checks = asyncio.run(contacts.check_domains({"working.example", "null.example", "missing.example", "unavailable.example"}))
    assert checks == {"working.example": "mx", "null.example": "missing_mx", "missing.example": "missing_mx", "unavailable.example": "unknown"}
    assert len(requests) == 4 and all(r.url.host == "cloudflare-dns.com" and r.url.params["type"] == "MX" and "@" not in str(r.url) for r in requests)


def test_preview_requires_attestation_commit_is_idempotent_and_stage_updates(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.gtm import journey
    d, contacts = journey_db, _module()
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    view = _preview(d, monkeypatch, 'name,email,company,country\nAda,ada@bakery.example,Ada Bakery,CA\nAda,ada@bakery.example,Bakery,CA\nBad,not-an-email,Bakery,CA')
    assert (view["valid_rows"], view["duplicates"], view["invalid_rows"]) == (1, 1, 1)
    with d["factory"]() as session:
        assert not list(session.scalars(select(GtmContact)))
    with pytest.raises(contacts.ContactImportError, match="lawful basis"):
        contacts.commit(d["wid"], d["vid"], view["preview_id"], actor=d["actor"], lawful_basis_attested=False, lawful_basis="consent")
    result = _commit(d, view)
    repeated = _commit(d, view)
    assert (result["imported"], result["skipped"]) == (1, 2) and repeated["contact_ids"] == result["contact_ids"]
    assert result["founder_stage"]["stage"] == "outreach" and len(result["journey"]["prospects"]) == 1
    assert not result["journey"]["drafts"] and not repeated["journey"]["drafts"]
    with d["factory"]() as session:
        contact = session.get(GtmContact, result["contact_ids"][0])
        assert contact.contact_provenance == "founder_import" and contact.source == f"venture:{d['vid']}:founder_import"
        assert contact.lawful_basis == "consent" and contact.email_confidence == "risky"
        assert json.loads(contact.enrichment_json)["founder_import"]["lawful_basis_attested"] is True
        assert not list(session.scalars(select(GtmTouch)))
        assert session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == d["vid"], VentureDocument.kind == "founder_stage"))


def test_unknown_mx_import_stays_visible_but_cannot_be_emailed(journey_db, monkeypatch):
    d, contacts = journey_db, _module()
    async def unknown(domains):
        return {domain: "unknown" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", unknown)
    view = asyncio.run(contacts.preview(d["wid"], d["vid"], 'email,country\nada@bakery.example,CA', format="csv", mapping=None, actor=d["actor"]))
    result = _commit_and_draft(d, view)
    assert result["imported"] == 1 and not result["journey"]["drafts"]
    assert result["journey"]["prospects"][0]["blocked_reason"] == "email_evidence_required"


def test_500_imported_contacts_without_an_audience_remain_visible_and_replay_safe(journey_db, monkeypatch):
    from app.db.models import GtmTouch
    from app.gtm import journey
    d = journey_db
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {"idea": {"offer": "Bakery order board", "confirmed": True}})
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    text = "name,email,company,status,last_contacted,country\n" + "\n".join(
        f"Founder {index},owner{index}@bakery.example,Bakery {index},{('contacted', 'replied', 'interested', 'new')[index % 4]},2026-01-01,CA"
        for index in range(500))
    view = _preview(d, monkeypatch, text)
    result, replay = _commit(d, view), _commit(d, view)
    assert result["imported"] == 500 and replay["contact_ids"] == result["contact_ids"]
    assert result["journey"]["icp"]["who"] == "" and result["journey"]["icp"]["mode"] == "company"
    assert len(result["journey"]["prospects"]) == len(replay["journey"]["prospects"]) == 500
    assert {draft["doc_id"] for draft in result["journey"]["drafts"]} == {draft["doc_id"] for draft in replay["journey"]["drafts"]}
    assert not result["journey"]["drafts"]
    assert {prospect.get("continuation_action") for prospect in result["journey"]["prospects"]} >= {"follow_up", "reply", "handoff"}
    assert result["journey"]["results"]["sent"] == result["journey"]["results"]["replied"] == 0
    with d["factory"]() as session:
        assert not list(session.scalars(select(GtmTouch)))


def test_imports_bypass_discovery_evidence_but_keep_email_and_suppression_gates(journey_db, monkeypatch):
    from app.db.models import GtmContact
    from app.gtm import journey
    d = journey_db
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {
        "idea": {"audience": "Independent bakery owners", "problem": "Bakery orders", "confirmed": True},
        "research": {"competitors": [{"name": "Bakery", "domain": "https://bakery.example"}]}})
    result = _commit_and_draft(d, _preview(d, monkeypatch, 'email,company,country\nada@bakery.example,Bakery,CA'))
    assert len(result["journey"]["prospects"]) == 1
    with d["factory"]() as session:
        contact = session.get(GtmContact, result["contact_ids"][0])
        evidence = json.loads(contact.enrichment_json)
        evidence.update(website="https://bakery.example", signals=[{"published_at": "2020-01-01"}])
        contact.enrichment_json = json.dumps(evidence)
        assert journey._block(session, d["wid"], contact) is None
        evidence["founder_import"]["email_check"] = "unknown"
        contact.enrichment_json = json.dumps(evidence)
        assert journey._block(session, d["wid"], contact) == "email_evidence_required"
        evidence["founder_import"]["email_check"] = "mx"
        evidence["founder_import"]["lawful_basis_attested"] = False
        contact.enrichment_json = json.dumps(evidence)
        assert journey._block(session, d["wid"], contact) == "email_evidence_required"
        contact.do_not_contact = 1
        assert journey._block(session, d["wid"], contact) == "suppressed_or_replied"


@pytest.mark.parametrize("audience,email", [("Warehouse dispatch teams", "known@logistics.example"),
    ("People growing balcony gardens", "known@garden.example")])
def test_import_and_replay_save_contacts_without_unrequested_drafts(journey_db, monkeypatch, audience, email):
    from app.gtm import journey
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], audience, problem="Coordinate the next step", start=False)
    view = _preview(d, monkeypatch, f'email,country\n{email},CA')
    calls = []
    monkeypatch.setattr(journey, "_draft_ready", lambda *args: calls.append(args))
    result = _commit(d, view)
    resumed = _commit(d, view)
    assert result["imported"] == 1 and resumed["contact_ids"] == result["contact_ids"]
    assert calls == [] and not resumed["journey"]["drafts"]
    assert "ask" in result["message"].lower()


@pytest.mark.parametrize("audience,email", [("Warehouse dispatch teams", "known@logistics.example"),
    ("People growing balcony gardens", "known@garden.example")])
def test_import_receipt_survives_separately_requested_draft_failure(journey_db, monkeypatch, audience, email):
    from app.gtm import journey
    d = journey_db
    journey.update_icp(d["wid"], d["vid"], audience, problem="Coordinate the next step", start=False)
    view = _preview(d, monkeypatch, f'email,country\n{email},CA')
    real_draft = journey._draft_ready
    def unavailable(*args):
        raise RuntimeError("Private provider details must not escape.")
    monkeypatch.setattr(journey, "_draft_ready", unavailable)
    result = _module().commit(d["wid"], d["vid"], view["preview_id"], actor=d["actor"],
                             lawful_basis_attested=True, lawful_basis="consent")
    assert result["imported"] == 1 and result["drafts_status"] == "not_requested"
    assert not result["journey"]["drafts"]
    assert "Private provider" not in str(result)
    with pytest.raises(RuntimeError, match="Private provider"):
        journey._draft_ready(d["wid"], d["vid"])
    monkeypatch.setattr(journey, "_draft_ready", real_draft)
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    resumed = _commit_and_draft(d, view)
    expected_drafts = int(resumed["journey"]["icp"]["mode"] != "consumer")
    assert resumed["drafts_status"] == ("ready" if expected_drafts else "pending")
    assert resumed["contact_ids"] == result["contact_ids"]
    assert len(resumed["journey"]["drafts"]) == expected_drafts


def _unknown_contact(d, monkeypatch, email="ada@bakery.example"):
    contacts = _module()
    async def unknown(domains):
        return {domain: "unknown" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", unknown)
    view = asyncio.run(contacts.preview(d["wid"], d["vid"], f'email,country\n{email},CA', format="csv", mapping=None, actor=d["actor"]))
    return _commit_and_draft(d, view)["contact_ids"][0]


@pytest.mark.parametrize("audience,email", [
    ("Warehouse dispatch teams", "known@logistics.example"),
    ("People growing balcony gardens", "known@garden.example"),
])
def test_unknown_mx_retry_is_bounded_recoverable_and_approval_gated(journey_db, monkeypatch, audience, email):
    from app.db.models import GtmContact, GtmTouch, Venture
    from app.gtm import journey
    d, contacts = journey_db, _module()
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).raw_idea = "A service for " + audience
        session.commit()
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    cid = _unknown_contact(d, monkeypatch, email)
    still_unknown = asyncio.run(contacts.verify_email(d["wid"], d["vid"], cid, actor=d["actor"]))
    assert still_unknown["email_check"] == "unknown" and not still_unknown["journey"]["drafts"]
    assert still_unknown["journey"]["prospects"][0]["blocked_reason"] == "email_evidence_required"
    assert journey.draft_for_review(d["vid"], contact_id=cid)["kind"] == "needs_you"
    async def mx(domains):
        assert domains == {email.split("@", 1)[1]}
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", mx)
    verified = asyncio.run(contacts.verify_email(d["wid"], d["vid"], cid, actor=d["actor"]))
    assert not verified["journey"]["drafts"]
    assert journey.draft_for_review(d["vid"], contact_id=cid)["kind"] == "text"
    verified["journey"] = journey.status(d["wid"], d["vid"], authorize=False)
    assert verified["email_check"] == "mx" and len(verified["journey"]["drafts"]) == 1
    assert verified["journey"]["drafts"][0]["review_state"] == "draft" and verified["journey"]["drafts"][0]["requires_batch_approval"]
    assert verified["journey"]["results"]["sent"] == 0
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert contact.lawful_basis == "consent" and contact.email_confidence == "risky"
        assert contact.gmail_thread_id is None and contact.replied_at is None and not list(session.scalars(select(GtmTouch)))


def test_mx_retry_rejects_stale_saved_email_and_foreign_contact(journey_db, monkeypatch):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import GtmContact, Venture
    from fastapi import HTTPException
    from core.clock import utcnow
    d, contacts = journey_db, _module()
    cid = _unknown_contact(d, monkeypatch)
    async def change_during_check(domains):
        assert domains == {"bakery.example"}
        with d["factory"]() as session:
            session.get(GtmContact, cid).email = "changed@bakery.example"
            session.commit()
        return {"bakery.example": "mx"}
    monkeypatch.setattr(contacts, "check_domains", change_during_check)
    with pytest.raises(contacts.ContactImportError, match="changed"):
        asyncio.run(contacts.verify_email(d["wid"], d["vid"], cid, actor=d["actor"]))
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert json.loads(contact.enrichment_json)["founder_import"]["email_check"] == "unknown" and contact.email_confidence == "guess"
        session.add(Venture(id="ven_verify_sibling", workspace_id=d["wid"], name="Sibling", raw_idea="Bakery", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    ensure_subject_person("usr_contact_foreign")
    with pytest.raises(HTTPException) as error:
        asyncio.run(contacts.verify_email(d["wid"], d["vid"], cid, actor="usr_contact_foreign"))
    assert error.value.status_code == 404
    with pytest.raises(contacts.ContactImportError, match="unavailable"):
        asyncio.run(contacts.verify_email(d["wid"], "ven_verify_sibling", cid, actor=d["actor"]))


def test_imported_conversations_offer_manual_continuation_and_no_fake_delivery(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    from app.gtm import journey
    d = journey_db
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    view = _preview(d, monkeypatch, 'name,email,status,last_contacted,country\nAda,ada@bakery.example,contacted,2026-01-01,CA\nBob,bob@bakery.example,replied,2026-01-01,CA\nCal,cal@bakery.example,interested,2026-01-01,CA\nDeb,deb@bakery.example,contacted,,CA')
    result = _commit(d, view)
    assert not result["journey"]["drafts"]
    journey._draft_ready(d["wid"], d["vid"])
    result["journey"] = journey.status(d["wid"], d["vid"], authorize=False)
    assert {d["continuation"] for d in result["journey"]["drafts"]} == {"follow_up", "reply", "handoff"}
    assert all(d["channel"] == "manual" and d["state"] == "draft" for d in result["journey"]["drafts"])
    assert result["journey"]["results"]["sent"] == result["journey"]["results"]["replied"] == 0
    assert any(p["continuation_action"] == "review_history" for p in result["journey"]["prospects"])
    assert all(b["draft_ids"] == [] for b in result["journey"]["batches"])
    with d["factory"]() as session:
        assert not list(session.scalars(select(GtmTouch)))
        assert all(c.gmail_thread_id is None for c in session.scalars(select(GtmContact)))


@pytest.mark.parametrize("audience,address,profile", [
    ("Warehouse dispatchers", "ada@warehouse.example", "warehouse-ada"),
    ("Family gardeners", "jo@garden.example", "garden-jo"),
])
def test_same_workspace_sibling_contact_is_not_reassigned(journey_db, monkeypatch, audience, address, profile):
    from app.db.models import GtmContact, Venture
    from core.clock import utcnow
    d = journey_db
    with d["factory"]() as session:
        session.add(Venture(id="ven_contact_sibling", workspace_id=d["wid"], name="Sibling", raw_idea=audience, stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.add(GtmContact(id="gtm_contact_sibling", workspace_id=d["wid"], email=address, source="venture:ven_contact_sibling:founder", stage_changed_at=utcnow(), created_at=utcnow()))
        session.commit()
    view = _preview(d, monkeypatch, f'email\n{address}')
    assert view["duplicates"] == 1
    assert _commit(d, view)["imported"] == 0
    with d["factory"]() as session:
        assert session.get(GtmContact, "gtm_contact_sibling").source == "venture:ven_contact_sibling:founder"
    first = _preview(d, monkeypatch, f'https://linkedin.com/in/{profile.upper()}', format="paste")
    assert _commit(d, first)["imported"] == 1
    repeat = _preview(d, monkeypatch, f'https://www.linkedin.com/in/{profile}/', format="paste")
    assert repeat["duplicates"] == 1 and _commit(d, repeat)["imported"] == 0


def test_history_edits_correct_unknown_dates_and_replace_manual_drafts(journey_db, monkeypatch):
    from app.gtm import journey
    d, contacts = journey_db, _module()
    monkeypatch.setattr(journey.outreach, "site_url", lambda vid: None)
    result = _commit_and_draft(d, _preview(d, monkeypatch, 'email,status,country\nada@bakery.example,contacted,CA'))
    cid = result["contact_ids"][0]
    assert not result["journey"]["drafts"] and result["journey"]["prospects"][0]["continuation_action"] == "review_history"
    def update(status, date=None):
        result = contacts.update_history(d["wid"], d["vid"], cid, actor=d["actor"], status=status, last_contacted=date)
        journey._draft_ready(d["wid"], d["vid"])
        return result | {"journey": journey.status(d["wid"], d["vid"], authorize=False)}
    view = update("contacted", "2026-01-01")
    assert [draft["continuation"] for draft in view["journey"]["drafts"]] == ["follow_up"]
    view = update("replied", "2026-01-01")
    assert [draft["continuation"] for draft in view["journey"]["drafts"]] == ["reply"]
    assert [draft["doc_id"] for draft in update("replied", "2026-01-01")["journey"]["drafts"]] == [draft["doc_id"] for draft in view["journey"]["drafts"]]
    assert [draft["continuation"] for draft in update("interested")["journey"]["drafts"]] == ["handoff"]
    corrected = update("new")
    assert not any(draft["channel"] == "manual" for draft in corrected["journey"]["drafts"])
    assert corrected["journey"]["prospects"][0]["last_contacted"] is None
    with pytest.raises(contacts.ContactImportError, match="past date"):
        update("contacted", "2050-01-01")


def test_history_cannot_clear_recorded_reply_suppression_or_steal_contact(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmTouch, Venture
    from core.clock import utcnow
    d, contacts = journey_db, _module()
    result = _commit(d, _preview(d, monkeypatch, 'email,status,country\nada@bakery.example,replied,CA'))
    cid, now = result["contact_ids"][0], utcnow()
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        contact.do_not_contact, contact.unsubscribed_at = 1, now
        contact.replied_at, contact.gmail_thread_id, contact.times_emailed = now, "verified-existing-thread", 3
        session.add(GtmTouch(id="touch_contact_real", workspace_id=d["wid"], venture_id=d["vid"], contact_id=cid,
            direction="in", channel="email", body_text="Stop emailing", simulated=0, created_at=now))
        session.add(Venture(id="ven_history_sibling", workspace_id=d["wid"], name="Sibling", raw_idea="Bakery", stage="draft", created_at=now, updated_at=now))
        session.commit()
    with pytest.raises(contacts.ContactImportError, match="unavailable"):
        contacts.update_history(d["wid"], "ven_history_sibling", cid, actor=d["actor"], status="new", last_contacted=None)
    view = contacts.update_history(d["wid"], d["vid"], cid, actor=d["actor"], status="new", last_contacted=None)
    assert view["journey"]["prospects"][0]["blocked_reason"] == "suppressed_or_replied"
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert contact.replied_at is not None and contact.do_not_contact and contact.unsubscribed_at
        assert contact.gmail_thread_id == "verified-existing-thread" and contact.times_emailed == 3
        assert len(list(session.scalars(select(GtmTouch)))) == 1


@pytest.mark.parametrize("audience,domain", [
    ("Industrial maintenance teams", "maintenance.example"),
    ("Parents planning family hikes", "hiking.example"),
])
@pytest.mark.parametrize("conflict", ["email", "linkedin", "two_contacts"])
@pytest.mark.parametrize("already_attested", [False, True])
def test_import_rejects_conflicting_identity_without_transferring_permission(journey_db, monkeypatch, audience, domain, conflict, already_attested):
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from core.clock import utcnow

    data, contacts = journey_db, _module()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {
        "idea": {"audience": audience, "problem": "Keep plans up to date", "confirmed": True}, "research": {}})
    email = "requested@" + domain
    linkedin = "https://www.linkedin.com/in/requested-person"
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        contact = GtmContact(id="existing_identity", workspace_id=data["wid"],
            email=email if conflict != "email" else "different@" + domain,
            linkedin_url=linkedin if conflict != "linkedin" else "https://www.linkedin.com/in/different-person",
            source=f'venture:{data["vid"]}:public', email_confidence="guess",
            contact_provenance="public", enrichment_json='{"email_evidence":"guess"}',
            created_at=utcnow(), stage_changed_at=utcnow())
        if already_attested:
            contact.source = f'venture:{data["vid"]}:founder_import'
            contact.contact_provenance = "founder_import"
            contact.lawful_basis = "consent"
            contact.lawful_basis_at = utcnow()
            contact.enrichment_json = json.dumps({"founder_import": {"lawful_basis_attested": True, "status": "new"}})
            contact.do_not_contact = 1
        session.add(contact)
        if conflict == "two_contacts":
            contact.linkedin_url = "https://www.linkedin.com/in/different-person"
            session.add(GtmContact(id="other_identity", workspace_id=data["wid"], email="different@" + domain,
                linkedin_url=linkedin, source=f'venture:{data["vid"]}:public', email_confidence="guess",
                created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
        before = (contact.email, contact.linkedin_url, contact.source, contact.email_confidence,
            contact.contact_provenance, contact.lawful_basis, contact.enrichment_json)
        block = journey._block(session, data["wid"], contact)
        assert block
    preview = _preview(data, monkeypatch, f"email,linkedin_url,country\n{email},{linkedin},CA")
    with pytest.raises(contacts.ContactImportError, match="Contacts > Import"):
        _commit(data, preview)
    with data["factory"]() as session:
        contact = session.get(GtmContact, "existing_identity")
        assert (contact.email, contact.linkedin_url, contact.source, contact.email_confidence,
            contact.contact_provenance, contact.lawful_basis, contact.enrichment_json) == before
        assert journey._block(session, data["wid"], contact) == block


@pytest.mark.parametrize("audience,domain", [
    ("Industrial maintenance teams", "maintenance.example"),
    ("Parents planning family hikes", "hiking.example"),
])
@pytest.mark.parametrize("already_imported", [False, True])
def test_reimport_merges_history_without_clearing_reply_suppression_or_sends(journey_db, monkeypatch, audience, domain, already_imported):
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from core.clock import utcnow

    data = journey_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(journey.outreach, "_brief", lambda vid: {
        "idea": {"audience": audience, "problem": "Keep plans up to date", "confirmed": True}, "research": {}})
    email = "member@" + domain
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = audience
        session.add(GtmContact(id="history_identity", workspace_id=data["wid"], email=email,
            source=f'venture:{data["vid"]}:public', email_confidence="guess",
            created_at=utcnow(), stage_changed_at=utcnow()))
        session.commit()
    if already_imported:
        _commit(data, _preview(data, monkeypatch, f"email,status,country\n{email},new,CA"))
    result = _commit(data, _preview(data, monkeypatch, f"email,status,country\n{email},replied,CA"))
    assert result["contact_ids"] == ["history_identity"]
    with data["factory"]() as session:
        contact = session.get(GtmContact, "history_identity")
        assert contact.replied_at is not None and contact.times_emailed == 1
        assert json.loads(contact.enrichment_json)["founder_import"]["status"] == "replied"
        assert journey._block(session, data["wid"], contact) == "suppressed_or_replied"
        contact.do_not_contact = 1
        contact.unsubscribed_at = contact.bounced_at = contact.last_emailed_at = utcnow()
        contact.times_emailed = 4
        session.commit()
        before = (contact.replied_at, contact.unsubscribed_at, contact.bounced_at,
            contact.last_emailed_at, contact.times_emailed, contact.relationship_stage)
    _commit(data, _preview(data, monkeypatch, f"email,status,last_contacted,country\n{email},new,2026-01-01,CA"))
    with data["factory"]() as session:
        contact = session.get(GtmContact, "history_identity")
        assert contact.do_not_contact
        assert json.loads(contact.enrichment_json)["founder_import"]["status"] == "replied"
        assert (contact.replied_at, contact.unsubscribed_at, contact.bounced_at,
            contact.last_emailed_at, contact.times_emailed, contact.relationship_stage) == before


@pytest.mark.parametrize("domain,audience", [("supply.example", "warehouse purchasing teams"),
    ("bakery.example", "home bakers")])
def test_imported_email_still_needs_exact_approval_and_uses_normal_send(journey_db, monkeypatch, domain, audience):
    from app.autopilot import outreach
    from app.gtm import journey
    from tests.unit.gtm.test_journey import _mail, _review
    d = journey_db
    from app.db.models import Venture, CreditReservation
    with d["factory"]() as session:
        session.get(Venture, d["vid"]).raw_idea = "A service for " + audience
        session.commit()
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    result = _commit(d, _preview(d, monkeypatch, f'email,status,country\nada@{domain},new,CA\nbob@{domain},replied,CA'))
    assert not result["journey"]["drafts"]
    _mail(d)
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_secrets import update_settings
        from app.approvals import external
        update_settings("capabilities", {"send_email": True}, {})
        monkeypatch.setattr(external, "get_policy", lambda *_: pytest.fail("Your SMTP send needs no administrator rate"))
    assert journey.draft_for_review(d["vid"])["kind"] == "text"
    _, request = _review(d)
    sent = []
    def send(box, to, subject, body, thread):
        assert "I have your address from my own contacts." in body
        assert "reply" in body.lower() and "stop" in body.lower()
        assert "1 Founder Street" in body
        sent.append(to)
        return {"provider_message_id": "offline-import-receipt", "thread_id": "offline-import-thread"}
    monkeypatch.setattr(outreach, "_send_one", send)
    assert asyncio.run(outreach.send_batch(d["vid"], {"approved_by_founder": True, "batch_id": request["batch_id"]}, actor_subject=d["actor"]))["kind"] == "needs_you"
    assert sent == []
    view = asyncio.run(journey.send(d["wid"], d["vid"], request))
    assert sent == [f"ada@{domain}"] and view["results"]["sent"] == 1, view["receipt"]
    assert any(draft["channel"] == "manual" and draft["state"] == "draft" for draft in view["drafts"])
    from app.db.models import GtmContact, GtmTouch
    from app.gtm.contact_work import log_reply
    cid = next(item["id"] for item in view["prospects"] if item["email"] == f"ada@{domain}")
    log_reply(d["wid"], d["vid"], cid, "Please stop emailing me.", "opted_out")
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        history = (contact.replied_at, contact.unsubscribed_at, contact.bounced_at,
            contact.last_emailed_at, contact.times_emailed, contact.gmail_thread_id)
        touch_ids = set(session.scalars(select(GtmTouch.id).where(GtmTouch.contact_id == cid)))
        assert contact.do_not_contact and contact.unsubscribed_at and contact.times_emailed == 1
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.contact_id == cid,
            GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None)))
    reimported = _commit(d, _preview(d, monkeypatch, f'email,status,country\nada@{domain},new,CA'))
    assert reimported["contact_ids"] == [cid]
    with d["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert (contact.replied_at, contact.unsubscribed_at, contact.bounced_at,
            contact.last_emailed_at, contact.times_emailed, contact.gmail_thread_id) == history
        assert contact.do_not_contact and journey._block(session, d["wid"], contact) == "suppressed_or_replied"
        assert set(session.scalars(select(GtmTouch.id).where(GtmTouch.contact_id == cid))) == touch_ids
    assert journey.draft_for_review(d["vid"], contact_id=cid)["kind"] == "needs_you"
    assert sent == [f"ada@{domain}"]
    if is_local():
        with d["factory"]() as session:
            assert not list(session.scalars(select(CreditReservation)))


def test_preview_is_actor_bound_expires_and_rejects_viewers(journey_db, monkeypatch):
    from app.db.models import VentureDocument, WorkspaceMember
    from core.clock import utcnow
    d, contacts = journey_db, _module()
    view = _preview(d, monkeypatch, 'email\nada@bakery.example')
    with d["factory"]() as session:
        row = session.scalar(select(VentureDocument).where(VentureDocument.kind == contacts._preview_kind(d["actor"])))
        data = json.loads(row.provenance_json)
        data["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.provenance_json = json.dumps(data)
        session.commit()
    with pytest.raises(contacts.ContactImportError, match="expired"):
        _commit(d, view)
    with d["factory"]() as session:
        member = session.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"], WorkspaceMember.person_id == d["actor"]))
        member.role = "viewer"
        session.commit()
    with pytest.raises(ValueError, match="gtm_write_forbidden"):
        _commit(d, view)


def test_founder_api_handles_multipart_and_blocks_other_actors(journey_db, monkeypatch):
    from app.api.venture_assets import router
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from core.clock import utcnow
    d, contacts = journey_db, _module()
    async def mx(domains):
        return {domain: "mx" for domain in domains}
    monkeypatch.setattr(contacts, "check_domains", mx)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/ventures/{d['vid']}/gtm/contacts/import"
    headers = {"X-Subject": d["actor"]}
    reply = client.post(path + "/preview", headers=headers, files={"file": ("contacts.csv", b"email,country\nada@bakery.example,CA", "text/csv")})
    assert reply.status_code == 200, reply.text
    view = reply.json()
    ensure_subject_person("usr_contact_other")
    assert client.post(path, headers={"X-Subject": "usr_contact_other"}, json={"preview_id": view["preview_id"], "lawful_basis_attested": True}).status_code == 404
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="member_contact_other", workspace_id=d["wid"], person_id="usr_contact_other", role="contributor", created_at=utcnow()))
        session.commit()
    # Workspace membership does not grant another founder's preview token.
    assert client.post(path, headers={"X-Subject": "usr_contact_other"}, json={"preview_id": view["preview_id"], "lawful_basis_attested": True}).status_code == 409
    assert client.post(path, headers=headers, json={"preview_id": "changed", "lawful_basis_attested": True}).status_code == 409
    assert client.post(path, headers=headers, json={"preview_id": view["preview_id"], "lawful_basis_attested": False}).status_code == 422
    assert client.post(path, headers=headers, json={"preview_id": view["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"}).status_code == 200
