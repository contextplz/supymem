from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def local_app(monkeypatch, tmp_path, company_os_modules):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    for name in ("REDIS_URL", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    from app.core.providers_local import save_settings

    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
                  models={"chat": "local-model"})


@pytest.fixture(scope="module")
def company_os_modules(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("SUPYMEM_MODE", raising=False)
        patch.delenv("COS_TEST_APP_REUSE", raising=False)
        patch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path_factory.mktemp("warden-local") / "working.sqlite"))
        for name in ("REDIS_URL", "COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
            patch.delenv(name, raising=False)
        load_company_os_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
async def test_local_tool_policy_uses_owner_capabilities_without_http(local_app, monkeypatch, domain):
    from app.core.warden_client import WardenClient
    from app.runtime.toolbus import ToolBus

    settings = {"send_email": False, "publish_pages": False, "connected_apps": False}
    capabilities = ModuleType("app.core.local_capabilities")
    capabilities.enabled = lambda name: settings[name]
    capabilities.blocked_message = lambda name: f"Open Keys and connections and configure {name}, then retry."
    monkeypatch.setitem(sys.modules, capabilities.__name__, capabilities)
    network = Mock(side_effect=AssertionError("Local policy must not create an HTTP client"))
    monkeypatch.setattr(httpx, "AsyncClient", network)
    client = WardenClient(emit=lambda *_args: None)
    bus = ToolBus(warden=client)
    handler = AsyncMock(return_value={"domain": domain})
    for tool in ("draft_email", "read", "read_page", "list", "get", "search", "explore_web"):
        bus.register(tool, handler)
        assert (await bus.call(tool, {"domain": domain}))["ok"]
    for tool, required in (("send_email", "send_email"), ("message_customer", "send_email"),
                           ("publish_pages", "publish_pages"), ("deploy", "publish_pages"),
                           ("connection:write", "connected_apps"), ("post_public", "connected_apps")):
        handler.reset_mock()
        bus.register(tool, handler)
        denied = await bus.call(tool, {"domain": domain})
        assert not denied["ok"] and "Keys and connections" in denied["reason"]
        handler.assert_not_called()
        settings[required] = True
        assert (await bus.call(tool, {"domain": domain}))["ok"]
        settings[required] = False
        assert not (await bus.call(tool, {"domain": domain}))["ok"]
    assert (await client.check(capability="db:destructive", target=domain)).decision == "deny"
    assert (await client.check(capability="tool:unrecognized_action", target=domain)).decision == "deny"
    assert await client.kill_switch() is False
    network.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
async def test_local_policy_fallback_reads_saved_settings(local_app, monkeypatch, domain):
    from app.core import local_secrets
    from app.core.warden_client import WardenClient

    monkeypatch.setitem(sys.modules, "app.core.local_capabilities", None)
    monkeypatch.setattr(httpx, "AsyncClient", Mock(side_effect=AssertionError("No local policy HTTP client")))
    values = {"smtp": {"host": domain, "user": f"owner@{domain}", "postal_address": "12 Studio Road"},
              "publishing": {"provider": "vercel", "project": domain},
              "composio": {"auth_configs": {"calendar": domain}}}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": values[section]})
    secrets = {}
    secret = Mock(side_effect=lambda section, name: secrets.get((section, name)))
    monkeypatch.setattr(local_secrets, "get_secret", secret)
    monkeypatch.setenv("SUPYMEM_SMTP_PASSWORD", "unused-environment-value")
    client = WardenClient(emit=lambda *_args: None)
    assert (await client.check(capability="message_customer")).decision == "deny"
    secrets["smtp", "password"] = "saved-test-value"
    assert (await client.check(capability="message_customer")).decision == "allow"
    secret.assert_called_with("smtp", "password")
    for provider in ("vercel", "netlify", "s3"):
        values["publishing"] = {"provider": provider, "project": domain, "bucket": domain, "region": "us-west-2"}
        secrets.clear()
        assert (await client.check(capability="deploy")).decision == "deny"
        secrets.update({("publishing", name): "saved-test-value" for name in ("token", "access_key_id", "secret_access_key")})
        assert (await client.check(capability="deploy")).decision == "allow"
    assert (await client.check(capability="connection:write")).decision == "deny"
    secrets["composio", "api_key"] = "saved-test-value"
    assert (await client.check(capability="connection:write")).decision == "allow"


@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
def test_local_sse_never_opens_redis(local_app, monkeypatch, domain):
    from app.core.sse_hub import SSEHub

    redis = ModuleType("redis")
    redis.Redis = SimpleNamespace(from_url=Mock(side_effect=AssertionError("No local Redis dial")))
    monkeypatch.setitem(sys.modules, "redis", redis)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    hub = SSEHub()
    event = hub.publish("draft.ready", {"domain": domain}, venture_id=domain)
    assert hub.replay(None, {"draft.ready"}, domain) == [event]
    redis.Redis.from_url.assert_not_called()


@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
@pytest.mark.parametrize("name,scheme", [("REDIS_URL", "rediss"), ("DATABASE_URL", "postgresql+psycopg"),
                                        ("COMPANY_OS_DB_URL", "postgres"), ("DB_URL", "postgresql"),
                                        ("RDS_PROXY_ENDPOINT", None)])
def test_local_state_rejects_remote_configuration_without_disclosing_it(local_app, monkeypatch, caplog, domain, name, scheme):
    from app.core.sse_hub import SSEHub
    from desktop_runtime.hosted import current_mode, validate_local_state

    value = f"{scheme}://private-user:private-password@{domain}:5432/private-db" if scheme else domain
    monkeypatch.setenv(name, value)
    for boot in (validate_local_state, current_mode, SSEHub):
        caplog.clear()
        with pytest.raises(RuntimeError) as failure:
            boot()
        message = str(failure.value)
        assert name in message and "restart" in message.lower()
        assert domain not in message and "private-password" not in message
        assert domain not in caplog.text and "private-password" not in caplog.text
        assert sum(record.levelname == "ERROR" for record in caplog.records) == 1
    monkeypatch.setenv(name, "localhost" if scheme is None else f"{scheme}://[::1]:5432/local")
    validate_local_state()
    if scheme and scheme.startswith("postgres"):
        for url in (f"{scheme}://localhost/local?host={domain}", f"{scheme}:///local", f"{scheme}://localhost/local?host="):
            monkeypatch.setenv(name, url)
            with pytest.raises(RuntimeError):
                validate_local_state()
        monkeypatch.setenv(name, f"{scheme}:///local?host=/tmp")
        validate_local_state()
    monkeypatch.setenv(name, value)
    monkeypatch.delenv("SUPYMEM_MODE")
    validate_local_state()


@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
@pytest.mark.parametrize("app_env", ["local", "staging", "production"])
def test_desktop_and_core_agree_on_explicit_local_mode(local_app, monkeypatch, domain, app_env):
    from app.core.local_mode import is_local
    from desktop_runtime.hosted import current_mode

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("COMPANY_OS_HOSTED", "true")
    monkeypatch.setenv("SUPYMEM_MODE", " LOCAL ")
    assert is_local() and current_mode() == "local"
    monkeypatch.setitem(sys.modules, "app.core.local_mode", None)
    assert current_mode() == "local"


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
async def test_local_stream_failure_cannot_fall_back_to_bedrock(local_app, monkeypatch, domain):
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import ProviderConfig

    cfg = ProviderConfig(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1",
                         api_key=SecretStr(""), deployment_pro="local-model", deployment_flash="local-model",
                         source="founder_override")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "bedrock")
    monkeypatch.setattr(sole, "load_provider_config_from_env", lambda: cfg)
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    response = httpx.Response(503, request=httpx.Request("POST", "http://127.0.0.1:1234/v1"))
    create = AsyncMock(side_effect=sole.APIStatusError("Unavailable", response=response, body=None))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock())
    client.with_options = Mock(return_value=client)
    monkeypatch.setattr(sole, "create_async_client_from_config", lambda config: (client, config))
    fallback = AsyncMock(side_effect=AssertionError("Local models must not reach Bedrock"))
    bedrock = sole._bedrock_fallback
    monkeypatch.setattr(sole, "_bedrock_fallback", fallback)
    with pytest.raises(ProviderCallError):
        async for _chunk in sole.transmit_stream([{"role": "user", "content": domain}], purpose="env_chat_completion"):
            pass
    fallback.assert_not_called()
    client.with_options.assert_called_once_with(max_retries=0)
    create.assert_awaited_once()
    client.close.assert_awaited_once()
    assert sole.fallback_needed() is False
    with pytest.raises(ProviderCallError, match="Keys and connections"):
        await bedrock([{"role": "user", "content": domain}], tier="pro", max_tokens=1,
                      purpose="env_chat_completion", workspace_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
async def test_local_stream_refuses_captured_platform_provider(local_app, monkeypatch, domain):
    from llm_gateway import sole
    from llm_gateway.boundary import Decision
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.provider_foundry import ProviderConfig

    cfg = ProviderConfig(provider="openai_compatible", base_url="https://platform.example/v1",
                         api_key=SecretStr("synthetic-platform-value"), deployment_pro="platform-model",
                         deployment_flash="platform-model", source="platform_default",
                         routing_purpose="env_chat_completion", captured_tier="pro", api_mode="chat_completions")
    monkeypatch.setattr(sole, "boundary_decision", lambda messages, **kwargs: Decision(messages=messages))
    network = Mock(side_effect=AssertionError("No platform provider client in local mode"))
    monkeypatch.setattr(sole, "create_async_client_from_config", network)
    with pytest.raises(ProviderCallError, match="Keys and connections"):
        async for _chunk in sole.transmit_stream([{"role": "user", "content": domain}], purpose="env_chat_completion", config=cfg):
            pass
    network.assert_not_called()
    with pytest.raises(ProviderCallError, match="Keys and connections"):
        await sole.transmit([{"role": "user", "content": domain}], purpose="env_chat_completion", config=cfg, single_attempt=True)
    with pytest.raises(ProviderCallError, match="Keys and connections"):
        sole.require_env_api_key(cfg)
    network.assert_not_called()
