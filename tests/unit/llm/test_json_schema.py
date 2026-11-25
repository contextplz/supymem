"""json_schema mode + escalate_on_schema_failure."""

from __future__ import annotations

import pytest

from llm_gateway.messages import (
    SchemaFailure,
    apply_schema_or_escalate,
    json_schema_response_format,
    parse_json_schema_content,
)

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}


def test_response_format_payload() -> None:
    fmt = json_schema_response_format(SCHEMA, name="classify")
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "classify"
    assert fmt["json_schema"]["strict"] is True


def test_parse_valid() -> None:
    assert parse_json_schema_content('{"label":"ok"}', SCHEMA) == {"label": "ok"}


@pytest.mark.parametrize("provider,model", [
    ("azure_foundry", "DeepSeek-V4-Pro"),
    ("azure_foundry", "deepseek-v4-flash"),
    ("deepseek_direct", "deepseek-v4-pro"),
])
def test_deepseek_chat_uses_json_mode_and_still_rejects_invalid_schema(provider, model):
    assert json_schema_response_format(SCHEMA, provider=provider, model=model) == {"type": "json_object"}
    with pytest.raises(SchemaFailure):
        parse_json_schema_content('{"wrong_field":"valid JSON, invalid contract"}', SCHEMA)


def test_foundry_non_gpt_deployments_use_json_mode_and_explicit_formats_are_respected():
    assert json_schema_response_format(SCHEMA, provider="azure_foundry", model="other-deployment") == {"type": "json_object"}
    assert json_schema_response_format(SCHEMA, provider="azure_foundry", model="gpt-4o")["type"] == "json_schema"
    assert json_schema_response_format({"type": "json_object"}) == {"type": "json_object"}
    explicit = {"type": "json_schema", "json_schema": {"name": "reply", "strict": True, "schema": SCHEMA}}
    assert json_schema_response_format(explicit, provider="azure_foundry", model="other-deployment") == explicit


@pytest.mark.parametrize("model", ["DeepSeek-V4-Pro", "other-deployment"])
def test_foundry_json_mode_carries_anyof_schema_contract_without_mutating_messages(model):
    import json
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    schema = {"type": "object", "properties": {"parts": {"type": "array", "items": {"anyOf": [SCHEMA]}}}, "required": ["parts"]}
    messages = [{"role": "system", "content": "Stable instructions"}, {"role": "user", "content": "Explain the process"}]
    config = ProviderConfig(provider="azure_foundry", deployment_flash=model, deployment_pro=model)
    kwargs = sole._completion_kwargs(messages, config=config, model=model, purpose="venture.chat.assistant", response_schema=schema)
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][1] == {"role": "system", "content": "Return only a JSON object matching this schema: " + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))}
    assert len(messages) == 2 and messages[0]["content"] == "Stable instructions"


def test_escalate_on_invalid() -> None:
    with pytest.raises(SchemaFailure):
        apply_schema_or_escalate("not-json", SCHEMA, escalate=True)
