"""An allowance cannot leak when wallet or nested task admission refuses a call."""
import pytest
import asyncio
import json
from contextlib import asynccontextmanager

from tests.credit_fixtures import ensure_test_wallet
from tests.unit.tasks.test_reasoning import deep_env as deep_env
from tests.unit.credits.test_chat_provider_captures import chat_capture as chat_capture


@pytest.fixture()
def account(deep_env):
    return deep_env.wallet


def test_deep_chat_completions_reserves_cjk_and_tool_envelope_before_dispatch(deep_env, monkeypatch):
    from app.chat import assistant
    from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
    from app.llm import founder_provider
    from llm_gateway import sole, routing
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import ProviderConfig
    from types import SimpleNamespace
    env = deep_env
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    config = ProviderConfig(api_mode="chat_completions", deployment_flash="deepseek-v4-flash",
        source="platform_default", max_output_tokens=16_384)
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(source="platform_default"))
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: config)
    monkeypatch.setattr(routing, "resolve_purpose_config", lambda purpose, config, **kwargs: config)
    monkeypatch.setattr(sole, "resolve_purpose_config", lambda purpose, config, **kwargs: config)
    messages = [{"role": "user", "content": env.domain + "界" * 40_000}]
    tools = [{"type": "function", "function": {"name": "read_url", "description": "界" * 1000,
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]
    cap = estimate_llm_micro(config.deployment_flash, prompt_tokens=count_prompt_tokens(messages), max_tokens=16_384)
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(kwargs)
        pytest.fail("Over-cap CJK requests must not reach a provider")
    monkeypatch.setattr(__import__("app.credits.metered", fromlist=["_transmit"]), "_transmit", forbidden)
    with pytest.raises(ProviderCallError) as refused:
        asyncio.run(assistant._complete(messages, workspace_id=env.wid, venture_id=env.vid,
            request_id="cjk", raw=True, tools=tools, deep_answer=True, output_cap=16_384, max_estimate_micro=cap))
    assert refused.value.code == "turn_budget_exceeded" and not calls
    amount = sole.transport_prompt_tokens(messages, purpose=assistant.ASSISTANT_PURPOSE, config=config,
        tools=tools, clock_text=" " * 256)
    assert amount >= len(json.dumps(messages, ensure_ascii=False).encode()) + 3000
    from app.credits import gate, metered
    from llm_gateway import provider_foundry
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    quotes = []
    async def capture(callback, **kwargs):
        quotes.append(kwargs)
        return "quoted"
    monkeypatch.setattr(metered, "call", capture)
    envelope = {"tools": tools, "response_format": {"type": "json_schema", "json_schema": {
        "name": "reply", "schema": {"description": "界" * 5000, "type": "object"}}}}
    async def quote():
        with gate.bounded_calls(env.wid, env.vid, lambda *args: None):
            return await metered.llm(messages, purpose=assistant.ASSISTANT_PURPOSE,
                workspace_id=env.wid, venture_id=env.vid, tier="flash", max_tokens=16_384, **envelope)
    assert asyncio.run(quote()) == "quoted"
    assert quotes[0]["estimate_micro"] >= estimate_llm_micro(config.deployment_flash,
        prompt_tokens=amount + len(json.dumps(envelope, ensure_ascii=False).encode()), max_tokens=16_384)


@pytest.mark.parametrize("domain", ["industrial valve procurement", "family meal planning"])
def test_allowance_is_released_when_nested_task_admission_fails(credits_env, monkeypatch, domain):
    from app.credits import gate
    from app.credits.errors import CreditsUnavailable
    wallet = ensure_test_wallet("ven_allowance_" + domain.split()[0])
    wid, vid = "ws_wallet:" + wallet.attribution_venture_id, wallet.attribution_venture_id
    calls = []
    def admit(account_id, request_id, amount):
        assert account_id == wallet.id and amount == 5000
        return lambda result: calls.append(result)
    def fail(*args, **kwargs):
        raise CreditsUnavailable("nested task refused")
    monkeypatch.setattr(gate, "_task_usage", fail)
    with gate.bounded_calls(wid, vid, admit):
        with pytest.raises(CreditsUnavailable, match="nested task refused"):
            with gate.charge_sync(workspace_id=wid, venture_id=vid, purpose="venture.chat.web_search", estimate_micro=5000):
                pytest.fail("No provider dispatch is permitted")
    assert calls == [None]


def test_deep_timeout_keeps_task_and_wallet_reserved_instead_of_absorbing_or_retrying(deep_env, chat_capture, monkeypatch):
    from app.chat import assistant
    from app.credits import gate, metered
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError
    from openai import AsyncOpenAI, _base_client
    from tests.unit.tasks.test_reasoning import prepare
    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    env = deep_env
    view = prepare(env)
    seen = []
    async def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("Offline timeout", request=request)
    def factory(config):
        return AsyncOpenAI(api_key="offline", base_url="https://offline.invalid/openai/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=0), config
    @asynccontextmanager
    async def charge(deep_answer, **kwargs):
        assert deep_answer
        with gate.charge_sync(**kwargs) as meter:
            yield meter
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setattr(assistant, "_completion_charge", charge)
    monkeypatch.setattr(metered, "absorb_hosted_chat_failure", lambda meter: pytest.fail("Deep calls cannot hide an uncertain cost"))
    async def execute():
        with metered.bounded_calls(env.wid, env.vid, env.reasoning.Admission(view["run_id"], env.wid, env.vid)):
            with pytest.raises(ProviderCallError):
                await assistant._complete([{"role": "user", "content": env.domain}], workspace_id=env.wid,
                    venture_id=env.vid, request_id="deep-timeout", raw=True, deep_answer=True,
                    reasoning_effort="high", output_cap=128_000)
    asyncio.run(execute())
    final = env.reasoning.snapshot(view["run_id"], env.wid, env.vid)
    assert len(seen) == 1 and final["state"] == "uncertain" and final["reserved_micro"] > 0
    assert final["spend_micro"] == 0 and chat_capture.wallet().reserved_micro > 0
