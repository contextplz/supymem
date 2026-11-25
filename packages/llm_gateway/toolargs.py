"""Tool-argument validation — additionalProperties: false (17 §4.2)."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError


def enforce_additional_properties_false(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of schema with additionalProperties: false on objects."""
    out = dict(schema)
    if out.get("type") == "object" or "properties" in out:
        out.setdefault("additionalProperties", False)
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            k: enforce_additional_properties_false(v) if isinstance(v, dict) else v
            for k, v in props.items()
        }
    return out


def validate_tool_call(
    call: Any, schema: dict[str, Any]
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Validate tool call arguments. Never dispatch unvalidated args.

    Returns (ok, error_message, parsed_args).
    """
    if hasattr(call, "function"):
        raw_args = call.function.arguments
    elif isinstance(call, dict):
        raw_args = (call.get("function") or {}).get("arguments", "{}")
    else:
        raw_args = getattr(call, "arguments", "{}")

    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return False, f"arguments were not valid JSON: {e}", None

    strict = enforce_additional_properties_false(schema)
    try:
        Draft202012Validator(strict).validate(args)
    except JsonSchemaValidationError as e:
        return False, f"argument validation failed: {e.message}", None
    return True, None, args


def openai_tools(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise harness/catalog entries to OpenAI tools format."""
    out: list[dict[str, Any]] = []
    for item in catalog:
        if item.get("type") == "function" and "function" in item:
            out.append(item)
            continue
        name = str(item.get("name") or "")
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": item.get("description") or name,
                    "parameters": item.get("parameters")
                    or {
                        "type": "object",
                        "properties": item.get("properties") or {},
                        "required": item.get("required") or [],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return out


def corrective_tool_result(tool_call_id: str, error: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps({"ok": False, "error": error}),
    }
