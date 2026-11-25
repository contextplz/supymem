"""Ops cutover rows for chat: exact purpose only, and removing a row is a true fallback."""

import json

import pytest
from llm_gateway.provider_foundry import load_provider_config_from_env
from llm_gateway.routing import resolve_purpose_config
from tests.unit.chat import test_assistant_routing
from tests.unit.credits.test_chat_provider_captures import COUNTS

chat_api_env = test_assistant_routing.env
SOL = {"model": "supymem-gpt-6-1-sol", "catalog_model": "gpt-6.1-sol", "reasoning_effort": "low"}
ROUTE, ASSISTANT = "venture.chat.route", "venture.chat.assistant"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "prod")
    monkeypatch.setenv("LLM_API_KEY", "cutover-" + "fixture")
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Flash")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
    return lambda rows: monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps(rows))


def _route(purpose):
    return resolve_purpose_config(purpose, load_provider_config_from_env())


def test_route_row_moves_only_the_router(env):
    env({"app.proxy": SOL, ROUTE: SOL})
    route = _route(ROUTE)
    assert (route.api_mode, route.deployment_flash, route.catalog_model, route.reasoning_effort) == (
        "responses", "supymem-gpt-6-1-sol", "gpt-6.1-sol", "low")
    for other in (ASSISTANT, "intake.classify", "intake.suggest_name"):
        assert _route(other).api_mode == "chat_completions"
        assert _route(other).deployment_flash == "DeepSeek-V4-Flash"


def test_both_chat_rows_and_removal_falls_back_to_deepseek(env):
    env({"app.proxy": SOL, ROUTE: SOL, ASSISTANT: SOL})
    assert _route(ASSISTANT).api_mode == _route(ROUTE).api_mode == "responses"
    env({"app.proxy": SOL})
    for purpose in (ROUTE, ASSISTANT):
        cfg = _route(purpose)
        assert cfg.api_mode == "chat_completions" and cfg.deployment_flash == "DeepSeek-V4-Flash"


def test_unknown_purpose_row_rejects_the_whole_map(env):
    env({ROUTE: SOL, "venture.chat.unknown": SOL})
    with pytest.raises(Exception):
        _route(ROUTE)


@pytest.mark.hosted_only
@pytest.mark.parametrize("tools", [False, True], ids=["answer", "tools"])
@pytest.mark.parametrize("bad_output", [False, True], ids=["usable", "paid-invalid-router"])
def test_chat_api_sol_capture_and_deepseek_rollback(chat_api_env, monkeypatch, tools, bad_output):
    from openai import AsyncOpenAI, _base_client
    from sqlalchemy import select

    from app.chat import llm_router
    from app.credits import accounts, policy
    from app.db.models import CreditLedgerEntry, CreditReservation, EventLog, Venture
    from app.db.supymem_models import FounderRequest
    from app.state import db
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.provider_foundry import normalize_base_url
    from tests.unit.credits.conftest import _seed_credit_infra

    client, vid = chat_api_env
    from app.chat import agent
    monkeypatch.setattr(agent, "TURN_BUDGET_MICRO", 500_000)
    _seed_credit_infra()
    policy.invalidate()
    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    wallet = accounts.ensure_account(vid, workspace_id=wid)
    for name, value in {"LLM_OFFLINE": "0", "LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_API_KEY": "synthetic-chat-cutover-key", "LLM_BASE_URL": "https://offline.invalid/models",
            "LLM_DEPLOYMENT_FLASH": "DeepSeek-V4-Flash", "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro",
            "CHAT_ROUTER_MODE": "live", "CHAT_AGENT_TOOLS": "1" if tools else "0"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    for name in ("_primary_chat", "_fallback_chat", "_usage_recorder"):
        monkeypatch.setattr(sole, name, None)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(llm_router, "_state_hint", lambda *args: {"venture": "Synthetic saved state"})
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    message = "Explain the saved context in plain language, then describe what people say about this idea."
    wire = []

    def respond(request):
        payload = json.loads(request.content)
        modern = request.url.path.endswith("/responses")
        routing = (payload.get("text", {}).get("format", {}).get("type") == "json_object" if modern
                   else payload.get("response_format", {}).get("type") == "json_object")
        purpose = ROUTE if routing else ASSISTANT
        with db()() as session:
            holds = session.scalars(select(CreditReservation).where(
                CreditReservation.account_id == wallet.id, CreditReservation.status == "reconciling")).all()
            assert len(holds) == 1 and holds[0].purpose == purpose
            if modern:
                assert payload["max_output_tokens"] == (2048 if routing else 2304 if tools else 3072)
                events = session.scalars(select(EventLog).where(EventLog.topic == "credits.model.receipt.calling")).all()
                assert any(json.loads(event.payload_json)["usage"]["request_id"] == holds[0].request_id
                           for event in events)
        wire.append((purpose, modern, payload["model"]))
        text = (json.dumps({"acts": [{"intent": "assistant", "text": message}], "clarify": ""})
                if routing else "The saved idea is early; evidence about demand is still uncertain.")
        if bad_output and routing:
            text = "Private synthetic invalid JSON"
        if modern:
            return httpx.Response(200, json={"id": "resp_chat_cutover", "object": "response", "created_at": 0,
                "status": "completed", "model": payload["model"], "service_tier": "default", "usage": COUNTS,
                "output": [{"id": "msg_chat_cutover", "type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}]})
        return httpx.Response(200, json={"id": "chatcmpl_cutover", "object": "chat.completion", "created": 0,
            "model": payload["model"], "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    def factory(config):
        return AsyncOpenAI(api_key=config.api_key.get_secret_value(),
            base_url=normalize_base_url(config.provider, config.base_url, api_mode=config.api_mode), max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)), config

    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({"app.proxy": SOL, ROUTE: SOL, ASSISTANT: SOL}))
    reply = client.post(f"/ventures/{vid}/chat", json={"message": message, "idempotency_key": "sol-cutover"},
                        headers={"X-Subject": "founder"})
    assert reply.status_code == 200, reply.text
    assert "Private synthetic invalid JSON" not in reply.text
    assert reply.json()["kind"] != "error" and "evidence about demand" in reply.json()["body_md"]
    assert wire == [(ROUTE, True, SOL["model"]), (ASSISTANT, True, SOL["model"])]
    with db()() as session:
        admission = session.scalar(select(FounderRequest).where(FounderRequest.source == "chat_turn_v1"))
        assert admission.outcome == "completed"
        suffix = ":act:0:tools:model-round:0" if tools else ":act:0:answer"
        expected = {admission.id + carrier + ":provider-attempt:0" for carrier in (":router", suffix)}
        holds = session.scalars(select(CreditReservation).where(CreditReservation.account_id == wallet.id)).all()
        spends = session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == wallet.id, CreditLedgerEntry.kind == "spend")).all()
        events = session.scalars(select(EventLog).where(EventLog.topic == "credits.model.receipt.completed")).all()
        assert {hold.request_id for hold in holds} == {spend.request_id for spend in spends} == expected
        assert len(holds) == len(spends) == len(events) == 2
        assert all(hold.status == "settled" and hold.model == SOL["model"] for hold in holds)
        assert {json.loads(event.payload_json)["usage"]["request_id"] for event in events} == expected
    replay = client.post(f"/ventures/{vid}/chat", json={"message": message, "idempotency_key": "sol-cutover"},
        headers={"X-Subject": "founder"})
    assert replay.status_code == 200 and replay.json() == reply.json() and len(wire) == 2
    conflict = client.post(f"/ventures/{vid}/chat", json={"message": message + " changed", "idempotency_key": "sol-cutover"},
        headers={"X-Subject": "founder"})
    assert conflict.status_code == 409 and len(wire) == 2
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({"app.proxy": SOL}))
    rollback = client.post(f"/ventures/{vid}/chat", json={"message": message, "idempotency_key": "sol-rollback"},
                           headers={"X-Subject": "founder"})
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["kind"] != "error" and "evidence about demand" in rollback.json()["body_md"]
    assert wire[2:] == [(ROUTE, False, "DeepSeek-V4-Flash"), (ASSISTANT, False, "DeepSeek-V4-Flash")]
    with db()() as session:
        holds = session.scalars(select(CreditReservation).where(CreditReservation.account_id == wallet.id)).all()
        assert len(holds) == 4 and all(hold.status == "settled" for hold in holds)
