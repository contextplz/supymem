"""Responses wire conversion for the existing chat gateway, without retries.

Keep all output items for stateless reasoning/tool rounds. The gateway owns
provider selection, the clock, client lifetime and settlement.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from llm_gateway.errors import ProviderCallError
from llm_gateway.usage import extract_confirmed_token_usage


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _dump(value: Any) -> Any:
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, SimpleNamespace):
        return _dump(vars(value))
    return deepcopy(value)


def _content(content: Any) -> str | list[dict[str, Any]]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TypeError("Responses message content must be text or content parts")
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
        elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
            parts.append({"type": "input_text", "text": part["text"]})
        elif isinstance(part, dict) and part.get("type") == "image_url":
            image = part["image_url"]
            image = {"url": image} if isinstance(image, str) else image
            parts.append({"type": "input_image", "image_url": image["url"],
                          **({"detail": image["detail"]} if image.get("detail") else {})})
        elif isinstance(part, dict) and part.get("type") in {"input_image", "input_file"}:
            parts.append(deepcopy(part))
        else:
            raise ValueError("Unsupported Responses content part")
    return parts


def _input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("responses_output_items"):
            # Replaying only text/tool calls drops encrypted reasoning and phase.
            items.extend(_dump(message["responses_output_items"]))
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise ValueError("Responses tool result needs tool_call_id")
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": _content(message.get("content"))})
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("Unsupported Responses message role")
        content = _content(message.get("content"))
        if content or role != "assistant":
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls") or []:
            function = call["function"]
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": function["name"], "arguments": function["arguments"]})
    return items


def responses_request_kwargs(
    *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None, max_completion_tokens: int | None = None,
    max_output_tokens: int | None = None, response_format: dict[str, Any] | None = None,
    reasoning_effort: str | None = None, parallel_tool_calls: bool | None = None,
    tool_choice: Any = None, extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact inference payload, also used by pre-dispatch estimates."""
    if parallel_tool_calls is not None and type(parallel_tool_calls) is not bool:
        raise ValueError("parallel_tool_calls must be a boolean")
    # DeepSeek's thinking switch has no meaning in Responses. Other body
    # overrides could overwrite this validated payload, so reject them.
    if extra_body and set(extra_body) - {"thinking", "reasoning_effort"}:
        raise ValueError("Unsupported Responses extra_body option")
    effort = reasoning_effort or (extra_body or {}).get("reasoning_effort") or "low"
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported Sol reasoning effort")
    kwargs: dict[str, Any] = {
        "model": model, "input": _input(messages), "store": False,
        "include": ["reasoning.encrypted_content"], "reasoning": {"effort": effort},
        "service_tier": "default",
    }
    limit = max_output_tokens if max_output_tokens is not None else max_completion_tokens
    limit = max_tokens if limit is None else limit
    if limit is not None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("Responses output limit must be a positive integer")
        kwargs["max_output_tokens"] = limit
    if tools:
        converted = []
        for tool in tools:
            if tool.get("type", "function") != "function":
                raise ValueError("The chat gateway supports function tools only")
            function = tool.get("function", tool)
            converted.append({"type": "function", "name": function["name"],
                "parameters": deepcopy(function.get("parameters", {"type": "object", "properties": {}})),
                # Existing tool schemas have optional fields; never silently
                # normalize them into all-required strict schemas.
                "strict": function.get("strict", False),
                **({"description": function["description"]} if function.get("description") else {})})
        kwargs["tools"] = converted
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            tool_choice = {"type": "function", "name": tool_choice.get("function", tool_choice)["name"]}
        kwargs["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = parallel_tool_calls
    if response_format:
        format_type = response_format.get("type")
        if format_type == "json_schema":
            kwargs["text"] = {"format": {"type": "json_schema", **deepcopy(response_format["json_schema"])}}
        elif format_type in {"json_object", "text"}:
            kwargs["text"] = {"format": deepcopy(response_format)}
        else:
            raise ValueError("Unsupported Responses response_format")
    return kwargs


def _usage_details_present(usage: Any) -> bool:
    if type(usage) is dict and all(usage.get(key) is None for key in ("input_tokens_details", "output_tokens_details")):
        return type(usage.get("total_tokens")) is int and extract_confirmed_token_usage(usage) is not None
    return type(usage) is dict and all(
        type(usage.get(container)) is dict and counter in usage[container]
        for container, counter in (("input_tokens_details", "cached_tokens"),
                                   ("output_tokens_details", "reasoning_tokens")))


def _chat_usage(usage: Any) -> Any:
    if usage is None:
        return None
    raw = _dump(usage)
    if not _usage_details_present(raw):
        return None
    if any(raw.get(key) is not None for key in ("input_tokens_details", "output_tokens_details")):
        for container, counter in (("input_tokens_details", "cached_tokens"),
                                   ("output_tokens_details", "reasoning_tokens")):
            if type(raw[container][counter]) is not int:
                return None
    # A Responses wire receipt must not silently discard a second counter
    # protocol while renaming its canonical fields for chat consumers.
    if any(key in raw for key in (
        "prompt_tokens", "completion_tokens", "prompt_tokens_details", "completion_tokens_details",
    )):
        return None
    # Copy only provider-supplied counters. Missing or invalid receipts remain
    # unknown to extract_confirmed_token_usage instead of becoming fake zeros.
    mapped = {target: raw[source] for source, target in (
        ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"), ("input_tokens_details", "prompt_tokens_details"),
        ("output_tokens_details", "completion_tokens_details"),
    ) if source in raw}
    return SimpleNamespace(**mapped)


def responses_to_chat(response: Any) -> Any:
    """Expose finish state and billed usage through existing chat contracts."""
    usage = _chat_usage(_get(response, "usage"))
    output = _dump(_get(response, "output", []))
    text, refusals, calls = [], [], []
    invalid_output = False
    try:
        if type(output) is not list:
            raise ValueError("invalid output")
        for item in output:
            if type(item) is not dict:
                raise ValueError("invalid output item")
            if item.get("type") == "message":
                if type(item.get("content")) is not list:
                    raise ValueError("invalid message content")
                for part in item["content"]:
                    if type(part) is not dict:
                        raise ValueError("invalid content part")
                    if part.get("type") == "output_text":
                        if type(part.get("text")) is not str:
                            raise ValueError("invalid text")
                        text.append(part["text"])
                    elif part.get("type") == "refusal":
                        if type(part.get("refusal")) is not str:
                            raise ValueError("invalid refusal")
                        refusals.append(part["refusal"])
            elif item.get("type") == "function_call":
                if any(type(item.get(key)) is not str for key in ("call_id", "name", "arguments")):
                    raise ValueError("invalid function call")
                calls.append(SimpleNamespace(id=item["call_id"], type="function",
                    function=SimpleNamespace(name=item["name"], arguments=item["arguments"])))
    except (ValueError, TypeError, KeyError, AttributeError):
        invalid_output = True
        output, text, refusals, calls = [], [], [], []
    status = "failed" if invalid_output else _get(response, "status")
    incomplete = _dump(_get(response, "incomplete_details"))
    error = {"code": "invalid_provider_output"} if invalid_output else _dump(_get(response, "error"))
    reason = _get(incomplete, "reason")
    error_code = str(_get(error, "code", ""))
    filtered = bool(refusals) or reason == "content_filter" or "content_filter" in error_code
    if filtered:
        finish = "content_filter"
    elif status == "incomplete":
        finish = "length"
    elif status != "completed":
        finish = "provider_error"
    else:
        finish = "tool_calls" if calls else "stop"
    # Failed or filtered responses can have usage. Return the receipt for
    # settlement before workflow validation, never an executable tool call.
    message = SimpleNamespace(role="assistant", content=None if filtered or finish == "provider_error" else "".join(text) or None,
        tool_calls=calls if finish == "tool_calls" else None, refusal="\n".join(refusals) or None,
        responses_output_items=output)
    return SimpleNamespace(id=_get(response, "id"), model=_get(response, "model"),
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)],
        usage=usage, status=status,
        service_tier=_get(response, "service_tier"),
        incomplete_details=incomplete, error=error, responses_raw=(
            {"id": _get(response, "id"), "model": _get(response, "model"),
             "service_tier": _get(response, "service_tier"), "status": status,
             "output": [], "usage": _get(response, "usage"), "error": error,
             "provider_output_error": "invalid_output"}
            if invalid_output else response),
        provider_output_error="invalid_output" if invalid_output else None)


async def responses_create(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Keep billing primitives from the wire before SDK model coercion."""
    response = await client.responses.with_raw_response.create(**kwargs)
    payload = response.http_response.json()
    if type(payload) is not dict:
        raise ProviderCallError("provider_error", "The provider returned an invalid response.", retryable=False)
    if not _usage_details_present(payload.get("usage")):
        payload = {**payload, "usage": None}
    return payload


async def responses_chat(client: Any, **kwargs: Any) -> Any:
    """One call; caller supplies a no-retry client and settles returned usage."""
    return responses_to_chat(await responses_create(client, **responses_request_kwargs(**kwargs)))


class ResponsesFailure(ProviderCallError):
    """Stream ended without a valid terminal response; never automatically replay."""


async def responses_stream(client: Any, *, provider_used: str | None = None, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
    """Convert text events; emit the terminal receipt before any failure signal."""
    from openai import AsyncStream
    response = await client.responses.with_raw_response.create(**responses_request_kwargs(**kwargs), stream=True)
    stream = response.parse(to=AsyncStream[object])
    terminal = None
    try:
        async for event in stream:
            kind = _get(event, "type")
            if kind == "response.output_text.delta":
                yield {"type": "delta", "content": _get(event, "delta", ""),
                       **({"provider_used": provider_used} if provider_used else {})}
            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                terminal = responses_to_chat(_get(event, "response"))
                finish = terminal.choices[0].finish_reason
                counts = extract_confirmed_token_usage(terminal.usage)
                usage = None if counts is None else {**counts,
                    "prompt_tokens_details": {"cached_tokens": counts["cached_tokens"]},
                    "completion_tokens_details": {"reasoning_tokens": counts["reasoning_tokens"]}}
                yield {"type": "usage", "usage": usage,
                       "response": terminal, "finish_reason": finish, "status": terminal.status,
                       **({"provider_used": provider_used} if provider_used else {})}
                if counts is None or finish not in {"stop", "tool_calls"}:
                    failure = ResponsesFailure("usage_unconfirmed" if counts is None else "content_filter" if finish == "content_filter" else "provider_error",
                        "The provider could not finish this response.", retryable=False)
                    failure.response = terminal
                    raise failure
            elif kind == "error":
                raise ResponsesFailure("provider_error", "The provider stream failed.", retryable=False)
        if terminal is None:
            raise ResponsesFailure("provider_error", "The provider stream ended before its receipt.", retryable=False)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            await close()
