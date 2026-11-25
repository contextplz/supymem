"""Offline Responses contract checks; no live model or founder data."""

import json
from types import SimpleNamespace

import httpx
import pytest
from llm_gateway.responses import (
    ResponsesFailure,
    responses_chat,
    responses_request_kwargs,
    responses_stream,
    responses_to_chat,
)
from llm_gateway.usage import extract_confirmed_token_usage, extract_token_usage
from openai import AsyncOpenAI, RateLimitError

MODEL = "gpt-6.1-sol"
MESSAGES = [{"role": "system", "content": "Follow the supplied task."},
            {"role": "user", "content": "Read the project."}]
TOOL = {"type": "function", "function": {"name": "read_file",
        "description": "Read a project file.", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}}}}}
USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
         "input_tokens_details": {"cached_tokens": 60},
         "output_tokens_details": {"reasoning_tokens": 8}}
REASONING = {"type": "reasoning", "id": "rs_offline", "summary": [],
             "encrypted_content": "offline-reasoning"}
CALLS = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}",
          "name": "read_file", "arguments": json.dumps({"path": f"file-{i}"}),
          "status": "completed"} for i in range(2)]


def response(*, output=None, status="completed", **kwargs):
    return {"id": "resp_offline", "object": "response", "created_at": 0,
            "model": MODEL, "status": status,
            "output": output if output is not None else [
                {"type": "message", "id": "msg_offline", "role": "assistant",
                 "phase": "final_answer", "status": "completed",
                 "content": [{"type": "output_text", "text": "Read it.", "annotations": []}]}],
            "usage": USAGE, **kwargs}


def test_wire_payload_maps_caps_effort_tools_and_preserves_policy():
    wire = responses_request_kwargs(model=MODEL, messages=MESSAGES, tools=[TOOL],
        max_completion_tokens=8000, reasoning_effort="medium", parallel_tool_calls=False,
        tool_choice={"type": "function", "function": {"name": "read_file"}},
        extra_body={"thinking": {"type": "disabled"}})
    assert wire["model"] == MODEL and wire["max_output_tokens"] == 8000
    assert wire["input"] == MESSAGES and wire["reasoning"] == {"effort": "medium"}
    assert wire["store"] is False and wire["include"] == ["reasoning.encrypted_content"]
    assert wire["service_tier"] == "default"
    assert wire["tools"] == [{"type": "function", **TOOL["function"], "strict": False}]
    assert wire["parallel_tool_calls"] is False
    assert wire["tool_choice"] == {"type": "function", "name": "read_file"}
    assert not ({"max_tokens", "max_completion_tokens", "extra_body", "messages"} & wire.keys())


def test_round_trip_keeps_all_reasoning_function_calls_and_phase():
    original = response(output=[REASONING, *CALLS])
    chat = responses_to_chat(original)
    message = chat.choices[0].message
    assert chat.choices[0].finish_reason == "tool_calls"
    assert [call.id for call in message.tool_calls] == ["call_0", "call_1"]
    transcript = [*MESSAGES, {"role": "assistant", "content": message.content,
        "tool_calls": [{"id": "ignored", "function": {"name": "ignored", "arguments": "{}"}}],
        "responses_output_items": message.responses_output_items},
        *[{"role": "tool", "tool_call_id": f"call_{i}", "content": "Read."} for i in range(2)]]
    wire = responses_request_kwargs(model=MODEL, messages=transcript, max_tokens=100)
    assert wire["input"] == [*MESSAGES, REASONING, *CALLS,
        *[{"type": "function_call_output", "call_id": f"call_{i}", "output": "Read."} for i in range(2)]]
    assert original["output"] == [REASONING, *CALLS]
    assert wire["service_tier"] == "default"
    final = responses_to_chat(response())
    assert final.choices[0].message.responses_output_items[0]["phase"] == "final_answer"


def test_legacy_chat_tool_transcript_and_vision_convert_without_loss():
    wire = responses_request_kwargs(model=MODEL, messages=[
        {"role": "user", "content": [{"type": "text", "text": "Inspect this."},
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png", "detail": "high"}}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_0", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "done"}])
    assert wire["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Inspect this."},
            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "high"}]},
        {"type": "function_call", "call_id": "call_0", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_0", "output": "done"}]


@pytest.mark.parametrize("fmt", [{"type": "json_object"}, {"type": "json_schema", "json_schema": {
    "name": "answer", "strict": True, "schema": {"type": "object", "properties": {
        "answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}}}])
def test_text_format_supports_json_mode_and_explicit_strict_schema(fmt):
    wire = responses_request_kwargs(model=MODEL, messages=MESSAGES, response_format=fmt)
    assert wire["text"]["format"] == ({"type": "json_schema", **fmt["json_schema"]}
        if fmt["type"] == "json_schema" else fmt)


def test_usage_is_confirmed_including_cache_and_reasoning():
    chat = responses_to_chat(response())
    expected = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                "cached_tokens": 60, "reasoning_tokens": 8}
    assert extract_confirmed_token_usage(chat.usage) == expected
    assert extract_token_usage(chat.usage) == expected


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 1, "output_tokens": 1},
    {**USAGE, "total_tokens": 121}, {**USAGE, "input_tokens": True},
    {**USAGE, "input_tokens_details": {"cached_tokens": 101}},
    {**USAGE, "output_tokens_details": {"reasoning_tokens": 21}}])
def test_missing_or_malformed_receipts_stay_unknown(usage):
    assert extract_confirmed_token_usage(responses_to_chat(response(usage=usage)).usage) is None


@pytest.mark.parametrize("status,details,error,refusal,finish", [
    ("incomplete", {"reason": "max_output_tokens"}, None, False, "length"),
    ("incomplete", {"reason": "content_filter"}, None, False, "content_filter"),
    ("completed", None, None, True, "content_filter"),
    ("failed", None, {"code": "content_filter"}, False, "content_filter"),
    ("failed", None, {"code": "server_error"}, False, "provider_error"),
])
def test_unfinished_or_refused_response_exposes_receipt_before_validation(status, details, error, refusal, finish):
    output = [{"type": "message", "content": [{"type": "refusal", "refusal": "Cannot do that."}]}] if refusal else CALLS
    chat = responses_to_chat(response(status=status, incomplete_details=details, error=error, output=output))
    assert chat.choices[0].finish_reason == finish and chat.status == status
    assert chat.choices[0].message.tool_calls is None
    assert extract_confirmed_token_usage(chat.usage)["total_tokens"] == 120
    if refusal:
        assert chat.choices[0].message.refusal == "Cannot do that."
        assert chat.choices[0].message.content is None


@pytest.mark.parametrize("options", [{"parallel_tool_calls": "false"}, {"max_tokens": 0},
    {"reasoning_effort": "none"}, {"extra_body": {"model": "other"}}])
def test_invalid_options_are_rejected_before_dispatch(options):
    with pytest.raises(ValueError):
        responses_request_kwargs(model=MODEL, messages=MESSAGES, **options)


@pytest.mark.asyncio
async def test_real_sdk_wire_conversion_and_no_retry_after_ambiguous_failure():
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=response())
        return httpx.Response(429, json={"error": {"type": "rate_limit_error", "message": "offline"}})
    client = AsyncOpenAI(api_key="offline", base_url="https://example.test/openai/v1",
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    try:
        chat = await responses_chat(client, model=MODEL, messages=MESSAGES, max_tokens=512)
        assert chat.choices[0].message.content == "Read it."
        assert extract_confirmed_token_usage(chat.usage)["cached_tokens"] == 60
        with pytest.raises(RateLimitError):
            await responses_chat(client, model=MODEL, messages=MESSAGES)
        assert len(requests) == 2 and requests[0]["max_output_tokens"] == 512
    finally:
        await client.close()


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
    async def __aiter__(self):
        for event in self.events:
            yield event
    async def close(self):
        self.closed = True


def stream_client(events):
    stream = Stream(events)
    async def create(**kwargs):
        assert kwargs["stream"] is True and "stream_options" not in kwargs
        assert kwargs["service_tier"] == "default"
        def parse(*, to):
            from openai import AsyncStream
            assert to == AsyncStream[object]
            return stream
        return SimpleNamespace(parse=parse)
    return SimpleNamespace(responses=SimpleNamespace(with_raw_response=SimpleNamespace(create=create))), stream


@pytest.mark.asyncio
async def test_stream_yields_text_and_confirmed_final_receipt_and_preserves_output_items():
    client, stream = stream_client([{"type": "response.output_text.delta", "delta": "Read"},
        {"type": "response.output_text.delta", "delta": " it."},
        {"type": "response.completed", "response": response()}])
    chunks = [chunk async for chunk in responses_stream(client, model=MODEL,
        messages=MESSAGES, max_tokens=100, provider_used="foundry")]
    assert [chunk["content"] for chunk in chunks[:-1]] == ["Read", " it."]
    receipt = chunks[-1]
    assert receipt["type"] == "usage" and receipt["finish_reason"] == "stop"
    assert receipt["usage"]["cached_tokens"] == 60 and receipt["provider_used"] == "foundry"
    assert extract_confirmed_token_usage(receipt["response"].usage)["reasoning_tokens"] == 8
    assert receipt["response"].choices[0].message.responses_output_items[0]["phase"] == "final_answer"
    assert stream.closed


@pytest.mark.asyncio
async def test_incomplete_stream_emits_billed_receipt_then_fails_without_replay():
    client, stream = stream_client([{"type": "response.incomplete", "response": response(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"})}])
    chunks = []
    with pytest.raises(ResponsesFailure) as error:
        async for chunk in responses_stream(client, model=MODEL, messages=MESSAGES):
            chunks.append(chunk)
    assert len(chunks) == 1 and chunks[0]["finish_reason"] == "length"
    assert extract_confirmed_token_usage(error.value.response.usage)["total_tokens"] == 120
    assert error.value.retryable is False and stream.closed


@pytest.mark.parametrize("events", [[], [{"type": "error", "message": "private upstream detail"}]])
@pytest.mark.asyncio
async def test_stream_without_receipt_fails_as_unknown_without_leaking_body(events):
    client, stream = stream_client(events)
    with pytest.raises(ResponsesFailure) as error:
        _ = [chunk async for chunk in responses_stream(client, model=MODEL, messages=MESSAGES)]
    assert error.value.retryable is False
    assert "private" not in str(error.value) and stream.closed


@pytest.mark.asyncio
async def test_completed_stream_without_usage_never_invents_confirmed_zero():
    value = response()
    value['usage'] = None
    client, stream = stream_client([{'type': 'response.completed', 'response': value}])
    chunks = []
    with pytest.raises(ResponsesFailure, match='could not finish') as error:
        async for chunk in responses_stream(client, model=MODEL, messages=MESSAGES):
            chunks.append(chunk)
    assert error.value.code == 'usage_unconfirmed' and stream.closed
    assert len(chunks) == 1 and chunks[0]['usage'] is None
    assert extract_confirmed_token_usage(chunks[0]['response'].usage) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("actual_tier", [None, "priority"])
async def test_real_sdk_default_wire_preserves_captured_role_tool_round_and_actual_tier(streaming, actual_tier):
    from llm_gateway.provider_foundry import ProviderConfig
    from openai import _base_client as sdk_base
    sdk_httpx = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
    captured = ProviderConfig(provider="azure_foundry", source="platform_default",
        base_url="https://example.test", deployment_pro="sol-captured-deployment", catalog_model=MODEL,
        api_mode="responses", routing_purpose="build.model", captured_tier="pro",
        reasoning_effort="low", max_output_tokens=128)
    messages = [*MESSAGES, {"role": "assistant", "content": None,
        "responses_output_items": [REASONING, *CALLS]},
        {"role": "tool", "tool_call_id": "call_0", "content": "Exact first result."},
        {"role": "tool", "tool_call_id": "call_1", "content": "Exact second result."}]
    original = json.dumps(messages, sort_keys=True)
    value = response(output=[REASONING, *CALLS], service_tier=actual_tier)
    requests = []
    def handle(request):
        requests.append(request)
        if not streaming:
            return sdk_httpx.Response(200, json=value)
        events = [{"type": "response.output_text.delta", "delta": "Read.", "content_index": 0,
            "item_id": "msg_offline", "output_index": 0, "sequence_number": 1, "logprobs": []},
            {"type": "response.completed", "response": value, "sequence_number": 2}]
        payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events)
        return sdk_httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())
    client = AsyncOpenAI(api_key="offline", base_url="https://example.test/openai/v1", max_retries=0,
        http_client=sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(handle), trust_env=False, follow_redirects=False))
    kwargs = dict(model=captured.deployment_pro, messages=messages, tools=[TOOL],
        max_tokens=captured.max_output_tokens, reasoning_effort=captured.reasoning_effort, parallel_tool_calls=False)
    try:
        if streaming:
            chunks = [chunk async for chunk in responses_stream(client, **kwargs)]
            reply = chunks[-1]["response"]
            assert chunks[-1]["finish_reason"] == "tool_calls"
        else:
            reply = await responses_chat(client, **kwargs)
        assert len(requests) == 1 and requests[0].url.path == "/openai/v1/responses"
        wire = json.loads(requests[0].content)
        expected = responses_request_kwargs(**kwargs)
        assert wire == {**expected, **({"stream": True} if streaming else {})}
        assert wire["service_tier"] == "default" and wire["model"] == captured.deployment_pro
        assert wire["reasoning"] == {"effort": "low"} and wire["max_output_tokens"] == 128
        assert wire["input"][2:5] == [REASONING, *CALLS]
        assert reply.service_tier == actual_tier and reply.model == MODEL
        assert reply.choices[0].message.responses_output_items == [REASONING, *CALLS]
        assert extract_confirmed_token_usage(reply.usage)["reasoning_tokens"] == 8
        assert json.dumps(messages, sort_keys=True) == original
    finally:
        await client.close()
    assert client.is_closed()


def test_transport_estimate_counts_default_tier_in_exact_sol_wire_and_leaves_legacy_chat_unchanged():
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig
    clock = "Now (server clock): fixed offline quote"
    config = ProviderConfig(provider="azure_foundry", source="platform_default", base_url="https://example.test",
        deployment_pro="sol-captured-deployment", catalog_model=MODEL, api_mode="responses",
        routing_purpose="build.model", captured_tier="pro", reasoning_effort="low", max_output_tokens=128)
    quoted_messages = [MESSAGES[0], {"role": "system", "content": clock}, MESSAGES[1]]
    chat_kwargs = sole._completion_kwargs(quoted_messages, config=config, model=config.deployment_pro,
        purpose="build.model", tools=[TOOL])
    wire = responses_request_kwargs(**chat_kwargs)
    encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    estimate = sole.transport_prompt_tokens(MESSAGES, purpose="build.model", config=config, tools=[TOOL], clock_text=clock)
    assert estimate == len(encoded) + 128 + 16 * len(quoted_messages)
    without_tier = {key: value for key, value in wire.items() if key != "service_tier"}
    assert len(encoded) - len(json.dumps(without_tier, ensure_ascii=False, separators=(",", ":")).encode()) == len(',"service_tier":"default"')
    legacy = config.model_copy(update={"deployment_pro": "DeepSeek-V4-Pro", "catalog_model": "DeepSeek-V4-Pro",
        "api_mode": "chat_completions", "reasoning_effort": ""})
    legacy_kwargs = sole._completion_kwargs(quoted_messages, config=legacy, model=legacy.deployment_pro,
        purpose="build.model", tools=[TOOL])
    assert "service_tier" not in legacy_kwargs
    assert sole.transport_prompt_tokens(MESSAGES, purpose="build.model", config=legacy, tools=[TOOL], clock_text=clock) == (
        len(json.dumps(legacy_kwargs, ensure_ascii=False, separators=(",", ":")).encode()) + 128 + 16 * len(quoted_messages))
