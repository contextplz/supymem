"""Exercise the real gateway/SDK boundary; the network client alone is offline."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from tests.unit.simulation.test_rehearse import env as env, lease, start  # noqa: F401


@pytest.fixture
def sdk(monkeypatch):
    from llm_gateway import sole
    calls = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        async def create(self, **kwargs):
            calls.append(kwargs)
            panel = {"personas": [{"id": f"p{i}", "role": f"Hypothetical owner {i}",
                "situation": "A busy morning"} for i in range(1, 9)]}
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(panel)))],
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})

        async def close(self):
            pass

    monkeypatch.setattr(sole, "create_async_client_from_config", lambda config: (Client(), config))
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    return calls


@pytest.mark.hosted_only
@pytest.mark.parametrize("byok", [False, True], ids=["platform", "byok"])
def test_actual_metered_runtime_forwards_schema_and_disables_only_deepseek_thinking(env, monkeypatch, sdk, byok):
    from app.db.models import CreditReservation
    from app.llm import founder_provider
    from app.simulation import runtime
    from app.simulation.contracts import Panel
    from llm_gateway import sole
    from llm_gateway.provider_foundry import load_provider_config_from_env

    monkeypatch.setenv("LLM_PROVIDER", "azure_foundry")
    monkeypatch.setenv("LLM_DEPLOYMENT_PRO", "DeepSeek-V4-Pro")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.invalid")
    if byok:
        config = load_provider_config_from_env().model_copy(update={"provider": "deepseek_direct",
            "source": "founder_override"})
        resolved = founder_provider.ResolvedLLMConfig(workspace_id=env["wid"], provider=config.provider,
            base_url=config.base_url, model=config.deployment_pro, api_key_ref="offline-key-reference",
            source="founder_override")
        monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda _: resolved)
        monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: config)

    transmitted = []
    actual_transmit = sole.transmit

    async def capture(**kwargs):
        transmitted.append(kwargs)
        return await actual_transmit(**kwargs)

    monkeypatch.setattr(sole, "transmit", capture)
    queued, _ = start(env)
    payload = lease(env, queued["job_id"])
    runtime.mutate(queued["job_id"], payload,
        lambda row, doc, session: doc["_runtime"].update(deadline=time.time() + 150))
    answer = runtime.call(queued["job_id"], "panel", payload, {"audience": "Hypothetical bakery owners",
        "schema": Panel.model_json_schema()}, Panel,
        lambda result: runtime._exact([p["id"] for p in result["personas"]], [f"p{i}" for i in range(1, 9)]))

    assert len(answer["personas"]) == 8 and len(transmitted) == len(sdk) == 1
    request = transmitted[0]
    assert request["purpose"] == "simulation.rehearse" and request["response_schema"] == Panel.model_json_schema()
    assert request["single_attempt"] is True and request["use_workspace_override"] is False
    assert request["max_tokens"] == 4096 and not request.get("tools")
    assert sdk[0]["response_format"] == {"type": "json_object"}
    assert sdk[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    operation = runtime.document(queued["job_id"])["_runtime"]["operations"]["panel"]
    assert operation["state"] == "completed" and operation["usage"]["settled"] is True
    with env["factory"]() as session:
        holds = list(session.scalars(select(CreditReservation).where(CreditReservation.purpose == "simulation.rehearse")))
        assert len(holds) == (0 if byok else 1)
        assert all(hold.status == "settled" for hold in holds)


@pytest.mark.parametrize("byok", [False, True], ids=["platform", "byok"])
@pytest.mark.parametrize("excluded", ["other-purpose", "tools", "other-model", "other-provider", "no-schema"])
def test_actual_sdk_boundary_preserves_other_purposes_tools_models_and_defaults(monkeypatch, sdk, byok, excluded):
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    monkeypatch.setenv("LLM_API_KEY", "offline-fixture-key")
    config = ProviderConfig(provider="deepseek_direct", base_url="https://offline.invalid",
        api_key=SecretStr("offline-fixture-key"), deployment_pro="DeepSeek-V4-Pro",
        reasoning_effort="high", source="founder_override" if byok else "platform_default")
    purpose = "build.peer_review" if excluded == "other-purpose" else "simulation.rehearse"
    tools = [{"type": "function", "function": {"name": "offline_check", "parameters": {"type": "object"}}}] if excluded == "tools" else None
    schema = None if excluded == "no-schema" else {"type": "object", "properties": {"answer": {"type": "string"}}}
    if excluded == "other-model":
        config.deployment_pro = "gpt-6-sol"
    if excluded == "other-provider":
        config.provider = "openai_compatible"
    args = dict(tools=tools, tier="pro", max_tokens=4096, purpose=purpose, workspace_id="offline-workspace",
        response_schema=schema, single_attempt=True)
    messages = [{"role": "system", "content": "Offline boundary check"}, {"role": "user", "content": "{}"}]
    if byok:
        asyncio.run(sole.byok_chat_completion(messages, config=config, **args))
    else:
        asyncio.run(sole._foundry_chat(messages, captured_config=config, **args))
    assert len(sdk) == 1
    assert sdk[0].get("extra_body", {}).get("thinking") is None
    assert config.reasoning_effort == "high"
    token_key = "max_completion_tokens" if config.deployment_pro.startswith("gpt-6") else "max_tokens"
    assert sdk[0]["model"] == config.deployment_pro and sdk[0][token_key] == 4096
    if tools:
        assert sdk[0]["tools"] and sdk[0]["tool_choice"] == "auto"
