"""Environment deployment identities reach the actual SDK wire unchanged."""

import json
from pathlib import Path

import pytest
from llm_gateway import provider_foundry, sole
from llm_gateway.boundary import Decision
from llm_gateway.provider_foundry import (
    load_provider_config_from_env,
    normalize_base_url,
)
from llm_gateway.provider_openai import chat_raw
from llm_gateway.responses import responses_request_kwargs
from llm_gateway.routing import (
    MODEL_PRICING,
    load_pricing,
    resolve_purpose_config,
    sol_output_cap,
)
from llm_gateway.usage import extract_confirmed_token_usage
from openai import AsyncOpenAI, _base_client

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2


@pytest.fixture(autouse=True)
def deployment_configuration(monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(Path(__file__).resolve().parents[3] / "config/deployment.example.json"))
    load_pricing.cache_clear()
    yield
    load_pricing.cache_clear()


def test_platform_routing_log_excludes_key_and_url_credentials(monkeypatch, caplog):
    import logging

    from llm_gateway.provider_foundry import ProviderConfig
    from pydantic import SecretStr
    key = "synthetic-private-key"
    config = ProviderConfig(provider="azure_foundry", api_key=SecretStr(key),
        base_url=f"https://username:password@offline.invalid/api/projects/synthetic?api_key={key}#private",
        deployment_pro="supymem-pro", deployment_flash="supymem-flash")
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: config)
    with caplog.at_level(logging.INFO, logger="llm_gateway.routing"):
        provider_foundry.log_platform_routing_config()
    records = [record for record in caplog.records if record.message == "platform_routing_config"]
    assert len(records) == 1
    record = records[0]
    assert (record.provider, record.deployment_pro, record.deployment_flash) == (
        "azure_foundry", "supymem-pro", "supymem-flash")
    assert record.catalog_model is record.api_mode is None
    assert record.base_url_host == "offline.invalid" and record.base_url_path == "/api/projects/synthetic"
    assert record.is_sol_pro is record.is_sol_flash is False
    assert all(value not in str(vars(record)) for value in (key, "username", "password", "#private"))


@pytest.mark.asyncio
async def test_captured_sol_route_cannot_be_replaced_by_unrecognized_env(monkeypatch):
    for key, value in {"LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_API_KEY": "offline", "LLM_API_VERSION": "2024-05-01-preview",
            "LLM_BASE_URL": "https://offline.invalid/api/projects/synthetic",
            "LLM_DEPLOYMENT_PRO": "supymem-pro", "LLM_DEPLOYMENT_FLASH": "supymem-flash",
            "MODEL_ROUTING_OVERRIDES": json.dumps({"research.prior_art.plan": {
                "model": "supymem-gpt-6-1-sol", "catalog_model": "gpt-6.1-sol"}})}.items():
        monkeypatch.setenv(key, value)
    env = load_provider_config_from_env()
    assert env.catalog_model is None
    captured = resolve_purpose_config("research.prior_art.plan", env)
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "{}")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "resp_offline", "object": "response",
            "model": captured.deployment_pro, "status": "completed", "output": [],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 80},
                "output_tokens_details": {"reasoning_tokens": 7}}})

    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient",
        lambda **kwargs: httpx.AsyncClient(**{**kwargs, "transport": httpx.MockTransport(respond)}))
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "_require_platform_server", lambda: None)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    await sole.transmit([{"role": "user", "content": "Synthetic captured route"}],
        purpose="research.prior_art.plan", config=captured, single_attempt=True)
    assert len(requests) == 1 and requests[0].url.path == "/openai/v1/responses"
    assert json.loads(requests[0].content)["model"] == captured.deployment_pro


@pytest.mark.asyncio
async def test_contradictory_captured_protocol_refuses_before_client(monkeypatch):
    from llm_gateway.provider_foundry import ProviderConfig
    config = ProviderConfig(provider="azure_foundry", source="platform_default",
        deployment_pro="supymem-gpt-6-1-sol", deployment_flash="supymem-gpt-6-1-sol",
        catalog_model="gpt-6.1-sol", api_mode="chat_completions",
        routing_purpose="research.prior_art.plan", captured_tier="pro")
    monkeypatch.setattr(sole, "create_async_client_from_config",
        lambda *args: pytest.fail("Contradictory protocol must not open a client"))
    with pytest.raises(ValueError, match="protocol"):
        await sole.transmit([], purpose="research.prior_art.plan", config=config,
            single_attempt=True, use_workspace_override=False)


@pytest.mark.parametrize("deployment,api_mode", [
    ("supymem-gpt-6-1-sol", "responses"),
    ("DeepSeek-V4-Pro", "chat_completions"),
])
@pytest.mark.asyncio
async def test_versioned_foundry_project_uses_captured_protocol(monkeypatch, deployment, api_mode):
    for key in ("MODEL_ROUTING_OVERRIDES", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_API_KEY": "offline", "LLM_API_VERSION": "2024-05-01-preview",
            "LLM_BASE_URL": "https://fixture-resource.services.ai.azure.com/api/projects/fixture-project",
            "LLM_DEPLOYMENT_NAME": deployment, "LLM_DEPLOYMENT_PRO": deployment,
            "LLM_DEPLOYMENT_FLASH": deployment}.items():
        monkeypatch.setenv(key, value)
    config = resolve_purpose_config("research.deep", load_provider_config_from_env())
    assert config.provider == "azure_foundry" and config.api_mode == api_mode
    requests, clients = [], []
    chat_usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 7}}

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"id": "chat_offline", "object": "chat.completion",
                "created": 0, "model": deployment, "usage": chat_usage,
                "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"}}]})
        return httpx.Response(200, json={"id": "resp_offline", "object": "response",
            "model": deployment, "status": "completed", "output": [],
            "usage": {"input_tokens": chat_usage["prompt_tokens"],
                "output_tokens": chat_usage["completion_tokens"], "total_tokens": chat_usage["total_tokens"],
                "input_tokens_details": chat_usage["prompt_tokens_details"],
                "output_tokens_details": chat_usage["completion_tokens_details"]}})

    def transport(**kwargs):
        client = httpx.AsyncClient(**{**kwargs, "transport": httpx.MockTransport(respond)})
        clients.append(client)
        return client

    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", transport)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "_require_platform_server", lambda: None)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    result = await sole.transmit([{"role": "user", "content": "Synthetic production config check"}],
        purpose="research.deep", config=config, single_attempt=True, use_workspace_override=False)
    expected_path = "/openai/v1/responses" if api_mode == "responses" else "/api/projects/fixture-project/openai/v1/chat/completions"
    assert len(requests) == 1 and requests[0].url.path == expected_path
    assert requests[0].headers["x-stainless-retry-count"] == "0"
    assert json.loads(requests[0].content)["model"] == deployment
    assert result.provider_used == "foundry"
    assert extract_confirmed_token_usage(result.usage) == {
        "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        "cached_tokens": 80, "reasoning_tokens": 7}
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("deployment,catalog", [
    ("supymem-gpt-6-1-sol", "gpt-6.1-sol"),
    ("custom-sol-deployment", "gpt-6.1-sol"),
    ("DeepSeek-V4-Pro", "DeepSeek-V4-Pro"),
])
@pytest.mark.parametrize("flow", ["env", "pinned", "stream", "raw"])
@pytest.mark.asyncio
async def test_env_deployment_selects_wire_and_preserves_caller_cap(monkeypatch, deployment, catalog, flow):
    for key in ("MODEL_ROUTING_OVERRIDES", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LLM_PROFILE": "prod", "LLM_PROVIDER": "azure_foundry",
            "LLM_API_KEY": "offline", "LLM_BASE_URL": "https://offline.invalid",
            "LLM_DEPLOYMENT_PRO": deployment, "LLM_DEPLOYMENT_FLASH": deployment}.items():
        monkeypatch.setenv(key, value)
    aliases = load_pricing(str(MODEL_PRICING))["aliases"]
    monkeypatch.setitem(aliases, "custom-sol-deployment", "gpt-6.1-sol")
    sol = catalog == "gpt-6.1-sol"
    cfg = load_provider_config_from_env()
    assert (cfg.catalog_model, cfg.api_mode, cfg.reasoning_effort) == (
        catalog, "responses" if sol else "chat_completions", "low" if sol else "high")
    for tier in ("pro", "flash"):
        captured = resolve_purpose_config("env_chat_completion", cfg, tier=tier)
        assert (captured.catalog_model, captured.api_mode, captured.reasoning_effort) == (
            cfg.catalog_model, cfg.api_mode, cfg.reasoning_effort)
        mixed = cfg.model_copy(update={"deployment_pro": deployment,
            "deployment_flash": "DeepSeek-V4-Pro", "catalog_model": None, "api_mode": None,
            "reasoning_effort": "high"})
        mixed = resolve_purpose_config("env_chat_completion", mixed, tier=tier)
        assert mixed.api_mode == ("responses" if sol and tier == "pro" else "chat_completions")
    requests, clients = [], []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        if sol:
            receipt = {"id": "resp_offline", "object": "response", "model": deployment,
                "status": "completed", "output": [{"type": "message", "id": "msg_offline",
                    "role": "assistant", "status": "completed", "content": [
                        {"type": "output_text", "text": "ok", "annotations": []}]}],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 1}}}
            if body.get("stream"):
                event = json.dumps({"type": "response.completed", "response": receipt})
                return httpx.Response(200, text=f"data: {event}\n\ndata: [DONE]\n\n",
                    headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=receipt)
        receipt = {"id": "chat_offline", "object": "chat.completion", "created": 0,
            "model": deployment, "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"}}]}
        if body.get("stream"):
            receipt.update(object="chat.completion.chunk", choices=[
                {"index": 0, "finish_reason": "stop", "delta": {"content": "ok"}}])
            return httpx.Response(200, text=f"data: {json.dumps(receipt)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=receipt)

    def factory(config):
        client = AsyncOpenAI(api_key="offline", base_url=normalize_base_url(
            config.provider, config.base_url, api_mode=config.api_mode),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        clients.append(client)
        return client, config

    monkeypatch.setattr(sole, "open_env_client", lambda _: factory(load_provider_config_from_env()))
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: None)
    monkeypatch.setattr(sole, "_require_platform_server", lambda: None)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    messages = [{"role": "user", "content": "Synthetic deployment check"}]
    try:
        if flow == "env":
            await sole.env_chat_completion(messages, tier="flash", max_tokens=400)
        elif flow == "pinned":
            monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", "invalid after admission")
            await sole.transmit(messages, purpose="env_chat_completion", tier="pro", config=cfg,
                single_attempt=True, use_workspace_override=False, max_tokens=400)
        elif flow == "stream":
            chunks = [chunk async for chunk in sole.transmit_stream(messages,
                purpose="env_chat_completion", tier="pro", max_tokens=400)]
            assert chunks[-1]["type"] == "usage"
        else:
            client, _ = factory(cfg)
            await chat_raw(client, purpose="env_chat_completion", model=deployment,
                messages=messages, max_tokens=400)
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body["model"] == deployment
        if sol:
            assert requests[0].url.path == "/openai/v1/responses"
            assert body["max_output_tokens"] == 400
            assert body["reasoning"] == {"effort": "low"}
            assert not {"max_tokens", "max_completion_tokens", "messages"} & body.keys()
        else:
            assert requests[0].url.path == "/openai/v1/chat/completions"
            assert body["max_tokens"] == 400
            assert "max_output_tokens" not in body and "reasoning" not in body
            if flow in {"env", "pinned"}:
                assert body["reasoning_effort"] == "high"
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize("token_key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
@pytest.mark.parametrize("cap", [80, 400, 700, 900, 1200, 2047, 2048, 4096])
def test_sol_wire_output_limit_does_not_exceed_the_quoted_cap(token_key, cap):
    wire = responses_request_kwargs(model="supymem-gpt-6-1-sol", messages=[], **{token_key: cap})
    assert wire["max_output_tokens"] == cap
    assert wire["reasoning"] == {"effort": "low"}
    assert not {"max_tokens", "max_completion_tokens"} & wire.keys()


@pytest.mark.parametrize("mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("cap", [80, 400, 700, 900, 1200, 2048, 4096])
def test_caller_sol_cap_is_the_same_ceiling_serialized_on_the_wire(mode, cap):
    from llm_gateway.provider_foundry import ProviderConfig
    config = ProviderConfig(api_mode=mode)
    quoted_cap = sol_output_cap(config, cap)
    assert quoted_cap == (max(cap, 2048) if mode == "responses" else cap)
    if mode == "responses":
        assert responses_request_kwargs(model="gpt-6.1-sol", messages=[],
            max_tokens=quoted_cap)["max_output_tokens"] == quoted_cap


@pytest.mark.parametrize("cap", [0, -1, True, "700"])
def test_sol_output_cap_rejects_invalid_caller_limits(cap):
    from llm_gateway.provider_foundry import ProviderConfig
    with pytest.raises(ValueError):
        sol_output_cap(ProviderConfig(api_mode="responses"), cap)
