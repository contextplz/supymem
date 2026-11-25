"""Founder-reported recovery never invokes Gmail or reuses send authority."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from tests.unit.gtm.test_journey import _add, _mail, _review, journey_db
from tests.unit.autopilot.test_outreach_send_admission import postgres_admission


@pytest.fixture(autouse=True)
def gmail_backend(monkeypatch):
    """These receipts belong to hosted Gmail, not the local SMTP backend."""
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.fixture()
def held(journey_db, monkeypatch, request):
    from app.autopilot import outreach
    from app.db.models import GtmContact, GtmEmailEvent
    from app.gtm import journey
    from app.gtm.supymem import composio_gmail

    data = journey_db
    _add(data, monkeypatch, country="US", basis="legitimate_interest")
    journey.update_settings(data["wid"], data["vid"], {"sender_postal_address": "1 Main Street, New York, NY 10001"})
    _mail(data)
    _, send_request = _review(data)
    legacy = getattr(request, "param", False)
    if legacy:
        send = composio_gmail.send_gmail_email
        monkeypatch.setattr(composio_gmail, "send_gmail_email", lambda **kwargs: send(**(kwargs | {"draft_id": None})))

    def timeout(**kwargs):
        raise TimeoutError("Gmail acknowledgement lost")

    monkeypatch.setattr(composio_gmail, "_send_gmail_email", timeout)
    result = asyncio.run(journey.send(data["wid"], data["vid"], send_request))
    draft = outreach.drafts(data["vid"])[0]
    assert draft["state"] == "delivery_unconfirmed"
    with data["factory"]() as session:
        reservation = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "cold_reserved"))
        assert (json.loads(reservation.metadata_json or "{}").get("draft_id")
                == (None if legacy else draft["doc_id"]))
        contact = session.get(GtmContact, draft["contact_id"])
        from core.clock import utcnow
        from core.ids import new_id
        peer = GtmContact(id=new_id("peer"), workspace_id=data["wid"], company_id=contact.company_id,
            email="peer@bakery.example", source=f"venture:{data['vid']}:public", created_at=utcnow(), stage_changed_at=utcnow())
        session.add(peer)
        session.commit()
        data = data | {"draft": draft, "peer": peer.id, "reservation": reservation.id, "card": result["receipt"]}

    def never_provider(*args, **kwargs):
        pytest.fail("Resolution must never call Composio or Gmail")

    monkeypatch.setattr(outreach, "_send_one", never_provider)
    monkeypatch.setattr(composio_gmail, "_request", never_provider)
    monkeypatch.setattr(composio_gmail, "_send_gmail_email", never_provider)
    return data


def _resolve(data, outcome, **extra):
    from app.chat.execute import execute_action

    return asyncio.run(execute_action(data["vid"], "outreach.delivery_resolve",
        payload={"draft_id": data["draft"]["doc_id"], "outcome": outcome, **extra}, actor_subject=data["actor"]))


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_resolves_accounting_once_without_sending_and_lifts_uncertain_company_hold(held, outcome):
    from app.autopilot import outreach, pmf
    from app.db.models import Approval, CreditAccount, CreditLedgerEntry, CreditReservation, GtmContact, GtmEmailEvent, GtmTouch
    from app.gtm.relationships.cadence import company_block

    data = held
    with data["factory"]() as session:
        assert company_block(session, data["wid"], session.get(GtmContact, data["peer"])) == "company_delivery_unconfirmed"
        account = session.get(CreditAccount, data["account"])
        assert account.spent_micro == 0 and account.reserved_micro == 10_000
    labels = {action["label"] for action in data["card"]["actions"]}
    assert {"It's in my Sent folder", "It's not in Sent", "Reconnect Gmail", "Do it another way"} <= labels
    first = _resolve(data, outcome)
    assert "Recorded as" in first["body_md"]
    assert "already resolved" in _resolve(data, outcome)["body_md"]
    assert "already resolved" in _resolve(data, "not_sent" if outcome == "sent" else "sent")["body_md"]
    draft = outreach.drafts(data["vid"])[0]
    assert draft["state"] == ("sent" if outcome == "sent" else "draft")
    assert draft["delivery_resolution"]["evidence"] == "founder_attested"
    assert draft["delivery_resolution"]["actor"] == data["actor"]
    assert pmf.read_metric(data["vid"], "outbound_touches") == (1 if outcome == "sent" else 0)
    assert outreach.send_slots_used_today(data["vid"]) == (1 if outcome == "sent" else 0)
    with data["factory"]() as session:
        account = session.get(CreditAccount, data["account"])
        assert account.reserved_micro == 0 and account.spent_micro == (10_000 if outcome == "sent" else 0)
        approval = session.get(Approval, draft["delivery_approval_id"])
        detail = json.loads(approval.detail_json)
        execution = detail["external_execution"]
        assert execution["state"] == ("succeeded" if outcome == "sent" else "failed_definitive")
        assert execution["delivery_resolution"]["evidence"] == "founder_attested"
        reservation = session.get(CreditReservation, execution["reservation_id"])
        assert reservation.status == ("settled" if outcome == "sent" else "released")
        ledger = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.idempotency_key == "settle:" + reservation.id)).all()
        assert len(ledger) == (1 if outcome == "sent" else 0)
        events = session.scalars(select(GtmEmailEvent).where(
            GtmEmailEvent.event_type == ("cold_sent" if outcome == "sent" else "cold_released"))).all()
        assert len(events) == 1
        assert json.loads(events[0].metadata_json)["evidence"] == "founder_attested"
        contact = session.get(GtmContact, draft["contact_id"])
        assert contact.times_emailed == (1 if outcome == "sent" else 0)
        assert company_block(session, data["wid"], session.get(GtmContact, data["peer"])) == (
            "company_contact_hold" if outcome == "sent" else None)
        if outcome == "sent":
            touch = session.scalar(select(GtmTouch).where(GtmTouch.venture_id == data["vid"]))
            assert touch.message_id is None and not touch.simulated
            assert json.loads(touch.honesty_json)["evidence"] == "founder_attested"
            assert detail["execution_consumed_at"]
    if outcome == "not_sent":
        assert draft["requires_batch_approval"] and draft["review_state"] == "draft"
        assert draft["batch_id"] != data["draft"]["batch_id"]
        for payload in ({}, {"approved_by_founder": True, "batch_id": data["draft"]["batch_id"]}):
            asyncio.run(outreach.send_batch(data["vid"], payload, actor_subject=data["actor"]))
        assert outreach.drafts(data["vid"])[0]["state"] == "draft"


def test_attested_sent_does_not_feed_shared_email_learning(held, monkeypatch):
    from app.gtm import email_learning
    from app.gtm.shared_models import SharedEmailOutcome

    def never_record(*args, **kwargs):
        pytest.fail("Founder attestation must not feed shared email learning")

    monkeypatch.setattr(email_learning, "record", never_record)
    assert "Recorded as" in _resolve(held, "sent")["body_md"]
    with held["factory"]() as session:
        assert session.scalar(select(SharedEmailOutcome)) is None


@pytest.mark.parametrize("state", ["draft", "sent", "manual", "skipped", "delivery_pending"])
def test_wrong_state_or_live_send_refused(held, state):
    from app.autopilot import outreach
    from core.clock import utcnow

    draft = held["draft"] | {"state": state, "delivery_reserved_at": utcnow().isoformat()}
    outreach._save_draft(held["vid"], draft, draft["doc_id"])
    with pytest.raises(ValueError, match="not awaiting"):
        _resolve(held, "sent")
    assert not outreach.delivery_resolution_actions(draft)


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_stale_pending_delivery_can_be_resolved(held, outcome):
    from app.autopilot import outreach
    from core.clock import utcnow

    draft = held["draft"] | {"state": "delivery_pending", "delivery_reserved_at": (utcnow() - timedelta(minutes=2)).isoformat()}
    outreach._save_draft(held["vid"], draft, draft["doc_id"])
    assert len(outreach.delivery_resolution_actions(draft)) == 2
    assert "Recorded as" in _resolve(held, outcome)["body_md"]


def test_other_venture_and_missing_draft_refused(held):
    from app.autopilot import outreach
    from app.db.models import Venture
    from core.clock import utcnow

    with held["factory"]() as session:
        session.add(Venture(id="ven_other", workspace_id=held["wid"], name="Other", raw_idea="Other",
            stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    for draft_id in (held["draft"]["doc_id"], "missing"):
        with pytest.raises(ValueError, match="not saved in this venture"):
            outreach.resolve_delivery("ven_other", {"draft_id": draft_id, "outcome": "sent"}, actor_subject=held["actor"])


@pytest.mark.parametrize("outcome", ["delivered", None, [], {}])
def test_invalid_outcome_refused(held, outcome):
    with pytest.raises(ValueError, match="whether it is"):
        _resolve(held, outcome)


def test_requires_the_send_approve_permission(held, monkeypatch):
    from app.autopilot import outreach
    from app.chat.actions import get_action
    from app.chat.execute import execute_action
    from app.workspaces import service

    assert get_action("outreach.delivery_resolve").required_permission == "approve"
    monkeypatch.setattr(service, "role_of", lambda *args: "viewer")
    payload = {"draft_id": held["draft"]["doc_id"], "outcome": "sent"}
    with pytest.raises(PermissionError):
        asyncio.run(execute_action(held["vid"], "outreach.delivery_resolve", payload=payload, actor_subject=held["actor"]))
    with pytest.raises(PermissionError):
        outreach.resolve_delivery(held["vid"], payload, actor_subject=held["actor"])


def test_held_batch_card_keeps_resolution_buttons(held):
    from app.autopilot import outreach

    reply = asyncio.run(outreach.send_batch(held["vid"], {"approved_by_founder": True,
        "batch_id": held["draft"]["batch_id"]}, actor_subject=held["actor"]))
    assert "earlier Gmail delivery" in reply["body_md"]
    assert {action["label"] for action in reply["actions"]} >= {"It's in my Sent folder", "It's not in Sent"}


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_accounting_interruption_resumes_same_attestation(held, monkeypatch, outcome):
    from app.approvals import external

    original = external._set_execution
    failed = False

    def interrupt(approval_id, update):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("interrupted after credit accounting")
        return original(approval_id, update)

    monkeypatch.setattr(external, "_set_execution", interrupt)
    assert "same Sent folder button" in _resolve(held, outcome)["body_md"]
    with pytest.raises(ValueError, match="earlier Sent folder check"):
        _resolve(held, "not_sent" if outcome == "sent" else "sent")
    assert "Recorded as" in _resolve(held, outcome)["body_md"]
    assert "already resolved" in _resolve(held, outcome)["body_md"]


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_buttons_execute_through_chat_confirm(held, outcome):
    from fastapi.testclient import TestClient
    from app.main import create_app

    card = next(action for action in held["card"]["actions"]
                if (action.get("confirmation", {}).get("resulting_state") or {}).get("outcome") == outcome)
    client = TestClient(create_app())
    payload = {"action": card["action"], "payload": card["confirmation"]["resulting_state"]}
    response = client.post(f"/ventures/{held['vid']}/chat/confirm", json=payload, headers={"X-Subject": held["actor"]})
    assert response.status_code == 200, response.text
    assert "Recorded as" in response.json()["body_md"]
    again = client.post(f"/ventures/{held['vid']}/chat/confirm", json=payload, headers={"X-Subject": held["actor"]})
    assert again.status_code == 200 and "already resolved" in again.json()["body_md"]


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
@pytest.mark.parametrize("held", [True], indirect=True)
def test_legacy_held_draft_finds_original_approval_and_cold_reservation(held, outcome):
    from app.autopilot import outreach

    draft = dict(held["draft"])
    draft.pop("delivery_approval_id")
    outreach._save_draft(held["vid"], draft, draft["doc_id"])
    assert "Recorded as" in _resolve(held, outcome)["body_md"]


def test_active_send_admission_prevents_attestation(held):
    from app.autopilot.send_admission import try_send_admission
    from app.autopilot import outreach

    with try_send_admission(held["vid"]) as admitted:
        assert admitted
        assert "still in progress" in _resolve(held, "sent")["body_md"]
    assert outreach.drafts(held["vid"])[0]["state"] == "delivery_unconfirmed"


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
@pytest.mark.parametrize("state", ["delivery_pending", "delivery_unconfirmed"])
@pytest.mark.parametrize("held", [False, True], indirect=True, ids=["metered", "legacy"])
def test_orphaned_send_fence_allows_only_founder_reconciliation(held, postgres_admission, outcome, state):
    from app.autopilot import outreach
    from app.db.models import CreditAccount, CreditLedgerEntry
    from core.clock import utcnow

    factory, _ = postgres_admission
    draft = held["draft"] | {"state": state, "delivery_reserved_at": (utcnow() - timedelta(minutes=2)).isoformat()}
    outreach._save_draft(held["vid"], draft, draft["doc_id"])
    with factory() as session:
        session.execute(text("INSERT INTO provider_admission_fences (scope,token) VALUES (:scope,'killed-owner')"),
            {"scope": held["vid"]})
        session.commit()
    retry = asyncio.run(outreach.send_batch(held["vid"], {
        "approved_by_founder": True, "batch_id": draft["batch_id"]}, actor_subject=held["actor"]))
    assert "Held drafts will not be sent automatically again" in retry["body_md"]
    assert outreach.drafts(held["vid"])[0]["state"] == state
    with held["factory"]() as session:
        account = session.get(CreditAccount, held["account"])
        assert account.reserved_micro == 10_000 and account.spent_micro == 0
    with factory() as session:
        session.execute(text("INSERT INTO provider_admission_fences (scope,token) VALUES (:scope,'killed-owner')"),
            {"scope": held["vid"]})
        session.commit()
    assert "Recorded as" in _resolve(held, outcome)["body_md"]
    assert "already resolved" in _resolve(held, outcome)["body_md"]
    recovered = outreach.drafts(held["vid"])[0]
    assert recovered["state"] == ("sent" if outcome == "sent" else "draft")
    if outcome == "not_sent":
        assert recovered["requires_batch_approval"] and recovered["review_state"] == "draft"
        assert recovered["batch_id"] != draft["batch_id"]
    with held["factory"]() as session:
        account = session.get(CreditAccount, held["account"])
        assert account.reserved_micro == 0 and account.spent_micro == (10_000 if outcome == "sent" else 0)
        assert len(session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == held["account"],
            CreditLedgerEntry.idempotency_key.like("settle:%"))).all()) == (1 if outcome == "sent" else 0)
    with factory() as session:
        assert not session.scalar(text("SELECT count(*) FROM provider_admission_fences"))
