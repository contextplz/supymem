"""Message helpers — preserve reasoning_content on assistant turns (17 §4.1)."""

from __future__ import annotations

from typing import Any


def flatten_message_content(content: Any) -> Any:
    """Hermes/ACP often sends ``[{type:text,text:...}]``. Foundry wants a string."""
    if isinstance(content, str) or content is None:
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part:
                    parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return content


def normalize_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        msg = dict(raw)
        if "content" in msg:
            content = msg.get("content")
            if not (isinstance(content, list) and any(isinstance(part, dict)
                    and part.get("type") in {"image_url", "input_image"} for part in content)):
                msg["content"] = flatten_message_content(content)
        out.append(msg)
    return out


def sanitize_chat_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(fn.get("description") or name),
                    "parameters": params,
                },
            }
        )
    return out or None


def assistant_message_preserving_reasoning(resp: Any) -> dict[str, Any]:
    """Build an assistant message that round-trips ``reasoning_content``.

    Dropping this field causes intermittent 400s on Foundry/DeepSeek multi-turn
    tool loops.
    """
    choice = resp.choices[0].message
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": getattr(choice, "content", None),
    }
    tool_calls = getattr(choice, "tool_calls", None)
    if tool_calls:
        serialized = []
        for tc in tool_calls:
            serialized.append(
                {
                    "id": tc.id,
                    "type": getattr(tc, "type", "function") or "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
            )
        msg["tool_calls"] = serialized
    rc = getattr(choice, "reasoning_content", None)
    if rc is not None:
        msg["reasoning_content"] = rc
    output_items = getattr(choice, "responses_output_items", None)
    if output_items is not None:
        msg["responses_output_items"] = output_items
    return msg


def ensure_reasoning_roundtrip(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy messages, ensuring assistant turns keep reasoning_content if present."""
    return [dict(m) for m in messages]


def json_schema_response_format(schema: dict[str, Any], *, name: str = "result",
                                provider: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Choose the deployed model's supported chat output format.

    DeepSeek chat completions document JSON-object mode. Foundry's strict-schema
    path accepted our request but returned a minimal, semantically empty schema
    instance in live acceptance. Keep the schema in the workflow prompt and
    validate the response locally; format selection never authorizes its content.
    https://api-docs.deepseek.com/guides/json_mode/
    """
    normalized_model = (model or "").casefold().replace("_", "-")
    if schema.get("type") == "json_schema" and "json_schema" in schema:
        return schema
    # ponytail: unknown non-GPT Foundry deployments use JSON mode, enable strict schemas after model-specific verification.
    if (schema == {"type": "json_object"} or normalized_model.startswith("gpt-6.1-sol") or provider == "deepseek_direct"
            or (provider == "azure_foundry" and not normalized_model.startswith("gpt-"))):
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


class SchemaFailure(ValueError):
    """Model output did not match the requested json_schema."""


def parse_json_schema_content(content: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Parse assistant content as JSON and validate against schema."""
    import json

    from jsonschema import Draft202012Validator

    try:
        data = json.loads(content or "")
    except json.JSONDecodeError as exc:
        raise SchemaFailure(f"model output is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaFailure("model output JSON must be an object")
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise SchemaFailure(errors[0].message)
    return data


def apply_schema_or_escalate(
    content: str,
    schema: dict[str, Any],
    *,
    escalate: bool | None = None,
) -> dict[str, Any]:
    """Validate structured output. When escalate is true, SchemaFailure is the signal to retry on Pro."""
    from llm_gateway.routing import escalate_on_schema_failure as flag

    should = flag() if escalate is None else escalate
    try:
        return parse_json_schema_content(content, schema)
    except SchemaFailure:
        if should:
            raise
        raise
