from __future__ import annotations

import asyncio
import csv
import io
import json
import socket

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import _mail, journey_db as journey_db
from tests.unit.gtm.test_venture_leads import DOMAINS
from tests.unit.intake.test_contact_import import _commit, _preview
from tests.unit.lead_library.test_service import entry_id, library as library


@pytest.fixture(params=list(zip(DOMAINS, (1, 20), ("gmail.com", "outlook.com"))), ids=("b2b", "consumer"))
def lead_case(journey_db, monkeypatch, request):
    from app.autopilot import outreach
    from app.db.models import Venture
    from app.gtm import journey

    domain_case, library_number, email_host = request.param
    venture_id, title, audience, problem, name, role = domain_case
    data = journey_db
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = title, f"{problem}. Audience: {audience}"
        session.commit()
    monkeypatch.setattr(outreach, "_brief", lambda vid: {
        "idea": {"audience": audience, "problem": problem, "confirmed": True}, "research": {},
    })
    monkeypatch.setattr(outreach, "site_url", lambda vid: None)
    monkeypatch.setattr("app.intake.contacts._prepare_drafts", lambda wid, vid: "ready")

    def no_network(*args, **kwargs):
        raise AssertionError("This probe must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    _mail(data)
    return data | {"title": title, "audience": audience, "problem": problem,
        "name": name, "role": role, "domain": venture_id.removeprefix("ven_") + ".example",
        "library_entry_id": entry_id(library_number), "email_host": email_host}


def import_rows(data, monkeypatch, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    preview = _preview(data, monkeypatch, output.getvalue())
    assert preview["valid_rows"] == len(rows)
    result = _commit(data, preview)
    assert result["imported"] == len(rows)
    return result["contact_ids"]


def test_csv_keeps_distinct_companies_using_shared_email_host(lead_case, monkeypatch):
    from app.db.models import GtmContact
    from app.gtm.leads import saved_leads

    data = lead_case
    companies = [data["title"] + " East", data["title"] + " West"]
    import_rows(data, monkeypatch, [
        {"name": data["name"] + f" {index}", "email": f"member{index}@{data['email_host']}",
            "company": company, "title": data["role"], "country": "US"}
        for index, company in enumerate(companies)
    ])
    leads = saved_leads(data["wid"], data["vid"])["items"]
    with data["factory"]() as session:
        contacts = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == data["wid"])))
        actual = {contact.company_id for contact in contacts}
    assert {lead["company"] for lead in leads} == set(companies) and len(actual) == 2


def test_csv_does_not_invent_a_role_when_title_is_blank(lead_case, monkeypatch):
    from app.db.models import GtmContact
    from app.gtm.leads import saved_leads

    data = lead_case
    import_rows(data, monkeypatch, [{"name": data["name"], "email": "member@" + data["domain"],
        "company": data["title"], "title": "", "country": "US"}])
    lead = saved_leads(data["wid"], data["vid"])["items"][0]
    assert lead["name"] == data["name"]
    assert not lead["role"], f"You supplied no title, but your lead is displayed as {lead['role']}"
    with data["factory"]() as session:
        dossier = json.loads(session.get(GtmContact, lead["id"]).dossier_json)
        assert not dossier["role"], "You supplied no title, but the dossier still invents one"


def test_search_keeps_different_people_on_the_same_profile_host(lead_case):
    from app.gtm.quality import dedupe
    from tests.unit.gtm.test_leads_to_drafts import _lead

    data = lead_case
    leads = [_lead(f"person-{index}", data["name"] + f" {index}", data["domain"])
        | {"kind": "person", "company": "", "role": data["role"],
            "profile_url": f"https://{data['domain']}/members/person-{index}",
            "source_url": f"https://{data['domain']}/members/person-{index}",
            "routes": [{"kind": "email", "email": f"person{index}@{data['domain']}",
                "source_url": f"https://{data['domain']}/members/person-{index}",
                "quote": f"Contact person{index}@{data['domain']}", "route_type": "published"}]}
        for index in range(2)]
    actual = dedupe(leads + [dict(leads[0])])
    assert {lead["id"] for lead in actual} == {lead["id"] for lead in leads}


def test_removing_a_lead_stops_an_already_reviewed_legacy_email(lead_case, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey, playbooks
    from app.gtm.leads import saved_leads
    from app.gtm.relationships.email_compliance import validate_send
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.supymem import composio_gmail

    data = lead_case
    contact_ids = import_rows(data, monkeypatch, [{"name": data["name"], "email": "member@" + data["domain"],
        "company": data["title"], "title": data["role"], "country": "US"}])

    async def pitch(*args, **kwargs):
        return data["problem"]

    async def drafts(*args, **kwargs):
        return {}

    monkeypatch.setattr(playbooks, "pitch", pitch)
    monkeypatch.setattr(outreach, "_llm_drafts", drafts)
    asyncio.run(outreach.draft_batch(data["vid"], n=1, review_only=True))
    draft = outreach.drafts(data["vid"], "draft")[0]
    assert draft["requires_batch_approval"] and not draft.get("requires_journey_review")
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_ids[0])
        validate_send(session, data["wid"], contact,
            draft["body"], outreach.founder_signature(data["vid"]))
        contact.do_not_contact = 1
        assert outreach._suppressed(session, data["wid"], contact)
        session.rollback()
    journey.patch_prospect(data["wid"], data["vid"], contact_ids[0], {"skip": True})
    assert saved_leads(data["wid"], data["vid"])["total"] == 0
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_ids[0])
        assert journey._block(session, data["wid"], contact) == "skipped"
        assert email_suppressed(session, data["wid"], contact, replied_ok=True, suppressed_emails=set())
    sent = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs:
        sent.append(kwargs["to"]) or {"provider_message_id": "offline-removal-receipt", "thread_id": "offline-removal-thread"})
    result = asyncio.run(outreach.send_batch(data["vid"], {
        "approved_by_founder": True, "batch_id": draft["batch_id"],
    }, actor_subject=data["actor"]))
    assert not sent, f"You removed the lead, but sending still called the provider: {sent}; {result}"
    saved = next(item for item in outreach.drafts(data["vid"]) if item["doc_id"] == draft["doc_id"])
    assert (saved["subject"], saved["body"]) == (draft["subject"], draft["body"])


@pytest.mark.hosted_only
def test_library_email_offer_requires_supported_source_disclosure(lead_case, library):
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import source_line
    from app.lead_library import service
    from app.lead_library.loader import _fill
    from app.lead_library.models import LeadLibraryEntry

    data = lead_case
    with data["factory"]() as session:
        entry = session.get(LeadLibraryEntry, data["library_entry_id"])
        assert entry.license_source and entry.email and entry.email_status not in service.PUBLISHED
        item = service._item(entry, [], {"function": None, "country": None})
    assert "email" not in item["channels"] and "company inbox email" not in item["channels"], (
        f"Your library card offers email without public or founder-import source evidence: {item}"
    )
    with data["factory"]() as session:
        entry = session.get(LeadLibraryEntry, data["library_entry_id"])
        entry.usable_channels = '["email"]'
        flags = entry.quality_flags
        entry.quality_flags = '["email_generic"]'
        assert not service.channels(entry)
        entry.quality_flags = flags
        session.commit()
    assert service.claim(data["wid"], data["vid"], [data["library_entry_id"]], data["actor"])["unavailable"] == 1
    with data["factory"]() as session:
        entry = session.get(LeadLibraryEntry, data["library_entry_id"])
        entry.email_status = "public"
        service._set_email_status(entry, "delivered")
        assert service.channels(entry) == ["email"]
        from tests.unit.lead_library.test_service import _rows
        row = next(row for row in _rows() if row["external_lead_id"] == entry.external_lead_id)
        _fill(entry, row, entry.email_hash, entry.email_status)
        assert service.channels(entry) == row["usable_channels"]
        entry.usable_channels = '["email"]'
        session.commit()
    claimed = service.claim(data["wid"], data["vid"], [data["library_entry_id"]], data["actor"])
    with data["factory"]() as session:
        contact = session.get(GtmContact, claimed["contact_ids"][0])
        assert source_line(contact).startswith("Your email address is listed publicly on ")
        entry = session.get(LeadLibraryEntry, data["library_entry_id"])
        row["source_url"] = "https://different." + data["domain"] + "/contacts"
        _fill(entry, row, entry.email_hash, entry.email_status)
        assert "email" not in service.channels(entry)
        entry.email_status = "public"
        service._set_email_status(entry, "bounced")
        assert service.channels(entry) == [channel for channel in row["usable_channels"] if channel != "email"]
        row["email"] = "changed@" + data["domain"]
        _fill(entry, row, None, "none")
        assert "email" not in service.channels(entry)


@pytest.mark.hosted_only
def test_explicit_library_add_restores_your_removed_lead(lead_case, library, monkeypatch):
    from app.db.models import GtmContact, Venture
    from app.gtm import journey
    from app.gtm.leads import saved_leads
    from app.gtm.relationships.suppression import email_suppressed
    from app.lead_library import service
    from app.lead_library.models import LeadLibraryEntry, LeadLibraryUsage
    from core.clock import utcnow

    data = lead_case
    with data["factory"]() as session:
        session.get(LeadLibraryEntry, data["library_entry_id"]).email_status = "public"
        session.commit()
    result = service.claim(data["wid"], data["vid"], [data["library_entry_id"]], data["actor"])
    assert result["claimed"] == 1
    contact_id = result["contact_ids"][0]
    journey.patch_prospect(data["wid"], data["vid"], contact_id, {"skip": True})
    assert saved_leads(data["wid"], data["vid"])["total"] == 0
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_id)
        contact.do_not_contact = 1
        contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note = "consent", utcnow(), "Requested a follow-up"
        evidence = journey._json(contact.enrichment_json)
        session.commit()
        before = (contact.source, contact.contact_provenance, contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note)
    monkeypatch.setattr("app.intake.contacts._prepare_drafts", lambda *args:
        pytest.fail("Restoring your lead must not start new drafting"))
    repeated = service.claim(data["wid"], data["vid"], [data["library_entry_id"]], data["actor"])
    leads = saved_leads(data["wid"], data["vid"])["items"]
    assert [lead["id"] for lead in leads] == [contact_id], f"Your requested add does not restore the lead: {repeated}"
    assert repeated["claimed"] == 1 and repeated["contact_ids"] == [contact_id]
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_id)
        assert (contact.source, contact.contact_provenance, contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note) == before
        assert journey._json(contact.enrichment_json) == evidence | {"skipped": False}
        assert contact.do_not_contact and email_suppressed(session, data["wid"], contact, replied_ok=True)
        assert len(session.scalars(select(LeadLibraryUsage).where(LeadLibraryUsage.entry_id == data["library_entry_id"])).all()) == 1
        other = Venture(id="ven_readd_other", workspace_id=data["wid"], name="Other venture", raw_idea=data["problem"],
            stage="draft", created_at=utcnow(), updated_at=utcnow())
        session.add(other)
        contact.enrichment_json = json.dumps(evidence)
        session.commit()
    elsewhere = service.claim(data["wid"], "ven_readd_other", [data["library_entry_id"]], data["actor"])
    assert elsewhere["already"] == 1 and not elsewhere["contact_ids"]
    with data["factory"]() as session:
        assert journey._json(session.get(GtmContact, contact_id).enrichment_json)["skipped"] is True


def test_received_reply_keeps_the_request_at_the_end(lead_case, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    from app.gtm import journey
    from core.clock import utcnow

    data = lead_case
    contact_ids = import_rows(data, monkeypatch, [{"name": data["name"], "email": "member@" + data["domain"],
        "company": data["title"], "title": data["role"], "country": "US"}])
    reply_text = (f"Here is some background about {data['problem'].lower()}. " * 15
        + "Please move our conversation to next Tuesday at 10 AM and reply to confirm.")
    assert len(reply_text) > 500
    now = utcnow()
    with data["factory"]() as session:
        session.get(GtmContact, contact_ids[0]).replied_at = now
        session.add(GtmTouch(id="received-hunt-reply", workspace_id=data["wid"], venture_id=data["vid"],
            contact_id=contact_ids[0], direction="in", channel="email", message_id="offline-reply-message",
            classification="human_neutral", body_text=reply_text, simulated=0, created_at=now))
        session.commit()
        assert session.get(GtmTouch, "received-hunt-reply").body_text == reply_text
    view = journey.status(data["wid"], data["vid"])
    reply = next(item for item in view["results"]["replies"] if item["id"] == "received-hunt-reply")
    assert reply["body"] == reply_text, "Your saved reply is cut off before the requested time and next step"
