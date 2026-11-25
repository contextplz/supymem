"""Abandoned chat admissions become recoverable without repeating paid work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest

from tests.unit.chat.test_chat_request_status import chat_request, get_status


@pytest.mark.parametrize("idea", ["Procurement receipts for businesses", "Dance classes for families"])
def test_stale_pending_becomes_unknown_without_redispatch(chat_request, monkeypatch, caplog, idea):
    from app.chat import requests
    from app.db.supymem_models import FounderRequest
    from app.tenancy import bind
    from app.chat.conversations import bind_conversation
    from core.clock import utcnow
    from app.db.models import Venture

    chat = chat_request
    admission = chat["reserve"]()
    now = utcnow()
    with chat["factory"]() as session:
        session.get(Venture, chat["vid"]).raw_idea = idea
        row = session.get(FounderRequest, admission.id)
        row.created_at = now - timedelta(seconds=requests.STALE_TURN_SECONDS + 1)
        original = row.text
        session.commit()

    async def forbidden(*args, **kwargs):
        pytest.fail("An abandoned turn must never run again")

    monkeypatch.setattr(chat["api"], "handle_message", forbidden)
    assert get_status(chat).json() == {"state": "pending"}
    assert requests.reconcile_stale_turns(now=now) == 1
    response = get_status(chat)
    assert response.status_code == 200
    assert response.json() == {"state": "unknown"}
    assert requests.reconcile_stale_turns(now=now) == 0
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        assert (row.outcome, row.failure_reason, row.text, row.resolved_at) == (
            "unknown", "chat_turn_unconfirmed", original, None)
    path = f"/ventures/{chat['vid']}/chat"
    replay = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": "Original paid request", "conversation_id": chat["cid"], "idempotency_key": "status-nonce"})
    assert replay.status_code == 409
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]), bind_conversation(chat["vid"], chat["cid"]):
        with pytest.raises(requests.ChatRequestError, match="chat_request_outcome_unknown"):
            requests.complete(admission, {"id": "late-reply"})
    assert {record.reason_code for record in caplog.records if hasattr(record, "reason_code")} >= {
        "chat_turn_abandoned", "chat_reply_after_lease_loss"}
    assert idea not in caplog.text


@pytest.mark.parametrize("outcome,age,source", [
    ("pending", 0, "chat_turn_v1"), ("pending", 900, "chat_turn_v1"),
    ("completed", 901, "chat_turn_v1"), ("unknown", 901, "chat_turn_v1"),
    ("pending", 901, "other_source"),
])
def test_reconcile_leaves_ineligible_rows_untouched(chat_request, monkeypatch, outcome, age, source):
    from app.chat import requests
    from app.db.supymem_models import FounderRequest
    from core.clock import utcnow

    monkeypatch.setattr(requests, "STALE_TURN_SECONDS", 900)
    chat = chat_request
    admission = chat["reserve"]()
    now = utcnow()
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        row.outcome, row.source = outcome, source
        row.created_at = now - timedelta(seconds=age)
        if outcome == "completed":
            row.text = json.dumps({**json.loads(row.text), "response": {"id": "saved-reply"}})
            row.resolved_at = row.created_at
        session.commit()
        session.refresh(row)
        original = (row.outcome, row.failure_reason, row.text, row.resolved_at)
    assert requests.reconcile_stale_turns(now=now) == 0
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        assert (row.outcome, row.failure_reason, row.text, row.resolved_at) == original


def test_reconcile_is_bounded_and_concurrent_sweeps_count_once(chat_request):
    from app.chat import requests
    from app.chat.conversations import bind_conversation
    from app.db.supymem_models import FounderRequest
    from app.tenancy import bind
    from core.clock import utcnow

    chat = chat_request
    chat["reserve"]()
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]), bind_conversation(chat["vid"], chat["cid"]):
        requests.admit(chat["vid"], chat["actor"], "second-nonce", {"text": "Another turn"})
    now = utcnow()
    with chat["factory"]() as session:
        for row in session.query(FounderRequest).all():
            row.created_at = now - timedelta(seconds=requests.STALE_TURN_SECONDS + 1)
        session.commit()
    assert requests.reconcile_stale_turns(now=now, limit=0) == 0
    assert requests.reconcile_stale_turns(now=now, limit=1) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: requests.reconcile_stale_turns(now=now, limit=1), range(2)))
    assert sorted(results) == [0, 1]
    assert requests.reconcile_stale_turns(now=now) == 0


@pytest.mark.parametrize("outcome,age,blocked", [
    ("pending", 901, True), ("unknown", 0, True),
    ("unknown", 900, True), ("unknown", 901, False),
])
def test_voice_only_releases_stale_unknown_turns(chat_request, monkeypatch, outcome, age, blocked):
    from app.chat import requests
    from app.chat.conversations import bind_conversation
    from app.db.supymem_models import FounderRequest
    from app.api.voice_api import _unconfirmed_chat_in_conversation
    from app.tenancy import bind
    from core.clock import utcnow

    monkeypatch.setattr(requests, "STALE_TURN_SECONDS", 900)
    chat = chat_request
    admission = chat["reserve"]()
    now = utcnow()
    monkeypatch.setattr(requests, "utcnow", lambda: now)
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        row.outcome = outcome
        row.created_at = now - timedelta(seconds=age)
        session.commit()
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]), bind_conversation(chat["vid"], chat["cid"]):
        assert _unconfirmed_chat_in_conversation(chat["vid"], chat["wid"], chat["actor"]) is blocked
        if not blocked:
            with chat["factory"]() as session:
                session.get(FounderRequest, admission.id).text = "unreadable receipt"
                session.commit()
            assert _unconfirmed_chat_in_conversation(chat["vid"], chat["wid"], chat["actor"]) is True
