"""Offline transport doubles verify capture, exact usage and single dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from llm_gateway import sole
from llm_gateway.cassette import Cassette
from llm_gateway.client import LLMClient
from llm_gateway.errors import ProviderCallError
from llm_gateway.ledger import TokenLedger, cost_usd
from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.provider_openai import chat_raw
from llm_gateway.routing import UnroutedPurposeError, resolve_purpose_config
from pydantic import SecretStr


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"sol-deploy","catalog_model":"gpt-6.1-sol"}}')
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: None)
    return ProviderConfig(provider="azure_foundry", source="platform_default",
        base_url="https://resource.services.ai.azure.com", api_key=SecretStr("offline-test"),
        max_output_tokens=512)


def response():
    call = SimpleNamespace(id="call-offline", type="function",
        function=SimpleNamespace(name="read_state", arguments="{}"))
    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100, total_tokens=300,
        prompt_tokens_details=SimpleNamespace(cached_tokens=100),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=40)),
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[call],
            responses_output_items=[{"type": "reasoning", "encrypted_content": "offline-fixture"}]))])


@pytest.mark.asyncio
async def test_live_client_captures_route_and_dispatches_only_once(config, monkeypatch):
    dials = []
    tools = [{"type": "function", "function": {"name": "read_state", "parameters": {"type": "object"}}}]
    schema = {"type": "json_object"}
    async def transmit(messages, **kwargs):
        dials.append(kwargs)
        monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"DeepSeek-V4-Pro"}}')
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    client = LLMClient(config=config, offline=False)
    result = await client.chat([{"role": "user", "content": "Read the current state"}],
        purpose="harness.turn", tools=tools, response_schema=schema, ledger_ref="wsp_offline")
    assert len(dials) == 1
    dial = dials[0]
    assert dial["tools"] == tools and dial["response_schema"] == schema
    assert dial["single_attempt"] is True and dial["use_workspace_override"] is False
    assert (dial["purpose"], dial["tier"], dial["workspace_id"]) == ("harness.turn", "flash", "wsp_offline")
    assert dial["config"].deployment_flash == "sol-deploy"
    assert dial["config"].catalog_model == "gpt-6.1-sol"
    assert dial["config"].captured_tier == "flash"
    assert result.cost_usd == pytest.approx(0.00121)
    assert result.reasoning_tokens == 40
    assert result.tool_calls[0]["function"]["name"] == "read_state"
    assert result.assistant_message["responses_output_items"][0]["type"] == "reasoning"
    assert client.ledger.entries[0].cached_tokens == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("daily_cap_usd", [None, 0, 0.001])
async def test_live_client_never_stops_for_legacy_daily_spend_caps(config, monkeypatch, daily_cap_usd):
    dials = []
    async def transmit(*args, **kwargs):
        dials.append(kwargs)
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    client = LLMClient(config=config, offline=False, daily_cap_usd=daily_cap_usd)
    for _ in range(2):
        await client.chat([{"role": "user", "content": "Read state"}], purpose="harness.turn")
    assert len(dials) == 2 and len(client.ledger.entries) == 2
    assert client.ledger.today_spend(None) > 0.001


@pytest.mark.asyncio
async def test_unregistered_sol_suffix_still_uses_unknown_price_ceiling(config, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", '{"*":{"model":"gpt-6.1-sol-2099-01-01","catalog_model":"gpt-6.1-sol-2099-01-01"}}')
    async def transmit(*args, **kwargs):
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    result = await LLMClient(config=config, offline=False).chat(
        [{"role": "user", "content": "Read state"}], purpose="harness.turn")
    assert result.cost_usd == cost_usd("flash", 200, 100, cached_tokens=100,
        catalog_model="gpt-6.1-sol-2099-01-01")
    assert result.cost_usd > cost_usd("flash", 200, 100, cached_tokens=100, catalog_model="gpt-6.1-sol")


@pytest.mark.asyncio
async def test_client_accepts_an_already_captured_tier_without_recapturing(config, monkeypatch):
    config = resolve_purpose_config("harness.turn", config, tier="pro")
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid after capture")
    monkeypatch.setattr(sole, "transport_prompt_tokens", lambda *args, **kwargs: 100)
    async def transmit(messages, **kwargs):
        assert kwargs["tier"] == "pro"
        assert kwargs["config"].deployment_pro == "sol-deploy"
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    result = await LLMClient(config=config, offline=False).chat([], purpose="harness.turn")
    assert result.tier == "pro"


@pytest.mark.asyncio
async def test_unknown_usage_and_provider_failure_are_never_replayed(config, monkeypatch):
    monkeypatch.setattr(sole, "transport_prompt_tokens", lambda *args, **kwargs: 100)
    calls = []
    async def unknown(*args, **kwargs):
        calls.append(1)
        value = response()
        value.usage = None
        return value
    monkeypatch.setattr(sole, "transmit", unknown)
    client = LLMClient(config=config, offline=False)
    with pytest.raises(ProviderCallError, match="usage"):
        await client.chat([], purpose="harness.turn")
    assert len(calls) == 1 and not client.ledger.entries
    async def failed(*args, **kwargs):
        calls.append(1)
        raise ProviderCallError("rate_limited", "Offline rejection", retryable=True)
    monkeypatch.setattr(sole, "transmit", failed)
    with pytest.raises(ProviderCallError, match="Offline rejection"):
        await client.chat([], purpose="harness.turn")
    assert len(calls) == 2 and not client.ledger.entries


@pytest.mark.asyncio
async def test_live_cassette_retains_confirmed_catalog_cost_and_assistant_items(config, monkeypatch):
    monkeypatch.setattr(sole, "transport_prompt_tokens", lambda *args, **kwargs: 100)
    async def transmit(*args, **kwargs):
        return response()
    monkeypatch.setattr(sole, "transmit", transmit)
    cassette = Cassette(mode="record")
    messages = [{"role": "user", "content": "Read state"}]
    recorded = await LLMClient(config=config, offline=False, cassette=cassette).chat(messages, purpose="harness.turn")
    cassette.mode = "replay"
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid rollout")
    replay = await LLMClient(config=config, offline=True, cassette=cassette).chat(messages, purpose="harness.turn")
    assert replay.cost_usd == recorded.cost_usd
    assert replay.tool_calls == recorded.tool_calls
    assert replay.assistant_message == recorded.assistant_message


@pytest.mark.asyncio
async def test_historical_offline_fixture_budget_and_pricing_remain_separate(config):
    result = await LLMClient(config=config, offline=True).chat([], purpose="intake.classify")
    assert result.tokens_in == 100 and result.tokens_out == 80
    assert result.cost_usd == cost_usd("flash", 100, 80)
    with pytest.raises(UnroutedPurposeError):
        await LLMClient(config=config, offline=True).chat([], purpose="unknown.purpose", tier="pro")


def test_catalog_output_includes_reasoning_and_preserves_fixture_convention():
    assert cost_usd("pro", 1_000_000, 1_000_000, catalog_model="gpt-6.1-sol",
        reasoning_tokens=600_000) == 19.0
    assert cost_usd("pro", 100, 100, reasoning_tokens=40) == pytest.approx(0.0003616)


@pytest.mark.parametrize("prompt,cached,written,expected", [
    (272000, 0, 0, 0.554), (272001, 0, 0, 1.103004),
    (1000, 500, 0, 0.01105), (1000, 0, 400, 0.0122),
    (300000, 100000, 0, 0.835), (300000, 0, 100000, 1.315),
])
def test_sol_context_boundary_and_confirmed_cache_subsets(prompt, cached, written, expected):
    assert cost_usd("flash", prompt, 1000, catalog_model="gpt-6.1-sol",
        cached_tokens=cached, cache_write_tokens=written) == pytest.approx(expected)


@pytest.mark.parametrize("kwargs", [
    {"reasoning_tokens": 1001}, {"cached_tokens": 1001},
    {"cached_tokens": 600, "cache_write_tokens": 500}, {"cached_tokens": -1},
])
def test_catalog_requires_valid_subsets(kwargs):
    with pytest.raises(ValueError):
        cost_usd("pro", 1000, 1000, catalog_model="gpt-6.1-sol", **kwargs)


def test_catalog_aliases_snapshots_unknown_ceiling_and_ledger_metadata():
    assert cost_usd("flash", 1000, 1000, catalog_model="gpt-6.1-sol-2026-09-29") == 0.012
    assert cost_usd("flash", 1000, 1000, catalog_model="supymem-gpt-6-1-sol") == 0.012
    assert cost_usd("flash", 1000, 1000, catalog_model="gpt-6.1-sol-2099-01-01") >= 0.035
    assert cost_usd("flash", 1000, 1000, catalog_model="DeepSeek-V4-Flash") == 0.00042
    assert cost_usd("flash", 1000, 1000, catalog_model="unknown-deployment") >= 0.035
    entry = TokenLedger().record(purpose="harness.turn", tier="flash", tokens_in=200, tokens_out=100,
        reasoning_tokens=40, catalog_model="gpt-6.1-sol", cached_tokens=100)
    assert entry.cost_usd == pytest.approx(0.00121) and entry.catalog_model == "gpt-6.1-sol"


class RawClient:
    def __init__(self):
        self.options, self.chat_calls, self.response_calls = [], [], []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.chat_create))
        self.responses = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.response_create))
    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self
    async def chat_create(self, **kwargs):
        self.chat_calls.append(kwargs)
        return response()
    async def response_create(self, **kwargs):
        self.response_calls.append(kwargs)
        payload = {"status": "completed", "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        return SimpleNamespace(http_response=SimpleNamespace(json=lambda: payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("model,endpoint,token_key", [
    ("gpt-6.1-sol", "responses", "max_output_tokens"),
    ("gpt-6-sol", "chat", "max_completion_tokens"),
    ("DeepSeek-V4-Pro", "chat", "max_tokens"),
])
async def test_raw_adapter_has_explicit_purpose_one_attempt_and_correct_token_limit(model, endpoint, token_key):
    client = RawClient()
    await chat_raw(client, purpose="harness.turn", model=model, messages=[], max_tokens=400)
    assert client.options == [{"max_retries": 0}]
    calls = client.response_calls if endpoint == "responses" else client.chat_calls
    assert len(calls) == 1 and calls[0][token_key] == 400
    assert not (client.chat_calls if endpoint == "responses" else client.response_calls)
    with pytest.raises(UnroutedPurposeError):
        await chat_raw(client, purpose="unknown.purpose", model=model, messages=[])
    assert len(calls) == 1
