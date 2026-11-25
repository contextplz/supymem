"""Billing primitives reach strict validation from actual SDK JSON/SSE unchanged."""
from copy import deepcopy
import json
import warnings

import pytest
from openai import AsyncOpenAI, _base_client
from pydantic import SecretStr

from llm_gateway import sole
from llm_gateway.provider_foundry import ProviderConfig, normalize_base_url
from llm_gateway.responses import ResponsesFailure, responses_stream
from llm_gateway.usage import extract_confirmed_token_usage

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2
MODEL = "supymem-gpt-6-1-sol"
MARKER = "synthetic-private-wire-marker"
COUNTS = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
    "input_tokens_details": {"cached_tokens": 20}, "output_tokens_details": {"reasoning_tokens": 7}}
MODES = ("chat", "raw-media", "stream")
DEFAULT_OUTPUT = object()


def _field(value, path):
    for key in path.split("."):
        value = value[key]
    return value


def _replace(value, path, replacement):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value[key]
    value[keys[-1]] = replacement


async def _wire(monkeypatch, mode, usage, *, tier=None, oversized=False, output=DEFAULT_OUTPUT):
    """Patch the network factory only; keep gateway conversion and the real SDK."""
    from app.core.local_mode import is_local

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("LLM_API_KEY", "offline-wire-key")
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "time_context", lambda: "Trusted fixed wire clock")
    cfg = ProviderConfig(provider="azure_foundry", base_url="https://offline.invalid/models",
        api_key=SecretStr("offline-wire-key"), source="founder_override" if is_local() else "platform_default",
        deployment_pro=MODEL, deployment_flash=MODEL, catalog_model="gpt-6.1-sol",
        routing_purpose="device.vision" if mode == "raw-media" else "build.model",
        captured_tier="pro", api_mode="responses", reasoning_effort="low", max_output_tokens=128)
    payload = {"id": "resp_wire", "object": "response", "status": "completed", "model": MODEL,
        "service_tier": tier, "output": [{"id": "msg_wire", "type": "message", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "annotations": [],
                "text": "x" * 200001 if oversized else '{"reply":"synthetic"}'}]}]}
    if output is not DEFAULT_OUTPUT:
        payload["output"] = deepcopy(output)
    if usage is not None:
        payload["usage"] = deepcopy(usage)
    requests, clients = [], []

    def transport(request):
        requests.append(request)
        if mode != "stream":
            return httpx.Response(200, content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"})
        events = [{"type": "response.output_text.delta", "delta": "synthetic", "content_index": 0,
            "item_id": "msg_wire", "output_index": 0, "sequence_number": 1, "logprobs": []},
            {"type": "response.completed", "response": payload, "sequence_number": 2}]
        data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data.encode())

    def factory(config):
        client = AsyncOpenAI(api_key="offline-wire-key",
            base_url=normalize_base_url(config.provider, config.base_url, api_mode=config.api_mode),
            max_retries=2, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport),
                follow_redirects=False, trust_env=False))
        clients.append(client)
        return client, config

    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    chunks, error = [], None
    if mode == "chat":
        result = await sole.transmit([{"role": "user", "content": "Synthetic JSON wire probe"}],
            purpose=cfg.routing_purpose, workspace_id="wire-workspace", config=cfg, tier="pro",
            max_tokens=128, response_schema={"type": "json_object"}, single_attempt=True, use_workspace_override=False)
        raw, mapped = result.responses_raw, result.usage
        assert result.provider_output_error == raw.get("provider_output_error")
    elif mode == "raw-media":
        raw = await sole.transmit_responses_request({"input": [{"role": "user", "content": "Synthetic raw wire probe"}],
            "max_output_tokens": 128, "text": {"format": {"type": "json_object"}}},
            purpose=cfg.routing_purpose, workspace_id="wire-workspace", config=cfg)
        mapped = raw.get("usage")
    else:
        client, _ = factory(cfg)
        try:
            try:
                async for item in responses_stream(client.with_options(max_retries=0), model=MODEL,
                        messages=[{"role": "user", "content": "Synthetic stream wire probe"}], max_tokens=128):
                    chunks.append(item)
            except ResponsesFailure as exc:
                error = exc
            receipt = next(item["response"] for item in chunks if item["type"] == "usage")
            raw, mapped = receipt.responses_raw, receipt.usage
        finally:
            await client.close()
    assert len(requests) == len(clients) == 1 and clients[0].is_closed()
    assert requests[0].headers["x-stainless-retry-count"] == "0"
    assert requests[0].url.path == "/openai/v1/responses"
    body = json.loads(requests[0].content)
    assert body["model"] == MODEL and body["max_output_tokens"] == 128
    assert body["reasoning"] == {"effort": "low"}
    assert raw["model"] == MODEL
    assert raw["service_tier"] == tier
    return raw, mapped, chunks, error


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("path,value", [
    ("input_tokens", "100"), ("input_tokens", True), ("input_tokens", 100.0), ("input_tokens", 100.5),
    ("output_tokens", "50"), ("total_tokens", "150"),
    ("input_tokens_details.cached_tokens", "20"), ("input_tokens_details.cached_tokens", True),
    ("output_tokens_details.reasoning_tokens", "7"), ("output_tokens_details.reasoning_tokens", 7.0),
])
async def test_raw_counter_types_remain_unknown_before_owner_pricing(monkeypatch, mode, path, value):
    usage = deepcopy(COUNTS)
    _replace(usage, path, value)
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, usage)
    actual = _field(raw["usage"], path)
    assert type(actual) is type(value) and actual == value
    assert extract_confirmed_token_usage(mapped) is None
    if mode == "stream":
        assert chunks[-1]["type"] == "usage" and chunks[-1]["usage"] is None
        assert error is not None and error.code == "usage_unconfirmed" and error.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("fault", ["total", "mixed-aliases", "missing-input", "absent-usage"])
async def test_missing_or_contradictory_wire_counts_remain_unknown(monkeypatch, mode, fault):
    usage = deepcopy(COUNTS)
    if fault == "total":
        usage["total_tokens"] = 151
    elif fault == "mixed-aliases":
        usage.update(prompt_tokens=101, completion_tokens=50)
    elif fault == "missing-input":
        usage.pop("input_tokens")
    else:
        usage = None
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, usage)
    if usage is None:
        assert raw.get("usage") is None
    else:
        assert raw["usage"] == usage
    assert extract_confirmed_token_usage(mapped) is None
    if mode == "stream":
        assert chunks[-1]["usage"] is None and error.code == "usage_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tier", [None, "priority"])
async def test_integer_receipt_and_actual_nullable_tier_survive_sdk_boundary(monkeypatch, mode, tier):
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, COUNTS, tier=tier)
    assert raw["usage"] == COUNTS and raw["service_tier"] == tier
    assert extract_confirmed_token_usage(mapped) == {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cached_tokens": 20, "reasoning_tokens": 7}
    if mode == "stream":
        assert chunks[-1]["usage"]["reasoning_tokens"] == 7 and error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_raw_malformed_marker_never_reaches_warning_or_log(monkeypatch, caplog, mode):
    usage = deepcopy(COUNTS)
    usage["input_tokens"] = MARKER
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        raw, mapped, _, _ = await _wire(monkeypatch, mode, usage)
    assert raw["usage"]["input_tokens"] == MARKER and extract_confirmed_token_usage(mapped) is None
    assert MARKER not in caplog.text and all(MARKER not in str(warning.message) for warning in emitted)


@pytest.mark.asyncio
async def test_oversized_raw_receipt_retains_original_invalid_counter_and_actual_identity(monkeypatch, caplog):
    usage = deepcopy(COUNTS)
    usage["input_tokens"] = "100"
    raw, mapped, _, _ = await _wire(monkeypatch, "raw-media", usage, tier=None, oversized=True)
    assert raw["status"] == "failed" and raw["output"] == [] and raw["error"]["code"] == "response_too_large"
    assert raw["usage"] == usage and type(raw["usage"]["input_tokens"]) is str
    assert raw["model"] == MODEL and raw["service_tier"] is None and extract_confirmed_token_usage(mapped) is None
    assert len(json.dumps(raw).encode()) < 200000 and MARKER not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [float("nan"), "\ud800"])
async def test_raw_media_serialization_failure_retains_paid_receipt(monkeypatch, text):
    raw, mapped, _, _ = await _wire(monkeypatch, "raw-media", COUNTS, tier="default",
        output=[{"type": "message", "content": [{"type": "output_text", "text": text}]}])
    assert raw["status"] == "failed" and raw["output"] == []
    assert raw["error"]["code"] == "invalid_provider_output"
    assert raw["provider_output_error"] == "invalid_output"
    assert raw["usage"] == COUNTS and raw["model"] == MODEL and raw["service_tier"] == "default"
    assert extract_confirmed_token_usage(mapped) == {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cached_tokens": 20, "reasoning_tokens": 7}
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) < 200000


@pytest.mark.parametrize("invalid", [None, {"input_tokens": "100", "output_tokens": 50, "total_tokens": 150}])
def test_sol_usage_recorder_never_turns_unknown_wire_counts_into_confirmed_numbers(monkeypatch, invalid):
    from llm_gateway import sole
    from llm_gateway.responses import responses_to_chat
    calls = []
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: calls.append(kwargs))
    sole.note_usage(responses_to_chat({"status": "completed", "model": MODEL, "usage": invalid}), workspace_id="ws_wire",
                    model=MODEL, purpose="venture.chat.route")
    assert not calls


def test_sol_usage_recorder_keeps_confirmed_integer_subsets(monkeypatch):
    from llm_gateway import sole
    from llm_gateway.responses import responses_to_chat
    calls = []
    monkeypatch.setattr(sole, "_usage_recorder", lambda *args, **kwargs: calls.append(kwargs))
    sole.note_usage(responses_to_chat({"status": "completed", "model": MODEL, "usage": COUNTS}), workspace_id="ws_wire",
        model=MODEL, purpose="venture.chat.route")
    assert len(calls) == 1 and calls[0]["prompt_tokens"] == 100
    assert calls[0]["cached_tokens"] == 20 and calls[0]["reasoning_tokens"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("output", [
    [None],
    [{"type": "message", "content": [{"type": "output_text", "text": 123}]}],
    [{"type": "function_call", "call_id": "call_wire", "name": "synthetic", "arguments": {"private": MARKER}}],
    [{"type": "message", "content": [{"type": "refusal", "refusal": {"private": MARKER}}]}],
])
async def test_malformed_output_preserves_known_paid_usage_without_body(monkeypatch, caplog, mode, output):
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, COUNTS, output=output)
    assert raw["status"] == "failed" and raw["output"] == []
    assert raw["error"]["code"] == "invalid_provider_output" and raw["usage"] == COUNTS
    assert raw["provider_output_error"] == "invalid_output"
    assert raw["model"] == MODEL and raw["service_tier"] is None
    assert extract_confirmed_token_usage(mapped) == {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cached_tokens": 20, "reasoning_tokens": 7}
    assert MARKER not in json.dumps(raw) and MARKER not in caplog.text
    if mode == "stream":
        receipt = chunks[-1]
        assert receipt["type"] == "usage" and receipt["usage"]["total_tokens"] == 150
        assert receipt["finish_reason"] == "provider_error"
        assert receipt["response"].provider_output_error == "invalid_output"
        assert receipt["response"].choices[0].message.content is None
        assert receipt["response"].choices[0].message.tool_calls is None
        assert error is not None and error.code == "provider_error" and error.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("container", ["input_tokens_details", "output_tokens_details"])
@pytest.mark.parametrize("shape", ["missing", "false", "string", "list", "empty", "null", "missing-counter"])
async def test_absent_or_malformed_wire_detail_container_never_supplies_fake_zero(monkeypatch, mode, container, shape):
    usage = deepcopy(COUNTS)
    if shape == "missing":
        usage.pop(container)
    else:
        usage[container] = {"false": False, "string": "private malformed", "list": [], "empty": {},
            "null": None, "missing-counter": {"unrelated": 0}}[shape]
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, usage)
    assert raw["usage"] == (usage if mode == "stream" else None)
    assert extract_confirmed_token_usage(mapped) is None
    if mode == "stream":
        assert chunks[-1]["usage"] is None and error.code == "usage_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_explicit_integer_zero_details_are_confirmed_without_defaults(monkeypatch, mode):
    usage = deepcopy(COUNTS)
    usage["input_tokens_details"] = {"cached_tokens": 0}
    usage["output_tokens_details"] = {"reasoning_tokens": 0}
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, usage)
    assert raw["usage"] == usage
    assert extract_confirmed_token_usage(mapped) == {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cached_tokens": 0, "reasoning_tokens": 0}
    if mode == "stream":
        assert error is None and chunks[-1]["usage"]["cached_tokens"] == chunks[-1]["usage"]["reasoning_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("details", [{}, {"input_tokens_details": None, "output_tokens_details": None}])
async def test_both_unreported_wire_details_charge_no_cache_discount(monkeypatch, mode, details):
    usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, **details}
    raw, mapped, chunks, error = await _wire(monkeypatch, mode, usage)
    assert raw["usage"] == usage
    assert extract_confirmed_token_usage(mapped) == {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "cached_tokens": 0, "reasoning_tokens": 0}
    if mode == "stream":
        assert error is None and chunks[-1]["usage"]["cached_tokens"] == chunks[-1]["usage"]["reasoning_tokens"] == 0
