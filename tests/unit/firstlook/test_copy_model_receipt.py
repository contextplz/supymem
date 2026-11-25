"""Firstlook's actual SDK request and SQLite wallet, without a provider connection."""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, _base_client

from tests.unit.firstlook.test_generation_admission import admitted as admitted

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
MODEL = "supymem-gpt-6-1-sol"


@pytest.mark.parametrize("purpose,limit", [("intake.classify", 900), ("intake.suggest_name", 80), ("firstlook.copy", 900)])
@pytest.mark.parametrize("deployment,idea_text", [
    (MODEL, "A warehouse shift handoff board"),
    ("DeepSeek-V4-Pro", "A family meal planning journal"),
])
def test_captured_json_quote_matches_real_sdk_wire(admitted, monkeypatch, purpose, limit, deployment, idea_text):
    from app.core.local_mode import is_local
    from app.intake.model_capture import capture_request
    from app.llm import pricing
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.provider_foundry import ProviderConfig

    monkeypatch.setenv("LLM_API_KEY", "offline-wire-key")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "{}")
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "_workspace_resolver", lambda _: pytest.fail("captured route re-resolved"))
    config = ProviderConfig(source="platform_default", api_key="offline-wire-key",
                            base_url="https://offline.invalid", deployment_flash=deployment)
    if is_local():
        from app.core.providers_local import load_local_config, save_settings
        save_settings(provider="azure_openai", base_url="https://offline.invalid",
                      models={"chat": deployment}, api_key="offline-wire-key")
        config = load_local_config()
    config, messages, schema, captured = capture_request([
        {"role": "system", "content": "Return JSON."}, {"role": "user", "content": idea_text}],
        purpose=purpose, request_id="original-synthetic-intent", max_tokens=limit,
        response_schema={"type": "json_object"}, config=config)
    bodies, clients = [], []
    responses = config.api_mode == "responses"

    def respond(request):
        bodies.append(json.loads(request.content))
        assert request.headers["x-stainless-retry-count"] == "0"
        if responses:
            assert request.url.path == "/openai/v1/responses"
            return httpx.Response(200, json={"id": "resp_capture", "object": "response", "status": "completed",
                "model": deployment, "service_tier": "default", "output": [{"type": "message", "id": "msg_capture",
                    "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "{}",
                        "annotations": []}]}], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})
        assert request.url.path == "/openai/v1/chat/completions"
        return httpx.Response(200, json={"id": "chat_capture", "object": "chat.completion", "model": deployment,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def factory(frozen):
        client = AsyncOpenAI(api_key="offline-wire-key", base_url="https://offline.invalid/openai/v1",
            max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False))
        clients.append(client)
        return client, frozen

    def boundary(messages, **kwargs):
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid map after capture")
        monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("repriced after capture"))
        return Decision(messages=messages)

    monkeypatch.setattr(sole, "boundary_decision", boundary)
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    cap = captured["model_binding"]["max_output_tokens"]
    asyncio.run(sole.transmit(messages, purpose=purpose, config=config, tier=config.captured_tier,
        max_tokens=cap, response_schema=schema, single_attempt=True, use_workspace_override=False))
    assert len(bodies) == 1 and clients[0].is_closed()
    assert cap == config.max_output_tokens == (2048 if responses else limit)
    assert bodies[0]["model"] == captured["model_binding"]["deployment"] == deployment
    assert bodies[0]["max_output_tokens" if responses else "max_tokens"] == cap
    assert captured["token_ceiling"] == captured["model_binding"]["input_token_limit"] + cap
    assert captured["estimate_micro"] > 0
    if responses:
        assert bodies[0]["text"]["format"] == schema and bodies[0]["reasoning"] == {"effort": "low"}
    else:
        assert bodies[0]["response_format"] == schema


@pytest.fixture
def paid(admitted, monkeypatch):
    from app.credits import accounts, gate, metered, provider_receipts
    if "before_dispatch" not in inspect.signature(metered.call).parameters:
        pytest.skip("S02 post-reservation dispatch hook is not available; Firstlook capture stays default-off")
    from app.db.models import OnboardingStep
    from app.firstlook import copy
    from app.llm import founder_provider, pricing
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from tests.unit.credits.conftest import _seed_credit_infra
    from tests.integration.test_postgres_firstlook_publication import COPY

    _seed_credit_infra()
    account = accounts.ensure_account("ven_firstlook")
    monkeypatch.setenv("LLM_API_KEY", "offline-wire-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.invalid/models")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("FIRSTLOOK_COPY_MODE", "live")
    monkeypatch.setenv("COS_FIRSTLOOK_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({copy.PURPOSE: {
        "model": MODEL, "catalog_model": "gpt-6.1-sol", "reasoning_effort": "low"}}))
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "_workspace_resolver", lambda _: pytest.fail("captured route re-resolved BYOK"))
    monkeypatch.setattr(gate, "_task_usage", lambda *a, **k: None)
    bodies, clients, receipts = [], [], []
    options = {"body": json.dumps(COPY), "tier": "default", "input_tokens": 100}
    original_price = provider_receipts.validate_confirmed_provider_receipt

    def price(*args, **kwargs):
        with admitted.db()() as session:
            saved = session.get(OnboardingStep, admitted.step.id)
            assert "first" in json.loads(saved.output_json)["_firstlook_copy"]
        result = original_price(*args, **kwargs)
        receipts.append(result)
        return result

    monkeypatch.setattr(provider_receipts, "validate_confirmed_provider_receipt", price)

    def respond(request):
        bodies.append(json.loads(request.content))
        assert request.url.path == "/openai/v1/responses"
        assert request.headers["x-stainless-retry-count"] == "0"
        return httpx.Response(200, json={"id": "resp_copy", "object": "response", "status": "completed",
            "model": MODEL, "service_tier": options["tier"], "output": [{"type": "message", "id": "msg_copy",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                    "text": options["body"], "annotations": []}]}], "usage": {
                "input_tokens": options["input_tokens"], "output_tokens": 20, "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80},
                "output_tokens_details": {"reasoning_tokens": 7}}})

    def factory(config):
        assert config.captured_tier == "flash" and config.catalog_model == "gpt-6.1-sol"
        assert config.max_output_tokens == 2048
        assert accounts.get_account_by_id(account.id).reserved_micro > 0
        client = AsyncOpenAI(api_key="offline-wire-key", base_url="https://offline.invalid/openai/v1",
            max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False))
        clients.append(client)
        return client, config

    def boundary(messages, **kwargs):
        assert kwargs["provider"] == "foundry:" + MODEL
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid replacement after hold")
        monkeypatch.setattr(pricing, "load_pricing", lambda: pytest.fail("repriced after hold"))
        return Decision(messages=messages)

    monkeypatch.setattr(sole, "boundary_decision", boundary)
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)

    def run():
        return asyncio.run(copy.generate_copy("A bakery order board", workspace_id="ws_firstlook",
            venture_id="ven_firstlook", generation_intent=admitted.intent))

    return SimpleNamespace(run=run, options=options, bodies=bodies, clients=clients,
        wallet=lambda: accounts.get_account_by_id(account.id), receipts=receipts)


def test_real_sdk_quote_dial_receipt_and_replay_keep_original_capture(paid):
    result = paid.run()
    assert result[2] is False and result[1] == 248 / 1_000_000
    assert paid.run() == result
    assert len(paid.bodies) == 1 and paid.clients[0].is_closed()
    body = paid.bodies[0]
    assert body["model"] == MODEL and body["max_output_tokens"] == 2048
    assert body["reasoning"] == {"effort": "low"} and body["service_tier"] == "default"
    assert body["text"]["format"] == {"type": "json_object"}
    assert paid.wallet().spent_micro == 248 and paid.wallet().reserved_micro == 0


@pytest.mark.parametrize("unusable", ["bad-json", "known-priority-tier"])
def test_known_paid_unusable_output_is_charged_once_then_plain_copy(paid, unusable):
    if unusable == "bad-json":
        paid.options["body"] = "not JSON"
    else:
        paid.options["tier"] = "priority"
    expected = 248 if unusable == "bad-json" else 496
    first = paid.run()
    assert first[2] is True and first[1] == expected / 1_000_000
    assert paid.run() == first and len(paid.bodies) == 1
    assert paid.wallet().spent_micro == expected and paid.wallet().reserved_micro == 0


def test_unknown_original_receipt_remains_held_and_cannot_redial_or_become_copy(paid):
    from llm_gateway.errors import ProviderCallError
    paid.options["input_tokens"] = True
    for _ in range(2):
        with pytest.raises(ProviderCallError, match="reconciliation"):
            paid.run()
    assert len(paid.bodies) == 1 and paid.clients[0].is_closed()
    assert paid.wallet().spent_micro == 0 and paid.wallet().reserved_micro > 0


def test_revoked_admission_after_reserve_releases_unused_hold_without_sdk(paid, monkeypatch):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    from app.db.models import OnboardingRun
    from app.state import db
    enter = gate._enter

    def revoke(**kwargs):
        meter = enter(**kwargs)
        run_id = kwargs["request_id"].split(":", 2)[1]
        with db()() as session:
            session.get(OnboardingRun, run_id).status = "cancelled"
            session.commit()
        return meter

    monkeypatch.setattr(gate, "_enter", revoke)
    with pytest.raises(CreditsUnavailable):
        paid.run()
    assert paid.bodies == [] and paid.clients == []
    assert paid.wallet().spent_micro == paid.wallet().reserved_micro == 0
