import json
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI, _base_client

from llm_gateway.errors import ProviderCallError
from llm_gateway.local_transport import LoopbackJSONClient
from tests.company_os_harness import load_company_os_app

sdk_httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2


@pytest.fixture(params=["equipment scheduling for factories", "meal planning for households"])
def audience(request):
    return request.param


def completion(content, tokens=3):
    return {"id": "local_reply", "object": "chat.completion", "created": 0, "model": "small-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": tokens, "total_tokens": 7 + tokens}}


def client_for(handler, timeout=120):
    return LoopbackJSONClient(AsyncOpenAI(api_key="not-required", base_url="http://127.0.0.1:11434/v1",
        timeout=timeout, max_retries=0, http_client=sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(handler))))


def request_for(audience):
    return {"model": "small-model", "messages": [{"role": "user", "content": audience}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "idea", "schema": {
                "type": "object", "properties": {"audience": {"type": "string"}},
                "required": ["audience"], "additionalProperties": False}}}}


@pytest.mark.asyncio
async def test_loopback_bounds_context_and_keeps_latest_request(audience):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json=completion("You can test this."))

    client = client_for(handler).with_options(context_window=2048)
    try:
        await client.create(model="small-model", max_tokens=8192, messages=[
            {"role": "system", "content": "Only write a private plan. Do not start work."},
            {"role": "user", "content": "Obsolete context " * 2000},
            {"role": "assistant", "content": "Previous reply"},
            {"role": "user", "content": audience + " " + "Saved notes " * 3000 + " Do not send anything."},
        ])
    finally:
        await client.close()
    sent, = requests
    assert sent["max_tokens"] <= 1024
    assert len(json.dumps(sent["messages"]).encode()) <= 3 * (2048 - sent["max_tokens"])
    assert "Obsolete context" not in str(sent["messages"])
    assert "Do not start work." in sent["messages"][0]["content"]
    assert audience in sent["messages"][-1]["content"] and "Do not send anything." in sent["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_loopback_never_splices_oversized_json(audience):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json=completion("You can test this."))

    client = client_for(handler).with_options(context_window=2048)
    document = {"audience": audience, "sources": ["Evidence " * 400]}
    try:
        await client.create(model="small-model", messages=[{"role": "user", "content": json.dumps(document)}])
        with pytest.raises(ProviderCallError, match="Your AI provider") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": json.dumps({
                **document, "sources": ["Evidence " * 1000],
            })}])
    finally:
        await client.close()
    sent, = requests
    assert json.loads(sent["messages"][0]["content"]) == document
    assert 256 <= sent["max_tokens"] < 1024
    assert error.value.code == "context_length_exceeded"


@pytest.mark.asyncio
async def test_loopback_uses_text_when_auto_tools_are_rejected(audience):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body.get("tools"):
            return sdk_httpx.Response(400, json={"error": {"message": "This model does not support tools"}})
        reply = completion("Ask me to search for a specific term to continue.")
        reply["choices"][0]["message"]["tool_calls"] = [{"type": "function", "id": "unrequested",
            "function": {"name": "lookup", "arguments": "{}"}}]
        return sdk_httpx.Response(200, json=reply)

    client = client_for(handler)
    try:
        result = await client.create(model="small-model", messages=[{"role": "user", "content": audience}],
            tool_choice="auto", tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}])
    finally:
        await client.close()
    assert len(requests) == 2 and "tools" not in requests[1] and "tool_choice" not in requests[1]
    assert result.choices[0].message.content and not result.choices[0].message.tool_calls


@pytest.mark.asyncio
async def test_rejected_format_validates_and_repairs_once(audience):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sdk_httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        assert "response_format" not in body
        return sdk_httpx.Response(200, json=completion(json.dumps({"audience": 3 if len(requests) == 2 else audience})))

    client = client_for(handler)
    try:
        reply = await client.create(**request_for(audience))
        assert json.loads(reply.choices[0].message.content) == {"audience": audience}
        assert reply.usage.prompt_tokens == 14 and reply.usage.completion_tokens == 6
        assert len(requests) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_json_stops_after_one_repair(audience):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(200, json=completion("not JSON"))

    client = client_for(handler)
    try:
        with pytest.raises(ProviderCallError, match="Settings > Keys and connections") as error:
            await client.create(**request_for(audience))
        assert error.value.code == "invalid_structured_output" and len(requests) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_context_error_names_limit_without_echoing_server_text(audience):
    body = {"error": {"message": "requested token count exceeds the model's maximum context length of 4096 tokens; private-server-text"}}
    client = client_for(lambda request: sdk_httpx.Response(400, json=body))
    try:
        with pytest.raises(ProviderCallError) as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
        assert error.value.code == "context_length_exceeded"
        assert "4,096" in str(error.value) and "larger-context" in str(error.value)
        assert "private-server-text" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cpu_timeout_is_configurable_and_actionable(audience):
    def handler(request):
        raise sdk_httpx.ReadTimeout("private-server-text", request=request)

    client = client_for(handler)
    explicit = client_for(handler, timeout=45)
    try:
        assert client.timeout == 600 and explicit.timeout == 45
        assert client.with_options(timeout=120).timeout == 120
        with pytest.raises(ProviderCallError, match="smaller model.*Settings > Keys and connections"):
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
    finally:
        await client.close()
        await explicit.close()


@pytest.mark.asyncio
async def test_rejected_tools_are_not_silently_dropped(audience):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sdk_httpx.Response(400, json={"error": {"message": "small-model does not support tools"}})

    client = client_for(handler)
    try:
        with pytest.raises(ProviderCallError, match="supports tools.*Settings > Keys and connections"):
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}],
                tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}])
        assert len(requests) == 1 and requests[0]["tools"]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_format", [False, True])
async def test_stream_requests_usage_and_validates_before_exposing_json(audience, reject_format):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if reject_format and len(requests) == 1:
            return sdk_httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported"}})
        if not body.get("stream"):
            return sdk_httpx.Response(200, json=completion(json.dumps({"audience": audience})))
        assert body["stream_options"]["include_usage"] is True
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "invalid JSON"}, "finish_reason": None}]},
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        ]
        text = "".join("data: " + json.dumps({"id": "local_reply", "model": "small-model", "created": 0,
            "object": "chat.completion.chunk", **chunk}) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return sdk_httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    client = client_for(handler)
    try:
        stream = await client.create(**request_for(audience), stream=True)
        content, usage = "", None
        async for chunk in stream:
            if chunk.choices:
                content += chunk.choices[0].delta.content or ""
            if chunk.usage:
                usage = chunk.usage
        assert json.loads(content) == {"audience": audience}
        assert usage.prompt_tokens == 14 and len(requests) == (3 if reject_format else 2)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_structured_requests_preserve_tool_calls(audience):
    body = completion(None)
    body["choices"][0]["message"]["tool_calls"] = [{"id": "lookup_one", "type": "function", "function": {
        "name": "lookup", "arguments": json.dumps({"query": audience})}}]
    body["choices"][0]["finish_reason"] = "tool_calls"
    client = client_for(lambda request: sdk_httpx.Response(200, json=body))
    try:
        reply = await client.create(**request_for(audience), tools=[{"type": "function", "function": {"name": "lookup"}}])
        assert json.loads(reply.choices[0].message.tool_calls[0].function.arguments) == {"query": audience}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stream_timeout_is_actionable(audience):
    class FailingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise sdk_httpx.ReadTimeout("private-server-text")

        async def close(self):
            pass

    client = client_for(lambda request: sdk_httpx.Response(200))
    try:
        with pytest.raises(ProviderCallError, match="smaller model.*Settings > Keys and connections"):
            async for chunk in client._stream_reply(FailingStream(), request_for(audience), None):
                pass
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_connection_lists_models_and_explains_missing_pull(audience, installed, monkeypatch):
    load_company_os_app()
    from app.llm import founder_provider
    from llm_gateway import provider_foundry

    def handler(request):
        assert request.url.path == "/v1/models"
        return sdk_httpx.Response(200, json={"object": "list", "data": [
            {"id": "small-model" if installed else "other-model", "object": "model", "created": 0, "owned_by": "library"}]})

    monkeypatch.setattr(provider_foundry, "create_async_client_from_config", lambda config: (client_for(handler), config))
    config = provider_foundry.ProviderConfig(base_url="http://127.0.0.1:11434/v1")
    config._local_role_models = {"chat": "small-model"}
    if installed:
        assert await founder_provider.compatible_provider_models(config) == ["small-model"]
    else:
        with pytest.raises(ProviderCallError, match="ollama pull small-model.*Settings > Keys and connections") as error:
            await founder_provider.compatible_provider_models(config)
        assert "other-model" in str(error.value)


@pytest.mark.asyncio
async def test_connection_accepts_ollama_default_tag(audience, monkeypatch):
    load_company_os_app()
    from app.llm import founder_provider
    from llm_gateway import provider_foundry

    def handler(request):
        return sdk_httpx.Response(200, json={"object": "list", "data": [
            {"id": "small-model:latest", "object": "model", "created": 0, "owned_by": "library"}]})

    monkeypatch.setattr(provider_foundry, "create_async_client_from_config", lambda config: (client_for(handler), config))
    config = provider_foundry.ProviderConfig(base_url="http://127.0.0.1:11434/v1")
    config._local_role_models = {"chat": "small-model"}
    assert await founder_provider.compatible_provider_models(config) == ["small-model:latest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"message": "model 'qwen2.5:not-pulled' not found", "type": "not_found_error", "param": None, "code": None}, 404),
    ({"message": 'invalid format: "[]"; expected "json" or a valid JSON Schema object', "type": "api_error", "param": None, "code": None}, 500),
    ({"message": "json: cannot unmarshal array into Go struct field ToolFunction.tools.function.parameters of type api.ToolFunctionParameters", "type": "invalid_request_error", "param": None, "code": None}, 400),
])
async def test_real_ollama_error_bodies_are_safe_and_not_retried(audience, body, status):
    requests = []

    def handler(request):
        requests.append(request)
        return sdk_httpx.Response(status, json=body)

    client = client_for(handler)
    try:
        with pytest.raises(ProviderCallError, match="Settings > Keys and connections") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
        assert "Go struct" not in str(error.value) and "api_error" not in str(error.value)
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_format_rejection_does_not_echo_server_details(audience):
    client = client_for(lambda request: sdk_httpx.Response(400, json={"error": {
        "message": "response_format is unsupported; private-server-text"}}))
    try:
        with pytest.raises(ProviderCallError, match="Settings > Keys and connections") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}],
                response_format={"type": "unknown"})
        assert "private-server-text" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_usage_names_the_model_setting_without_retry(audience):
    body = completion(audience)
    body.pop("usage")
    requests = []

    def handler(request):
        requests.append(request)
        return sdk_httpx.Response(200, json=body)

    client = client_for(handler)
    try:
        with pytest.raises(ProviderCallError, match="Choose another model in Settings > Keys and connections") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
        assert error.value.code == "usage_unconfirmed" and len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_server_key_rejection_names_your_local_key_setting(audience, status, monkeypatch):
    handler = lambda request: sdk_httpx.Response(status, json={"error": {"message": "context length maximum 4096; private-server-text"}})
    client = client_for(handler)
    try:
        with pytest.raises(ProviderCallError, match="model API key in Settings > Keys and connections") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
        assert error.value.code == "bad_api_key" and "private-server-text" not in str(error.value)
    finally:
        await client.close()
    load_company_os_app()
    from app.llm import founder_provider
    from llm_gateway import provider_foundry

    monkeypatch.setattr(provider_foundry, "create_async_client_from_config", lambda config: (client_for(handler), config))
    with pytest.raises(ProviderCallError, match="model API key in Settings > Keys and connections"):
        await founder_provider.compatible_provider_models(SimpleNamespace())


@pytest.mark.asyncio
async def test_busy_server_gives_a_wait_step(audience):
    client = client_for(lambda request: sdk_httpx.Response(429, json={"error": {"message": "private-server-text"}}))
    try:
        with pytest.raises(ProviderCallError, match="Wait for your current request") as error:
            await client.create(model="small-model", messages=[{"role": "user", "content": audience}])
        assert error.value.code == "rate_limited" and "private-server-text" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.parametrize("endpoint,provider,free", [
    ("http://127.0.0.1:11434/v1", "openai_compatible", True),
    ("http://localhost:1234/v1", "openai_compatible", True),
    ("http://[::1]:11434/v1", "openai_compatible", True),
    ("https://remote.example/v1", "openai_compatible", False),
    ("http://127.0.0.1:11434/v1", "openai", False),
])
def test_only_local_loopback_compatible_calls_are_free(audience, monkeypatch, endpoint, provider, free):
    load_company_os_app()
    from app.llm.founder_provider import keyless_local_config

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    config = SimpleNamespace(provider=provider, base_url=endpoint)
    assert keyless_local_config(config) is free
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    assert keyless_local_config(config) is False
