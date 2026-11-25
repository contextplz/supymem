import base64
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.lead_library.test_service import entry_id, journey_db, library, published_library

pytestmark = [pytest.mark.hosted_only, pytest.mark.usefixtures("published_library")]

DOMAINS = [("ciphercloud.test", "Cipher Security", "Security Director"),
           ("meadowwear.test", "Meadow Apparel", "Marketing Manager")]


def _contact(session, data, domain, company_name, role, *, private=False):
    from app.db.models import GtmCompany, GtmContact, GtmTouch
    from core.clock import utcnow
    now = utcnow()
    contact_id = "feedback_" + domain + str(private)
    company = session.scalar(select(GtmCompany).where(GtmCompany.workspace_id == data["wid"], GtmCompany.domain == domain))
    if company is None:
        company = GtmCompany(id="company_" + contact_id, workspace_id=data["wid"], canonical_name=company_name, domain=domain, created_at=now)
        session.add(company)
        session.flush()
    contact = GtmContact(id=contact_id, company_id=company.id, workspace_id=data["wid"], email=("private" if private else "alex") + "@" + domain,
        first_name="Alex", last_name="Lee", role=role, source=f"venture:{data['vid']}:public",
        contact_provenance="founder_import" if private else None, recipient_geo="US", email_confidence="risky",
        times_emailed=1, last_emailed_at=now, stage_changed_at=now, created_at=now,
        enrichment_json=json.dumps(dict(found_at="https://" + domain + "/team", website="https://" + domain,
            person_name="Alex Lee", business=company_name, email_evidence="published")))
    session.add(contact)
    session.add(GtmTouch(id="touch_" + contact_id, workspace_id=data["wid"], venture_id=data["vid"], contact_id=contact_id,
        provider_thread_id="thread_" + contact_id, direction="out", channel="email", simulated=0, sent_at=now, created_at=now))
    session.flush()
    return contact


def _dsn(email, action, status, message_id):
    text = f"Final-Recipient: rfc822; {email}\r\nAction: {action}\r\nStatus: {status}\r\n"
    return {"id": message_id, "payload": {"parts": [{"mimeType": "message/delivery-status",
        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}]}}


@pytest.mark.parametrize("domain,company,role", DOMAINS)
@pytest.mark.parametrize("kind", ["delivery", "reply"])
def test_real_handlers_promote_public_outcomes_shared_but_not_private_imports(library, domain, company, role, kind):
    from app.gtm.email_learning import opaque
    from app.gtm.shared_models import SharedEmailOutcome
    from app.gtm.supymem import pipeline, workers
    from app.lead_library.models import LeadLibraryEntry as Entry
    from app.lead_library.service import claim
    for private in (True, False):
        with library["factory"]() as session:
            contact = _contact(session, library, domain, company, role, private=private)
            contact_id, email = contact.id, contact.email
            session.commit()
        if kind == "reply":
            assert pipeline.apply_reply(contact_id, body="Thanks for your email", classification="human_neutral", venture_id=library["vid"],
                provider_thread_id="thread_" + contact_id, provider_message_id="reply_" + contact_id)
            workers._apply_delivery_status(_dsn(email, "delivered", "2.0.0", "later_" + contact_id), {email: contact_id})
        else:
            message = _dsn(email, "delivered", "2.0.0", "receipt_" + contact_id)
            for repeat in range(2):
                assert workers._apply_delivery_status(message, {email: contact_id}) == []
        with library["factory"]() as session:
            entry = session.scalar(select(Entry).where(Entry.email_hash == opaque(email)))
            if private:
                assert entry is None
            else:
                assert entry.email == email and entry.email_status == ("replied" if kind == "reply" else "delivered")
                shared_id = entry.id
                events = list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.kind == kind, SharedEmailOutcome.address_hash == opaque(email))))
                assert len(events) == 1
    result = claim(library["wid2"], library["vid2"], [shared_id], library["actor2"])
    assert result["claimed"] == 1


@pytest.mark.parametrize("number", [1, 9])
@pytest.mark.parametrize("status", ["5.1.1", "5.2.1"])
def test_hard_bounce_immediately_blacklists_all_shared_copies_and_claimed_founders(library, number, status):
    from app.db.models import GtmContact
    from app.gtm.email_learning import candidate, globally_suppressed, opaque
    from app.gtm.supymem import workers
    from app.lead_library.models import LeadLibraryEntry as Entry
    from app.lead_library.service import _visible, claim, on_outcome
    from core.clock import utcnow
    ident = entry_id(number)
    contacts = {}
    for suffix in ("", "2"):
        result = claim(library["wid" + suffix], library["vid" + suffix], [ident], library["actor" + suffix])
        assert result["claimed"] == 1
        contacts[suffix] = result["contact_ids"][0]
    with library["factory"]() as session:
        entry = session.get(Entry, ident)
        sibling = session.get(Entry, entry_id(2))
        sibling.email, sibling.email_hash = entry.email, entry.email_hash
        email = entry.email
        contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == library["wid"], GtmContact.email == email))
        contact.times_emailed, contact.last_emailed_at = 1, utcnow()
        other = session.get(GtmContact, contacts["2"])
        other.times_emailed, other.last_emailed_at = 1, utcnow()
        contact_id = contact.id
        session.commit()
    assert workers._apply_delivery_status(_dsn(email, "delayed", status, "delay"), {email: contact_id}) == []
    with library["factory"]() as session:
        assert session.get(Entry, ident).status == "active"
    message = _dsn(email, "failed", status, "failure")
    assert workers._apply_delivery_status(message, {email: contact_id}) == [contact_id]
    assert workers._apply_delivery_status(message, {email: contact_id}) == []
    assert workers._apply_delivery_status(_dsn(email, "delivered", "2.0.0", "late"), {email: contact_id}) == []
    assert workers._apply_delivery_status(_dsn(email, "delivered", "2.0.0", "other_late"), {email: contacts["2"]}) == []
    with library["factory"]() as session:
        for entry in session.scalars(select(Entry).where(Entry.email == email)):
            assert entry.email_status == "bounced" and entry.status == "retired" and entry.status_reason == "email_bounced"
        session.info["gtm_shared_suppression"] = {opaque(email): False}
        on_outcome(session, session.get(GtmContact, contact_id), "soft_bounce")
        assert globally_suppressed(session, email)
        for suffix in ("", "2"):
            assert not session.scalar(select(Entry.id).where(Entry.id == ident, *_visible(library["wid" + suffix], utcnow())))
    assert candidate(email, library["wid2"])["blocked"] is True


@pytest.mark.parametrize("number", [1, 9])
@pytest.mark.parametrize("second_failure", ["other_founder", "same_send", "new_send_23h", "new_send_24h", "unknown_send"])
def test_soft_bounce_needs_two_founders_or_two_sends_at_least_24h_apart(library, number, second_failure):
    from app.db.models import GtmContact
    from app.gtm.email_learning import candidate, globally_suppressed
    from app.gtm.relationships.suppression import email_suppressed
    from app.gtm.supymem import workers
    from app.lead_library.models import LeadLibraryEntry as Entry, LeadLibraryUsage as Usage
    from app.lead_library.service import claim
    from core.clock import utcnow
    contacts = {}
    ident = entry_id(number)
    for suffix in ("", "2"):
        result = claim(library["wid" + suffix], library["vid" + suffix], [ident], library["actor" + suffix])
        assert result["claimed"] == 1
        contacts[suffix] = result["contact_ids"][0]
    sent_at = utcnow() - timedelta(days=2)
    with library["factory"]() as session:
        for contact_id in contacts.values():
            contact = session.get(GtmContact, contact_id)
            contact.times_emailed, contact.last_emailed_at = 1, None if second_failure == "unknown_send" else sent_at
        email = session.get(Entry, ident).email
        session.commit()
    message = _dsn(email, "failed", "4.2.2", "soft_first")
    assert workers._apply_delivery_status(message, {email: contacts[""]}) == [contacts[""]]
    assert workers._apply_delivery_status(message, {email: contacts[""]}) == []
    with library["factory"]() as session:
        entry = session.get(Entry, ident)
        assert entry.status == "active" and entry.email_status != "bounced"
        assert email_suppressed(session, library["wid"], session.get(GtmContact, contacts[""]))
        assert not email_suppressed(session, library["wid2"], session.get(GtmContact, contacts["2"]))
        assert not globally_suppressed(session, email)
        assert len(list(session.scalars(select(Usage).where(Usage.outcome == "soft_bounce")))) == 1
    assert not candidate(email, library["wid2"])["blocked"]
    target = contacts["2"] if second_failure == "other_founder" else contacts[""]
    if second_failure.startswith("new_send"):
        with library["factory"]() as session:
            contact = session.get(GtmContact, target)
            contact.bounced_at, contact.do_not_contact = None, 0
            contact.times_emailed = 2
            contact.last_emailed_at = sent_at + timedelta(hours=24 if second_failure == "new_send_24h" else 23)
            session.commit()
    assert workers._apply_delivery_status(_dsn(email, "failed", "4.2.2", "soft_second"), {email: target}) == [target]
    retire = second_failure in {"other_founder", "new_send_24h"}
    with library["factory"]() as session:
        entry = session.get(Entry, ident)
        assert entry.status == ("retired" if retire else "active")
        assert (entry.email_status == "bounced") is retire
        assert globally_suppressed(session, email) is retire
        failures = list(session.scalars(select(Usage).where(Usage.outcome == "soft_bounce")))
        assert len(failures) == (1 if second_failure in {"same_send", "unknown_send"} else 2)
