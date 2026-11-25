"""Synthetic, offline regression checks for BYOK credentials and ECS boot guards."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from core import ssrf
from core.env_contract import EnvContractError, Problem, collect_violations, cookies_secure, ensure_boot_env, parse_app_env
from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def byok_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/company_os.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-platform-key")
    load_company_os_app()
    from app.llm import founder_provider
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    workspace_id = ensure_desk(LOCAL_PERSON_ID).id
    founder_provider.set_founder_config(
        workspace_id=workspace_id, provider="openai", model="synthetic-model",
        base_url="https://byok.example/v1", api_key="synthetic-founder-key",
    )
    return workspace_id


@pytest.fixture()
def public_dns(monkeypatch):
    lookup = Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lookup)
    return lookup


@pytest.mark.parametrize("url", [
    "http://localhost/v1", "http://127.0.0.1/v1", "http://10.0.0.1/v1",
    "http://169.254.169.254/v1", "http://169.254.170.2/v1",
    "http://[::1]/v1", "http://[fe80::1]/v1", "http://[::ffff:169.254.170.2]/v1",
])
def test_hosted_byok_blocks_private_destinations_before_vault_write(byok_workspace, monkeypatch, url):
    from app.llm import founder_provider

    monkeypatch.setenv("APP_ENV", "production")
    vault = Mock()
    monkeypatch.setattr(founder_provider, "secret_store", lambda: vault)
    with pytest.raises(founder_provider.FounderLLMConfigError):
        founder_provider.set_founder_config(
            workspace_id=byok_workspace, provider="openai", model="synthetic-model",
            base_url=url, api_key="synthetic-new-key",
        )
    vault.put.assert_not_called()


def test_hosted_byok_rejects_a_host_with_any_private_dns_answer(byok_workspace, monkeypatch, public_dns):
    from app.llm import founder_provider

    monkeypatch.setenv("APP_ENV", "production")
    public_dns.return_value.append(
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.170.2", 443))
    )
    with pytest.raises(founder_provider.FounderLLMConfigError):
        founder_provider.set_founder_config(
            workspace_id=byok_workspace, provider="openai", model="synthetic-model",
            base_url="https://byok.example/v1", api_key="synthetic-new-key",
        )


@pytest.mark.parametrize("change", [
    {"provider": "anthropic"}, {"base_url": "https://other-provider.example/v1"},
])
def test_saved_key_cannot_be_redirected_without_reentry(byok_workspace, monkeypatch, public_dns, change):
    from app.llm import founder_provider

    monkeypatch.setenv("APP_ENV", "production")
    before = founder_provider.get_founder_config(byok_workspace)
    with pytest.raises(founder_provider.FounderLLMConfigError, match="Re-enter"):
        founder_provider.set_founder_config(**{
            "workspace_id": byok_workspace, "provider": "openai", "model": "other-model",
            "base_url": "https://byok.example/v1", "api_key": None, **change,
        })
    assert founder_provider.get_founder_config(byok_workspace) == before
    assert founder_provider.to_provider_config(byok_workspace).api_key.get_secret_value() == "synthetic-founder-key"


def test_model_change_preserves_key_but_endpoint_change_uses_new_key(byok_workspace, monkeypatch, public_dns):
    from app.llm import founder_provider

    monkeypatch.setenv("APP_ENV", "production")
    before = founder_provider.get_founder_config(byok_workspace)
    updated = founder_provider.set_founder_config(
        workspace_id=byok_workspace, provider="openai", model="other-model",
        base_url="https://byok.example/v1/", api_key=None,
    )
    assert updated.api_key_ref == before.api_key_ref
    assert updated.model == "other-model"
    founder_provider.set_founder_config(
        workspace_id=byok_workspace, provider="anthropic", model="other-model",
        base_url="https://other-provider.example/v1", api_key="synthetic-reentered-key",
    )
    assert founder_provider.to_provider_config(byok_workspace).api_key.get_secret_value() == "synthetic-reentered-key"


def test_dns_is_rechecked_before_a_saved_key_is_retrieved(byok_workspace, monkeypatch, public_dns):
    from app.llm import founder_provider

    monkeypatch.setenv("APP_ENV", "production")
    resolved = founder_provider.resolve_llm_config(byok_workspace)
    assert founder_provider.to_provider_config(byok_workspace, resolved_config=resolved) is not None
    public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.170.2", 443))]
    vault = Mock()
    monkeypatch.setattr(founder_provider, "secret_store", lambda: vault)
    with pytest.raises(founder_provider.FounderLLMConfigError):
        founder_provider.to_provider_config(byok_workspace, resolved_config=resolved)
    vault.get.assert_not_called()


def test_client_factory_blocks_an_unsafe_pinned_byok_config(monkeypatch):
    from llm_gateway import provider_foundry

    monkeypatch.setenv("APP_ENV", "production")
    client_factory = Mock()
    monkeypatch.setattr(provider_foundry, "AsyncOpenAI", client_factory)
    config = provider_foundry.ProviderConfig(
        provider="openai", base_url="http://169.254.170.2/v1",
        api_key=SecretStr("synthetic-founder-key"), source="founder_override",
    )
    with pytest.raises(RuntimeError, match="public address"):
        provider_foundry.create_async_client_from_config(config)
    client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_byok_client_does_not_follow_provider_redirects(monkeypatch, public_dns):
    from llm_gateway import provider_foundry
    from openai import APIStatusError
    from openai import _base_client as sdk_base

    # Match the real SDK transport (httpx locally, httpx2 in fresh CI).
    sdk_http = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
    monkeypatch.setenv("APP_ENV", "production")
    requested = []

    def redirect(request):
        requested.append((str(request.url), request.headers["host"], request.extensions["sni_hostname"]))
        return sdk_http.Response(307, headers={"Location": "http://169.254.170.2/credentials"})

    real_http_client = provider_foundry.DefaultAsyncHttpxClient
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
        real_http_client(**kwargs, transport=sdk_http.MockTransport(redirect)))
    config = provider_foundry.ProviderConfig(
        provider="openai", base_url="https://byok.example/v1",
        api_key=SecretStr("synthetic-founder-key"), source="founder_override",
    )
    client, _ = provider_foundry.create_async_client_from_config(config)
    try:
        assert client.timeout == config.timeout_seconds
        with pytest.raises(APIStatusError):
            await client.chat.completions.create(model="synthetic-model", messages=[])
    finally:
        await client.close()
    assert requested == [("https://8.8.8.8/v1/chat/completions", "byok.example", "byok.example")]


@pytest.mark.asyncio
async def test_byok_pins_every_attempt_after_dns_validation(monkeypatch, public_dns):
    from llm_gateway import provider_foundry
    from openai import APIConnectionError
    from openai import _base_client as sdk_base

    sdk_http = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
    monkeypatch.setenv("APP_ENV", "production")
    requested = []

    def respond(request):
        requested.append(request)
        public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        return sdk_http.Response(200, json={"id": "synthetic", "choices": []})

    real_http_client = provider_foundry.DefaultAsyncHttpxClient
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", lambda **kwargs:
        real_http_client(**kwargs, transport=sdk_http.MockTransport(respond)))
    config = provider_foundry.ProviderConfig(provider="openai", base_url="https://byok.example/v1",
        api_key=SecretStr("synthetic-founder-key"), source="founder_override")
    client, _ = provider_foundry.create_async_client_from_config(config)
    try:
        await client.chat.completions.create(model="synthetic-model", messages=[])
        with pytest.raises(APIConnectionError) as caught:
            await client.with_options(max_retries=0).chat.completions.create(model="synthetic-model", messages=[])
        assert isinstance(caught.value.__cause__, PermissionError)
    finally:
        await client.close()
    assert len(requested) == 1
    assert str(requested[0].url) == "https://8.8.8.8/v1/chat/completions"
    assert requested[0].headers["host"] == "byok.example"
    assert requested[0].extensions["sni_hostname"] == "byok.example"


def test_local_ollama_still_works_without_a_key(byok_workspace):
    from app.llm import founder_provider

    founder_provider.clear_founder_config(byok_workspace)
    founder_provider.set_founder_config(
        workspace_id=byok_workspace, provider="local", model="synthetic-local-model",
        base_url=None, api_key=None,
    )
    config = founder_provider.to_provider_config(byok_workspace)
    assert config.base_url == "http://localhost:11434/v1"
    assert config.api_key.get_secret_value() == "ollama"


@pytest.mark.parametrize("url", [
    "https://user:password@byok.example/v1", "https://byok.example/v1?api_key=synthetic",
    "https://byok.example/v1#fragment", "https://byok.example:bad/v1",
])
def test_byok_does_not_store_credentials_or_invalid_ports_in_url(byok_workspace, url):
    from app.llm import founder_provider

    with pytest.raises(founder_provider.FounderLLMConfigError):
        founder_provider.set_founder_config(
            workspace_id=byok_workspace, provider="openai", model="synthetic-model",
            base_url=url, api_key="synthetic-new-key",
        )


@pytest.mark.parametrize("outcome", ["reply", "raw_error", "classified_error", "unknown_code"])
def test_connection_route_never_echoes_provider_content(byok_workspace, monkeypatch, caplog, outcome):
    from app.api import llm_settings_api
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError

    sensitive = "synthetic-provider-sensitive-body"

    async def completion(*args, **kwargs):
        if outcome == "raw_error":
            raise RuntimeError(sensitive)
        if outcome == "classified_error":
            raise ProviderCallError("provider_error", sensitive, status=502)
        if outcome == "unknown_code":
            raise ProviderCallError(sensitive, sensitive)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=sensitive))])

    monkeypatch.setattr(sole, "byok_chat_completion", completion)
    app = FastAPI()
    app.include_router(llm_settings_api.router)
    response = TestClient(app).post(f"/workspaces/{byok_workspace}/llm-settings/test")
    assert sensitive not in response.text
    if outcome == "reply":
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    else:
        assert response.status_code == 502
        assert response.json()["detail"]["error"]["code"] == ("connection_test_failed" if outcome == "raw_error" else "provider_error")
        if outcome == "raw_error":
            error = response.json()["detail"]["error"]
            assert "on our side" in error["message"] and "retry" in error["hint"].lower()
            assert not any(word in error["message"].lower() for word in ("model", "credits", "provider", "key", "—"))
            assert sensitive not in caplog.text
            assert any(getattr(record, "action", "") == "llm.connection_test" and
                       record.error_type == "RuntimeError" for record in caplog.records)


def test_connection_route_refuses_unavailable_byok_instead_of_platform_ping(byok_workspace, monkeypatch):
    from app.api import llm_settings_api
    from app.llm import founder_provider
    from llm_gateway import sole

    monkeypatch.setattr(founder_provider, "to_provider_config", lambda _: None)
    platform = Mock(side_effect=AssertionError("platform must not be called"))
    monkeypatch.setattr(sole, "env_chat_completion", platform)
    app = FastAPI()
    app.include_router(llm_settings_api.router)
    response = TestClient(app).post(f"/workspaces/{byok_workspace}/llm-settings/test")
    assert response.status_code == 502
    platform.assert_not_called()


@pytest.mark.parametrize("metadata_var", ["ECS_CONTAINER_METADATA_URI", "ECS_CONTAINER_METADATA_URI_V4"])
@pytest.mark.parametrize("app_env", [None, "", "  "])
def test_ecs_missing_app_env_fails_closed(metadata_var, app_env):
    env = {metadata_var: "http://169.254.170.2/synthetic-task"}
    if app_env is not None:
        env["APP_ENV"] = app_env
    for service in ("core", "gateway", "warden"):
        assert any(v.name == "APP_ENV" and v.problem == Problem.MISSING for v in collect_violations(env, service=service))
        with pytest.raises(EnvContractError) as caught:
            ensure_boot_env(env, service=service)
        assert any(v.name == "APP_ENV" and v.problem == Problem.MISSING for v in caught.value.violations)
    with pytest.raises(EnvContractError):
        parse_app_env(env)
    assert cookies_secure(env) is True
    assert "169.254" not in str(caught.value)


def test_non_ecs_local_default_is_preserved():
    assert parse_app_env({}) == "local"
    assert cookies_secure({}) is False
