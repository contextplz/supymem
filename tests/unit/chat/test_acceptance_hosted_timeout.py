"""Hosted retry is separate from the unchanged BYOK no-replay contract."""

import asyncio
import json
import os

import pytest

from tests.unit.chat.test_assistant_safe_retries import response, transport_boundary

pytestmark = pytest.mark.hosted_only


@pytest.mark.parametrize("idea", ["Invoice reminders for design studios", "Reading challenges for families"])
@pytest.mark.parametrize("failure_kind", ["timeout", "provider_outcome_unknown"])
def test_hosted_timeout_retries_once_on_fast_fallback_without_founder_charge(transport_boundary, monkeypatch, idea, failure_kind):
    from app.credits import metered
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.sole import deployment_for

    boundary = transport_boundary
    monkeypatch.setenv("MODEL_ROUTING_OVERRIDES", json.dumps({
        boundary.assistant.ASSISTANT_PURPOSE: {"model": "supymem-gpt-6-1-sol"}}))
    monkeypatch.setenv("LLM_DEPLOYMENT_FLASH", "DeepSeek-V4-Flash")
    monkeypatch.setattr(boundary.assistant, "_captured_model_call", lambda config, messages, **kwargs:
        ({}, "captured-digest", 1000, kwargs["max_output_tokens"]))
    monkeypatch.setattr(metered, "begin_model_call", lambda meter: meter.protect_outcome())
    monkeypatch.setattr(metered, "record_model_failure", lambda *args: None)
    failures = [TimeoutError("A request took too long") if failure_kind == "timeout" else
                ProviderCallError("provider_outcome_unknown", "The result was not observed") for _ in range(4)]
    boundary.install([failures[0], response("You can keep going.")])
    messages = [{"role": "user", "content": f"What should I try next for {idea}?"}]
    result = asyncio.run(boundary.assistant._complete(messages, workspace_id="workspace", request_id="captured-retry"))
    assert result == "You can keep going."
    assert len(boundary.calls) == 2 and len(boundary.scopes) == 2
    models = [(call["config"].provider, deployment_for(call["config"], call["config"].captured_tier))
              for call in boundary.calls]
    assert models[0] != models[1], "Retry must use a different fallback model, not only a different config object"
    assert boundary.calls[1]["config"].captured_tier == "flash"
    assert models[1][1] == os.environ["LLM_DEPLOYMENT_FLASH"]
    assert all(meter.payer != "founder" for meter in boundary.scopes)
    assert not boundary.scopes[0].recorded and boundary.scopes[1].recorded

    boundary.calls.clear()
    boundary.scopes.clear()
    boundary.install(failures[1:3])
    with pytest.raises(ProviderCallError):
        asyncio.run(boundary.assistant._complete(messages, workspace_id="workspace", request_id="captured-retry"))
    assert len(boundary.calls) == 2, "A second timeout must not create an unbounded retry loop"

    boundary.calls.clear()
    boundary.scopes.clear()
    boundary.config.source = "founder_override"
    boundary.install([failures[3]])
    with pytest.raises(ProviderCallError):
        asyncio.run(boundary.assistant._complete(messages, workspace_id="workspace", request_id="captured-retry"))
    assert len(boundary.calls) == 1, "An ambiguous BYOK call must not be replayed"
