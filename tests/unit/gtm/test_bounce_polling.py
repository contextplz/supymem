import asyncio
import base64
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.gtm.test_journey import _add, _mail, journey_db


@pytest.mark.parametrize("audience", ["Veterinary clinic managers", "Parents planning children's meals"])
def test_attributed_bounce_posts_once_in_chat_with_a_next_step(journey_db, monkeypatch, audience):
    from types import SimpleNamespace
    from tests.company_os_harness import company_os_test_client as TestClient
    from app import main
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm.supymem import composio_gmail, workers

    data = journey_db
    cid = _sent(data, monkeypatch, count=1)
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        contact.first_name = f"{audience} volunteer"
        session.commit()
    monkeypatch.setattr("app.admin.authn.verify_caller", lambda *args, **kwargs: SimpleNamespace(sub=data["actor"]))
    dsn = "Final-Recipient: rfc822; ada@bakery.example\r\nAction: failed\r\nStatus: 5.1.1\r\n"
    encoded = base64.urlsafe_b64encode(dsn.encode()).decode()
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: [{"id": "chat-dsn", "payload": {"parts": [
        {"mimeType": "message/delivery-status", "body": {"data": encoded}}]}}])
    monkeypatch.setattr(workers, "tick_replies", lambda *args, **kwargs: {"replies": []})
    for _ in range(2):
        assert asyncio.run(outreach.poll_replies(data["vid"], force=True)) == 0
    with TestClient(main.create_app()) as client:
        response = client.get(f"/ventures/{data['vid']}/chat", headers={"X-Subject": data["actor"]})
    assert response.status_code == 200, response.text
    messages = response.json()["items"]
    bounces = [message for message in messages if "bounced" in message["body_md"].lower()]
    assert len(bounces) == 1, messages
    assert audience in bounces[0]["body_md"] and "ada@bakery.example" in bounces[0]["body_md"]
    assert bounces[0]["actions"] and "—" not in bounces[0]["body_md"]


@pytest.mark.parametrize("audience", ["Veterinary clinic managers", "Parents planning children's meals"])
def test_distinct_replies_in_one_minute_are_both_saved(journey_db, monkeypatch, audience):
    from app.autopilot import outreach
    from app.chat.store import list_messages
    from core.clock import utcnow

    data = journey_db
    fixed = utcnow()
    monkeypatch.setattr(outreach, "utcnow", lambda: fixed)
    for index in range(2):
        outreach._notify_replies(data["vid"], [{"email": "person@audience.example", "kind": "human_neutral",
            "text": f"Your question about {audience}, reply {index}.", "message_id": f"reply-{index}"}])
    messages = [message for message in list_messages(data["vid"]) if audience in message["body_md"]]
    assert len(messages) == 2
    assert all(message["actions"] for message in messages)


def _sent(data, monkeypatch, count=10, *, days_ago=0):
    from app.db.models import GtmContact, GtmTouch
    from app.gtm.relationships.service import _settings
    from core.clock import utcnow

    cid = _add(data, monkeypatch)["prospects"][0]["id"]
    _mail(data)
    with data["factory"]() as session:
        contact = session.get(GtmContact, cid)
        contact.times_emailed, contact.last_emailed_at = count, utcnow()
        _settings(session, data["wid"]).send_enabled = True
        for index in range(count):
            session.add(GtmTouch(id=f"poll-send-{index}", workspace_id=data["wid"], venture_id=data["vid"], contact_id=cid,
                                 direction="out", channel="email", simulated=0, send_mode="founder_gmail", sent_at=utcnow() - timedelta(days=days_ago), created_at=utcnow()))
        session.commit()
    return cid


def test_dsn_marks_once_and_pauses_existing_threshold(journey_db, monkeypatch):
    from app.db.models import GtmContact, GtmEmailEvent, GtmOutreachSettings
    from app.gtm.supymem import composio_gmail, pipeline, workers

    data = journey_db
    cid = _sent(data, monkeypatch)
    dsn = "Final-Recipient: rfc822; ada@bakery.example\r\nAction: failed\r\nStatus: 5.1.1\r\n"
    encoded = base64.urlsafe_b64encode(dsn.encode()).decode()
    messages = [{"id": "dsn-1", "snippet": "Delivery failed", "payload": {"parts": [{"mimeType": "message/delivery-status", "body": {"data": encoded}}]}},
                {"id": "unrelated", "body": "Final-Recipient: rfc822; other@example.com\nOriginal message: ada@bakery.example"}]
    monkeypatch.setattr(composio_gmail, "fetch_query", lambda **kwargs: messages)
    assert workers.tick_bounces(data["wid"])["marked"] == [cid]
    with data["factory"]() as session:
        bounced_at = session.get(GtmContact, cid).bounced_at
        assert bounced_at and not session.get(GtmOutreachSettings, data["wid"]).send_enabled
    assert workers.tick_bounces(data["wid"])["marked"] == []
    assert not pipeline.apply_bounce(cid, provider_message_id="dsn-1")
    with data["factory"]() as session:
        assert session.get(GtmContact, cid).bounced_at == bounced_at
        assert len(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "bounce")).all()) == 1


@pytest.mark.parametrize("field", [
    "Final-Recipient: rfc822; <ADA@bakery.example>",
    "Original-Recipient: rfc822; ADA@bakery.example",
    "X-Failed-Recipients: ADA@bakery.example, other@example.com",
    "Your message wasn't delivered to ADA@bakery.example because the address couldn't be found.",
])
def test_dsn_matches_recipient_not_quoted_or_substring_addresses(field):
    from app.gtm.supymem.workers import _bounce_recipients

    recipients, _ = _bounce_recipients({"body": "Final-Recipient: rfc822; notada@bakery.example\nOriginal text: ada@bakery.example"})
    assert recipients == {"notada@bakery.example"}
    expected = {"ada@bakery.example", "other@example.com"} if field.startswith("X-Failed") else {"ada@bakery.example"}
    recipients, _ = _bounce_recipients({"body": field + "\nOriginal text: unrelated@example.com"})
    assert recipients == expected
    recipients, _ = _bounce_recipients({"body": "ada@bakery.example: delivery failure"})
    assert not recipients
    if field.startswith(("Final-Recipient", "Original-Recipient", "X-Failed-Recipients")):
        name, value = field.split(":", 1)
        header_recipients, _ = _bounce_recipients({"payload": {"headers": [{"name": name.lower(), "value": value}]}})
        assert header_recipients == expected


def test_maintenance_polls_recent_sends_without_discovery_and_throttles(journey_db, monkeypatch):
    import json

    from app.db.models import Job
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail, workers
    from sqlalchemy import select

    data = journey_db
    _sent(data, monkeypatch, count=1)
    calls = []
    monkeypatch.setattr(workers, "tick_bounces", lambda wid: calls.append(wid))
    monkeypatch.setattr(composio_gmail, "fetch_inbound_from", lambda **kwargs: [])
    monkeypatch.setattr(journey, "_discovery_eligible", lambda *args: False)
    asyncio.run(journey.maintain_all())
    asyncio.run(journey.maintain_all())
    assert calls == []
    with data["factory"]() as session:
        queued = list(session.scalars(select(Job).where(Job.kind == "gtm.gmail.read", Job.workspace_id == data["wid"])))
        assert len(queued) == 1 and json.loads(queued[0].payload_json)["mailbox_id"] == "mail_journey"


def test_older_sends_do_not_trigger_bounce_poll(journey_db, monkeypatch):
    from app.autopilot import outreach
    from app.gtm.supymem import composio_gmail, workers

    data = journey_db
    _sent(data, monkeypatch, count=1, days_ago=8)
    calls = []
    monkeypatch.setattr(workers, "tick_bounces", lambda wid: calls.append(wid))
    monkeypatch.setattr(composio_gmail, "fetch_inbound_from", lambda **kwargs: [])
    asyncio.run(outreach.poll_replies(data["vid"], force=True))
    assert calls == []
