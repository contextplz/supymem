import pytest

from tests.company_os_harness import ROOT, load_company_os_app


@pytest.mark.parametrize("domain", ["dispatch planning", "family gardening"], ids=["b2b", "consumer"])
@pytest.mark.parametrize("feature", ["search", "reader"])
def test_local_research_builtin_defaults_need_no_search_key_or_firecrawl(tmp_path, monkeypatch, domain, feature):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DEPLOYMENT_CONFIG", str(ROOT / "config/deployment.example.json"))
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / domain))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/research.db")
    for name in ("EXA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_SELF_HOSTED_URL", "FIRECRAWL_SELF_HOSTED_TOKEN",
                 "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    load_company_os_app()
    from app.core.local_secrets import update_settings
    from app.research import local_providers

    update_settings("search", {"provider": "builtin"}, {})
    provider = local_providers.search_provider() if feature == "search" else local_providers.scrape_provider()
    assert provider is not None
