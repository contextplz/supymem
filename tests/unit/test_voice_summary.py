"""Empty live calls do not create transcript notes or founder chat turns."""
from sqlalchemy import select
import pytest

from tests.unit.chat.test_chat_request_status import chat_request


@pytest.fixture
def local_voice_request(chat_request, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.company_os_harness import local_owner_headers
    from app.core.local_owner import ensure_owner
    from app.workspaces.service import create_workspace
    from app.db.models import Venture
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    owner = ensure_owner()
    workspace = create_workspace(kind="company", name="Voice test", owner_id=owner.id)
    venture_id = "ven_local_typed"
    with chat_request["factory"]() as session:
        session.add(Venture(id=venture_id, workspace_id=workspace.id, name="Voice test",
            raw_idea="Voice test", stage="draft", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with TestClient(chat_request["client"].app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        yield {**chat_request, "client": client, "actor": owner.id, "wid": workspace.id, "vid": venture_id,
            "headers": lambda *_: {**local_owner_headers(), "X-Subject": owner.id}}


@pytest.mark.parametrize("provider,model,key", [
    ("openai", "saved-realtime-model", "fixture-openai-key"),
    ("anthropic", "saved-chat-model", "fixture-other-key"),
    ("openai", None, "fixture-openai-key"),
    ("openai", "saved-realtime-model", ""),
])
def test_local_voice_uses_saved_openai_provider_and_realtime_role(local_voice_request, monkeypatch, provider, model, key):
    from types import SimpleNamespace
    from pydantic import SecretStr
    from fastapi import HTTPException
    from app.api import voice_api, greeting_api
    from app.core import providers_local

    chat_request = local_voice_request
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("SUPYMEM_VOICE_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_REALTIME_API_KEY", raising=False)
    config = SimpleNamespace(provider=provider, api_key=SecretStr(key), _local_role_models={"realtime": model})
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    assert voice_api._api_key() == (key if provider == "openai" else "")
    assert greeting_api._api_key() == (key if provider == "openai" else "")
    assert voice_api.voice_enabled(chat_request["actor"]) is (provider == "openai" and bool(key))
    if provider == "openai" and key:
        assert voice_api.voice_model() == (model or voice_api.DEFAULT_MODEL)
    else:
        with pytest.raises(HTTPException) as error:
            voice_api._mint(chat_request["actor"], None, None, voice_api.build_entry_session)
        assert "OpenAI key" in error.value.detail["error"]["message"]
        assert "Settings" in error.value.detail["error"]["message"]
    path = f"/ventures/{chat_request['vid']}/chat"
    response = chat_request["client"].post(path, headers=chat_request["headers"](path, "POST"),
        json={"message": "Who are you?"})
    assert response.status_code == 200 and response.json().get("kind") != "error", response.text


def test_local_voice_receipts_allow_shared_tools_and_saved_summary_without_analytics(local_voice_request, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from pydantic import SecretStr
    import httpx
    from app.api import voice_api
    from app.chat.conversations import create
    from app.core import providers_local
    from app.db.models import AnalyticsEvent, ChatMessage, Venture
    from app.tenancy import bind
    from core.clock import utcnow

    chat_request = local_voice_request
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: SimpleNamespace(provider="openai",
        api_key=SecretStr("fixture-voice-key"), _local_role_models={"realtime": "saved-voice-model"}))
    monkeypatch.setattr(voice_api, "emit", lambda *_args, **_kwargs: pytest.fail("Local voice must not require analytics"))
    monkeypatch.setattr(voice_api, "_live_state", lambda *_: "")
    monkeypatch.setattr(voice_api, "_conversation_context", lambda *_: "")
    started = utcnow()
    monkeypatch.setattr(voice_api.httpx, "post", lambda url, **kwargs: httpx.Response(200,
        json={"value": "fixture-ephemeral-secret", "expires_at": int((started + timedelta(minutes=1)).timestamp())},
        request=httpx.Request("POST", url)))
    async def execute(_name, _args, _context):
        return "Your saved context is available."
    monkeypatch.setattr("app.tools.execute_tool", execute)
    chat_request["client"].app.include_router(voice_api.router)
    for index, domain in enumerate(["Supplier invoice reconciliation", "Family travel planning"]):
        venture_id = f"ven_operational_voice_{index}"
        with chat_request["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=chat_request["wid"], name=domain,
                raw_idea=domain, stage="draft", created_at=started, updated_at=started))
            session.commit()
        with bind(workspace_id=chat_request["wid"], venture_id=venture_id, actor=chat_request["actor"]):
            conversation = create(venture_id, idempotency_key="local-voice")
        start_path = f"/ventures/{venture_id}/voice/session"
        start_body = {"idempotency_key": "local-start", "conversation_id": conversation["id"],
            "tool_capability": "shared_tools_v1"}
        start = chat_request["client"].post(start_path, headers=chat_request["headers"](start_path, "POST"), json=start_body)
        assert start.status_code == 200, start.text
        minted = start.json()
        assert chat_request["client"].post(start_path, headers=chat_request["headers"](start_path, "POST"), json=start_body).json() == minted
        receipt_id = minted["voice_telemetry_id"]
        assert receipt_id and minted["model"] == "saved-voice-model"
        assert "need the founder to click the confirmation card" not in minted["instructions"] and "tap Approve" not in minted["instructions"]
        with chat_request["factory"]() as session:
            assert session.get(AnalyticsEvent, receipt_id) is None
            receipt = voice_api._session_receipt(session, receipt_id)
            assert (receipt.person_id, receipt.workspace_id, receipt.venture_id) == (
                chat_request["actor"], chat_request["wid"], venture_id)
        tool_path = f"/ventures/{venture_id}/voice/tools/venture_context"
        body = {"session_event_id": receipt_id, "turn_id": "turn-one", "call_id": "call-one",
            "conversation_id": conversation["id"], "arguments": '{"section":"idea"}'}
        reply = chat_request["client"].post(tool_path, headers=chat_request["headers"](tool_path, "POST"), json=body)
        assert reply.status_code == 200, reply.text
        assert chat_request["client"].post(tool_path, headers=chat_request["headers"](tool_path, "POST"), json=body).json() == reply.json()
        summary_path = f"/ventures/{venture_id}/voice/summary"
        summary = {"session_event_id": receipt_id, "conversation_id": conversation["id"], "turns": [
            {"id": "one", "role": "founder", "text": f"Help me test {domain}", "final": True, "posted": True}]}
        response = chat_request["client"].post(summary_path, headers=chat_request["headers"](summary_path, "POST"), json=summary)
        assert response.status_code == 200, response.text
        with chat_request["factory"]() as session:
            notes = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                ChatMessage.kind == "voice_summary")).all()
            assert len(notes) == 1 and domain in notes[0].body_md
        for person, venture, workspace in [("another-owner", venture_id, chat_request["wid"]),
                (chat_request["actor"], "another-venture", chat_request["wid"]),
                (chat_request["actor"], venture_id, "another-workspace")]:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as denied:
                voice_api._reserve_tool(voice_api.VoiceToolCall(**body), "venture_context", {"section": "idea"}, person, venture, workspace)
            assert denied.value.status_code == 404
        monkeypatch.setattr(voice_api, "utcnow", lambda: started + timedelta(minutes=21))
        expired = chat_request["client"].post(summary_path, headers=chat_request["headers"](summary_path, "POST"), json=summary)
        assert expired.status_code == 404
        assert "Start a new call" in expired.json()["detail"]["error"]["message"]
        monkeypatch.setattr(voice_api, "utcnow", lambda: started)


def test_local_voice_receipt_storage_error_has_safe_recovery_step(local_voice_request, monkeypatch):
    from app.api import voice_api
    from app.chat import store
    from fastapi import HTTPException
    def unavailable():
        raise OSError("fixture-private-detail")
    monkeypatch.setattr(store, "db", unavailable)
    for domain in ["Supplier invoice reconciliation", "Family travel planning"]:
        with pytest.raises(HTTPException) as error:
            voice_api._save_session_receipt(local_voice_request["actor"], local_voice_request["wid"], domain)
        assert error.value.status_code == 503
        message = error.value.detail["error"]["message"]
        assert "free space" in message and "restart your app" in message
        assert "fixture-private-detail" not in message


@pytest.mark.parametrize("domain", ["Supplier invoice reconciliation", "Family travel planning"], ids=["b2b", "consumer"])
def test_voice_service_keeps_session_retries_encrypted_and_scoped(local_voice_request, monkeypatch, domain):
    from datetime import timedelta
    from fastapi import HTTPException
    from app.api import voice_api
    from app import voice_service
    from app.connectors.secrets import decrypt_at_rest
    from app.db.models import IdempotencyRecord
    from core.clock import utcnow

    assert voice_api._service is voice_service
    started = utcnow()
    monkeypatch.setattr(voice_api, "utcnow", lambda: started)
    calls = []
    def mint(person, workspace_id, venture_id, build, *, session_id):
        calls.append((person, workspace_id, venture_id))
        return {"client_secret": "fixture-ephemeral-token", "voice_session_id": session_id,
            "expires_at": int((started + timedelta(minutes=1)).timestamp())}
    monkeypatch.setattr(voice_api, "_mint", mint)
    person, workspace = local_voice_request["actor"], local_voice_request["wid"]
    body = voice_api.VoiceSessionRequest(idempotency_key="retry-scope", screen={"title": domain})
    response = voice_api._start_session(person, workspace, domain, None, None, body)
    assert voice_api._start_session(person, workspace, domain, None, None, body) == response
    assert calls == [(person, workspace, domain)]
    with local_voice_request["factory"]() as session:
        record = session.scalar(select(IdempotencyRecord).where(IdempotencyRecord.scope.startswith("voice-start:")))
        assert "fixture-ephemeral-token" not in record.response_json
        assert voice_api.json.loads(decrypt_at_rest(voice_api.json.loads(record.response_json)["response"])) == response
    with pytest.raises(HTTPException) as conflict:
        voice_api._start_session(person, workspace, domain, None, None,
            body.model_copy(update={"screen": {"title": "Different context"}}))
    assert conflict.value.status_code == 409 and len(calls) == 1
    other = voice_api._start_session(person, workspace, "another-venture", None, None, body)
    assert other["voice_session_id"] != response["voice_session_id"] and len(calls) == 2
    monkeypatch.setattr(voice_api, "utcnow", lambda: started + timedelta(minutes=2))
    with pytest.raises(HTTPException) as expired:
        voice_api._start_session(person, workspace, domain, None, None, body)
    assert expired.value.detail["error"]["code"] == "voice_start_expired" and len(calls) == 2


def test_local_entry_voice_summary_uses_operational_receipt(local_voice_request, monkeypatch):
    from app.api import voice_api
    chat_request = local_voice_request
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    chat_request["client"].app.include_router(voice_api.router)
    for domain in ["Warehouse inventory planning", "Family music lessons"]:
        receipt = voice_api._save_session_receipt(chat_request["actor"], None, None)
        path = "/entry/voice/summary"
        body = {"session_event_id": receipt, "turns": [
            {"id": "one", "role": "founder", "text": f"Help me test {domain}", "final": True}]}
        response = chat_request["client"].post(path, headers=chat_request["headers"](path, "POST"), json=body)
        assert response.status_code == 200, response.text
        assert domain in response.json()["body_md"]
        assert domain in next(note["body_md"] for note in voice_api.entry_voice_notes(chat_request["actor"]) if note["id"] == response.json()["id"])


def test_local_spoken_greeting_uses_saved_key_and_metered_call(local_voice_request, monkeypatch):
    from types import SimpleNamespace
    from pydantic import SecretStr
    import httpx
    from app.api import greeting_api
    from app.core import providers_local
    from app.credits import metered
    chat_request = local_voice_request
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: SimpleNamespace(provider="openai",
        api_key=SecretStr("fixture-greeting-key"), _local_role_models={}))
    greeting_api._cache.clear()
    calls = []
    original = metered.call_sync
    def meter(callback, **kwargs):
        calls.append(kwargs)
        return original(callback, **kwargs)
    monkeypatch.setattr(metered, "call_sync", meter)
    monkeypatch.setattr(greeting_api.httpx, "post", lambda url, **kwargs: httpx.Response(200,
        content=b"fixture-audio", request=httpx.Request("POST", url)))
    chat_request["client"].app.include_router(greeting_api.router)
    path = "/me/greeting/audio"
    for name in ["Alex", "Riley"]:
        monkeypatch.setattr(greeting_api, "_state", lambda _person, name=name: {"first_name": name})
        response = chat_request["client"].post(path, headers=chat_request["headers"](path, "POST"))
        assert response.status_code == 200, response.text
        assert name in response.json()["text"]
    assert len(calls) == 2 and all(call["purpose"] == "greeting.tts" and call["estimate_micro"] > 0 for call in calls)


def test_voice_summary_ignores_greetings_noise_and_unconfirmed_fragments(chat_request, monkeypatch):
    from app.api import voice_api
    from app.db.models import AnalyticsEvent, ChatMessage, IdempotencyRecord, Venture
    from core.clock import utcnow
    chat = chat_request
    chat["client"].app.include_router(voice_api.router)
    for index, domain in enumerate(["Supplier invoice reconciliation", "Family travel planning"]):
        venture_id = f"ven_voice_{index}"
        with chat["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=chat["wid"], name=domain, raw_idea=domain,
                stage="draft", created_at=utcnow(), updated_at=utcnow()))
            session.add(AnalyticsEvent(id=f"ane_voice_{index}", person_id=chat["actor"], workspace_id=chat["wid"],
                venture_id=venture_id, event_name="voice.session_started", occurred_at=utcnow()))
            session.add(AnalyticsEvent(id=f"ane_entry_{index}", person_id=chat["actor"],
                event_name="voice.session_started", occurred_at=utcnow()))
            session.commit()
        for entry in [False, True]:
            path = "/entry/voice/summary" if entry else f"/ventures/{venture_id}/voice/summary"
            event_id = f"ane_entry_{index}" if entry else f"ane_voice_{index}"
            # Multi-word unconfirmed speech is saved for review (never dispatched); see tests/security/test_voice_session.py.
            for caption, final in [(None, True), ("E?", False), ("um", True), ("hm uh", True), ("...", True), ("help", False)]:
                turns = [{"id": "greeting", "role": "assistant", "text": "Hello. What is on your mind?", "final": True}]
                if caption is not None:
                    turns.append({"id": "founder", "role": "founder", "text": caption, "final": final})
                response = chat["client"].post(path, headers=chat["headers"](path, "POST"),
                    json={"session_event_id": event_id, "turns": turns})
                assert response.status_code == 200, response.text
                assert response.json() == {"ignored": True}
            real = voice_api.VoiceSummaryTurn(id="real", role="founder", text=f"Help me test {domain}", final=True)
            assert voice_api._has_founder_speech([real])
        with chat["factory"]() as session:
            assert session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture_id)) is None
            assert not list(session.scalars(select(IdempotencyRecord).where(
                IdempotencyRecord.scope.like("%voice%"))).all())
