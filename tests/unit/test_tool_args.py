"""Tool args: malformed JSON, missing required, hallucinated extras all rejected."""

from __future__ import annotations

from types import SimpleNamespace

from llm_gateway.toolargs import corrective_tool_result, validate_tool_call

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def _call(arguments: str, call_id: str = "c1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name="read_file", arguments=arguments),
    )


def test_malformed_json_rejected() -> None:
    ok, err, args = validate_tool_call(_call("{not-json"), SCHEMA)
    assert not ok
    assert args is None
    assert err and "not valid JSON" in err


def test_missing_required_rejected() -> None:
    ok, err, args = validate_tool_call(_call('{"limit": 1}'), SCHEMA)
    assert not ok
    assert args is None
    assert err and "validation failed" in err


def test_hallucinated_extra_params_rejected() -> None:
    ok, err, args = validate_tool_call(
        _call('{"path": "a.py", "url": "http://evil"}'), SCHEMA
    )
    assert not ok
    assert args is None
    assert err and "validation failed" in err


def test_valid_args_accepted() -> None:
    ok, err, args = validate_tool_call(_call('{"path": "a.py", "limit": 2}'), SCHEMA)
    assert ok
    assert err is None
    assert args == {"path": "a.py", "limit": 2}


def test_corrective_message_for_model() -> None:
    msg = corrective_tool_result("c1", "argument validation failed: extra")
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "c1"
    assert "error" in msg["content"]
