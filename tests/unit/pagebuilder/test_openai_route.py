import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("workspace", ["wholesale", "gardening"])
@pytest.mark.hosted_only
def test_sol_reasoning_uses_catalog_aliases_without_deployment_file(tmp_path, monkeypatch, workspace):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pagebuilder.db")
    load_company_os_app()
    from app.llm import founder_provider
    from app.llm.founder_provider import ResolvedLLMConfig
    from app.pagebuilder import api

    platform = ResolvedLLMConfig(workspace_id=workspace, provider="azure_foundry", base_url="https://provider.example",
        model="DeepSeek-V4-Pro", api_key_ref=None, source="platform_default")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: platform)
    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setenv("PAGEBUILDER_OPENAI_API_KEY", "dummy-key")
    for model in ("gpt-6.1-sol", "supymem-gpt-6-1-sol"):
        monkeypatch.setenv("PAGEBUILDER_OPENAI_MODEL", model)
        route = api.resolve_route(workspace)
        assert route.model == model
        assert api.reasoning_policy(route.provider, route.model) == {
            "planning": "low", "reviewer": "low", "generation": "low"}


@pytest.mark.hosted_only
def test_landing_only_openai_route(monkeypatch):
    load_company_os_app()
    from app.pagebuilder import api
    from app.llm import founder_provider
    from app.llm.founder_provider import ResolvedLLMConfig
    platform = ResolvedLLMConfig(workspace_id="ws", provider="azure_foundry", base_url="https://x", model="DeepSeek-V4-Pro",
        api_key_ref=None, source="platform_default")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: platform)
    monkeypatch.delenv("PAGEBUILDER_OPENAI_MODEL", raising=False)
    assert api.resolve_route("ws") is platform and api.provider_config(platform) is None
    monkeypatch.setenv("PAGEBUILDER_OPENAI_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("PAGEBUILDER_OPENAI_API_KEY", "sk-test")
    route = api.resolve_route("ws")
    cfg = api.provider_config(route)
    assert (route.provider, route.model, cfg.provider, cfg.deployment_pro) == ("openai", "gpt-6.1-sol", "openai", "gpt-6.1-sol")
    assert api.reasoning_policy("openai", "gpt-6.1-sol") == {"planning": "low", "reviewer": "low", "generation": "low"}
    byok = ResolvedLLMConfig(workspace_id="ws", provider="openai", base_url=None, model="m", api_key_ref="ref", source="founder_override")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: byok)
    assert api.resolve_route("ws") is byok  # founder BYOK always wins


@pytest.mark.hosted_only
def test_landing_route_can_target_the_platform_azure_sol_deployment(monkeypatch):
    load_company_os_app()
    from app.credits.estimate import _lookup_model_row
    from app.llm import founder_provider
    from app.llm.founder_provider import ResolvedLLMConfig
    from app.pagebuilder import api
    platform = ResolvedLLMConfig(workspace_id="ws", provider="azure_foundry", base_url="https://x", model="DeepSeek-V4-Pro",
        api_key_ref=None, source="platform_default")
    monkeypatch.setattr(founder_provider, "resolve_llm_config", lambda wid: platform)
    monkeypatch.setenv("PAGEBUILDER_OPENAI_API_KEY", "dummy-key")
    monkeypatch.setenv("PAGEBUILDER_OPENAI_MODEL", "supymem-gpt-6-1-sol")
    monkeypatch.setenv("PAGEBUILDER_OPENAI_BASE_URL", "https://fixture-resource.openai.azure.com/openai/v1/")
    route = api.resolve_route("ws_any")
    assert (route.provider, route.model, route.base_url) == ("openai", "supymem-gpt-6-1-sol", "https://fixture-resource.openai.azure.com/openai/v1")
    assert api.provider_config(route).base_url == route.base_url
    assert api.reasoning_policy("openai", "supymem-gpt-6-1-sol") == {"planning": "low", "reviewer": "low", "generation": "low"}
    assert _lookup_model_row("supymem-gpt-6-1-sol") == _lookup_model_row("gpt-6.1-sol") is not None


@pytest.mark.parametrize("workspace", ["wholesale", "gardening"])
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "supymem-gpt-6-1-sol"])
def test_local_landing_route_uses_saved_planning_model_not_hosted_environment(tmp_path, monkeypatch, workspace, model):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAGEBUILDER_OPENAI_MODEL", "hosted-only-model")
    monkeypatch.setenv("PAGEBUILDER_OPENAI_API_KEY", "hosted-only-key")
    load_company_os_app()
    from app.core.providers_local import save_settings
    from app.llm.founder_provider import LocalResolvedLLMConfig, to_provider_config
    from app.pagebuilder import api

    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1",
        models={"chat": "local-chat", "planning": model}, api_key="")
    route = api.resolve_route(workspace)
    assert isinstance(route, LocalResolvedLLMConfig)
    assert (route.model, route.source) == (model, "founder_override")
    assert to_provider_config(workspace, resolved_config=route).deployment_pro == model
    assert api.provider_config(route) is None
    assert api.reasoning_policy(route.provider, route.model) == {
        "planning": None, "reviewer": None, "generation": None}
