"""Prove workspace BYOK overrides the platform env provider for live dials."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.fixture(autouse=True)
def hosted_live_provider(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("LLM_OFFLINE", "0")


@pytest.mark.asyncio
async def test_env_chat_completion_uses_byok_when_resolver_set(monkeypatch):
    from llm_gateway.provider_foundry import ProviderConfig
    from llm_gateway import sole

    byok_cfg = ProviderConfig(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key=SecretStr("sk-founder-override"),
        deployment_pro="luna",
        deployment_flash="luna",
        source="founder_override",
    )
    sole.set_workspace_llm_resolver(lambda wid: byok_cfg if wid == "wsp_test" else None)

    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    called = {}

    async def fake_byok(messages, *, config, tools=None, tier="pro", max_tokens=None, workspace_id=None, **kwargs):
        called["api_key"] = config.api_key.get_secret_value()
        called["model"] = config.deployment_pro
        called["provider"] = config.provider
        return fake_resp

    monkeypatch.setattr(sole, "byok_chat_completion", fake_byok)
    # Platform path must not run
    monkeypatch.setattr(
        sole,
        "open_env_client",
        MagicMock(side_effect=AssertionError("must not use platform env")),
    )

    with sole.workspace_llm_context("wsp_test"):
        resp = await sole.env_chat_completion(
            [{"role": "user", "content": "hi"}],
            tier="flash",
            max_tokens=8,
        )

    assert resp is fake_resp
    assert called["api_key"] == "sk-founder-override"
    assert called["provider"] == "openai"
    assert called["model"] == "luna"
    sole.set_workspace_llm_resolver(None)


@pytest.mark.asyncio
async def test_env_chat_completion_falls_back_to_platform(monkeypatch):
    from llm_gateway import sole
    from llm_gateway.provider_foundry import ProviderConfig

    sole.set_workspace_llm_resolver(lambda _wid: None)
    fake_client = MagicMock()
    fake_client.with_options.return_value = fake_client
    fake_client.close = AsyncMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plat"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    fake_cfg = ProviderConfig(provider="deepseek_direct", base_url="https://api.deepseek.com",
        api_key=SecretStr("platform-key"), deployment_pro="DeepSeek-V4-Pro",
        deployment_flash="DeepSeek-V4-Pro", max_output_tokens=64, reasoning_effort="")

    monkeypatch.setenv("LLM_API_KEY", "platform-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek_direct")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setattr(sole, "open_env_client", lambda config=None: (fake_client, fake_cfg))
    monkeypatch.setattr(sole, "deployment_for", lambda cfg, tier: "DeepSeek-V4-Pro")

    with sole.workspace_llm_context("wsp_none"):
        resp = await sole.env_chat_completion(
            [{"role": "user", "content": "hi"}],
            max_tokens=8,
        )

    assert resp.choices[0].message.content == "plat"
    fake_client.chat.completions.create.assert_awaited()
    sole.set_workspace_llm_resolver(None)


def test_classify_provider_errors_map_for_onboarding():
    from llm_gateway.errors import ProviderCallError
    from tests.company_os_harness import load_company_os_app
    import os
    import tempfile

    td = tempfile.mkdtemp()
    os.environ["COMPANY_OS_DB_URL"] = f"sqlite:///{td}/t.db"
    load_company_os_app()
    from app.onboarding.failures import classify

    assert classify(ProviderCallError("bad_api_key", "bad")).name == "provider_auth"
    assert classify(ProviderCallError("rate_limited", "slow", retryable=True)).name == (
        "provider_rate_limit"
    )
    assert classify(ProviderCallError("out_of_credits", "broke")).name == "provider_credits"
