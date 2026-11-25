"""A metered platform build must reach its captured Foundry client exactly once."""
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tests.company_os_harness import load_company_os_app
from llm_gateway import sole, provider_foundry
from llm_gateway.boundary import Decision, register_filter


@pytest.fixture(autouse=True)
def current_app_modules(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    # Other tests reload the app namespace; patch the modules used at call time.
    load_company_os_app()
    global model_proxy, founder_provider
    from app.builder import model_proxy
    from app.llm import founder_provider


@pytest.mark.parametrize("configured_timeout", [30, 120, 325, 600])
@pytest.mark.parametrize("change_endpoint", [False, True])
def test_build_final_dial_keeps_platform_route(monkeypatch, change_endpoint, configured_timeout):
    cfg = provider_foundry.ProviderConfig(provider="azure_foundry", base_url="https://foundry.example.test/openai/v1",
        api_key=SecretStr("fixture-platform-key"), deployment_pro="DeepSeek-V4-Pro", deployment_flash="DeepSeek-V4-Pro",
        source="platform_default", timeout_seconds=configured_timeout)
    resolved = SimpleNamespace(source="platform_default", provider=cfg.provider, model=cfg.deployment_pro, base_url=cfg.base_url)
    calls = []
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_API_KEY", "fixture-platform-key")
    monkeypatch.setenv("SUPYMEM_PLATFORM_SERVER", "1")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: resolved)
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: cfg)
    monkeypatch.setattr(sole, "_primary_chat", None)
    monkeypatch.setattr(sole, "_usage_recorder", None)
    monkeypatch.setattr(sole, "_workspace_resolver", lambda wid: pytest.fail("Build must not resolve a later founder override"))
    monkeypatch.setattr(sole, "open_env_client", lambda _: pytest.fail("Build must not reload a later environment route"))

    class Client:
        async def close(self):
            pass
        def __init__(self, config):
            self.config = config
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        async def create(self, **kwargs):
            assert self.config.timeout_seconds == 325
            calls.append((self.config.base_url, kwargs["model"], kwargs["parallel_tool_calls"]))
            assert self.config.api_key.get_secret_value() == "fixture-platform-key"
            return SimpleNamespace(choices=[], usage=None)

    monkeypatch.setattr(sole, "create_async_client_from_config", lambda config: (Client(config), config))
    # Change ambient settings after model_proxy captured its route, inside the
    # real gateway boundary, before its concrete client is constructed.
    def boundary(messages, **kwargs):
        if change_endpoint:
            cfg.base_url = "https://changed.example.test/openai/v1"
            cfg.api_key = SecretStr("changed-key")
        segments = kwargs["segments"]
        assert segments[0].origin == "first_party"
        assert segments[1].origin == "reviewed_build_tool"
        assert str(segments[1].declared_class) == "workspace_code"
        return Decision(messages=list(messages))
    register_filter(boundary)
    try:
        model_proxy._provider_call("workspace-fixture", {"messages": [
            {"role": "user", "content": "Build"},
            {"role": "tool", "content": "\n".join(f"file-{index}.html\t{index}" for index in range(30)),
             "tool_call_id": "tool-1"},
        ],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}], "max_tokens": 20})
    finally:
        register_filter(None)
    assert calls == [("https://foundry.example.test/openai/v1", "DeepSeek-V4-Pro", False)]


def test_build_rejects_changed_configuration_before_dial(monkeypatch):
    cfg = provider_foundry.ProviderConfig(provider="azure_foundry", base_url="https://changed.example.test/openai/v1",
        api_key=SecretStr("fixture"), deployment_pro="DeepSeek-V4-Pro", deployment_flash="DeepSeek-V4-Pro", source="platform_default")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(
        source="platform_default", provider="azure_foundry", model="DeepSeek-V4-Pro", base_url="https://approved.example.test/openai/v1"))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: cfg)
    monkeypatch.setattr(sole, "transmit", lambda *a, **kw: pytest.fail("Changed route cannot dial"))
    with pytest.raises(model_proxy.BuildModelError, match="configuration_changed"):
        model_proxy._provider_call("workspace-fixture", {})


def test_build_transport_accepts_the_priced_product_output_ceiling():
    request = {
        "messages": [{"role": "user", "content": "Build the reviewed product"}],
        "max_tokens": 16384,
    }
    assert model_proxy.bounded_request(request)["max_tokens"] == 16384
    with pytest.raises(model_proxy.BuildModelError, match="build_model_output_limit"):
        model_proxy.bounded_request({**request, "max_tokens": 16385})


def test_build_transport_accepts_full_application_context_but_keeps_a_hard_cap():
    request = {"messages": [{"role": "user", "content": "x" * 140_000}]}
    assert model_proxy.bounded_request(request)["messages"] == request["messages"]
    with pytest.raises(model_proxy.BuildModelError, match="build_model_input_limit"):
        model_proxy.bounded_request({"messages": [{"role": "user", "content": "x" * 270_000}]})


def test_build_provider_deadline_cancels_without_redial(monkeypatch):
    import asyncio
    cfg = provider_foundry.ProviderConfig(provider="azure_foundry",
        base_url="https://foundry.example.test/openai/v1", api_key=SecretStr("fixture"),
        deployment_pro="DeepSeek-V4-Pro", source="platform_default")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: SimpleNamespace(
        source=cfg.source, provider=cfg.provider, model=cfg.deployment_pro, base_url=cfg.base_url))
    monkeypatch.setattr(provider_foundry, "load_provider_config_from_env", lambda: cfg)
    monkeypatch.setattr(model_proxy, "PROVIDER_DEADLINE_SECONDS", 0.02)
    calls, cancelled = [], []
    async def pending(*args, **kwargs):
        calls.append(kwargs)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(sole, "transmit", pending)
    with pytest.raises(TimeoutError):
        model_proxy._provider_call("workspace-fixture", {"messages": [], "tools": [], "max_tokens": 20})
    assert len(calls) == 1 and calls[0]["single_attempt"] is True
    assert cancelled == [True]


@pytest.mark.parametrize("model", ["inventory-routing", "trail-guides"])
def test_local_saved_route_survives_an_older_settings_store(tmp_path, monkeypatch, model):
    from app.core import local_secrets, providers_local

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delattr(local_secrets, "_model_snapshot", raising=False)
    providers_local.save_settings(provider="openai_compatible", base_url="https://saved.example/v1",
        api_key="synthetic-owner-key", models={"chat": model})
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://different.example/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "synthetic-ambient-key")
    config = providers_local.load_local_config()
    assert config.base_url == "https://saved.example/v1"
    assert config.api_key.get_secret_value() == "synthetic-owner-key"
    assert config._local_role_models["chat"] == model
