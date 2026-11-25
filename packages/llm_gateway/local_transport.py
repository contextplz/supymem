"""Local native Messages transport behind the gateway's existing chat interface."""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx

from llm_gateway.errors import ProviderCallError
from llm_gateway.usage import extract_confirmed_token_usage

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
_SCHEMA_TOOL = "return_json"


def _error(status: int | None = None) -> ProviderCallError:
    if status in {401, 403}:
        code, message = "bad_api_key", "Your Anthropic key was rejected. Update your Anthropic key in Settings > Keys and connections, then retry."
    elif status == 402:
        code, message = "out_of_credits", "Your Anthropic account has insufficient funds. Add funds to your Anthropic account, then retry."
    elif status == 429:
        code, message = "rate_limited", "Your Anthropic service is busy. Wait a moment, then retry."
    elif status is None or status >= 500:
        code, message = "provider_down", "You could not reach Anthropic. Check your service address in Settings > Keys and connections and your network, then retry."
    else:
        code, message = "provider_error", "Your Anthropic request was rejected. Check your model and service address in Settings > Keys and connections, then retry."
    return ProviderCallError(code, message, status=status, retryable=code in {"rate_limited", "provider_down"})


def _invalid() -> ProviderCallError:
    return ProviderCallError("usage_unconfirmed", "Your Anthropic response was incomplete. Check your model in Settings > Keys and connections, then retry.")


def _content(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    output = []
    for part in value:
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            output.append({"type": "text", "text": part["text"]})
        elif kind in {"image_url", "input_image"}:
            image = part["image_url"]
            image = image["url"] if isinstance(image, dict) else image
            if image.startswith("data:"):
                header, data = image.split(",", 1)
                media_type = header[5:].removesuffix(";base64")
                if not header.endswith(";base64") or media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                    raise ValueError("Choose a PNG, JPEG, GIF or WebP image, then retry.")
                base64.b64decode(data, validate=True)
                source = {"type": "base64", "media_type": media_type, "data": data}
            else:
                from app.core.providers_local import validate_endpoint

                source = {"type": "url", "url": validate_endpoint(image)}
            output.append({"type": "image", "source": source})
        else:
            raise ValueError("Your model cannot read this message format. Choose another model in Settings > Keys and connections, then retry.")
    return output


def messages_request(**kwargs: Any) -> tuple[dict[str, Any], str | None]:
    limit = kwargs.get("max_tokens", kwargs.get("max_completion_tokens", kwargs.get("max_output_tokens", 4096)))
    if type(limit) is not int or limit <= 0:
        raise ValueError("Choose a positive output limit in Settings > Keys and connections, then retry.")
    body: dict[str, Any] = {"model": kwargs.get("model") or DEFAULT_ANTHROPIC_MODEL,
                            "max_tokens": limit, "messages": []}
    system = []
    for message in kwargs["messages"]:
        role = message["role"]
        if role in {"system", "developer"}:
            system.extend(_content(message.get("content")))
            continue
        if role == "tool":
            content = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                        "content": _content(message["content"]) if isinstance(message.get("content"), list)
                        else message.get("content") or ""}]
            role = "user"
        elif role in {"user", "assistant"}:
            content = _content(message.get("content"))
            for call in message.get("tool_calls") or []:
                function = call["function"]
                content.append({"type": "tool_use", "id": call["id"], "name": function["name"],
                                "input": json.loads(function["arguments"])})
        else:
            raise ValueError("Your message role is not supported. Retry your task with a new chat.")
        if not content:
            continue
        if body["messages"] and body["messages"][-1]["role"] == role:
            body["messages"][-1]["content"].extend(content)
        else:
            body["messages"].append({"role": role, "content": content})
    if system:
        body["system"] = system
    tools = []
    for tool in kwargs.get("tools") or []:
        function = tool["function"]
        tools.append({"name": function["name"], "input_schema": deepcopy(function.get("parameters", {"type": "object"})),
                      **({"description": function["description"]} if function.get("description") else {})})
    choice = kwargs.get("tool_choice", "auto")
    if isinstance(choice, dict):
        choice = {"type": "tool", "name": choice["function"]["name"]}
    else:
        choice = {"type": {"required": "any"}.get(choice, choice)}
    schema_name = None
    response_format = kwargs.get("response_format") or {}
    if response_format.get("type") in {"json_schema", "json_object"}:
        schema_name = _SCHEMA_TOOL
        while any(tool["name"] == schema_name for tool in tools):
            schema_name += "_result"
        schema = response_format.get("json_schema", {}).get("schema", {"type": "object", "properties": {}})
        tools.append({"name": schema_name, "description": "Return the requested JSON result.", "input_schema": deepcopy(schema)})
        choice = {"type": "tool", "name": schema_name}
    if tools:
        body.update(tools=tools, tool_choice=choice)
        if kwargs.get("parallel_tool_calls") is False:
            body["tool_choice"]["disable_parallel_tool_use"] = True
    for option in ("temperature", "top_p", "stop"):
        if kwargs.get(option) is not None:
            body["stop_sequences" if option == "stop" else option] = kwargs[option]
    if kwargs.get("stream"):
        body["stream"] = True
    return body, schema_name


def _completion(value: dict[str, Any], schema_name: str | None) -> Any:
    text, tools = [], []
    for block in value.get("content", []):
        if block.get("type") == "text":
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            if block["name"] == schema_name:
                text = [json.dumps(block["input"], ensure_ascii=False)]
            else:
                tools.append(SimpleNamespace(id=block["id"], type="function", function=SimpleNamespace(
                    name=block["name"], arguments=json.dumps(block["input"], ensure_ascii=False))))
    if schema_name and not any(block.get("type") == "tool_use" and block.get("name") == schema_name
                               for block in value.get("content", [])):
        raise _invalid()
    return SimpleNamespace(id=value["id"], model=value["model"], usage=_chat_usage(value.get("usage")), choices=[SimpleNamespace(index=0,
        finish_reason={"max_tokens": "length", "tool_use": "tool_calls" if tools else "stop"}.get(value.get("stop_reason"), "stop"),
        message=SimpleNamespace(role="assistant", content="".join(text), tool_calls=tools))])


def _chat_usage(usage: Any) -> Any:
    counts = extract_confirmed_token_usage(usage)
    if counts is None:
        raise _invalid()
    return SimpleNamespace(prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"],
        total_tokens=counts["total_tokens"], prompt_tokens_details=SimpleNamespace(cached_tokens=counts["cached_tokens"]))


def _read(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise _error(response.status_code)
    try:
        value = response.json()
        if not isinstance(value, dict) or value.get("type") != "message":
            raise _invalid()
        return value
    except (ValueError, KeyError, TypeError):
        raise _invalid() from None


class AnthropicMessagesClient:
    def __init__(self, *, api_key: str, base_url: str, timeout: int, hooks: dict):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, event_hooks=hooks,
            transport=httpx.AsyncHTTPTransport(retries=2, trust_env=False))
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs: Any):
        return self

    async def close(self):
        await self._client.aclose()

    async def create(self, **kwargs: Any):
        body, schema_name = messages_request(**kwargs)
        if kwargs.get("stream"):
            return self._stream(body, schema_name)
        try:
            response = await self._client.post(self.base_url + "/messages", json=body,
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            return _completion(_read(response), schema_name)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except httpx.RequestError:
            raise _error() from None
        except (ValueError, KeyError, TypeError):
            raise _invalid() from None

    async def _stream(self, body: dict, schema_name: str | None):
        usage, model, identifier = {}, body["model"], ""
        blocks = {}
        saw_output_usage = False
        try:
            async with self._client.stream("POST", self.base_url + "/messages", json=body,
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}) as response:
                if response.status_code != 200:
                    raise _error(response.status_code)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    kind = event.get("type")
                    delta = None
                    if kind == "error":
                        status = {"overloaded_error": 529, "rate_limit_error": 429,
                                  "authentication_error": 401}.get(event.get("error", {}).get("type"), 500)
                        raise _error(status)
                    if kind == "message_start":
                        message = event["message"]
                        usage.update(message.get("usage") or {})
                        identifier, model = message["id"], message["model"]
                    elif kind == "content_block_start":
                        block = event["content_block"]
                        blocks[event["index"]] = block
                        if block["type"] == "tool_use" and block["name"] != schema_name:
                            delta = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=event["index"],
                                id=block["id"], type="function", function=SimpleNamespace(name=block["name"], arguments=""))])
                        elif block["type"] == "text" and block.get("text"):
                            delta = SimpleNamespace(content=block["text"], tool_calls=None)
                    elif kind == "content_block_delta":
                        patch = event["delta"]
                        if patch["type"] == "text_delta":
                            delta = SimpleNamespace(content=patch["text"], tool_calls=None)
                        elif patch["type"] == "input_json_delta":
                            if blocks[event["index"]]["name"] == schema_name:
                                delta = SimpleNamespace(content=patch["partial_json"], tool_calls=None)
                            else:
                                delta = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=event["index"],
                                    function=SimpleNamespace(arguments=patch["partial_json"]))])
                    elif kind == "message_delta":
                        final_usage = event.get("usage") or {}
                        saw_output_usage = "output_tokens" in final_usage
                        usage.update(final_usage)
                    elif kind == "message_stop":
                        if not identifier or not saw_output_usage or extract_confirmed_token_usage(usage) is None:
                            raise _invalid()
                        yield SimpleNamespace(id=identifier, model=model, choices=[], usage=_chat_usage(usage))
                        return
                    if delta is not None:
                        yield SimpleNamespace(id=identifier, model=model, choices=[SimpleNamespace(index=0, delta=delta)], usage=None)
                raise _invalid()
        except httpx.RequestError:
            raise _error() from None
        except (ValueError, KeyError, TypeError):
            raise _invalid() from None


def anthropic_vision(body: dict, config) -> dict:
    from app.core.providers_local import validate_endpoint

    from llm_gateway.provider_foundry import normalize_base_url

    endpoint = validate_endpoint(normalize_base_url(config.provider, config.base_url)).rstrip("/") + "/messages"
    format_spec = body.get("text", {}).get("format", {})
    request, schema_name = messages_request(model=body["model"], messages=body["input"],
        max_tokens=body["max_output_tokens"], response_format={"type": "json_schema", "json_schema": format_spec}
        if format_spec.get("type") == "json_schema" else format_spec)
    try:
        with httpx.Client(timeout=httpx.Timeout(25, connect=5), follow_redirects=False, trust_env=False) as client:
            response = client.post(endpoint, json=request, headers={"x-api-key": config.api_key.get_secret_value(),
                                                                  "anthropic-version": "2023-06-01"})
            if len(response.content) > 100_000:
                raise _invalid()
            completion = _completion(_read(response), schema_name)
    except httpx.RequestError:
        raise _error() from None
    except (ValueError, KeyError, TypeError):
        raise _invalid() from None
    counts = extract_confirmed_token_usage(completion.usage)
    # Messages resolves aliases to dated IDs; receipts stay bound to the admitted model.
    return {"id": completion.id, "model": body["model"],
            "usage": {"input_tokens": counts["prompt_tokens"], "output_tokens": counts["completion_tokens"],
                      "total_tokens": counts["total_tokens"],
                      "input_tokens_details": {"cached_tokens": counts["cached_tokens"]}},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": completion.choices[0].message.content}]}]}


def missing_model_message(base_url, model):
    if urlsplit(str(base_url)).port == 11434:
        return f"Your model {model} is not installed. Run ollama pull {model} in your terminal, then test your connection in Settings > Keys and connections."
    return "Your chosen model is not installed. Load that model in your model server, or choose an installed model in Settings > Keys and connections, then test your connection again."


def _confirmed_usage(usage):
    counts = extract_confirmed_token_usage(usage)
    if counts is None:
        raise ProviderCallError("usage_unconfirmed", "Your model returned no usage counts. Choose another model in Settings > Keys and connections, then retry.")
    return counts


def local_model_error(status):
    if status in {401, 403}:
        return ProviderCallError("bad_api_key", "Your local model server rejected your key. Update your model API key in Settings > Keys and connections to match your server, then retry.", status=status)
    if status == 429:
        return ProviderCallError("rate_limited", "Your local model is busy. Wait for your current request to finish, then retry your task.", status=status, retryable=True)
    if status >= 500:
        return ProviderCallError("provider_down", "Restart your model server and check its service URL in Settings > Keys and connections, then retry.", status=status)
    return ProviderCallError("provider_error", "Your local model rejected this request. Check your model and service URL in Settings > Keys and connections, then retry your task.", status=status)


def loopback_message_size(messages: list) -> int:
    return len(json.dumps(messages, ensure_ascii=False).encode())


def loopback_input_budget(context_window: int, max_tokens: int) -> int:
    # shortcut: estimate three UTF-8 bytes per token with a framing reserve; use the server tokenizer when one is available.
    return 3 * (context_window - max_tokens - 128)


def _json_document(content: str) -> bool:
    if not content.lstrip().startswith(("{", "[", '"')):
        return False
    try:
        json.loads(content)
    except ValueError:
        return False
    except RecursionError:
        return True
    return True


class LoopbackJSONClient:
    def __init__(self, client, *, timeout=None, context_window=None):
        from llm_gateway.provider_foundry import DEFAULT_LOOPBACK_CONTEXT

        configured_timeout = 600 if client.timeout == 120 else client.timeout
        self._wrapped = client.with_options(timeout=timeout if timeout is not None else configured_timeout)
        self._context_window = context_window if context_window is not None else DEFAULT_LOOPBACK_CONTEXT
        self.chat = SimpleNamespace(completions=self)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def with_options(self, *, context_window=None, **kwargs):
        return LoopbackJSONClient(self._wrapped.with_options(**kwargs), timeout=kwargs.get("timeout", self._wrapped.timeout),
            context_window=self._context_window if context_window is None else context_window)

    def _fit_context(self, request):
        request = dict(request)
        limit = request.pop("max_completion_tokens", None) or request.get("max_tokens") or 4096
        request["max_tokens"] = min(limit, self._context_window // 2)
        messages = request["messages"] = [dict(message) for message in request["messages"]]

        tools_size = len(json.dumps(request.get("tools") or []).encode())

        def size(items):
            return loopback_message_size(items) + tools_size

        fixed = size([message for message in messages if message["role"] in {"system", "developer"}])
        request["max_tokens"] = min(request["max_tokens"], max(256, self._context_window - (fixed + 2) // 3 - 384))
        budget = loopback_input_budget(self._context_window, request["max_tokens"])
        if size(messages) <= budget:
            return request
        latest = next((index for index in range(len(messages) - 1, -1, -1) if messages[index]["role"] == "user"), None)
        if latest is not None:
            current = messages[latest]
            if not request.get("tools") and not any(message.get("tool_calls") or message["role"] == "tool" for message in messages):
                messages[:] = [message for index, message in enumerate(messages)
                               if index >= latest or message["role"] in {"system", "developer"}]
            content = current.get("content")
            prompt_size = size(messages)
            remaining = budget - prompt_size
            if remaining < 0 and isinstance(content, str) and not request.get("tools"):
                if _json_document(content):
                    output_room = self._context_window - (prompt_size + 2) // 3 - 128
                    if output_room >= 256:
                        request["max_tokens"] = output_room
                        return request
                else:
                    data = content.encode()
                    keep = max(0, (len(data) + remaining - 64) // 2)
                    if keep:
                        current["content"] = data[:keep].decode(errors="ignore") + "\n[Context shortened]\n" + data[-keep:].decode(errors="ignore")
        if size(messages) > budget:
            raise ProviderCallError("context_length_exceeded",
                "Your model cannot fit this request. Shorten your request or increase the context window in your model server and in Settings > Keys and connections > Your AI provider, then retry.", status=400)
        return request

    async def create(self, **kwargs):
        from openai import APIStatusError

        request = deepcopy(kwargs)
        if request.get("stream"):
            request.setdefault("stream_options", {"include_usage": True})
        schema = (request.get("response_format") or {}).get("json_schema", {}).get("schema")
        structured = (request.get("response_format") or {}).get("type") in {"json_schema", "json_object"}
        first_request = not request.get("stream")
        while True:
            unsent_eligible, first_request = first_request, False
            try:
                reply = await self._request(request, unsent_eligible=unsent_eligible)
                break
            except APIStatusError as exc:
                if exc.status_code not in {400, 422} or not request.pop("response_format", None):
                    raise
                contract = "Return only valid JSON, without markdown or explanation."
                if schema:
                    serialized = json.dumps(schema, ensure_ascii=False)
                    if not any(json.dumps(schema) in str(message.get("content")) or serialized in str(message.get("content"))
                               for message in request["messages"] if message["role"] == "system"):
                        contract += " Match this schema: " + serialized
                request["messages"].insert(0, {"role": "system", "content": contract})
            except ProviderCallError as exc:
                if (exc.code != "tools_unsupported" or request.get("tool_choice") != "auto"
                        or any(message.get("tool_calls") or message["role"] == "tool" for message in request["messages"])):
                    raise
                for field in ("tools", "tool_choice", "parallel_tool_calls"):
                    request.pop(field, None)
                request["messages"].insert(0, {"role": "system", "content": "Answer without tools. Do not claim to have taken any action or verified outside information. When needed, tell the reader what they can do next."})
        if request.get("stream"):
            return self._stream_reply(reply, request, schema if schema else {"type": "object"} if structured else None)
        _confirmed_usage(reply.usage)
        return await self._validated_reply(reply, request, schema or {"type": "object"}) if structured else reply

    async def _request(self, request, *, unsent_eligible=False):
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        try:
            reply = await self._wrapped.chat.completions.create(**self._fit_context(request))
            if not request.get("stream") and not request.get("tools"):
                for choice in reply.choices:
                    choice.message.tool_calls = None
            return reply
        except APIStatusError as exc:
            if exc.status_code in {401, 403, 429}:
                raise local_model_error(exc.status_code) from None
            error = str(exc.body).casefold()
            model = request["model"]
            if "context" in error and any(term in error for term in ("exceed", "length", "maximum", "too long")):
                limit = re.search(r"(?:context (?:length|window)(?: of)?|maximum(?: is)?|limit(?: is)?)\D{0,20}(\d[\d,]*)", error)
                size = f"{int(limit.group(1).replace(',', '')):,} tokens" if limit else "its context limit"
                raise ProviderCallError("context_length_exceeded",
                    f"Your model {model} cannot fit this request within {size}. Choose a larger-context model in Settings > Keys and connections, or shorten your request, then retry.",
                    status=exc.status_code) from None
            if exc.status_code == 404 and "model" in error:
                raise ProviderCallError("model_not_found", missing_model_message(self._wrapped.base_url, model), status=404) from None
            if exc.status_code in {400, 422} and "tool" in error and any(term in error for term in ("not support", "unsupported")):
                raise ProviderCallError("tools_unsupported",
                    "Choose a model that supports tools in Settings > Keys and connections, then retry your task.", status=exc.status_code) from None
            if (exc.status_code in {400, 422}
                    and (request.get("response_format") or {}).get("type") in {"json_schema", "json_object"}
                    and any(term in error for term in ("response_format", "json_schema"))
                    and any(term in error for term in ("not supported", "unsupported", "unknown field", "unknown parameter", "unrecognized", "extra_forbidden", "not implemented"))):
                raise
            raise local_model_error(exc.status_code) from None
        except APIConnectionError as exc:
            if unsent_eligible:
                from llm_gateway.sole import _connect_errors

                if isinstance(exc.__cause__, _connect_errors()):
                    raise
            if isinstance(exc, APITimeoutError):
                raise ProviderCallError("provider_timeout",
                    "Your local model took too long to answer. Choose a smaller model in Settings > Keys and connections, then retry your task.") from None
            raise ProviderCallError("provider_down",
                "You could not reach your local model. Start your model server, check its service URL in Settings > Keys and connections, then retry.") from None

    async def _validated_reply(self, reply, request, schema):
        from llm_gateway.messages import SchemaFailure, parse_json_schema_content

        if getattr(reply.choices[0].message, "tool_calls", None):
            return reply
        try:
            parse_json_schema_content(reply.choices[0].message.content, schema)
            return reply
        except SchemaFailure:
            previous = _confirmed_usage(reply.usage)
        repair = deepcopy(request)
        repair.pop("response_format", None)
        repair.pop("stream_options", None)
        repair["stream"] = False
        repair["messages"].extend([
            {"role": "assistant", "content": reply.choices[0].message.content or ""},
            {"role": "user", "content": "Correct your answer. Return only a JSON object matching this schema, without markdown or explanation: " + json.dumps(schema, ensure_ascii=False)},
        ])
        corrected = await self._request(repair)
        try:
            parse_json_schema_content(corrected.choices[0].message.content, schema)
        except SchemaFailure:
            raise ProviderCallError("invalid_structured_output",
                "Your model could not return the required JSON after one retry. Choose a model with structured-output support in Settings > Keys and connections, then retry your task.") from None
        counts = _confirmed_usage(corrected.usage)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            setattr(corrected.usage, field, counts[field] + previous[field])
        details = corrected.usage.prompt_tokens_details or deepcopy(reply.usage.prompt_tokens_details)
        if details is not None:
            details.cached_tokens = counts["cached_tokens"] + previous["cached_tokens"]
            corrected.usage.prompt_tokens_details = details
        return corrected

    async def _stream_reply(self, stream, request, schema):
        content, buffered, usage, last, has_tools = [], [], None, None, False
        try:
            async for chunk in stream:
                last = chunk
                if chunk.usage is not None:
                    usage = chunk.usage
                if not request.get("tools"):
                    for choice in chunk.choices:
                        choice.delta.tool_calls = None
                if schema is None:
                    yield chunk
                else:
                    buffered.append(chunk)
                    if chunk.choices:
                        content.append(chunk.choices[0].delta.content or "")
                        has_tools = has_tools or bool(chunk.choices[0].delta.tool_calls)
        except Exception as exc:  # noqa: BLE001
            if "timeout" in type(exc).__name__.casefold():
                raise ProviderCallError("provider_timeout",
                    "Your local model took too long to answer. Choose a smaller model in Settings > Keys and connections, then retry your task.") from None
            raise ProviderCallError("provider_down",
                "Your local model stopped answering. Restart your model server and check its service URL in Settings > Keys and connections, then retry.") from None
        finally:
            await stream.close()
        _confirmed_usage(usage)
        if schema is not None:
            if has_tools:
                for chunk in buffered:
                    yield chunk
                return
            reply = SimpleNamespace(id=last.id, model=last.model, usage=usage,
                choices=[SimpleNamespace(message=SimpleNamespace(content="".join(content)))])
            reply = await self._validated_reply(reply, request, schema)
            yield SimpleNamespace(id=reply.id, model=reply.model, usage=None,
                choices=[SimpleNamespace(index=0, delta=SimpleNamespace(role="assistant", content=reply.choices[0].message.content, tool_calls=None), finish_reason=None)])
            yield SimpleNamespace(id=reply.id, model=reply.model, choices=[], usage=reply.usage)
