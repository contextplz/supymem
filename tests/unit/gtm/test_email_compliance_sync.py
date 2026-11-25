import asyncio
import base64
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import _add, _mail, _review, journey_db


@pytest.fixture(autouse=True)
def hosted_compliance(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.mark.parametrize("full_name,domain,topic", [
    ("Casey Linden", "factory-inspections", "equipment maintenance"),
    ("Avery Bennett", "community-baking", "sourdough starters"),
])
@pytest.mark.usefixtures("journey_db")
def test_full_name_signature_keeps_one_identity_and_all_compliance(request, full_name, domain, topic):
    from types import SimpleNamespace

    from app.db.models import Person
    from app.gtm.relationships.email_compliance import (
        OPT_OUT,
        complete_body,
        sender_address,
        source_disclosure,
    )
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    data = request.getfixturevalue("journey_db")
    _mail(data)
    identity = full_name.split()[0]
    source = f"https://{domain}.example/contact"
    contact = SimpleNamespace(id="full-name-contact", email=f"hello@{domain}.example",
        enrichment_json=json.dumps({"contact_source_url": source}))
    body = f"Hi there,\n\nHow do you handle {topic}?\n\nThanks,\n{full_name}"
    with data["factory"]() as session:
        session.get(Person, data["actor"]).display_name = full_name
        session.commit()
        completed = complete_body(session, data["wid"], contact, body, identity)
        assert completed.startswith(body)
        assert full_name in completed and identity not in completed.splitlines()
        assert source_disclosure(source) in completed and OPT_OUT in completed
        assert sender_address(session, data["wid"]) in completed
        assert unsubscribe_url(data["wid"], contact.id, contact.email) in completed
        assert complete_body(session, data["wid"], contact, completed, identity) == completed
        with_sender = complete_body(session, data["wid"], contact, completed, "founder@gmail.example")
        assert "founder@gmail.example" in with_sender and identity not in with_sender.splitlines()


@pytest.mark.parametrize("identity,domain,topic", [
    ("Morgan", "ceramic-studios", "kiln maintenance scheduling"),
    ("Parker", "amateur-astronomy", "backyard telescope setup"),
])
@pytest.mark.usefixtures("journey_db")
def test_namesake_greeting_requires_an_exact_standalone_founder_signature(request, identity, domain, topic):
    from types import SimpleNamespace

    from app.gtm.relationships.email_compliance import (
        OPT_OUT,
        complete_body,
        source_line,
    )
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    data = request.getfixturevalue("journey_db")
    contact = SimpleNamespace(id="namesake_contact", email=f"hello@{domain}.example",
        source_url=f"https://{domain}.example/contact", enrichment_json="{}")
    body = f"Hi {identity},\n\nYou are invited to discuss {topic}.\n\n{identity}son"
    with data["factory"]() as session:
        completed = complete_body(session, data["wid"], contact, body, identity)
        assert sum(line.strip() == identity for line in completed.splitlines()) == 1
        assert source_line(contact) in completed and OPT_OUT in completed
        assert unsubscribe_url(data["wid"], contact.id, contact.email) in completed
        signed = complete_body(session, data["wid"], contact, body + f"\n\n  {identity}  ", identity)
        assert sum(line.strip() == identity for line in signed.splitlines()) == 1
        assert complete_body(session, data["wid"], contact, completed, identity) == completed


def _tracked(data, monkeypatch):
    from app.db.models import GtmContact, GtmTouch
    from core.clock import utcnow
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        contact.times_emailed, contact.gmail_thread_id = 1, "tracked-thread"
        session.add(GtmTouch(id="tracked-send", workspace_id=data["wid"], venture_id=data["vid"], contact_id=cid,
            direction="out", channel="email", simulated=0, send_mode="founder_gmail", sent_at=utcnow(),
            provider_thread_id="tracked-thread", message_id="<sent@example.com>", in_reply_to="gmail-sent", created_at=utcnow()))
        session.commit()
    return cid


def _dsn(recipient="ada@bakery.example", action="failed", status="5.1.1"):
    text = f"Final-Recipient: rfc822; {recipient}\r\nAction: {action}\r\nStatus: {status}\r\n"
    return {"mimeType": "message/delivery-status", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


@pytest.mark.parametrize("mode", ["thread", "in-reply-to", "references"])
def test_reply_matches_forwarder_or_alias_without_sender_query(journey_db, monkeypatch, mode):
    from app.db.models import GtmContact, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    message = {"id": "inbound", "threadId": "tracked-thread" if mode == "thread" else "new-thread",
        "snippet": "Yes, interested", "headers": {"From": "Assistant <assistant@other.example>"}}
    if mode != "thread":
        message["headers"][mode] = "<previous@example.com> <sent@example.com>"
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    monkeypatch.setattr(composio_gmail, "fetch_inbound_from", lambda **kwargs: pytest.fail("Never query by sender"))
    calls = []
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: calls.append(kwargs["thread_id"]) or {"messages": [message]})
    assert workers.tick_replies(data["wid"])["marked"] == [cid]
    assert calls == ["tracked-thread"]
    assert workers.tick_replies(data["wid"])["marked"] == []
    with data["factory"]() as session:
        assert session.get(GtmContact, cid).replied_at
        touch = session.scalar(select(GtmTouch).where(GtmTouch.direction == "in"))
        assert touch.message_id == "inbound" and touch.venture_id == data["vid"]


@pytest.mark.parametrize("body,headers,classification", [
    ("Acknowledged. Unsubscribe from these updates.", {"Auto-Submitted": "auto-replied"}, "automated"),
    ("Not interested; remove me", [{"name": "Auto-Submitted", "value": "auto-replied"}], "automated"),
    ("Not interested; stop emailing", {"Auto-Submitted": "no"}, "human_negative"),
    ("Remove me; I am on vacation.", None, "human_negative"),
])
def test_owned_opt_out_is_saved_once_without_automated_demand(journey_db, monkeypatch, body, headers, classification):
    from app.autopilot import outreach, pmf
    from app.db.models import GtmContact, GtmSuppression, GtmTouch, Venture
    from app.email import triggers
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    data = journey_db
    cid = _tracked(data, monkeypatch)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{
        "id": "opt-out", "threadId": "tracked-thread", "snippet": body, "payload": {"headers": headers}}]})
    assert asyncio.run(outreach.poll_replies(data["vid"], force=True)) == 1
    assert asyncio.run(outreach.poll_replies(data["vid"], force=True)) == 0
    results = journey.status(data["wid"], data["vid"])["results"]
    human = classification != "automated"
    assert results["replied"] == results["negative"] == int(human)
    assert results["interested"] == 0 and len(results["replies"]) == 1
    assert results["replies"][0]["classification"] == classification
    assert pmf.read_metric(data["vid"], "outbound_replies") == int(human)
    assert outreach.schedule_followups(data["vid"]) == 0
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert contact.do_not_contact and contact.unsubscribed_at and contact.replied_at
        assert contact.relationship_stage == "cooling"
        assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.email == contact.email))
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "in")))
        assert len(touches) == 1 and touches[0].outcome == "unsubscribe"
        assert triggers._real_touch(touches[0], session.get(Venture, data["vid"]), "in") is human


@pytest.mark.parametrize("kind", ["founder", "sent", "auto", "bounce", "unrelated"])
def test_non_human_or_untracked_messages_do_not_stop_followups(journey_db, monkeypatch, kind):
    from app.db.models import GtmContact, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    message = {"id": "inbound", "threadId": "tracked-thread", "snippet": "Yes, interested",
        "payload": {"headers": [{"name": "From", "value": "recipient@other.example"}]}}
    if kind == "founder":
        message["payload"]["headers"][0]["value"] = "Founder <FOUNDER@gmail.example>"
    elif kind == "sent":
        message["labelIds"] = ["SENT"]
    elif kind == "auto":
        message["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
    elif kind == "bounce":
        message["payload"]["parts"] = [_dsn()]
    else:
        message["threadId"] = "untracked"
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [message]})
    assert workers.tick_replies(data["wid"])["marked"] == []
    with data["factory"]() as session:
        assert session.get(GtmContact, cid).replied_at is None
        assert session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in")) is None


def test_history_paginates_and_advances_only_after_processing(journey_db, monkeypatch):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"data": {"historyId": "100"}}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": []})
    assert not workers.tick_replies(data["wid"])["marked"]
    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        if kwargs["page_token"] is None:
            return {"historyId": "110", "nextPageToken": "page-2", "history": [{"messagesAdded": [{"message": {"id": "reply"}}]}]}
        return {"historyId": "120", "history": []}
    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: pytest.fail("Incremental sync must not rescan"))
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: {"id": "reply", "threadId": "tracked-thread", "snippet": "Yes"})
    assert workers.tick_replies(data["wid"])["marked"] == [cid]
    assert [call["page_token"] for call in calls] == [None, "page-2"]
    assert all(call["start_history_id"] == "100" for call in calls)
    with data["factory"]() as session:
        cursor = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_sync_checkpoint").order_by(GtmEmailEvent.created_at.desc()))
        assert json.loads(cursor.metadata_json)["history_id"] == "120"


@pytest.mark.parametrize("email,idea", [("buyer@equipment.example", "Equipment service teams"),
    ("member@gardens.example", "Neighbourhood gardening groups")])
@pytest.mark.parametrize("reader", ["replies", "bounces"])
def test_compliance_drains_late_suppression_before_advancing_or_allowing_send(journey_db, monkeypatch, email, idea, reader):
    from app.db.models import GtmContact, GtmEmailEvent, GtmOutreachSettings, Venture
    from app.gtm.supymem import composio_gmail, pipeline, workers
    from core.clock import utcnow

    data = journey_db
    contact_id = _tracked(data, monkeypatch)
    event_type = "gmail_sync_checkpoint" if reader == "replies" else "gmail_bounce_checkpoint"
    with data["factory"]() as session:
        session.get(GtmContact, contact_id).email = email
        session.get(Venture, data["vid"]).raw_idea = idea
        session.get(GtmOutreachSettings, data["wid"]).send_enabled = True
        session.add(GtmEmailEvent(id="compliance-start", email_account_id="mail_journey", workspace_id=data["wid"],
            event_type=event_type, created_at=utcnow(), metadata_json=json.dumps({"history_id": "100"})))
        session.commit()

    def cursor():
        with data["factory"]() as session:
            row = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.event_type == event_type)
                .order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()))
            return json.loads(row.metadata_json)["history_id"]

    calls, processed = [], []
    def history(**kwargs):
        assert cursor() == "100"
        calls.append((kwargs["start_history_id"], kwargs["page_token"]))
        if kwargs["page_token"] is None:
            return {"historyId": "110", "nextPageToken": "last", "history": []}
        assert kwargs["page_token"] == "last"
        return {"historyId": "120", "history": [{"messagesAdded": [{"message": {"id": "late-suppression"}}]}]}

    message = {"id": "late-suppression", "threadId": "tracked-thread", "snippet": "Please unsubscribe me."}
    if reader == "bounces":
        message["payload"] = {"parts": [_dsn(recipient=email)]}
    original = pipeline.apply_reply if reader == "replies" else pipeline.apply_bounce
    def apply(*args, **kwargs):
        assert cursor() == "100"
        processed.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(composio_gmail, "fetch_history", history)
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: message)
    monkeypatch.setattr(pipeline, "apply_reply" if reader == "replies" else "apply_bounce", apply)
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: pytest.fail("A suppressed recipient must never reach Gmail"))
    result = (workers.tick_replies if reader == "replies" else workers.tick_bounces)(data["wid"])
    assert result["marked"] == processed == [contact_id]
    assert calls == [("100", None), ("100", "last")] and cursor() == "120"
    with data["factory"]() as session:
        contact = session.get(GtmContact, contact_id)
        assert contact.do_not_contact and (contact.unsubscribed_at if reader == "replies" else contact.bounced_at)
    with pytest.raises(composio_gmail.DefiniteFailure, match="contact_suppressed|Workspace sending is paused"):
        composio_gmail.send_gmail_email(composio_user_id="user_fixture", connected_account_id="ca_fixture",
            to=email, subject="Follow up", body="Not sent", thread_id="tracked-thread")


@pytest.mark.parametrize("action,status,expected", [("failed", "5.2.2", True), ("failed", "4.2.0", True),
    ("delayed", "4.2.0", False), ("delivered", "2.0.0", False), ("failed", "2.0.0", False)])
def test_dsn_action_and_status_not_subject_decide_bounce(journey_db, monkeypatch, action, status, expected):
    from app.gtm.supymem import composio_gmail, pipeline, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    message = {"id": "dsn", "subject": "Hello", "payload": {"parts": [_dsn(action=action, status=status)]}}
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: [message])
    calls = []
    monkeypatch.setattr(pipeline, "apply_bounce", lambda contact_id, **kwargs: calls.append((contact_id, kwargs)) or True)
    assert workers.tick_bounces(data["wid"])["marked"] == ([cid] if expected else [])
    if expected:
        assert calls[0][1]["hard_bounce"] == status.startswith("5.")
    message = {"id": "ordinary", "subject": "Undeliverable", "body": "Final-Recipient: rfc822; ada@bakery.example\nStatus: 5.1.1\nAction: failed"}
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: [message])
    assert not workers.tick_bounces(data["wid"])["marked"]


@pytest.mark.parametrize("subscriber", ["sole_trader", "ordinary_partnership", "consumer", "unverified_individual"])
def test_uk_subscribers_require_founder_recorded_consent(journey_db, monkeypatch, subscriber):
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import consent_block
    data = journey_db
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        contact.recipient_geo, contact.lawful_basis = "GB", "legitimate_interest"
        contact.enrichment_json = json.dumps({"found_at": "https://bakery.example/contact", "outreach_hint": {"country": "GB", "subscriber": subscriber}})
        assert consent_block(contact) == "gtm_recipient_consent_required"
        session.commit()
    journey.patch_prospect(data["wid"], data["vid"], cid, {"country": "GB", "lawful_basis": "consent", "lawful_basis_note": "They agreed by email on October 17, 2026."})
    with data["factory"]() as session:
        assert consent_block(session.get(GtmContact, cid)) is None


def test_missing_address_is_one_step_recoverable_and_exact_review_changes(journey_db, monkeypatch):
    from app.db.models import GtmOutreachSettings
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import validate_send
    data = journey_db
    _add(data, monkeypatch)
    _mail(data)
    state, request = _review(data)
    with data["factory"]() as session:
        session.get(GtmOutreachSettings, data["wid"]).sender_postal_address = ""
        session.commit()
        with pytest.raises(ValueError, match="gtm_sender_address_required"):
            validate_send(session, data["wid"], None, "Hello", "Founder")
    assert journey.status(data["wid"], data["vid"])["send_blocked_reason"] == "gtm_sender_address_required"
    state = journey.update_settings(data["wid"], data["vid"], {"sender_postal_address": "2 New Street, London, UK"})
    assert "2 New Street, London, UK" in state["drafts"][0]["body"]
    assert state["drafts"][0]["review_state"] == "draft"


def test_shared_mailbox_cap_keeps_uncertain_delivery_and_resets(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailEvent
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    data = journey_db
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    with data["factory"]() as session:
        from app.gtm.relationships.service import _settings
        _settings(session, data["wid"]).send_enabled = True
        contact = session.get(GtmContact, cid)
        contact.recipient_geo, contact.lawful_basis = "US", "legitimate_interest"
        session.commit()
    monkeypatch.setenv("OUTREACH_NEW_CONTACTS_PER_MAILBOX_DAY", "1")
    body = outreach.drafts(data["vid"])[0]["body"]
    args = {"composio_user_id": "user_fixture", "connected_account_id": "ca_fixture", "to": "ada@bakery.example", "subject": "Hi", "body": body}
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: (_ for _ in ()).throw(TimeoutError("unknown delivery")))
    with pytest.raises(TimeoutError):
        composio_gmail.send_gmail_email(**args)
    with pytest.raises(composio_gmail.DefiniteFailure, match="daily cap.*Resets at"):
        composio_gmail.send_gmail_email(**args)
    with data["factory"]() as session:
        held = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "cold_reserved"))
        assert held
    tomorrow = utcnow() + timedelta(days=1)
    monkeypatch.setattr("core.clock.utcnow", lambda: tomorrow)
    with pytest.raises(composio_gmail.DefiniteFailure, match="already sent or unconfirmed"):
        composio_gmail.send_gmail_email(**args)
    second = _add(data, monkeypatch, country="US", basis="legitimate_interest", email="bob@otherbakery.example", name="Bob")
    draft = next(draft for draft in second["drafts"] if draft["to"] == "bob@otherbakery.example")
    args.update(to=draft["to"], body=draft["body"])
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: {"provider_message_id": "gmail-id", "thread_id": "thread", "rfc_message_id": "<mail@example.com>"})
    assert composio_gmail.send_gmail_email(**args)["rfc_message_id"] == "<mail@example.com>"


def test_failed_history_message_fetch_does_not_advance_checkpoint(journey_db, monkeypatch):
    from app.db.models import GtmEmailEvent
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    _tracked(data, monkeypatch)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"data": {"historyId": "100"}}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": []})
    workers.tick_replies(data["wid"])
    monkeypatch.setattr(composio_gmail, "fetch_history", lambda **kwargs: {"historyId": "120", "history": [{"messagesAdded": [{"message": {"id": "reply"}}]}]})
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: (_ for _ in ()).throw(TimeoutError()))
    assert workers.tick_replies(data["wid"])["error"] == "gmail_message_fetch_failed"
    with data["factory"]() as session:
        rows = session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "gmail_sync_checkpoint")).all()
        assert len(rows) == 1 and json.loads(rows[0].metadata_json)["history_id"] == "100"


def test_dsn_uses_each_recipient_status_in_multipart_report(journey_db):
    from app.gtm.supymem.workers import _delivery_status
    raw = "Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r\n\r\n--dsn\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example\r\n\r\nFinal-Recipient: rfc822; ada@bakery.example\r\nAction: failed\r\nStatus: 5.2.2\r\n\r\nFinal-Recipient: rfc822; bob@bakery.example\r\nAction: delayed\r\nStatus: 4.2.0\r\n\r\n--dsn--\r\n"
    assert _delivery_status({"raw": base64.urlsafe_b64encode(raw.encode()).decode()}) == [
        ("ada@bakery.example", "failed", "5.2.2"), ("bob@bakery.example", "delayed", "4.2.0")]


def test_send_receipt_unwraps_composio_and_fetches_rfc_message_id(journey_db, monkeypatch):
    from app.gtm.supymem import composio_gmail
    calls = []
    def execute(slug, **kwargs):
        calls.append((slug, kwargs))
        if slug == "GMAIL_SEND_EMAIL":
            return {"successful": True, "data": {"response_data": {"messageId": "gmail-id", "threadId": "thread-id"}}}
        return {"data": {"payload": {"headers": [{"name": "Message-ID", "value": "<rfc@example.com>"}]}}}
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    result = composio_gmail._send_gmail_email(composio_user_id="user_fixture", connected_account_id="ca_fixture", to="ada@example.com", subject="Hi", body="Hello")
    assert result == {"provider_message_id": "gmail-id", "thread_id": "thread-id", "rfc_message_id": "<rfc@example.com>"}
    assert calls[1][1]["arguments"] == {"message_id": "gmail-id", "format": "full"}


@pytest.mark.parametrize("kind", ["consent", "followup", "suppressed"])
def test_only_new_cold_contacts_consume_mailbox_quota(journey_db, monkeypatch, kind):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailEvent, GtmSuppression
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    data = journey_db
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    with data["factory"]() as session:
        _settings(session, data["wid"]).send_enabled = True
        contact = session.get(GtmContact, cid)
        if kind == "followup":
            contact.times_emailed = 1
        if kind == "suppressed":
            session.add(GtmSuppression(id="suppressed-quota", workspace_id=data["wid"], email=contact.email, source="opt_out", created_at=utcnow()))
        session.commit()
    monkeypatch.setenv("OUTREACH_NEW_CONTACTS_PER_MAILBOX_DAY", "0")
    sent = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: sent.append(kwargs) or {"provider_message_id": "gmail-id", "thread_id": "thread-id"})
    args = {"composio_user_id": "user_fixture", "connected_account_id": "ca_fixture", "to": "ada@bakery.example", "subject": "Hi", "body": outreach.drafts(data["vid"])[0]["body"]}
    if kind == "suppressed":
        with pytest.raises(composio_gmail.DefiniteFailure, match="contact_suppressed"):
            composio_gmail.send_gmail_email(**args)
        assert not sent
    else:
        assert composio_gmail.send_gmail_email(**args)["provider_message_id"] == "gmail-id"
        assert len(sent) == 1
    with data["factory"]() as session:
        assert not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.event_type == "cold_reserved"))


def test_mailbox_quota_is_shared_by_same_sender_not_other_mailboxes(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem import composio_gmail
    from core.clock import utcnow
    data = journey_db
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    now = utcnow()
    with data["factory"]() as session:
        _settings(session, data["wid"]).send_enabled = True
        contact = session.get(GtmContact, cid)
        contact.recipient_geo, contact.lawful_basis = "US", "legitimate_interest"
        session.add(GtmEmailAccount(id="same-sender", workspace_id=data["wid"], email="FOUNDER@gmail.example", composio_user_id="another-user",
            connected_account_id="another-account", status="active", created_at=now, updated_at=now))
        session.commit()
        for index in range(25):
            session.add(GtmEmailEvent(id=f"prior-cold-{index}", workspace_id=data["wid"], email_account_id="same-sender", event_type="cold_reserved", created_at=now))
        session.commit()
    body = outreach.drafts(data["vid"])[0]["body"]
    args = {"composio_user_id": "user_fixture", "connected_account_id": "ca_fixture", "to": "ada@bakery.example", "subject": "Hi", "body": body}
    sent = []
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: sent.append(kwargs) or {"provider_message_id": "gmail-id", "thread_id": "thread-id"})
    with pytest.raises(composio_gmail.DefiniteFailure, match=r"daily cap \(25 new cold contacts\)"):
        composio_gmail.send_gmail_email(**args)
    assert not sent
    with data["factory"]() as session:
        session.get(GtmEmailAccount, "same-sender").email = "other@gmail.example"
        session.commit()
    assert composio_gmail.send_gmail_email(**args)["provider_message_id"] == "gmail-id"
    assert len(sent) == 1


def test_optout_sentence_reply_suppresses_future_sends(journey_db, monkeypatch):
    from app.db.models import GtmContact
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{"id": "optout-reply", "threadId": "tracked-thread", "snippet": "No thanks"}]})
    assert workers.tick_replies(data["wid"])["marked"] == [cid]
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert contact.do_not_contact and contact.unsubscribed_at


@pytest.mark.parametrize("domain", ["kestrel-audit.example", "willowcycles.example"])
def test_quoted_optout_footer_is_not_a_recipient_optout(journey_db, domain):
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.gtm.relationships.unsubscribe import issue_unsubscribe_token
    from app.gtm.supymem.workers import _classify_reply, _reply_opt_out
    assert not _reply_opt_out("Yes, interested.\n\n" + OPT_OUT)
    assert _reply_opt_out("No thanks.")
    token = issue_unsubscribe_token(journey_db["wid"], "quoted-contact", f"reader@{domain}")
    footer = OPT_OUT + f"\n\nStop these emails: https://{domain}/public/unsubscribe?t={token}"
    assert _reply_opt_out(footer.split("\n\n")[-1])
    for quoted in (footer, "\n".join("> " + line for line in footer.splitlines()),
            footer.replace("Stop these emails: https://", "Stop these emails:\nhttps://")):
        assert not _reply_opt_out("Yes, interested.\n\n" + quoted)
        assert _classify_reply("Yes, interested.\n\n" + quoted) == "human_positive"
        assert _reply_opt_out("No thanks.\n\n" + quoted)
        assert _reply_opt_out("Please unsubscribe me.\n\n" + quoted)
        assert _reply_opt_out(quoted + " Please unsubscribe me.")


@pytest.mark.parametrize("mime", ["text/plain", "text/html"])
def test_optout_uses_full_body_not_truncated_gmail_snippet(journey_db, monkeypatch, mime):
    from app.db.models import GtmContact
    from app.gtm.supymem import composio_gmail, workers
    data = journey_db
    cid = _tracked(data, monkeypatch)
    text = "Thanks for writing. " * 30 + "\nPlease unsubscribe me."
    if mime == "text/html":
        text = "<p>" + text.replace("\n", "</p><p>") + "</p>"
    payload = {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}
    monkeypatch.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [{"id": "long-optout", "threadId": "tracked-thread",
        "snippet": "Thanks for writing.", "payload": {"parts": [payload]}}]})
    assert workers.tick_replies(data["wid"])["marked"] == [cid]
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        assert contact.do_not_contact and contact.unsubscribed_at


def test_incremental_history_processes_exact_dsn_recipient_without_subject_search(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmEmailEvent, GtmTouch
    from app.gtm.supymem import composio_gmail, workers
    from core.clock import utcnow
    data = journey_db
    cid = _tracked(data, monkeypatch)
    with data["factory"]() as session:
        session.add(GtmEmailEvent(id="dsn-history-start", email_account_id="mail_journey", workspace_id=data["wid"],
            event_type="gmail_sync_checkpoint", created_at=utcnow(), metadata_json=json.dumps({"history_id": "100"})))
        session.commit()
    monkeypatch.setattr(composio_gmail, "fetch_history", lambda **kwargs: {"historyId": "120", "history": [{"messagesAdded": [{"message": {"id": "dsn-history"}}]}]})
    monkeypatch.setattr(composio_gmail, "fetch_message", lambda **kwargs: {"id": "dsn-history", "threadId": "not-outbound-thread", "subject": "Delivery status",
        "payload": {"parts": [_dsn(recipient="wrong@bakery.example"), _dsn(status="5.2.2")]}})
    monkeypatch.setattr(composio_gmail, "fetch_thread", lambda **kwargs: pytest.fail("Use incremental history"))
    assert workers.tick_replies(data["wid"])["marked"] == []
    with data["factory"]() as session:
        assert session.get(GtmContact, cid).bounced_at
        assert session.get(GtmContact, cid).do_not_contact
        assert not session.scalar(select(GtmTouch.id).where(GtmTouch.direction == "in"))


@pytest.mark.parametrize("field,reason", [("identity", "gtm_sender_identity_required"), ("source", "gtm_source_disclosure_required"),
    ("optout", "gtm_draft_one_ask_and_optout_required"), ("address", "gtm_sender_address_changed")])
def test_shared_delivery_boundary_blocks_each_removed_compliance_line(journey_db, monkeypatch, field, reason):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm.relationships.email_compliance import OPT_OUT, sender_address
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem import composio_gmail
    data = journey_db
    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    with data["factory"]() as session:
        _settings(session, data["wid"]).send_enabled = True
        contact = session.get(GtmContact, cid)
        lines = {"identity": outreach.founder_signature(data["vid"]),
            "optout": OPT_OUT, "address": sender_address(session, data["wid"])}
        if field == "source":
            contact.enrichment_json = "{}"
        session.commit()
    body = outreach.drafts(data["vid"])[0]["body"]
    if field != "source":
        body = body.replace(lines[field], "")
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", lambda **kwargs: pytest.fail("Compliance must block before Gmail"))
    with pytest.raises(composio_gmail.DefiniteFailure, match=reason):
        composio_gmail.send_gmail_email(composio_user_id="user_fixture", connected_account_id="ca_fixture", to="ada@bakery.example", subject="Hello", body=body)


@pytest.mark.parametrize("pitch,audience,problem,email", [
    ("a payment reconciliation platform", "finance operations teams", "Daily reconciliation takes too long", "buyer@payments.example"),
    ("a personal medication reminder app", "adults managing prescriptions", "People forget scheduled medication doses", "member@medication.example"),
])
@pytest.mark.parametrize("mode", ["template", "model", "interview", "requested"])
def test_review_email_footers_and_actual_delivery_require_a_real_source(journey_db, monkeypatch, pitch, audience, problem, email, mode):
    import asyncio
    import re

    from app.autopilot import outreach
    from app.chat import assistant
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.gtm.relationships.email_compliance import OPT_OUT, complete_body, sender_address, source_line
    from app.gtm.relationships.service import _settings
    from app.gtm.supymem import composio_gmail
    from app.planning import execute
    from app.ventures import state_brief

    data = journey_db
    contact_id = _add(data, monkeypatch, country="US", email=email)["prospects"][0]["id"]
    _mail(data)
    identity = outreach.founder_signature(data["vid"])
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda _vid: {
        "idea": {"raw_idea": pitch, "offer": pitch, "audience": audience, "problem": problem}})
    monkeypatch.setattr(outreach, "saved_pitch", lambda _vid: pitch)
    monkeypatch.setattr(journey, "_icp", lambda _vid: {})
    async def compose(*_args, **_kwargs):
        return ("\n\n".join(f"Draft {number}\nSubject: A conversation\n\nHi,\n\nI'm exploring {pitch}. "
                f"{problem}. I'm learning how {audience} handle this today. "
                f"Would you share your experience?\n\nThanks,\n{identity}" for number in ((1,) if mode == "requested" else (1, 2, 3)))
                if mode in {"model", "requested"} else "")
    monkeypatch.setattr(assistant, "_complete", compose)
    title = "Draft one interview email for me to review" if mode == "interview" else "Draft outreach emails for me to review"
    detail = f"Founder request: Draft an email to {email}. Do not send anything." if mode == "requested" else "Do not send anything."
    text = asyncio.run(execute._review_drafts(data["vid"], title, detail))
    bodies = re.split(r"(?m)^Draft \d+\nSubject:[^\n]*\n\n", text)[1:]
    assert len(bodies) == (1 if mode in {"interview", "requested"} else 3)
    with data["factory"]() as session:
        address = sender_address(session, data["wid"])
        contact = session.get(GtmContact, contact_id)
        source = source_line(contact)
        _settings(session, data["wid"]).send_enabled = True
        session.commit()
        for body in bodies:
            assert OPT_OUT in body
            assert address in body and identity in body and "founder@gmail.example" in body
            assert (source in body) == (mode == "requested")
        completed = complete_body(session, data["wid"], contact, bodies[0], identity)
    calls = []
    def deliver(**kwargs):
        calls.append(kwargs)
        assert all(line in kwargs["body"] for line in (OPT_OUT, address, identity, source))
        return {"id": "local_delivery", "threadId": "local_thread"}
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", deliver)
    with pytest.raises(composio_gmail.DefiniteFailure, match="gtm_source_disclosure_required"):
        composio_gmail.send_gmail_email(composio_user_id="user_fixture", connected_account_id="ca_fixture", to=email,
                                       subject="A conversation", body=bodies[0].replace(source, ""))
    assert not calls
    composio_gmail.send_gmail_email(composio_user_id="user_fixture", connected_account_id="ca_fixture", to=email,
                                   subject="A conversation", body=completed)
    assert len(calls) == 1


@pytest.mark.parametrize("domain", ["freight-operations", "community-recipes"])
@pytest.mark.usefixtures("journey_db")
def test_local_customer_status_does_not_require_smtp(request, monkeypatch, domain, tmp_path):
    from types import SimpleNamespace

    from app.gtm import journey
    from app.gtm.relationships.email_compliance import (
        OPT_OUT,
        complete_body,
        sender_address,
    )

    data = request.getfixturevalue("journey_db")
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "local-mail-status"))
    from app.core.local_secrets import update_settings
    for name in ("SUPYMEM_SMTP_HOST", "SUPYMEM_SMTP_USERNAME", "SUPYMEM_SMTP_PASSWORD", "SUPYMEM_SENDER_POSTAL_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    with data["factory"]() as session:
        assert sender_address(session, data["wid"]) == ""
        update_settings("smtp", {"postal_address": domain + " postal address"}, {})
        assert sender_address(session, data["wid"]) == domain + " postal address"
        update_settings("smtp", {"sender_name": domain}, {})
        contact = SimpleNamespace(id="local-draft", email=f"reader@{domain}.example",
            enrichment_json=json.dumps({"contact_source_url": f"https://{domain}.example/contact"}))
        body = complete_body(session, data["wid"], contact, "Would you like to discuss your experience?", domain)
        assert OPT_OUT in body and domain + " postal address" in body
        assert f"Your email address is listed publicly on {domain}.example." in body
        update_settings("smtp", {"sender_name": domain + "\nextra"}, {})
        with pytest.raises(ValueError, match="one line"):
            complete_body(session, data["wid"], contact, "Would you like to discuss your experience?", domain)
        update_settings("smtp", {"sender_name": domain}, {})
    status = journey.status(data["wid"], data["vid"], authorize=False)
    assert status["sender_postal_address"] == domain + " postal address"
