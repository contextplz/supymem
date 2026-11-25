"""OpenAI Responses grammar and envelope checks, without provider/network I/O."""
import pytest
from coding_agents import cu_ai_loop as loop


def assert_strict_objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            assert set(schema["required"]) == set(schema["properties"])
        for value in schema.values():
            assert_strict_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            assert_strict_objects(value)


@pytest.mark.parametrize("kind", ["step", "plan", "verify"])
def test_requests_use_strict_responses_schema_and_preserve_vision_and_privacy(kind):
    body = (loop.build_request_body("unchanged-model", "user task", "png", 2500) if kind == "step"
            else loop.build_text_request_body("unchanged-model", "system", "user task", kind, 700, "png"))
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert body["model"] == "unchanged-model" and body["store"] is False
    assert_strict_objects(fmt["schema"])
    assert body["input"][1]["content"][1]["image_url"] == "data:image/png;base64,png"
    if kind == "step":
        assert fmt["schema"]["properties"]["actions"]["items"]["properties"]["op"]["enum"] == list(loop.OPS)


def test_text_only_requests_do_not_add_an_image_or_accept_unknown_contracts():
    body = loop.build_text_request_body("model", "system", "user", "plan", 600)
    assert body["input"][1]["content"] == [{"type": "input_text", "text": "user"}]
    with pytest.raises(ValueError, match="openai_text_request_kind_invalid"):
        loop.build_text_request_body("model", "system", "user", "arbitrary", 600)


@pytest.mark.parametrize("response,code", [
    ({"status": "incomplete", "output": [{"content": [{"type": "output_text", "text": '{"passed":true}'}]}],
      "incomplete_details": {"reason": "PRIVATE provider detail"}}, "openai_response_incomplete"),
    ({"status": "completed", "output": [{"content": [{"type": "output_text", "text": '{"passed":true}'},
      {"type": "refusal", "refusal": "PRIVATE refusal"}]}]}, "openai_response_refused"),
    ({"status": "failed", "error": {"message": "PRIVATE error"}, "output": []}, "openai_response_not_completed"),
    ({"output": []}, "openai_response_not_completed"),
    ({"status": "completed", "output": []}, "openai_response_text_missing"),
    ({"status": "completed", "output": [{"content": None}]}, "openai_response_invalid"),
])
def test_incomplete_refused_or_failed_envelopes_never_supply_proposals_or_verdicts(response, code):
    with pytest.raises(loop.ActionError) as error:
        loop.response_text(response)
    assert str(error.value) == code and "PRIVATE" not in str(error.value)


def test_complete_output_keeps_parser_and_semantic_validation_authoritative():
    raw = '{"passed":false,"reason":"not saved"}'
    assert loop.response_text({"status": "completed", "output": [{"type": "reasoning"},
        {"content": [{"type": "output_text", "text": raw}]}]}) == raw
    assert loop.parse_verdict(raw) == (False, "not saved")
    with pytest.raises(loop.ActionError):
        loop.parse_reply({"actions": [{"op": "key", "chord": "illegal"}]})
