"""Durable paid calls own retries and propagate structured output to BYOK."""
from types import SimpleNamespace
import json

import pytest
from pydantic import SecretStr

from llm_gateway import sole
from llm_gateway.boundary import Decision, register_filter
from llm_gateway.errors import ProviderCallError
from llm_gateway.provider_foundry import ProviderConfig


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("provider,model,purpose,key", [
    ("openai_compatible", "gpt-6-luna", "pagebuilder.brief_direction_plan", "max_completion_tokens"),
    ("openai_compatible", "gpt-6-luna", "pagebuilder.capability_review_6", "max_completion_tokens"),
    ("openai", "gpt-6-luna", "pagebuilder.code", "max_completion_tokens"),
    ("openai", "gpt-6-luna", "pagebuilder.plan_repair_4", "max_completion_tokens"),
    ("azure_foundry", "DeepSeek-V4-Pro", "pagebuilder.brief_direction_plan", "max_tokens"),
    ("deepseek_direct", "DeepSeek-V4-Pro", "pagebuilder.code", "max_tokens"),
    ("deepseek_direct", "DeepSeek-V4-Pro", "pagebuilder.plan_repair_2", "max_tokens"),
    ("azure_foundry", "DeepSeek-V4-Pro", "pagebuilder.repair_1", "max_tokens"),
    ("openai_compatible", "gpt-4o-mini", "pagebuilder.code", "max_tokens"),
    ("openai_compatible", "gpt-6-luna", "research.deep", "max_completion_tokens"),
])
@pytest.mark.asyncio
async def test_pagebuilder_token_ceiling_survives_real_sdk_serialization(monkeypatch, byok, provider, model, purpose, key):
    """Observe the final SDK wire request, with no sockets or real credentials."""
    from openai import AsyncOpenAI, _base_client

    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    requests, usage = [], []
    cfg = ProviderConfig(provider=provider, base_url="https://offline.invalid/v1",
        api_key=SecretStr("offline"), deployment_pro=model, deployment_flash=model,
        reasoning_effort="low", timeout_seconds=300, max_output_tokens=4096,
        source="founder_override" if byok else "platform_default")

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "saved"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

    client = AsyncOpenAI(api_key="offline", base_url=cfg.base_url, timeout=cfg.timeout_seconds,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (client, cfg))
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: usage.append(kwargs))
    await sole.transmit([{"role": "user", "content": "offline"}], purpose=purpose,
        tier="pro", config=cfg, use_workspace_override=False, single_attempt=True, max_tokens=16384)

    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://offline.invalid/v1/chat/completions"
    assert body["model"] == model and body[key] == 16384
    assert ("max_tokens" in body) != ("max_completion_tokens" in body)
    assert request.headers["x-stainless-retry-count"] == "0"
    assert request.extensions["timeout"]["read"] == 300
    repair = purpose.startswith(("pagebuilder.plan_repair_", "pagebuilder.repair_")) and provider in {"azure_foundry", "deepseek_direct"}
    assert body.get("thinking") == ({"type": "disabled"} if repair else None)
    if provider in {"azure_foundry", "deepseek_direct"} and not repair:
        assert body["reasoning_effort"] == "low"
    else:
        assert "reasoning_effort" not in body
    assert len(usage) == 1 and usage[0]["purpose"] == purpose
    assert client.is_closed()


@pytest.mark.parametrize("idea", [
    "Draft an introduction for a freight scheduling service to a dispatch coordinator.",
    "Draft an introduction for a planting planner to a home gardener who opted in.",
])
@pytest.mark.parametrize("adapter", ["chat", "stream", "raw"])
@pytest.mark.parametrize("model", ["gpt-5.4", "deployment-gpt-6.1-sol"])
@pytest.mark.asyncio
async def test_outreach_reasoning_model_token_ceiling_survives_each_sdk_adapter(monkeypatch, idea, adapter, model):
    from openai import AsyncOpenAI, _base_client
    from llm_gateway.provider_openai import chat_raw

    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    requests = []
    config = ProviderConfig(provider="azure_foundry", base_url="https://offline.invalid/v1",
        api_key=SecretStr("offline"), deployment_pro=model, deployment_flash=model,
        reasoning_effort="low", source="founder_override")

    def respond(request):
        requests.append(json.loads(request.content))
        body = {"id": "offline", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "saved"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        if adapter == "stream":
            body.update(object="chat.completion.chunk", choices=[])
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=body)

    client = AsyncOpenAI(api_key="offline", base_url=config.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=0)
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (client, config))
    messages = [{"role": "user", "content": idea}]
    try:
        if adapter == "chat":
            await sole.transmit(messages, purpose="outreach.draft_batch", config=config,
                use_workspace_override=False, single_attempt=True, max_tokens=3000)
        elif adapter == "stream":
            chunks = [chunk async for chunk in sole.transmit_stream(messages,
                purpose="outreach.draft_batch", config=config, max_tokens=3000)]
            assert chunks[-1]["usage"]["total_tokens"] == 5
        else:
            await chat_raw(client, purpose="outreach.draft_batch", model=model, messages=messages, max_tokens=3000)
        assert len(requests) == 1
        assert requests[0].get("max_completion_tokens") == 3000
        assert "max_tokens" not in requests[0]
        assert any(message.get("content") == idea for message in requests[0]["messages"])
    finally:
        await client.close()


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.asyncio
async def test_pagebuilder_rejected_sdk_request_has_no_retry_fallback_or_usage(monkeypatch, byok):
    from openai import AsyncOpenAI, _base_client

    httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    requests, usage = [], []
    cfg = ProviderConfig(provider="openai_compatible", base_url="https://offline.invalid/v1",
        api_key=SecretStr("offline"), deployment_pro="gpt-6-luna",
        source="founder_override" if byok else "platform_default")

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "offline invalid parameter", "type": "invalid_request_error"}})

    client = AsyncOpenAI(api_key="offline", base_url=cfg.base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (client, cfg))
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: usage.append(kwargs))
    async def forbidden_fallback(*args, **kwargs):
        pytest.fail("Single-attempt paid calls cannot fall back")
    monkeypatch.setattr(sole, "_fallback_chat", forbidden_fallback)
    with pytest.raises(ProviderCallError):
        await sole.transmit([{"role": "user", "content": "offline"}], purpose="pagebuilder.code",
            tier="pro", config=cfg, use_workspace_override=False, single_attempt=True, max_tokens=16384)
    assert len(requests) == 1 and requests[0]["max_completion_tokens"] == 16384
    assert not usage and client.is_closed()


@pytest.fixture(autouse=True)
def boundary(monkeypatch):
    register_filter(lambda messages, **kwargs: Decision(messages=list(messages)))
    monkeypatch.setattr(sole, "_workspace_resolver", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    yield
    register_filter(None)


@pytest.mark.asyncio
async def test_single_attempt_does_not_retry_or_switch_provider(monkeypatch):
    calls = []
    async def primary(*args, **kwargs):
        calls.append("primary")
        raise ProviderCallError("provider_down", "timeout", retryable=True)
    async def fallback(*args, **kwargs):
        pytest.fail("A metered ambiguous request must not switch provider")
    monkeypatch.setattr(sole, "_primary_chat", primary)
    monkeypatch.setattr(sole, "_fallback_chat", fallback)
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock")
    with pytest.raises(ProviderCallError):
        await sole.transmit([{"role": "user", "content": "idea"}], purpose="idea.clarify_turn", single_attempt=True)
    assert calls == ["primary"]


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("model", ["DeepSeek-V4-Pro", "schema-deployment"])
@pytest.mark.asyncio
async def test_single_attempt_disables_sdk_retry_and_forwards_schema(monkeypatch, byok, model):
    captured = []
    class Client:
        async def close(self):
            pass
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            captured.append(kwargs)
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[], usage=None)
    cfg = ProviderConfig(provider="azure_foundry", base_url="https://model.example.test/openai/v1",
        api_key=SecretStr("test"), deployment_pro=model, deployment_flash=model,
        source="founder_override" if byok else "platform_default")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "open_env_client", lambda _: (Client(), cfg))
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (Client(), cfg))
    schema = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"], "additionalProperties": False}
    await sole.transmit([{"role": "user", "content": "idea"}], purpose="idea.clarify_turn",
                        config=cfg if byok else None, single_attempt=True, response_schema=schema)
    assert captured[0] == {"max_retries": 0}
    assert captured[1]["response_format"] == {"type": "json_object"}
    assert any("matching this schema" in message["content"] for message in captured[1]["messages"])
    assert captured[1]["model"] == model


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("purpose,model,schema,tools,disabled", [
    ("research.deep", "DeepSeek-V4-Pro", True, False, True),
    ("research.deep", "DeepSeek-V4-Pro", True, True, False),
    ("research.deep", "DeepSeek-V4-Pro", False, False, False),
    ("research.deep", "other-deployment", True, False, False),
    ("plan.compile", "DeepSeek-V4-Pro", True, False, True),
    ("plan.compile", "DeepSeek-V4-Pro", True, True, False),
    ("plan.compile", "DeepSeek-V4-Pro", False, False, False),
    ("plan.compile", "other-deployment", True, False, False),
    ("gtm.god_diagnose", "DeepSeek-V4-Pro", True, False, True),
    ("gtm.god_research", "DeepSeek-V4-Pro", True, False, True),
    ("gtm.god_research", "DeepSeek-V4-Pro", True, True, False),
    ("content.topics", "DeepSeek-V4-Pro", True, False, True),
    ("content.writer", "DeepSeek-V4-Pro", True, False, True),
    ("content.critic", "DeepSeek-V4-Pro", True, False, True),
    ("content.writer", "DeepSeek-V4-Pro", True, True, False),
    ("content.writer", "DeepSeek-V4-Pro", False, False, False),
    ("content.writer", "other-deployment", True, False, False),
    ("build.peer_review", "DeepSeek-V4-Pro", True, False, False),
    ("idea.clarify_turn", "DeepSeek-V4-Pro", True, False, True),
    ("idea.validate", "DeepSeek-V4-Pro", True, False, True),
    ("idea.clarify_turn", "DeepSeek-V4-Pro", True, True, False),
    ("idea.clarify_turn", "DeepSeek-V4-Pro", False, False, False),
    ("idea.clarify_turn", "other-deployment", True, False, False),
    ("build.model", "DeepSeek-V4-Pro", True, True, False),
])
@pytest.mark.asyncio
async def test_explicit_structured_mode_does_not_change_other_operations(monkeypatch, byok, purpose, model, schema, tools, disabled):
    calls = []
    class Client:
        async def close(self):
            pass
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[], usage=None)
    cfg = ProviderConfig(provider="azure_foundry", deployment_pro=model, deployment_flash=model,
        source="founder_override" if byok else "platform_default")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "open_env_client", lambda _: (Client(), cfg))
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (Client(), cfg))
    tool = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}
    await sole.transmit([{"role": "user", "content": "Review evidence"}], purpose=purpose,
        tier="pro", config=cfg if byok else None, single_attempt=True, max_tokens=8192,
        response_schema={"type": "object", "properties": {}} if schema else None,
        tools=[tool] if tools else None)
    assert len(calls) == 1 and calls[0]["max_tokens"] == 8192
    assert (calls[0].get("extra_body", {}).get("thinking") == {"type": "disabled"}) == disabled
    if not disabled and not byok:
        assert calls[0]["extra_body"] == {"reasoning_effort": "high"}


@pytest.mark.parametrize("byok", [False, True])
@pytest.mark.parametrize("parallel", [None, False, True])
@pytest.mark.asyncio
async def test_tool_call_preference_preserves_false_and_default(monkeypatch, byok, parallel):
    captured = []
    class Client:
        async def close(self):
            pass
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[], usage=None)
    cfg = ProviderConfig(provider="azure_foundry", base_url="https://model.example.test/openai/v1",
        api_key=SecretStr("test"), deployment_pro="DeepSeek-V4-Pro", deployment_flash="DeepSeek-V4-Pro",
        source="founder_override" if byok else "platform_default")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "open_env_client", lambda _: (Client(), cfg))
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda _: (Client(), cfg))
    tool = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}
    await sole.transmit([{"role": "user", "content": "Read the project."}], purpose="idea.clarify_turn",
                        config=cfg if byok else None, single_attempt=True, tools=[tool], parallel_tool_calls=parallel)
    assert len(captured) == 1
    assert captured[0]["tools"][0]["function"]["name"] == "read_file"
    if parallel is None:
        assert "parallel_tool_calls" not in captured[0]
    else:
        assert captured[0]["parallel_tool_calls"] is parallel


@pytest.mark.asyncio
async def test_explicit_tool_policy_cannot_disappear_on_fallback(monkeypatch):
    async def primary(*args, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        raise ProviderCallError("provider_down", "unavailable", status=503, retryable=True)
    async def fallback(*args, **kwargs):
        pytest.fail("The alternate adapter cannot preserve this explicit tool policy")
    monkeypatch.setattr(sole, "_primary_chat", primary)
    monkeypatch.setattr(sole, "_fallback_chat", fallback)
    monkeypatch.setattr(sole, "reliability_for", lambda tier: SimpleNamespace(run=lambda call: call()))
    monkeypatch.setattr(sole, "_should_fallback", lambda exc: True)
    with pytest.raises(ProviderCallError):
        await sole.transmit([{"role": "user", "content": "Build."}], purpose="idea.clarify_turn", parallel_tool_calls=False)


@pytest.mark.parametrize("invalid", ["false", 0, 1, {}, []])
@pytest.mark.asyncio
async def test_invalid_tool_preference_rejected_before_any_dial(monkeypatch, invalid):
    def reject_client(*args, **kwargs):
        pytest.fail("Invalid provider options must not dial")
    monkeypatch.setattr(sole, "open_env_client", reject_client)
    with pytest.raises(ValueError, match="parallel_tool_calls"):
        await sole.transmit([{"role": "user", "content": "Build."}], purpose="idea.clarify_turn", parallel_tool_calls=invalid)
