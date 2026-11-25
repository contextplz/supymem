"""The Mac bridge cannot dial platform keys; cloud inference and explicit BYOK remain separate."""

from __future__ import annotations

from typing import Any

import pytest
from llm_gateway.hosted import HostedAuthRequired
from llm_gateway.provider_foundry import ProviderConfig
from llm_gateway.sole import env_chat_completion
from pydantic import SecretStr
from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.fixture(autouse=True)
def hosted_provider(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.mark.asyncio
async def test_packaged_default_path_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1")
    monkeypatch.delenv("HOSTED_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("HOSTED_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "should-not-be-used")

    called = {"env": False}

    def boom(*_a: Any, **_k: Any):
        called["env"] = True
        raise AssertionError("open_env_client must not run on packaged default path")

    monkeypatch.setattr("llm_gateway.sole.open_env_client", boom)
    with pytest.raises(HostedAuthRequired):
        await env_chat_completion([{"role": "user", "content": "hi"}])
    assert called["env"] is False


@pytest.mark.asyncio
async def test_legacy_plaintext_session_cannot_enable_packaged_platform_inference(monkeypatch):
    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1")
    monkeypatch.setenv("HOSTED_LLM_BASE_URL", "https://api.supymem.com/api/v1/llm")
    monkeypatch.setenv("HOSTED_ACCOUNT_TOKEN", "legacy-user-jwt")
    monkeypatch.setenv("LLM_API_KEY", "platform-key-must-remain-on-server")

    def boom(*args, **kwargs):
        pytest.fail("Packaged inference cannot open a provider client")

    monkeypatch.setattr("llm_gateway.sole.open_env_client", boom)
    with pytest.raises(HostedAuthRequired):
        await env_chat_completion([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("single_attempt", [False, True])
async def test_real_packaged_default_factory_refuses_before_sdk_construction(monkeypatch, single_attempt):
    from llm_gateway import provider_foundry, sole

    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-platform-key")
    monkeypatch.setattr(sole, "resolve_override", lambda *args, **kwargs: None)
    constructed = []
    def forbidden_sdk(*args, **kwargs):
        constructed.append(True)
        pytest.fail("Packaged default must not construct a provider SDK client")
    monkeypatch.setattr(provider_foundry, "AsyncOpenAI", forbidden_sdk)
    with pytest.raises(HostedAuthRequired):
        await sole.transmit([{"role": "user", "content": "hi"}],
                            purpose="env_chat_completion", single_attempt=single_attempt)
    assert not constructed


@pytest.mark.asyncio
async def test_byok_does_not_open_platform_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1")
    monkeypatch.setenv("HOSTED_LLM_BASE_URL", "https://api.supymem.com/api/v1/llm")
    monkeypatch.setenv("HOSTED_ACCOUNT_TOKEN", "user-jwt")
    monkeypatch.setenv("LLM_API_KEY", "company-azure-key")

    called = {"hosted": False, "env": False, "byok": False}

    async def fake_byok(*_a: Any, **_k: Any) -> dict[str, Any]:
        called["byok"] = True
        return {"via": "byok"}

    def boom(*_a: Any, **_k: Any):
        called["env"] = True
        raise AssertionError("BYOK must not dial platform Azure")

    monkeypatch.setattr("llm_gateway.sole.byok_chat_completion", fake_byok)
    monkeypatch.setattr("llm_gateway.sole.open_env_client", boom)

    cfg = ProviderConfig(
        api_key=SecretStr("user-own-key"),
        base_url="https://api.openai.com/v1",
        provider="openai",
        source="founder_override",
    )
    out = await env_chat_completion(
        [{"role": "user", "content": "hi"}],
        config=cfg,
    )
    assert out["via"] == "byok"
    assert called["byok"] is True
    assert called["hosted"] is False
    assert called["env"] is False


@pytest.mark.asyncio
async def test_hosted_client_never_sends_llm_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTED_LLM_BASE_URL", "https://api.supymem.com/api/v1/llm")
    monkeypatch.setenv("HOSTED_ACCOUNT_TOKEN", "user-jwt")
    monkeypatch.setenv("LLM_API_KEY", "company-azure-key")

    captured: dict[str, Any] = {}

    class FakeResp:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"model": "company-os-default"}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> FakeResp:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    monkeypatch.setattr("llm_gateway.hosted.httpx.AsyncClient", FakeClient)
    from llm_gateway.hosted import hosted_chat_completion

    await hosted_chat_completion([{"role": "user", "content": "hi"}], purpose="test")
    dumped = str(captured)
    assert "company-azure-key" not in dumped
    assert captured["headers"]["Authorization"] == "Bearer user-jwt"
    assert captured["json"]["model"] == "company-os-default"
    assert "LLM_API_KEY" not in captured["headers"]
    assert "api_key" not in captured["json"]
