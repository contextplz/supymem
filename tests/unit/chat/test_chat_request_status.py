"""Read-only, actor/conversation-scoped resolution of opaque chat checkpoints."""
import json

import pytest

from tests.company_os_harness import load_company_os_app, local_owner_headers


@pytest.fixture
def chat_request(tmp_path, monkeypatch, request):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/requests.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", "chat-status-test-secret-at-least-32-bytes")
    load_company_os_app()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import state
    from app.api import chat_api
    from app.authz.workspace import ensure_subject_person
    from app.chat import requests
    from app.chat.conversations import bind_conversation, create
    from app.db.models import Venture
    from app.tenancy import bind
    from app.workspaces.service import create_workspace
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from core.assertion import sign_assertion
    from core.clock import utcnow

    state.reset_db_factory()
    local = "local_provider_chat" in request.fixturenames
    actor = LOCAL_PERSON_ID if local else "usr_chat_status"
    ensure_subject_person(actor)
    workspace = create_workspace(kind="company", name="Chat status", owner_id=actor)
    venture_id = "ven_chat_status"
    with state.db()() as session:
        session.add(Venture(id=venture_id, workspace_id=workspace.id, name="Chat status",
                            raw_idea="Chat status", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with bind(workspace_id=workspace.id, venture_id=venture_id, actor=actor):
        conversation = create(venture_id, idempotency_key="status-conversation")
    cid = conversation["id"]
    app = FastAPI()
    app.include_router(chat_api.router)

    def headers(path, method="GET", subject=actor):
        return {"X-Subject": subject, "X-COS-Assertion": sign_assertion(
            "chat-status-test-secret-at-least-32-bytes", sub=subject, email="", role="user", path=path, method=method)}

    def reserve():
        with bind(workspace_id=workspace.id, venture_id=venture_id, actor=actor):
            with bind_conversation(venture_id, cid):
                admission, replay = requests.admit(venture_id, actor, "status-nonce", {
                    "text": "Original paid request", "reply_to_question": False, "question_id": None, "source": None})
                assert replay is None
                return admission

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        if local:
            monkeypatch.setenv("SUPYMEM_MODE", "local")
            monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp_path / "owner-session.secret"))
            client.headers.update(local_owner_headers())
        yield {"client": client, "headers": headers, "reserve": reserve, "cid": cid, "actor": actor,
               "vid": venture_id, "wid": workspace.id, "factory": state.db(), "api": chat_api}


def get_status(chat, nonce="status-nonce", cid=None, subject=None):
    path = f"/ventures/{chat['vid']}/chat/requests/{nonce}"
    headers = chat["headers"](path, **({"subject": subject} if subject else {}))
    return chat["client"].get(path, headers=headers, params={"conversation_id": cid or chat["cid"]})


@pytest.mark.parametrize("domain", ["warehouse maintenance teams", "weekend walking groups"])
def test_saved_finished_reply_resolves_checkpoint_without_redispatch(chat_request, domain):
    from app.chat import requests
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind

    chat = chat_request
    admission = chat["reserve"]()
    reply = {"id": "finished", "role": "assistant", "body_md": f"Your results for {domain} are saved. Review them next.",
             "kind": "text", "conversation_id": chat["cid"]}
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            requests.uncertain(admission, reply)
    for _ in range(2):
        response = get_status(chat)
        assert response.status_code == 200, response.text
        assert response.json() == {"state": "done"}


@pytest.mark.parametrize("outcome,expected", [("pending", "pending"), ("completed", "done"), ("unknown", "unknown")])
def test_status_reads_admitted_outcome_without_mutation(chat_request, outcome, expected):
    from app.db.supymem_models import FounderRequest
    chat = chat_request
    admission = chat["reserve"]()
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        row.outcome = outcome
        if outcome == "completed":
            row.text = json.dumps({**json.loads(row.text), "response": {"id": "reply", "conversation_id": chat["cid"]}})
        session.commit()
        original = row.text
    for _ in range(2):
        response = get_status(chat)
        assert response.status_code == 200, response.text
        assert response.json() == {"state": expected}
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        assert (row.text, row.outcome) == (original, outcome)
        assert "Original paid request" not in row.text


def test_missing_or_other_conversation_nonce_is_unknown(chat_request):
    from app.chat.conversations import create
    from app.tenancy import bind
    chat = chat_request
    chat["reserve"]()
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        other = create(chat["vid"], idempotency_key="other-conversation")
    expected = {"state": "unknown", "missing": True}
    assert get_status(chat, "missing-nonce").json() == expected
    response = get_status(chat, cid=other["id"])
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize("corrupt", ["not-json", "[]", '{}', '{"request_digest": null}'])
def test_corrupt_completed_envelope_is_not_done(chat_request, corrupt):
    from app.db.supymem_models import FounderRequest
    chat = chat_request
    admission = chat["reserve"]()
    with chat["factory"]() as session:
        row = session.get(FounderRequest, admission.id)
        row.outcome, row.text = "completed", corrupt
        session.commit()
    assert get_status(chat).json() == {"state": "unknown"}


def test_status_denies_other_actor_even_if_workspace_member(chat_request):
    from app.authz.workspace import ensure_subject_person
    from app.db.models import WorkspaceMember
    from core.clock import utcnow
    chat = chat_request
    chat["reserve"]()
    other = "usr_other_chat_status"
    ensure_subject_person(other)
    with chat["factory"]() as session:
        session.add(WorkspaceMember(id="member_chat_status", workspace_id=chat["wid"], person_id=other, role="member", created_at=utcnow()))
        session.commit()
    assert get_status(chat, subject=other).status_code == 404
    assert get_status(chat, subject="usr_unrelated").status_code == 404


def test_status_requires_actor_and_valid_key(chat_request):
    chat = chat_request
    path = f"/ventures/{chat['vid']}/chat/requests/status-nonce"
    assert chat["client"].get(path, params={"conversation_id": chat["cid"]}).status_code == 404
    response = get_status(chat, nonce="n" * 129)
    assert response.status_code == 422


def test_status_of_real_chat_post_is_done_without_second_dispatch(chat_request, monkeypatch):
    from app.chat.store import append_message
    chat = chat_request
    calls = []

    async def handler(venture_id, text, **kwargs):
        calls.append(text)
        return append_message(venture_id, role="assistant", body_md="Saved outcome")

    monkeypatch.setattr(chat["api"], "handle_message", handler)
    path = f"/ventures/{chat['vid']}/chat"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": "What happened?", "conversation_id": chat["cid"], "idempotency_key": "status-nonce"})
    assert response.status_code == 200, response.text
    assert get_status(chat).json() == {"state": "done"}
    assert calls == ["What happened?"]


@pytest.mark.parametrize("idea", ["Fleet inspection planning for manufacturers", "Meal planning for families"])
@pytest.mark.parametrize("already_saved", [False, True])
def test_unkeyed_unknown_outcome_returns_a_saved_next_step_without_releasing_admission(chat_request, monkeypatch, idea, already_saved):
    from fastapi import HTTPException
    from sqlalchemy import select
    from app.chat.store import append_message
    from app.db.models import Venture
    from app.db.supymem_models import FounderRequest

    chat = chat_request
    with chat["factory"]() as session:
        session.get(Venture, chat["vid"]).raw_idea = idea
        session.commit()
    calls, receipts = [], []

    async def handler(venture_id, text, **kwargs):
        calls.append(kwargs["request_id"])
        append_message(venture_id, role="user", body_md=text)
        if already_saved:
            receipts.append(append_message(venture_id, role="assistant", kind="error",
                body_md="Your provider stopped before returning a result. Check your saved work in Tasks.",
                actions=[{"label": "Check saved work", "action": "status", "style": "primary"}],
                idempotency_key="chat-request-failure:" + kwargs["request_id"]))
        raise HTTPException(409, detail={"error": {"code": "chat_request_outcome_unknown"}})

    monkeypatch.setattr(chat["api"], "handle_message", handler)
    path = f"/ventures/{chat['vid']}/chat"
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": "What happened to my last request?", "conversation_id": chat["cid"], "reply_to_question": True})
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["role"] == "assistant" and reply["kind"] == "error"
    assert "saved work" in reply["body_md"] and "chat_request" not in reply["body_md"]
    assert reply["actions"] and all(action.get("action") == "status" for action in reply["actions"])
    if already_saved:
        assert reply["id"] == receipts[0]["id"]
    assert len(calls) == 1 and calls[0]
    keyed = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
        "message": "What happened to my last request?", "conversation_id": chat["cid"],
        "reply_to_question": True, "idempotency_key": "preserve-keyed-fence"})
    assert keyed.status_code == 409 and keyed.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert len(calls) == 2
    with chat["factory"]() as session:
        rows = session.scalars(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1")).all()
        assert {row.id for row in rows} == set(calls) and all(row.outcome == "unknown" for row in rows)


@pytest.fixture(params=["Inventory forecasting for wholesalers", "Cooking lessons for families"])
def local_provider_chat(chat_request, monkeypatch, request):
    from types import SimpleNamespace

    from app.chat import llm_router
    from app.chat.store import append_message
    from app.core import providers_local
    from llm_gateway import provider_foundry, sole
    from llm_gateway.boundary import Decision
    from llm_gateway.limiter import httpx

    chat = chat_request
    monkeypatch.setattr(provider_foundry, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    models = {role: "gpt-4o-mini" for role in ("chat", "planning", "extraction", "vision")}
    providers_local.save_settings(provider="openai", api_key="synthetic-model-key", models=models)
    received = []

    def post(transport, nonce="local-model-turn", **payload):
        monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
            httpx.AsyncClient(**{**kwargs, "transport": httpx.MockTransport(transport)}))
        path = f"/ventures/{chat['vid']}/chat"
        return chat["client"].post(path, headers=chat["headers"](path, "POST"), json={
            "message": request.param, "conversation_id": chat["cid"], "idempotency_key": nonce, **payload})

    async def handler(venture_id, text, *, request_id, **kwargs):
        received.append(text)
        await llm_router._ask_model(venture_id, text, SimpleNamespace(venture={"workspace_id": chat["wid"]}),
                                    request_id=f"{request_id}:router")
        return append_message(venture_id, role="assistant", body_md="You can review your notes next.")

    monkeypatch.setattr(chat["api"], "handle_message", handler)
    return post, received


@pytest.mark.parametrize("loopback", [False, True], ids=["cloud", "loopback"])
def test_local_connect_failure_offers_to_resend_the_same_message(local_provider_chat, loopback):
    from app.core.providers_local import save_settings
    from app.credits.local_meter import _database
    from app.org.characters import parse_speaker
    from llm_gateway.limiter import httpx

    if loopback:
        save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
                      models={"chat": "gpt-4o-mini"})

    post, received = local_provider_chat
    attempts = []

    def connect_failed(request):
        attempts.append(request)
        raise httpx.ConnectError("private TLS diagnostic", request=request)

    response = post(connect_failed)
    assert response.status_code == 200, response.text
    message = parse_speaker(response.json()["body_md"])[1]
    assert message.startswith("Charlie could not reach your AI provider. Nothing was sent. ")
    assert ("Start your model server" if loopback else "Check your internet connection") in message
    assert ("Check your internet connection" if loopback else "Start your model server") not in message
    assert "Service URL in Settings > Keys and connections > Your AI provider, then tap Try again." in message
    retry, = [action for action in response.json()["actions"] if action["label"] == "Try again"]
    assert retry["action"] == "chat.reply"
    assert len(attempts) == len(received) == 1
    with _database() as connection:
        spend, = connection.execute("SELECT status, cost_usd_micro FROM calls").fetchall()
        assert tuple(spend) == ("unspent", 0)

    def connected(request):
        attempts.append(request)
        return httpx.Response(200, json={"id": "chatcmpl_fixture", "object": "chat.completion", "created": 1,
            "model": "gpt-4o-mini", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"acts":[]}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}})

    retried = post(connected, nonce="fresh-click", message=retry["payload"]["text"])
    assert retried.status_code == 200, retried.text
    assert parse_speaker(retried.json()["body_md"])[1] == "You can review your notes next."
    assert len(attempts) == 2 and received == [received[0], received[0]]


def test_local_auth_failure_points_to_the_saved_key(local_provider_chat):
    from app.org.characters import parse_speaker
    from llm_gateway.limiter import httpx

    post, received = local_provider_chat
    response = post(lambda request: httpx.Response(401, json={"error": "private diagnostic"}))
    assert response.status_code == 200, response.text
    assert parse_speaker(response.json()["body_md"])[1] == ("Your AI provider rejected your API key.\n\n"
        "Open Settings > Keys and connections > Your AI provider, replace your API key, then test your connection.")
    assert not any(action["label"] == "Try again" for action in response.json()["actions"])
    assert len(received) == 1


def test_local_provider_error_saves_a_plain_next_step(chat_request, local_provider_chat):
    from app.chat.conversations import bind_conversation
    from app.chat.store import list_messages
    from app.org.characters import parse_speaker
    from app.tenancy import bind
    from llm_gateway.limiter import httpx

    chat = chat_request
    post, received = local_provider_chat
    attempts = []

    def read_failed(request):
        attempts.append(request)
        raise httpx.ReadError("private diagnostic", request=request)

    response = post(read_failed)
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            saved, = list_messages(chat["vid"], conversation_id=chat["cid"])
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "chat_request_outcome_unknown"
    assert saved["kind"] == "error" and parse_speaker(saved["body_md"])[1] == (
        "Your AI provider did not return a confirmed result.\n\n"
        "Open Settings > Keys and connections > Your AI provider, check your API key, service address and chat model, "
        "then test your connection. Check your saved work before trying again. Nothing was retried.")
    assert not any(action["label"] == "Try again" for action in saved["actions"])
    assert len(attempts) == len(received) == 1


@pytest.mark.parametrize("idea", ["Equipment quotes for manufacturers", "Music lessons for families"])
def test_disconnected_client_keeps_a_replayable_reply_and_logs_the_reason(chat_request, monkeypatch, caplog, idea):
    from app.chat.store import append_message
    from starlette.requests import Request

    chat = chat_request
    calls = []

    async def disconnected(request):
        return True

    async def handler(venture_id, text, **kwargs):
        calls.append(venture_id)
        return append_message(venture_id, role="assistant", body_md="Your draft is saved. Open Customers to review it.")

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    monkeypatch.setattr(chat["api"], "handle_message", handler)
    path = f"/ventures/{chat['vid']}/chat"
    payload = {"message": "Prepare interview invitations for " + idea,
               "conversation_id": chat["cid"], "idempotency_key": "disconnected-reply"}
    response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=payload)
    assert response.status_code == 200, response.text
    replay = chat["client"].post(path, headers=chat["headers"](path, "POST"), json=payload)
    assert replay.status_code == 200 and replay.json()["id"] == response.json()["id"]
    assert calls == [chat["vid"]]
    records = [record for record in caplog.records if getattr(record, "reason_code", None) == "chat_reply_after_disconnect"]
    assert len(records) == 1 and records[0].venture_id == chat["vid"]
    assert idea not in caplog.text


def test_every_founder_turn_gets_a_reply_or_saved_retry_error(chat_request, monkeypatch):
    from app.chat import recovery
    from app.chat.store import append_message
    from app.chat.conversations import bind_conversation, create
    from app.db.models import ChatMessage, Venture
    from app.tenancy import bind
    from core.clock import utcnow
    chat = chat_request
    for index, (idea, text) in enumerate([("Vendor payments for manufacturers", "Interview purchasing teams about late invoices"),
                                         ("Accessible holidays for families", "Interview parents about trip planning")]):
        venture_id = f"ven_reply_{index}"
        with chat["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=chat["wid"], name=idea, raw_idea=idea,
                stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        with bind(workspace_id=chat["wid"], venture_id=venture_id, actor=chat["actor"]):
            conversation_id = create(venture_id, idempotency_key=f"reply_{index}")["id"]
            with bind_conversation(venture_id, conversation_id):
                for notice in range(14):
                    append_message(venture_id, role="assistant", body_md=f"Saved milestone {notice}")
                assert not recovery.post_failure(venture_id, step="chat.action", key="unprompted", body_md="A background step failed.",
                    kind="action_failed", code="chat_reply_failed")
        for case, broken in enumerate([None, {}, {"id": "empty", "body_md": " "},
                                       {"id": "stamp", "body_md": "<!--employee:Charlie:Manager:manager-->"},
                                       {"id": "echo", "role": "user", "body_md": text}]):
            async def handler(vid, body, **kwargs):
                append_message(vid, role="user", body_md=body)
                return broken
            monkeypatch.setattr(chat["api"], "handle_message", handler)
            path = f"/ventures/{venture_id}/chat"
            response = chat["client"].post(path, headers=chat["headers"](path, "POST"), json={"message": text,
                "conversation_id": conversation_id, "idempotency_key": f"reply_{index}_{case}", "reply_to_question": True})
            assert response.status_code == 200, response.text
            reply = response.json()
            assert reply["role"] == "assistant" and "I couldn't prepare your reply." in reply["body_md"]
            assert "Choose Retry to check your saved work." in reply["body_md"]
            assert "Your saved work is kept" in reply["body_md"]
            retry = next(action for action in reply["actions"] if action.get("action") == "chat.reply")
            assert retry["payload"]["text"] == text
            with chat["factory"]() as session:
                assert session.get(ChatMessage, reply["id"]).conversation_id == conversation_id
