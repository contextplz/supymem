"""Effort escalation is bounded by the captured deployment, not the model."""
import json

import pytest
from openai import AsyncOpenAI, _base_client

from llm_gateway import sole
from llm_gateway.boundary import Decision
from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.routing import next_reasoning_effort, resolve_purpose_config, supported_reasoning_efforts

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2


@pytest.mark.parametrize("domain", ["industrial valve procurement", "family meal planning"])
def test_effort_escalates_one_supported_level_and_tool_loops_always_stay_low(domain):
    config = ProviderConfig(provider="azure_foundry", deployment_pro="gpt-6.1-sol",
        deployment_flash="gpt-6.1-sol", source="founder_override")
    captured = resolve_purpose_config("venture.chat.assistant", config, tier="flash")
    current = "low"
    assert supported_reasoning_efforts(captured) == ("low", "medium", "high", "xhigh", "max")
    for expected in ("medium", "high", "xhigh", "max"):
        current = next_reasoning_effort(captured, current, "max", phase="planning")
        assert current == expected, domain
        assert next_reasoning_effort(captured, current, "max", phase="tools") == "low"
    with pytest.raises(ValueError):
        next_reasoning_effort(captured, "low", "ultra", phase="synthesis")
    legacy = resolve_purpose_config("venture.chat.assistant", ProviderConfig(source="founder_override"), tier="flash")
    assert supported_reasoning_efforts(legacy) == ()
    assert next_reasoning_effort(legacy, "low", "max", phase="planning") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["industrial valve procurement", "family meal planning"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_captured_effort_survives_actual_offline_sol_dispatch(monkeypatch, domain, effort):
    monkeypatch.setenv("LLM_API_KEY", "offline")
    seen = []
    config = ProviderConfig(provider="azure_foundry", base_url="https://offline.invalid/openai/v1", api_key="offline",
        deployment_pro="gpt-6.1-sol", deployment_flash="gpt-6.1-sol", source="founder_override")
    captured = resolve_purpose_config("venture.chat.assistant", config, tier="flash")
    captured.reasoning_effort = effort
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_reasoning", "object": "response", "model": "gpt-6.1-sol",
            "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": domain}]}], "usage": {"input_tokens": 100, "output_tokens": 30,
                "total_tokens": 130, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 20}}})
    def factory(config):
        client = AsyncOpenAI(api_key="offline", base_url="https://offline.invalid/openai/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), max_retries=2)
        return client, config
    monkeypatch.setattr(sole, "create_async_client_from_config", factory)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    monkeypatch.setattr(sole, "time_context", lambda: "Trusted offline clock")
    response = await sole.transmit([{"role": "user", "content": "Analyze " + domain}], purpose="venture.chat.assistant",
        config=captured, tier="flash", single_attempt=True, use_workspace_override=False, max_tokens=4096)
    assert len(seen) == 1 and seen[0]["reasoning"] == {"effort": effort}
    assert response.choices[0].message.content == domain
    assert captured.reasoning_effort == effort
