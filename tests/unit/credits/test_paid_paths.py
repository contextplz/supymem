"""Synthetic provider receipts; no paid calls or network access."""
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture(scope="module", autouse=True)
def load_app():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()


@pytest.fixture
def metered(monkeypatch):
    from app.credits import metered as module
    from app.credits.gate import Meter
    from app.credits.reserve import Hold

    events = []
    meter = Meter(Hold("test_hold", "test_wallet", 10000), False, "test")
    monkeypatch.setattr(meter, "protect_outcome", lambda: events.append("protect"))

    @contextmanager
    def gate(**kwargs):
        events.append(("reserve", kwargs))
        meter.model = kwargs.get("model")
        yield meter
        if meter.recorded:
            meter.settle_result = SimpleNamespace(actual_micro=meter.actual_micro)
        events.append("exit")

    @asynccontextmanager
    async def async_gate(**kwargs):
        with gate(**kwargs) as current:
            yield current

    monkeypatch.setattr(module, "charge_sync", gate)
    monkeypatch.setattr(module, "charge", async_gate)
    return module, meter, events


def test_sync_fixed_event_reserves_before_call_and_settles(metered):
    module, meter, events = metered
    def provider():
        assert events[0][0] == "reserve" and events[1] == "protect"
        return "receipt"
    assert module.call_sync(provider, purpose="email", account_id="test_wallet", estimate_micro=900) == "receipt"
    assert meter.actual_micro == 900 and events[-1] == "exit"


@pytest.mark.asyncio
async def test_async_variable_cost_uses_confirmed_receipt(metered):
    module, meter, events = metered
    async def provider():
        assert events[1] == "protect"
        return {"micro": 432}
    await module.call(provider, purpose="speech", account_id="test_wallet", estimate_micro=900,
                      actual_micro=lambda response: response["micro"])
    assert meter.actual_micro == 432


@pytest.mark.parametrize("status,unspent", [(401, True), (429, True), (500, False)])
def test_only_definitive_rejection_releases(metered, status, unspent):
    module, meter, _ = metered
    def provider():
        httpx.Response(status, request=httpx.Request("POST", "https://example.test")).raise_for_status()
    with pytest.raises(httpx.HTTPStatusError):
        module.call_sync(provider, purpose="search", account_id="test_wallet", estimate_micro=900)
    assert meter.definitive_failure is unspent and not meter.recorded


@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
def test_unknown_usage_and_transport_keep_hold(metered, failure):
    module, meter, _ = metered
    def provider():
        raise failure("synthetic unknown outcome")
    with pytest.raises(failure):
        module.call_sync(provider, purpose="search", account_id="test_wallet", estimate_micro=900)
    assert not meter.definitive_failure and not meter.recorded


@pytest.mark.hosted_only
def test_zero_estimate_and_passthrough_refuse_paid_dispatch(metered):
    module, meter, _ = metered
    from app.credits.errors import CreditsUnavailable
    called = []
    with pytest.raises(CreditsUnavailable):
        module.call_sync(lambda: called.append(True), purpose="paid", estimate_micro=0)
    meter.passthrough = True
    with pytest.raises(CreditsUnavailable):
        module.call_sync(lambda: called.append(True), purpose="paid", estimate_micro=900)
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [pytest.param("hosted", marks=pytest.mark.hosted_only), "local"])
async def test_settlement_failure_does_not_apply_provider_output(metered, monkeypatch, runtime):
    module, meter, _ = metered
    from app.credits.errors import CreditsUnavailable
    from app.core.local_mode import is_local
    if runtime == "local":
        monkeypatch.setenv("SUPYMEM_MODE", "local")
    @asynccontextmanager
    async def unsettled_gate(**kwargs):
        yield meter
        if is_local():
            meter.task_usage_failed = True
    monkeypatch.setattr(module, "charge", unsettled_gate)
    calls = []
    async def provider():
        calls.append("called_once")
        return "output_must_not_apply"
    with pytest.raises(CreditsUnavailable, match="reconciliation"):
        await module.call(provider, purpose="paid", estimate_micro=900)
    assert calls == ["called_once"] and meter.recorded and meter.settle_result is None


def test_at_cost_rate_and_invalid_price(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from app.credits.metered import price_micro
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setenv("SUPYMEM_METER_EMAIL_SEND_USD_PER_UNIT", "0.0004")
    assert price_micro("email_send", 3) == 1200
    with pytest.raises(CreditsUnavailable):
        price_micro("not_priced")
    monkeypatch.setenv("SUPYMEM_METER_EMAIL_SEND_USD_PER_UNIT", "NaN")
    with pytest.raises(CreditsUnavailable):
        price_micro("email_send")


@pytest.mark.parametrize("domain", ["warehouse.example", "gardening.example"])
def test_local_email_has_no_hosted_usage_price(monkeypatch, domain):
    from app.credits.metered import price_micro
    from app.credits.errors import CreditsUnavailable

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_METER_EMAIL_SEND_USD_PER_UNIT", "NaN")
    assert price_micro("email_send", len(domain.split("."))) == 0
    with pytest.raises(CreditsUnavailable):
        price_micro("email_send", -1)


@pytest.mark.asyncio
async def test_llm_prices_selected_tier_and_actual_usage(metered, monkeypatch):
    module, meter, events = metered
    from app.llm import founder_provider
    from llm_gateway import sole
    from llm_gateway import provider_foundry
    config = provider_foundry.ProviderConfig(deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol")
    from app.credits import estimate
    monkeypatch.setattr(estimate, "pricing_table", lambda: {"gpt-6.1-sol": {
        "input_per_mtok": "2", "output_per_mtok": "10", "cached_input_per_mtok": "0.1"}})
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(workspace_id=wid, source="platform_default"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda wid, **kwargs: config)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    async def provider(*args, **kwargs):
        assert kwargs["single_attempt"] is True and kwargs["purpose"] == "gbrain.think"
        assert events[1] == "protect"
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                                      "prompt_tokens_details": {"cached_tokens": 8},
                                      "completion_tokens_details": {"reasoning_tokens": 0}})
    monkeypatch.setattr(sole, "transmit", provider)
    await module.llm([{"role": "user", "content": "synthetic memory"}], purpose="gbrain.think", workspace_id="test_workspace", tier="flash")
    assert meter.actual_micro == 205
    assert events[0][1]["model"] == "gpt-6.1-sol"
    assert meter.prompt_tokens == 10 and meter.completion_tokens == 20
    assert meter.cached_tokens == 8
    from app.credits.estimate import count_prompt_tokens
    assert events[0][1]["max_tokens"] == 4096 + count_prompt_tokens([{"role": "user", "content": "synthetic memory"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["Freight scheduling", "Weekend baking"])
@pytest.mark.parametrize("mode", [None, "local"])
async def test_llm_prices_purpose_route_used_by_transport(metered, monkeypatch, domain, mode):
    module, meter, events = metered
    from app.credits.estimate import (
        actual_llm_micro,
        count_prompt_tokens,
        estimate_llm_micro,
    )
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, routing, sole

    if mode:
        monkeypatch.setenv("SUPYMEM_MODE", mode)
    else:
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    config = provider_foundry.ProviderConfig(deployment_pro="gpt-6-luna", deployment_flash="gpt-6-luna")
    if mode:
        config._local_role_models = {"chat": "gpt-5.6-luna", "extraction": "gpt-6-luna"}
        monkeypatch.setattr(founder_provider, "to_provider_config", lambda wid, **kwargs: config)
    else:
        monkeypatch.setattr(routing, "_model_overrides", lambda: {"byok_chat_completion": {"model": "gpt-5.6-luna"}})
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(workspace_id=wid, source="platform_default"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda wid, **kwargs: config)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    messages = [{"role": "user", "content": domain}]

    async def provider(*args, **kwargs):
        routed_tier = sole._resolved_tier(kwargs["purpose"], kwargs["tier"])
        captured = routing.resolve_purpose_config(kwargs["purpose"], kwargs["config"], tier=routed_tier)
        assert provider_foundry.resolve_live_model(captured, tier=routed_tier) == "gpt-5.6-luna"
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    monkeypatch.setattr(sole, "transmit", provider)
    await module.llm(messages, purpose="byok_chat_completion", workspace_id="test_workspace", tier="flash", max_tokens=100)
    assert events[0][1]["model"] == "gpt-5.6-luna"
    assert events[0][1]["estimate_micro"] == estimate_llm_micro("gpt-5.6-luna", prompt_tokens=count_prompt_tokens(messages), max_tokens=100)
    assert meter.actual_micro == actual_llm_micro("gpt-5.6-luna", prompt_tokens=10, completion_tokens=20, cached_tokens=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
async def test_local_llm_uses_saved_provider_not_environment(metered, monkeypatch, domain):
    from app.llm import founder_provider
    from llm_gateway import provider_foundry, sole

    module, meter, events = metered
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    config = provider_foundry.ProviderConfig(provider="openai", deployment_pro="gpt-6-luna",
                                            deployment_flash="gpt-6-luna", source="founder_override")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(source="founder_override"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda wid, **kwargs: config)

    def no_environment():
        pytest.fail("local LLM requests must use the saved model provider")

    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", no_environment)

    async def provider(messages, **kwargs):
        assert kwargs["config"] is config
        assert messages[0]["content"] == domain
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    monkeypatch.setattr(sole, "transmit", provider)
    await module.llm([{"role": "user", "content": domain}], purpose="content.writer", workspace_id="workspace")
    assert events[0][1]["payer"] == "founder"
    assert meter.prompt_tokens == 10 and meter.completion_tokens == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["Fleet maintenance", "Community gardening"])
async def test_local_llm_setup_failure_names_keys_screen(metered, monkeypatch, domain):
    from app.credits.errors import CreditsUnavailable
    from app.llm import founder_provider

    module, _, events = metered
    monkeypatch.setenv("SUPYMEM_MODE", "local")

    def missing_provider(workspace_id):
        raise founder_provider.FounderLLMConfigError("Provider settings are missing.")

    monkeypatch.setattr(founder_provider, "resolve_llm_config", missing_provider)
    with pytest.raises(CreditsUnavailable, match="Keys and connections.*API key.*model choices"):
        await module.llm([{"role": "user", "content": domain}], purpose="content.writer", workspace_id="workspace")
    assert events == []


@pytest.mark.asyncio
async def test_llm_missing_usage_keeps_hold(metered, monkeypatch):
    module, meter, _ = metered
    from app.credits.errors import CreditsUnavailable
    from app.llm import founder_provider
    from llm_gateway import sole, provider_foundry
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    config = provider_foundry.ProviderConfig()
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(workspace_id=wid, source="platform_default"))
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda wid, **kwargs: config)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    async def provider(*args, **kwargs):
        return SimpleNamespace(usage=None)
    monkeypatch.setattr(sole, "transmit", provider)
    with pytest.raises(CreditsUnavailable):
        await module.llm([], purpose="gbrain.think", workspace_id="test_workspace")
    assert not meter.recorded and not meter.definitive_failure


def test_speech_sse_settles_tokens_and_rejects_missing_usage():
    import base64
    import json
    from app.credits.audio_meter import speech_receipt
    from app.credits.errors import CreditsUnavailable
    audio = {"type": "speech.audio.delta", "audio": base64.b64encode(b"synthetic mp3").decode()}
    usage = {"type": "speech.audio.done", "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
    receipt = SimpleNamespace(text="\n\n".join("data: " + json.dumps(item) for item in (audio, usage)))
    assert speech_receipt(receipt) == (b"synthetic mp3", 246)
    with pytest.raises(CreditsUnavailable):
        speech_receipt(SimpleNamespace(text="data: " + json.dumps(audio)))


@pytest.mark.asyncio
async def test_gbrain_and_localization_reach_metered_llm(monkeypatch):
    import json
    from app.credits import metered
    from app.gbrain import think
    from app.gtm.localization import service
    calls = []
    async def llm(messages, **kwargs):
        calls.append(kwargs)
        content = ({"answer": "Synthetic grounded answer", "citations": [], "gaps": []}
                   if kwargs["purpose"] == "gbrain.think" else
                   {"archetype": {}, "buying_profile": {}, "strategy": {}, "assumptions": [], "evidence": []})
        return SimpleNamespace(model="gpt-6.1-sol", choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))])
    async def gather(**kwargs):
        return think.Gathered(pages=[{"id": "mem_synthetic", "slug": "synthetic", "text": "Synthetic grounded answer"}])
    monkeypatch.setenv("LLM_OFFLINE", "false")
    monkeypatch.setattr(metered, "llm", llm)
    monkeypatch.setattr(think, "gather", gather)
    result = await think.run_think(venture_id="ven_synthetic", question="Synthetic question")
    assert result.status == "ok" and calls[0]["venture_id"] == "ven_synthetic"
    thesis, model = await service._llm_refine({}, {}, "ws_synthetic")
    assert model == "gpt-6.1-sol" and "evidence" in thesis
    assert calls[1]["workspace_id"] == "ws_synthetic" and calls[1]["purpose"] == "gtm.market_thesis"


@pytest.mark.asyncio
async def test_venture_email_reaches_fixed_event_meter(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    import importlib
    email = importlib.import_module("app.actuation.tools.email_send")
    from app.actuation.contract import ActuationMode
    calls = []
    async def metered_call(callback, **kwargs):
        calls.append(kwargs)
        return await callback()
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": "synthetic_receipt"})
    monkeypatch.setenv("RESEND_API_KEY", "synthetic_fixture_value")
    monkeypatch.delenv("SUPYMEM_METER_EMAIL_SEND_USD_PER_UNIT", raising=False)
    monkeypatch.setattr(email, "call", metered_call)
    monkeypatch.setattr(email.httpx, "AsyncClient", Client)
    result = await email.email_send("ven_synthetic", {"to": ["one@example.test", "two@example.test"], "cc": "three@example.test"})
    assert result.mode is ActuationMode.LIVE and result.external_ref == "synthetic_receipt"
    assert calls[0]["estimate_micro"] == 2700 and calls[0]["venture_id"] == "ven_synthetic"
