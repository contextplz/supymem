import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from llm_gateway import provider_foundry, sole
from llm_gateway.errors import ProviderCallError
from llm_gateway.usage import extract_confirmed_token_usage
from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=["warehouse-inspections", "home-fitness"])
def local_model(request, monkeypatch, tmp_path):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()
    from app.core import providers_local
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SUPYMEM_DESKTOP_INSTANCE_ID", raising=False)
    for name in ("SUPYMEM_LLM_PROVIDER", "SUPYMEM_LOCAL_MONTHLY_CAP_USD",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_COMPATIBLE_API_KEY",
                 "OPENAI_COMPATIBLE_BASE_URL", "LLM_RATE_LIMIT_RPM", "LLM_RATE_LIMIT_TPM",
                 *(f"SUPYMEM_MODEL_{role}" for role in ("CHAT", "PLANNING", "EXTRACTION", "VISION"))):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(provider_foundry, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "sample_mode_enabled", lambda: False)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    models = {role: request.param + "-" + role for role in ("chat", "planning", "extraction", "vision")}
    providers_local.save_settings(provider="anthropic", api_key="private-test-key", models=models)
    return request.param, models, tmp_path


def receipt(model, content=None, usage=None):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": content or [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": usage or {"input_tokens": 7, "output_tokens": 3,
                                "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}}


def mock_native(monkeypatch, handler):
    from llm_gateway import local_transport

    factory = httpx.AsyncClient
    monkeypatch.setattr(local_transport.httpx, "AsyncClient",
                        lambda **kwargs: factory(**{**kwargs, "transport": httpx.MockTransport(handler)}))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openai_compatible"])
async def test_saved_provider_transport_retries_only_connects(local_model, provider):
    from app.core import providers_local

    domain, models, _ = local_model
    providers_local.save_settings(provider=provider, api_key="fixture-key", models=models,
                                  base_url=f"https://{domain}.example/v1")
    client, _ = provider_foundry.create_async_client_from_config(providers_local.load_local_config())
    try:
        assert client._client._transport._pool._retries == 2
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic", "loopback"])
async def test_local_connect_failure_records_only_the_cause_class(local_model, monkeypatch, provider):
    from unittest.mock import Mock

    from app.core import providers_local
    from app.credits import metered
    from llm_gateway.limiter import httpx as sdk_httpx

    domain, models, directory = local_model
    base_url = "http://127.0.0.1:11434/v1" if provider == "loopback" else f"https://{domain}.example/v1"
    providers_local.save_settings(provider="openai_compatible" if provider == "loopback" else provider,
                                  api_key="fixture-key", models=models, base_url=base_url)
    private = f"private connection details for {domain}"

    def fail(request):
        transport = httpx if provider == "anthropic" else sdk_httpx
        raise transport.ConnectError(private, request=request)

    if provider == "anthropic":
        mock_native(monkeypatch, fail)
    else:
        monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
            sdk_httpx.AsyncClient(**{**kwargs, "transport": sdk_httpx.MockTransport(fail)}))
    with pytest.raises(sole.LocalProviderConnectError, match="Keys and connections") as error:
        await sole.byok_chat_completion([{"role": "user", "content": domain}],
            config=providers_local.load_local_config(), single_attempt=True)
    assert error.value.code == "provider_down" and error.value.retryable and error.value.__cause__ is None
    if provider == "loopback":
        assert "Start your model server" in str(error.value) and "Service URL" in str(error.value)
    else:
        assert "Start your model server" not in str(error.value)
    diagnostics = (directory / "logs/errors.jsonl").read_text()
    assert json.loads(diagnostics)["error_type"] == "ConnectError"
    assert private not in diagnostics and domain not in diagnostics
    meter = SimpleNamespace(local=True, confirm_unspent=Mock())
    metered.record_model_failure(meter, error.value)
    meter.confirm_unspent.assert_called_once_with()


def test_native_settings_default_missing_models(local_model, monkeypatch):
    from app.core import providers_local
    from llm_gateway.local_transport import DEFAULT_ANTHROPIC_MODEL

    domain, _, directory = local_model
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(directory / domain))
    providers_local.save_settings(provider="anthropic", api_key="synthetic-model-key")
    document = providers_local._read(providers_local._root())
    assert document["providers"]["anthropic"]["models"]["chat"] == DEFAULT_ANTHROPIC_MODEL
    document["providers"]["anthropic"]["models"] = {}
    settings = providers_local._effective("anthropic", document)
    assert settings.models == {role: DEFAULT_ANTHROPIC_MODEL for role in ("chat", "planning", "extraction", "vision")}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_local_provider_failure_only_uses_saved_model(local_model, monkeypatch, streaming):
    import os
    from unittest.mock import AsyncMock, Mock

    import boto3
    from openai import APIStatusError
    from app.core import providers_local
    from llm_gateway import provider_bedrock
    from llm_gateway.boundary import Decision

    domain, models, _ = local_model
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    aws = Mock(side_effect=AssertionError("Local inference must not use AWS"))
    fallback = Mock(return_value=True)
    monkeypatch.setattr(boto3, "client", aws)
    monkeypatch.setattr(provider_bedrock, "fallback_enabled", fallback)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    providers_local.save_settings(provider="openai", api_key="synthetic-owner-secret", models=models)
    error = APIStatusError("synthetic-owner-secret", response=httpx.Response(503,
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")), body=None)
    create = AsyncMock(side_effect=error)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    client.with_options = lambda **kwargs: client

    def factory(config):
        assert config.source == "founder_override"
        assert config.provider == "openai" and config.deployment_pro == models["chat"]
        return client, config

    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    messages = [{"role": "user", "content": domain}]
    with pytest.raises(ProviderCallError, match="Settings > Keys and connections > Your AI provider") as caught:
        if streaming:
            assert not [chunk async for chunk in sole.transmit_stream(messages, purpose="venture.chat.assistant")]
        else:
            await sole.transmit(messages, purpose="venture.chat.assistant", single_attempt=True)
    assert caught.value.code == "provider_down" and caught.value.retryable
    assert "synthetic-owner-secret" not in str(caught.value)
    create.assert_awaited_once()
    fallback.assert_not_called()
    aws.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,role", [("venture.chat.assistant", "chat"),
                                          ("task.plan", "planning"), ("memory.extract", "extraction")])
async def test_native_text_routes_and_metered_usage(local_model, monkeypatch, purpose, role):
    from app.credits import metered
    from app.credits.estimate import actual_llm_micro
    from llm_gateway.boundary import Decision

    domain, models, directory = local_model
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "private-test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        assert body["model"] == models[role] and body["max_tokens"] == 64
        assert body["system"] and body["messages"][-1]["content"][0]["text"] == domain
        return httpx.Response(200, json=receipt(models[role]))

    mock_native(monkeypatch, handler)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    reply = await metered.llm([{"role": "system", "content": "Be concise."},
                               {"role": "user", "content": domain}],
                              purpose=purpose, workspace_id="wsp_test", max_tokens=64)
    assert reply.choices[0].message.content == "ok"
    counts = extract_confirmed_token_usage(reply.usage)
    assert counts["prompt_tokens"] == 10 and counts["completion_tokens"] == 3
    assert counts["cached_tokens"] == 2
    with sqlite3.connect(directory / "local_spend.sqlite3") as connection:
        row = connection.execute("SELECT status, prompt_tokens, completion_tokens, cost_usd_micro FROM calls").fetchone()
    assert row == ("confirmed", 10, 3, actual_llm_micro(models[role], prompt_tokens=10,
                                                   completion_tokens=3, cached_tokens=2))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_native_connection_test_makes_one_messages_call(local_model, monkeypatch):
    from app.core import providers_local
    from starlette.requests import Request

    _, models, directory = local_model
    monkeypatch.setattr(providers_local, "_sample_mode", lambda: False)
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages" and body["max_tokens"] == 400
        assert body["model"] == models["chat"]
        schema_tool = body["tool_choice"]["name"]
        assert body["tool_choice"]["type"] == "tool"
        return httpx.Response(200, json=receipt(models["chat"],
            content=[{"type": "tool_use", "id": "tool_probe", "name": schema_tool, "input": {"connected": True}}]))

    mock_native(monkeypatch, handler)

    async def receive():
        return {"type": "http.request", "body": json.dumps({"provider": "anthropic"}).encode()}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/json")]}, receive)
    response = await providers_local.test_provider(request)
    assert json.loads(response.body) == {"ok": True, "verified": True} and len(requests) == 1
    with sqlite3.connect(directory / "local_spend.sqlite3") as connection:
        assert connection.execute("SELECT status FROM calls").fetchall() == [("confirmed",)]


@pytest.mark.asyncio
async def test_native_gateway_keeps_vision_image(local_model, monkeypatch):
    from app.credits import metered
    from llm_gateway.boundary import Decision

    domain, models, _ = local_model

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == models["vision"]
        assert body["messages"][-1]["content"][-1]["source"]["data"] == "aW1hZ2U="
        assert body["tools"][0]["input_schema"]["required"] == ["audience"]
        return httpx.Response(200, json=receipt(models["vision"], [{"type": "tool_use", "id": "extract_one",
            "name": body["tools"][0]["name"], "input": {"audience": domain}}]))

    mock_native(monkeypatch, handler)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    result = await metered.llm([{"role": "user", "content": [{"type": "text", "text": domain},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}]}],
        purpose="device.vision", workspace_id="wsp_test", max_tokens=64,
        response_schema={"type": "object", "properties": {"audience": {"type": "string"}}, "required": ["audience"]})
    assert json.loads(result.choices[0].message.content) == {"audience": domain}
    assert result.usage.prompt_tokens == 10 and result.usage.completion_tokens == 3


@pytest.mark.asyncio
async def test_native_schema_and_tool_roundtrip(local_model, monkeypatch):
    from app.core import providers_local
    domain, models, _ = local_model
    schema = {"type": "object", "properties": {"audience": {"type": "string"}}, "required": ["audience"]}

    def handler(request):
        body = json.loads(request.content)
        schema_tool = body["tools"][-1]
        assert schema_tool["input_schema"] == schema
        assert body["tool_choice"] == {"type": "tool", "name": schema_tool["name"]}
        assert body["messages"][0]["content"][-1] == {
            "type": "tool_use", "id": "lookup_one", "name": "lookup", "input": {"query": domain}}
        assert body["messages"][1]["content"][0] == {
            "type": "tool_result", "tool_use_id": "lookup_one", "content": domain}
        return httpx.Response(200, json=receipt(models["chat"], [{"type": "tool_use", "id": "schema_one",
            "name": schema_tool["name"], "input": {"audience": domain}}]))

    mock_native(monkeypatch, handler)
    client, config = provider_foundry.create_async_client_from_config(providers_local.load_local_config())
    try:
        reply = await client.chat.completions.create(model=models["chat"], max_tokens=64,
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
            messages=[{"role": "assistant", "content": None, "tool_calls": [{"id": "lookup_one", "type": "function",
                "function": {"name": "lookup", "arguments": json.dumps({"query": domain})}}]},
                {"role": "tool", "tool_call_id": "lookup_one", "content": domain}])
    finally:
        await client.close()
    assert json.loads(reply.choices[0].message.content) == {"audience": domain}
    assert not reply.choices[0].message.tool_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_native_stream_receipt_and_truncation(local_model, monkeypatch, complete):
    from app.core import providers_local
    domain, models, _ = local_model
    events = [{"type": "message_start", "message": receipt(models["chat"], usage={"input_tokens": 7, "output_tokens": 0})},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": domain}},
              {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}]
    if complete:
        events.append({"type": "message_stop"})
    payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events)
    mock_native(monkeypatch, lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, content=payload))
    usage = []
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: usage.append(kwargs))
    from llm_gateway.boundary import Decision
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    config = providers_local.load_local_config()
    if not complete:
        with pytest.raises(ProviderCallError, match="retry"):
            async for chunk in sole.transmit_stream([{"role": "user", "content": domain}],
                    purpose="venture.chat.assistant", config=config, workspace_id="wsp_test"):
                pass
        assert not usage
        return
    chunks = [chunk async for chunk in sole.transmit_stream([{"role": "user", "content": domain}],
        purpose="venture.chat.assistant", config=config, workspace_id="wsp_test")]
    assert chunks[0]["content"] == domain
    assert chunks[-1]["usage"]["prompt_tokens"] == 7 and chunks[-1]["usage"]["completion_tokens"] == 3
    assert usage[0]["prompt_tokens"] == 7 and usage[0]["completion_tokens"] == 3


@pytest.mark.parametrize("status,code", [(401, "bad_api_key"), (402, "out_of_credits"),
                                        (429, "rate_limited"), (500, "provider_down"), (400, "provider_error")])
@pytest.mark.asyncio
async def test_native_errors_do_not_echo_credentials(local_model, monkeypatch, status, code):
    from app.core import providers_local
    _, models, _ = local_model
    mock_native(monkeypatch, lambda request: httpx.Response(status, json={"error": {"message": "private-test-key"}}))
    client, _ = provider_foundry.create_async_client_from_config(providers_local.load_local_config())
    try:
        with pytest.raises(ProviderCallError) as caught:
            await client.chat.completions.create(model=models["chat"], max_tokens=8,
                messages=[{"role": "user", "content": "ok"}])
    finally:
        await client.close()
    assert caught.value.code == code and caught.value.status == status
    assert "private-test-key" not in str(caught.value) and "\u2014" not in str(caught.value)
    assert "Settings" in str(caught.value) or "retry" in str(caught.value)


@pytest.mark.parametrize("device_module", ["device_vision", "device_ai_step"])
def test_native_vision_messages(local_model, monkeypatch, device_module):
    from app.build import device_ai_step, device_vision
    from app.llm.founder_provider import local_vision_config
    from llm_gateway import local_transport

    domain, models, _ = local_model
    image = "data:image/png;base64,aW1hZ2U="
    report = {"verdict": "continue", "visible_facts": [domain], "next_step": "Read the screen.",
              "click_control_id": "none", "input_edit_control_id": "none"}
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages" and body["model"] == models["vision"]
        assert body["messages"][0]["content"][-1] == {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}}
        assert body["tools"][0]["input_schema"] == schema
        return httpx.Response(200, json=receipt(models["vision"] + "-20261114", [{"type": "tool_use", "id": "vision_one",
            "name": body["tools"][0]["name"], "input": report}]))

    factory = httpx.Client
    monkeypatch.setattr(local_transport.httpx, "Client", lambda **kwargs:
                        factory(transport=httpx.MockTransport(handler), **kwargs))
    config = local_vision_config("wsp_test")
    call = device_vision._provider_call if device_module == "device_vision" else device_ai_step._openai
    monkeypatch.setattr(device_ai_step, "_client", factory(transport=httpx.MockTransport(handler)))
    result = call({"model": models["vision"], "max_output_tokens": 64,
        "text": {"format": {"type": "json_schema", "name": "review", "schema": schema}},
        "input": [{"role": "system", "content": [{"type": "input_text", "text": "Read only."}]},
                  {"role": "user", "content": [{"type": "input_text", "text": domain},
                   {"type": "input_image", "image_url": image}]}]}, config=config)
    assert device_vision._usage(result, models["vision"]) == (10, 3)
    assert device_vision._review_text(result, []) == report


def test_native_desktop_review_is_admitted_and_locally_metered(local_model, monkeypatch):
    from app.build import device_vision
    from llm_gateway import local_transport

    domain, models, directory = local_model
    saved = {"workspace_id": "wsp_test", "state": "claimed", "manifest": {}, "review_digest": "a" * 64}
    manifest = SimpleNamespace(actions=["desktop.read", "desktop.interact"], instruction=domain,
        desktop_verification=SimpleNamespace(visual_review=True, founder_goal=domain))
    monkeypatch.setattr(device_vision.orders, "retrieve", lambda *args: saved)
    monkeypatch.setattr(device_vision.orders.Manifest, "model_validate", lambda *args: manifest)
    monkeypatch.setattr(device_vision, "_image", lambda value: b"image")
    admitted, results = [], []
    monkeypatch.setattr(device_vision.calls, "admit", lambda *args, **kwargs:
                        admitted.append(kwargs) or {"admitted_now": True})
    monkeypatch.setattr(device_vision.calls, "record_result", lambda *args, **kwargs: results.append(kwargs))
    monkeypatch.setattr(device_vision.calls, "check_current", lambda *args: None)
    report = {"verdict": "continue", "visible_facts": [domain], "next_step": "Read the screen.",
              "click_control_id": "none", "input_edit_control_id": "none"}

    def handler(request):
        assert len(admitted) == 1 and not results
        body = json.loads(request.content)
        assert body["model"] == models["vision"]
        return httpx.Response(200, json=receipt(models["vision"], [{"type": "tool_use", "id": "review_one",
            "name": body["tools"][0]["name"], "input": report}]))

    factory = httpx.Client
    monkeypatch.setattr(local_transport.httpx, "Client", lambda **kwargs:
                        factory(transport=httpx.MockTransport(handler), **kwargs))
    result = device_vision.review_desktop("ven_test", None, "order_one", invocation_id="review_one",
        snapshot_id="ps1_abc", screenshot_png_base64="aW1hZ2U=")
    assert result["authority"] == "observation_only" and result["report"] == report
    assert results[0]["usage"] == {"model": models["vision"], "input_tokens": 10, "output_tokens": 3}
    with sqlite3.connect(directory / "local_spend.sqlite3") as connection:
        assert connection.execute("SELECT status, prompt_tokens, completion_tokens FROM calls").fetchall() == [("confirmed", 10, 3)]


@pytest.mark.asyncio
async def test_ollama_json_fallback(local_model, monkeypatch):
    from app.core import providers_local
    from app.credits import metered
    from llm_gateway.boundary import Decision
    from openai import _base_client
    sdk_httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
    domain, _, _ = local_model
    providers_local.save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
                                  models={"chat": "local-model"})
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["response_format"]["type"] == "json_schema"
            return sdk_httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        assert "response_format" not in body
        assert "JSON" in body["messages"][0]["content"] and "audience" in body["messages"][0]["content"]
        return sdk_httpx.Response(200, json={"id": "local_one", "model": "local-model", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({"audience": domain})},
                         "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})

    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
        sdk_httpx.AsyncClient(**{**kwargs, "transport": sdk_httpx.MockTransport(handler)}))
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    reply = await metered.llm([{"role": "user", "content": domain}], purpose="memory.extract",
        workspace_id="wsp_test", max_tokens=64,
        response_schema={"type": "object", "properties": {"audience": {"type": "string"}}})
    assert json.loads(reply.choices[0].message.content) == {"audience": domain}
    assert len(requests) == 2
