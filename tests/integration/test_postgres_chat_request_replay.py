"""Signed migrated-PG chat turns survive client ambiguity without redispatch."""
import asyncio
import secrets

import httpx
import pytest
from sqlalchemy import delete, select

from tests.integration.test_postgres_auth_security import pg_database as pg_database  # noqa: F401
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db  # noqa: F401


@pytest.fixture
def chat_turn(journey_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import chat_api
    from core.assertion import sign_assertion

    d = journey_db
    secret = secrets.token_hex(32)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    app = FastAPI()
    app.include_router(chat_api.router)
    path = f"/ventures/{d['vid']}/chat"

    def headers(method="POST", suffix="", actor=None):
        actor = actor or d["actor"]
        return {"X-Subject": actor, "X-COS-Assertion": sign_assertion(
            secret, sub=actor, email="", role="user", path=path + suffix, method=method)}

    client = TestClient(app)
    response = client.post(path + "/conversations", headers=headers(suffix="/conversations"),
                           json={"idempotency_key": secrets.token_hex(8)})
    assert response.status_code == 200
    cid = response.json()["id"]
    body = {"message": "What evidence is saved?", "conversation_id": cid,
            "idempotency_key": secrets.token_hex(8)}
    return d, app, client, path, headers, body, chat_api


def test_same_key_returns_exact_saved_reply_without_second_handler(chat_turn, monkeypatch):
    from app.chat.store import append_message
    from app.db.models import ChatMessage

    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        append_message(vid, role="user", body_md=text)
        return append_message(vid, role="assistant", body_md="The complete original reply.")

    monkeypatch.setattr(api, "handle_message", handler)
    first = client.post(path, headers=headers(), json=body)
    repeated = client.post(path, headers=headers(), json=body)
    assert first.status_code == repeated.status_code == 200
    assert repeated.json() == first.json()
    assert len(calls) == 1
    with d["factory"]() as session:
        rows = list(session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == body["conversation_id"])))
        assert len(rows) == 2


def test_client_timeout_does_not_allow_concurrent_redispatch(chat_turn, monkeypatch):
    from app.chat.store import append_message

    _, app, _, path, headers, body, api = chat_turn
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(vid, text, **kwargs):
            calls.append(text)
            append_message(vid, role="user", body_md=text)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return append_message(vid, role="assistant", body_md="The retained reply after the client wait ended.")

        monkeypatch.setattr(api, "handle_message", handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://owned.test") as client:
            first = asyncio.create_task(client.post(path, headers=headers(), json=body))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(first), 0.02)
                repeated = await client.post(path, headers=headers(), json=body)
                assert repeated.status_code == 409
                assert repeated.json()["detail"]["error"]["code"] == "chat_request_in_progress"
                assert len(calls) == 1
            finally:
                release.set()
                original = await asyncio.wait_for(first, 5)
            assert original.status_code == 200
            replay = await client.post(path, headers=headers(), json=body)
            assert replay.status_code == 200 and replay.json() == original.json()
            assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("change", [{"message": "A changed whole question."},
                                   {"question_id": "another-question"}, {"reply_to_question": True}])
def test_completed_key_rejects_changed_payload(chat_turn, monkeypatch, change):
    from app.chat.store import append_message
    _, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        return append_message(vid, role="assistant", body_md="Saved whole reply.")

    monkeypatch.setattr(api, "handle_message", handler)
    assert client.post(path, headers=headers(), json=body).status_code == 200
    refused = client.post(path, headers=headers(), json={**body, **change})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"]["code"] == "chat_request_payload_conflict"
    assert len(calls) == 1


def test_cancelled_handler_is_permanently_held_without_redispatch(chat_turn, monkeypatch):
    from app.db.supymem_models import FounderRequest
    d, app, _, path, headers, body, api = chat_turn
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def handler(vid, text, **kwargs):
            calls.append(text)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(api, "handle_message", handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://owned.test") as client:
            task = asyncio.create_task(client.post(path, headers=headers(), json=body))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(2):
                refused = await client.post(path, headers=headers(), json=body)
                assert refused.status_code == 409
                assert refused.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
            assert len(calls) == 1
    asyncio.run(scenario())
    with d["factory"]() as session:
        row = session.scalar(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"]))
        assert row.outcome == "unknown"
        assert body["message"] not in row.text


def test_replay_rechecks_membership_and_private_actor(chat_turn, monkeypatch):
    from app.chat.store import append_message
    from app.db.models import Workspace, WorkspaceMember
    from app.authz.workspace import ensure_subject_person
    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        return append_message(vid, role="assistant", body_md="Private completed reply.")

    monkeypatch.setattr(api, "handle_message", handler)
    first = client.post(path, headers=headers(), json=body)
    assert first.status_code == 200
    other = "usr_other_" + secrets.token_hex(8)
    ensure_subject_person(other)
    with d["factory"]() as session:
        from core.clock import utcnow
        session.add(WorkspaceMember(id="wsm_" + secrets.token_hex(8), workspace_id=d["wid"], person_id=other, role="viewer", created_at=utcnow()))
        session.commit()
    assert client.post(path, headers=headers(actor=other), json=body).status_code == 404
    with d["factory"]() as session:
        session.get(Workspace, d["wid"]).owner_id = other
        session.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == d["wid"],
                                                      WorkspaceMember.person_id == d["actor"]))
        session.commit()
    assert client.post(path, headers=headers(), json=body).status_code == 404
    assert len(calls) == 1


def test_same_nonce_is_partitioned_by_server_conversation(chat_turn, monkeypatch):
    from app.chat.store import append_message
    _, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        return append_message(vid, role="assistant", body_md="This conversation's reply.")

    monkeypatch.setattr(api, "handle_message", handler)
    first = client.post(path, headers=headers(), json=body)
    new = client.post(path + "/conversations", headers=headers(suffix="/conversations"),
                      json={"idempotency_key": secrets.token_hex(8)}).json()
    second_body = {**body, "conversation_id": new["id"]}
    second = client.post(path, headers=headers(), json=second_body)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert second.json()["conversation_id"] == new["id"]
    assert client.post(path, headers=headers(), json=second_body).json() == second.json()
    assert len(calls) == 2


def test_reservation_commits_and_no_transaction_survives_provider_wait(chat_turn, monkeypatch):
    from app.chat.store import append_message
    from app.db.supymem_models import FounderRequest
    from sqlalchemy import text as sql_text
    d, _, client, path, headers, body, api = chat_turn
    replies = []

    async def handler(vid, text, **kwargs):
        with d["factory"]() as session:
            row = session.scalar(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"]))
            assert row.outcome == "pending"
            # Another connection can own the request lock while the handler awaits.
            assert session.scalar(sql_text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                                  {"key": "chat-request:" + row.id}) is True
        await asyncio.sleep(0)
        reply = append_message(vid, role="assistant", body_md="The provider-free exact response.", actions=[{"label": "Read"}])
        replies.append(reply)
        return reply

    monkeypatch.setattr(api, "handle_message", handler)
    result = client.post(path, headers=headers(), json=body)
    assert result.status_code == 200
    assert result.json() == replies[0]


def test_scope_revoked_during_handler_withholds_reply_and_holds_request(chat_turn, monkeypatch):
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import FounderRequest
    from core.clock import utcnow
    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        reply = append_message(vid, role="assistant", body_md="Not returned after retirement.")
        with d["factory"]() as session:
            session.get(Venture, vid).archived_at = utcnow()
            session.commit()
        return reply

    monkeypatch.setattr(api, "handle_message", handler)
    first = client.post(path, headers=headers(), json=body)
    assert first.status_code == 409
    assert first.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert client.post(path, headers=headers(), json=body).status_code == 404
    with d["factory"]() as session:
        row = session.scalar(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"]))
        assert row.outcome == "unknown"
    assert len(calls) == 1


def test_post_admission_http_refusal_stays_unknown_after_saved_effect(chat_turn, monkeypatch):
    from fastapi import HTTPException
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        append_message(vid, role="user", body_md=text)
        raise HTTPException(409, detail={"error": {"code": "ordinary_changed_state"}})

    monkeypatch.setattr(api, "handle_message", handler)
    for _ in range(2):
        result = client.post(path, headers=headers(), json=body)
        assert result.status_code == 409
        assert result.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(calls) == 1
    with d["factory"]() as session:
        rows = session.scalars(select(ChatMessage).where(ChatMessage.conversation_id == body["conversation_id"])).all()
        assert len(rows) == 1


def test_unconfirmed_provider_error_keeps_saved_history_and_unknown_request(chat_turn, monkeypatch):
    from app.chat.store import append_message
    from app.db.models import ChatMessage
    from app.db.supymem_models import FounderRequest
    from llm_gateway.errors import ProviderCallError

    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(vid, text, **kwargs):
        calls.append(text)
        append_message(vid, role="user", body_md=text)
        raise ProviderCallError("provider_outcome_unknown", "Private synthetic provider details.")

    monkeypatch.setattr(api, "handle_message", handler)
    for _ in range(2):
        reply = client.post(path, headers=headers(), json=body)
        assert reply.status_code == 409
        assert reply.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(calls) == 1
    history = client.get(path, headers=headers("GET"), params={"conversation_id": body["conversation_id"]})
    assert history.status_code == 200
    errors = [item for item in history.json()["items"] if item.get("kind") == "error"]
    assert len(errors) == 1
    assert errors[0]["actions"]
    assert "Private synthetic provider details" not in errors[0]["body_md"]
    with d["factory"]() as session:
        journal = session.scalars(select(FounderRequest).where(
            FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"])).one()
        assert journal.outcome == "unknown"
        assert len(session.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == body["conversation_id"])).all()) == 2


def test_provider_status_without_confirmed_release_cannot_complete_request(chat_turn, monkeypatch):
    from app.db.supymem_models import FounderRequest
    from llm_gateway.errors import ProviderCallError

    d, _, client, path, headers, body, api = chat_turn
    calls = []

    async def handler(*args, **kwargs):
        calls.append(True)
        # A status alone is not source proof that the protected hold released.
        raise ProviderCallError("bad_api_key", "Synthetic refusal.", status=401)

    monkeypatch.setattr(api, "handle_message", handler)
    for _ in range(2):
        result = client.post(path, headers=headers(), json=body)
        assert result.status_code == 409
        assert result.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(calls) == 1
    with d["factory"]() as session:
        assert session.scalars(select(FounderRequest).where(
            FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"])).one().outcome == "unknown"


@pytest.mark.parametrize("outcome", ["ambiguous_error", "missing_usage", "release_unconfirmed", "confirmed_rejection"])
def test_actual_assistant_credit_outcome_controls_chat_completion(chat_turn, monkeypatch, outcome):
    from types import SimpleNamespace

    from app.chat import assistant
    from app.chat.store import append_message
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.db.models import CreditLedgerEntry, CreditReservation
    from app.db.supymem_models import FounderRequest
    from app.llm import founder_provider
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError

    d, _, client, path, headers, body, api = chat_turn
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(
        model="DeepSeek-V4-Pro", provider="azure_foundry", source="platform_default"))
    account = ensure_account(d["vid"], workspace_id=d["wid"], owner_person_id=d["actor"])
    before = get_account_by_id(account.id)
    handler_calls, provider_calls = [], []
    if outcome == "release_unconfirmed":
        monkeypatch.setattr(assistant, "_rejection_release_confirmed", lambda *args, **kwargs: False)

    async def transport(messages, **kwargs):
        provider_calls.append(kwargs)
        with d["factory"]() as session:
            hold = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == account.id, CreditReservation.purpose == assistant.ASSISTANT_PURPOSE)).one()
            assert hold.status == "reconciling"
        if outcome == "missing_usage":
            return SimpleNamespace(usage={}, choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="Unconfirmed synthetic reply."))])
        code, status = ("provider_down", 503) if outcome == "ambiguous_error" else ("bad_api_key", 401)
        raise ProviderCallError(code, "Private synthetic provider details.", status=status)

    async def handler(vid, text, **kwargs):
        handler_calls.append(text)
        append_message(vid, role="user", body_md=text)
        # Only the transport is replaced. The actual source and migrated credit
        # ledger must decide known release versus permanently protected unknown.
        answer = await assistant._complete([{"role": "user", "content": text}], workspace_id=d["wid"])
        return append_message(vid, role="assistant", body_md=answer)

    monkeypatch.setattr(sole, "transmit", transport)
    monkeypatch.setattr(api, "handle_message", handler)
    first = client.post(path, headers=headers(), json=body)
    replay = client.post(path, headers=headers(), json=body)
    known = outcome == "confirmed_rejection"
    assert first.status_code == replay.status_code == (200 if known else 409)
    if known:
        assert first.json() == replay.json()
        assert first.json()["kind"] == "error"
    else:
        assert first.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
        assert replay.json() == first.json()
    assert len(handler_calls) == len(provider_calls) == 1
    assert provider_calls[0]["single_attempt"] is True
    history = client.get(path, headers=headers("GET"), params={"conversation_id": body["conversation_id"]})
    assert history.status_code == 200
    errors = [item for item in history.json()["items"] if item.get("kind") == "error"]
    assert len(errors) == 1 and errors[0]["actions"]
    assert "Private synthetic provider details" not in errors[0]["body_md"]
    with d["factory"]() as session:
        journal = session.scalars(select(FounderRequest).where(
            FounderRequest.source == "chat_turn_v1", FounderRequest.venture_id == d["vid"])).one()
        assert journal.outcome == ("completed" if known else "unknown")
        hold = session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == account.id, CreditReservation.purpose == assistant.ASSISTANT_PURPOSE)).one()
        assert hold.status == ("released" if outcome in {"confirmed_rejection", "release_unconfirmed"} else "reconciling")
        assert hold.settled_micro is None
        assert not session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account.id, CreditLedgerEntry.kind == "spend")).all()
    assert get_account_by_id(account.id).spent_micro == before.spent_micro
