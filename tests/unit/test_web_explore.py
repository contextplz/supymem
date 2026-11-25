"""Hermes-shaped explore loop uses our search + fetch, not invented pages."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def explore_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/explore.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.research import explore
    from app.core import local_capabilities, local_secrets, providers_local
    sections = {}
    def saved_settings(section):
        return {"values": sections.setdefault(section, {}), "sources": {}}
    monkeypatch.setattr(local_secrets, "read_settings", saved_settings)
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: None)
    monkeypatch.setattr(local_capabilities, "read_settings", lambda section: local_secrets.read_settings(section))
    monkeypatch.setattr(local_capabilities, "get_secret", lambda section, name: local_secrets.get_secret(section, name))
    monkeypatch.setattr(providers_local, "read_settings", lambda section: local_secrets.read_settings(section))

    return explore


async def provider_search(provider, query, **kwargs):
    return await provider.search(query)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
async def test_explore_reads_pages(explore_mod, monkeypatch, query):
    from app.research import local_providers

    class Fake:
        name = "duckduckgo"  # Explicitly free offline fixture.

        async def search(self, query: str):
            return [
                {
                    "url": "https://example.com/forms",
                    "title": "Form tools",
                    "snippet": "A list of form builders.",
                }
            ]

    monkeypatch.setattr(explore_mod, "_provider_from_env", lambda: Fake())
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Fake())
    monkeypatch.setattr(local_providers, "search", provider_search)
    monkeypatch.setattr(explore_mod, "fetch_page_markdown", lambda url: "Typeform and Tally compete on simple forms.")
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: Fake())
    async def page(url, **kwargs):
        return "Typeform and Tally compete on simple forms."
    monkeypatch.setattr(local_providers, "fetch_page", page)
    result = await explore_mod.explore_web(query)
    assert result["mode"] == "live"
    assert result["hits"][0]["url"] == "https://example.com/forms"
    assert "Tally" in result["pages"][0]["excerpt"]
    body = explore_mod.render_explore(result)
    assert "What the pages actually say" in body


@pytest.mark.asyncio
async def test_explore_empty_query(explore_mod):
    result = await explore_mod.explore_web("  ")
    assert result["mode"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
async def test_rate_limit_is_closed_and_gives_manual_source_steps(explore_mod, monkeypatch, caplog, query):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.research import local_providers

    import httpx
    class Limited:
        name = "firecrawl"
        async def search(self, query):
            response = httpx.Response(429, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search?secret=private"))
            response.raise_for_status()
    monkeypatch.setattr(explore_mod, "_provider_from_env", lambda: Limited())
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Limited())
    # Synthetic transport admission only; real credit refusal is checked in
    # test_paid_search_paths. Preserve all founder recovery/redaction assertions.
    monkeypatch.setattr(explore_mod, "metered_search", provider_search)
    monkeypatch.setattr(local_providers, "search", provider_search)
    async def no_platforms(*_a, **_kw):
        return []
    monkeypatch.setattr(explore_mod, "_platform_lookups", no_platforms)
    result = await explore_mod.explore_web(query)
    assert result["mode"] == "error" and result["error_code"] == "search_rate_limited"
    assert "rate-limited" in result["summary"]
    body = explore_mod.render_explore(result)
    assert "Open Google" in body and "original website" in body and "nothing was sent" in body.lower()
    assert "Client error" not in str(result) and "api.firecrawl" not in str(result) and "private" not in str(result)
    assert "search_rate_limited" in caplog.text and "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
async def test_platform_and_page_errors_never_render_raw_exception(explore_mod, monkeypatch, query):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.research import local_providers

    class Fake:
        name = "duckduckgo"  # Explicitly free offline fixture.
        async def search(self, query):
            return [{"url": "https://example.com", "title": "Source"}]
    monkeypatch.setattr(explore_mod, "_provider_from_env", lambda: Fake())
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Fake())
    monkeypatch.setattr(local_providers, "search", provider_search)
    async def failed_platforms(*_a, **_kw):
        return [{"platform": "youtube", "hits": [], "error": "private api key and provider text"}]
    monkeypatch.setattr(explore_mod, "_platform_lookups", failed_platforms)
    def failed_page(url):
        raise RuntimeError("private api key and provider text")
    monkeypatch.setattr(explore_mod, "fetch_page_markdown", failed_page)
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: Fake())
    async def page(url, **kwargs):
        return failed_page(url)
    monkeypatch.setattr(local_providers, "fetch_page", page)
    result = await explore_mod.explore_web(query)
    assert result["pages"][0]["error"] == "page_read_unavailable"
    body = explore_mod.render_explore(result)
    assert "private" not in body and "unavailable" in body
    if explore_mod.is_local():
        assert "Settings" in body and "another public source" in body


@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
@pytest.mark.parametrize("configured", ["none", "exa", "tavily", "firecrawl", "openai"])
def test_local_provider_uses_only_configured_credentials(explore_mod, monkeypatch, query, configured):
    from app.research import deep, local_providers
    from app.core import local_secrets, providers_local
    from types import SimpleNamespace

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for name in ("FIRECRAWL_API_KEY", "FIRECRAWL_SELF_HOSTED_URL", "FIRECRAWL_SELF_HOSTED_TOKEN",
                 "EXA_API_KEY", "TAVILY_API_KEY", "WEB_SEARCH_OPENAI_URL", "WEB_SEARCH_OPENAI_KEY"):
        monkeypatch.delenv(name, raising=False)
    selected = "builtin" if configured == "none" else configured
    local_secrets.read_settings("search")["values"].update(provider=selected,
        **({"firecrawl_url": "http://127.0.0.1:9187"} if selected == "firecrawl" else {}))
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: "fixture-key" if name == "api_key" else None)
    config = SimpleNamespace(provider="openai", base_url="https://search.example/v1",
        api_key=SimpleNamespace(get_secret_value=lambda: "fixture-model-key"), deployment_flash="saved-model")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    monkeypatch.setattr(providers_local, "configured_providers", lambda: {"provider": "openai",
        "providers": {"openai": {"configured": True, "api_key_set": True}}})
    provider = local_providers.search_provider()
    assert getattr(provider, "name", None) == ("duckduckgo" if configured == "none" else configured)
    assert getattr(deep._web_provider(), "name", None) == getattr(provider, "name", None)
    assert getattr(deep._company_provider(provider), "name", None) == getattr(provider, "name", None)
    assert deep._scrape_provider(provider) is not None
    assert local_providers.search_provider(exclude={selected}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
async def test_local_explore_missing_search_has_actionable_gap(explore_mod, monkeypatch, query):
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: None)
    def forbidden(*args, **kwargs):
        pytest.fail("An unconfigured provider was dispatched")
    monkeypatch.setattr(explore_mod, "_provider_from_env", forbidden)
    monkeypatch.setattr(explore_mod, "_platform_lookups", forbidden)
    result = await explore_mod.explore_web(query)
    assert result["mode"] == "error" and result["error_code"] == "search_unavailable"
    assert "Keys and connections" in result["summary"] and "you" in result["summary"].lower()
    assert result["hits"] == result["pages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
async def test_local_explore_meters_user_search_without_platform_fallback(explore_mod, monkeypatch, query, url):
    from app.credits import metered
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    class Provider:
        name = "exa"
        async def search(self, received):
            assert received == query
            return [{"url": url, "title": query, "snippet": query}]
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Provider())
    async def admitted(callback, **kwargs):
        assert kwargs["payer"] == "founder" and kwargs["provider"] == "exa"
        return await callback()
    monkeypatch.setattr(metered, "call", admitted)
    def forbidden(*args, **kwargs):
        pytest.fail("A platform fallback was dispatched")
    monkeypatch.setattr(explore_mod, "_platform_lookups", forbidden)
    result = await explore_mod.explore_web(query, extract=0)
    assert result["mode"] == "live" and result["hits"][0]["url"] == url


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://warehouse.example/tools", "https://pottery.example/classes"])
async def test_local_firecrawl_empty_page_uses_builtin_not_hosted_or_model_fallback(explore_mod, monkeypatch, url):
    import httpx
    from app.research import local_providers
    from app.signals import research
    from app.signals import local_reader
    from app.core import local_secrets

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local_secrets.read_settings("search")["values"]["firecrawl_url"] = "http://127.0.0.1:9187"
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)
    monkeypatch.setattr(local_reader, "_basic_read", lambda target, main_only: local_reader._page_response(target, "Public page"))
    def forbidden(*args, **kwargs):
        pytest.fail("A hosted fallback or limiter was dispatched")
    monkeypatch.setattr(research.HttpResearchProvider, "_web_search", forbidden)
    monkeypatch.setattr(research, "_pace_firecrawl", forbidden)
    class Client:
        async def post(self, endpoint, **kwargs):
            assert endpoint == "http://127.0.0.1:9187/v2/scrape"
            return httpx.Response(200, request=httpx.Request("POST", endpoint),
                json={"success": True, "data": {"markdown": ""}})
    provider = local_providers.scrape_provider()
    response = await provider._firecrawl_post(Client(), "scrape", {"url": url})
    assert response.json()["data"]["markdown"] == "Public page"


@pytest.mark.parametrize("url", ["https://warehouse.example/tools", "https://pottery.example/classes"])
def test_local_page_fetch_blocked_reader_has_settings_next_step(explore_mod, monkeypatch, url):
    from app.research import local_providers, web_fetch

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: None)
    with pytest.raises(ValueError, match="Keys and connections"):
        web_fetch.fetch_page_markdown(url)


@pytest.mark.parametrize("workspace", ["warehouse", "pottery"])
def test_local_github_credentials_ignore_environment_and_keep_workspace_scope(explore_mod, monkeypatch, workspace):
    import uuid

    from app.research import github_search

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    token = uuid.uuid4().hex
    monkeypatch.setenv("RESEARCH_GITHUB_TOKEN", token)
    from sqlalchemy import select
    from app.db.models import Connection
    from app.state import db
    with db()() as session:
        assert session.scalar(select(Connection.id).where(Connection.workspace_id == workspace)) is None
    def forbidden(*args, **kwargs):
        pytest.fail("A credential was read without an eligible saved workspace connection")
    monkeypatch.setattr(github_search, "store", forbidden)
    assert github_search._credentials(workspace) == (None, "anonymous")
    monkeypatch.delenv("RESEARCH_GITHUB_TOKEN")
    assert github_search._credentials(workspace) == (None, "anonymous")


@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
@pytest.mark.parametrize("bridge", ["openworker_search", "platform_search", "openworker_read", "platform_read"])
def test_local_bridges_share_configured_search_and_reader(explore_mod, monkeypatch, query, url, bridge):
    from app.credits import metered
    from app.research import (
        agent_reach_bridge,
        local_providers,
        openworker_bridge,
        web_fetch,
    )

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    class Provider:
        name = "tavily"
        async def search(self, received):
            assert received == query
            return [{"url": url, "title": query}]
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Provider())
    async def admitted(callback, **kwargs):
        assert kwargs["payer"] == "founder"
        return await callback()
    monkeypatch.setattr(metered, "call", admitted)
    monkeypatch.setattr(web_fetch, "fetch_page_markdown", lambda received: query if received == url else "")
    async def read(received):
        from app.signals.research import ScrapedText
        assert received == url
        return ScrapedText(query, metadata={"sourceURL": url, "title": query, "fetched_at": "2026-11-14T00:00:00+00:00"}, usage={})
    monkeypatch.setattr(local_providers, "local_read", read)
    if bridge == "openworker_search":
        result = openworker_bridge.search_web(query)
        assert result[0]["url"] == url
    elif bridge == "platform_search":
        result = agent_reach_bridge.search_platform("exa", query)
        assert result["hits"][0]["url"] == url and result["error"] is None
    elif bridge == "openworker_read":
        assert openworker_bridge.fetch_url(url)["text"] == query
    else:
        assert agent_reach_bridge.read_url(url) == query


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
async def test_local_search_and_page_read_use_independent_keys(explore_mod, monkeypatch, query, url):
    from app.research import local_providers
    from app.core import local_secrets
    from app.signals import research

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local_secrets.read_settings("search")["values"].update(provider="exa", firecrawl_url="http://127.0.0.1:9187")
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: "fixture-key" if name == "api_key" else None)
    provider = local_providers.search_provider()
    assert provider.name == "exa" and local_providers.scrape_provider().name == "firecrawl"
    class Reader:
        async def scrape(self, received, *, main_only):
            assert received == url and main_only is False
            return query
    monkeypatch.setattr(research.HttpResearchProvider, "scrape", Reader.scrape)
    assert await provider.scrape(url, main_only=False) == query


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
@pytest.mark.parametrize("blocked", [False, True])
async def test_local_page_read_keeps_ssrf_validation_and_metering(explore_mod, monkeypatch, query, url, blocked):
    from app.credits import metered
    from app.research import local_providers
    from core import ssrf

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls = []
    reject = False
    class Reader:
        name = "firecrawl"
        async def scrape(self, received, *, main_only):
            calls.append("read")
            assert received == url and main_only is True
            return query
    monkeypatch.setattr(local_providers, "scrape_provider", lambda: Reader())
    def validate(received):
        assert received == url
        calls.append("validate")
        if reject:
            raise ValueError("Unsafe page")
    monkeypatch.setattr(ssrf, "assert_safe_remote_url", validate)
    async def admitted(callback, **kwargs):
        calls.append("meter")
        assert kwargs["payer"] == "founder" and kwargs["provider"] == "firecrawl"
        return await callback()
    monkeypatch.setattr(metered, "call", admitted)
    assert await local_providers.fetch_page(url) == query
    assert calls == ["validate", "meter", "read"]
    if blocked:
        calls.clear()
        reject = True
        with pytest.raises(ValueError, match="Choose a public HTTP or HTTPS URL"):
            await local_providers.fetch_page(url)
        assert calls == ["validate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["warehouse inventory tools", "weekend pottery classes"])
async def test_local_recon_keeps_unrelated_results_out(explore_mod, monkeypatch, query):
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    async def results(*args, **kwargs):
        return [{"url": "https://weather.example", "title": "Weather forecast", "snippet": "Rain and snow tomorrow."}]
    monkeypatch.setattr(local_providers, "local_search", results)
    result = await local_providers.research_recon(query)
    assert result["mode"] == "blocked" and result["sources"] == []
    assert "You" in result["summary"] and "Try" in result["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
@pytest.mark.parametrize("configured", ["exa", "tavily", "firecrawl", "openai"])
async def test_local_http_search_dispatches_only_user_credentials(explore_mod, monkeypatch, query, url, configured):
    import json
    import uuid

    import httpx
    from app.core import local_secrets, providers_local
    from app.research import local_providers
    from app.tools import reader
    from types import SimpleNamespace

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for name in ("EXA_API_KEY", "TAVILY_API_KEY", "FIRECRAWL_SELF_HOSTED_URL", "FIRECRAWL_SELF_HOSTED_TOKEN",
                 "WEB_SEARCH_OPENAI_URL", "WEB_SEARCH_OPENAI_KEY"):
        monkeypatch.delenv(name, raising=False)
    token = uuid.uuid4().hex
    local_secrets.read_settings("search")["values"].update(provider=configured,
        **({"firecrawl_url": "http://127.0.0.1:9187"} if configured == "firecrawl" else {}))
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: token if name in {"api_key", "firecrawl_key"} else None)
    config = SimpleNamespace(provider="openai", base_url="https://search.example/v1",
        api_key=SimpleNamespace(get_secret_value=lambda: token), deployment_flash="saved-model")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    monkeypatch.setattr(providers_local, "configured_providers", lambda: {"provider": "openai",
        "providers": {"openai": {"configured": True, "api_key_set": True}}})
    requests = []
    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        if configured == "exa":
            assert request.headers["x-api-key"] == token and body["query"] == query
        elif configured == "tavily":
            assert body["api_key"] == token and body["query"] == query
        elif configured == "firecrawl":
            assert request.headers["Authorization"] == "Bearer " + token and body["query"] == query
        else:
            assert request.headers["Authorization"] == "Bearer " + token and body["input"] == query
        row = {"url": url, "title": query, "snippet": query}
        response = ({"success": True, "data": [row]} if configured == "firecrawl" else
                    {"output": [{"type": "web_search_call", "action": {"sources": [row]}}]} if configured == "openai" else
                    {"results": [row]})
        return httpx.Response(200, json=response)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    provider = local_providers.search_provider()
    async def page(target, **kwargs):
        assert target == url
        return query
    monkeypatch.setattr(provider, "scrape", page)
    if configured == "openai":
        def forbidden(*args, **kwargs):
            pytest.fail("Search bypassed the protected page reader")
        monkeypatch.setattr(provider, "_firecrawl_post", forbidden)
    results = await provider.search(query, include_content=True)
    assert len(requests) == 1 and results[0]["url"] == url
    assert results[0]["provider"] == configured
    if configured == "openai":
        monkeypatch.setattr(reader, "enabled", lambda: True)
        assert (await provider.search(query))[0]["url"] == url
        assert len(requests) == 2
        from app.credits.gate import _call_allowance
        monkeypatch.setattr(reader, "enabled", lambda: False)
        allowance = _call_allowance.set(object())
        try:
            assert (await provider.search(query))[0]["url"] == url
            assert len(requests) == 3
        finally:
            _call_allowance.reset(allowance)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [("warehouse inventory tools", "https://warehouse.example/tools"),
                                       ("weekend pottery classes", "https://pottery.example/classes")])
async def test_local_deep_report_never_starts_vendor_provider_calls(explore_mod, monkeypatch, query, url):
    from app.research import gpt_researcher_bridge

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("DEEP_RESEARCH", "1")
    def forbidden():
        pytest.fail("The vendor provider cascade was started")
    monkeypatch.setattr(gpt_researcher_bridge, "_ensure_path", forbidden)
    async def found(received, **kwargs):
        assert received == query and kwargs["public_only"] is True
        return {"mode": "live", "summary": "Your retrieved sources.", "provider": "exa",
                "hits": [{"url": url, "title": query}], "pages": [], "platforms": []}
    monkeypatch.setattr(explore_mod, "explore_web", found)
    result = await gpt_researcher_bridge.run_gpt_researcher(query)
    assert result["ok"] is True and result["engine"] == "configured_search"
    assert result["source_urls"] == [url] and "research_costs" not in result


@pytest.mark.parametrize("endpoint", ["http://warehouse.example", "http://pottery.example"])
def test_local_reader_requires_https_outside_loopback(explore_mod, monkeypatch, endpoint):
    from app.research import local_providers
    from app.core import local_secrets

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local_secrets.read_settings("search")["values"]["firecrawl_url"] = endpoint
    assert local_providers._reader_endpoint() is None


@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
def test_shared_firecrawl_resolver_accepts_local_loopback_without_changing_hosted_defaults(explore_mod, monkeypatch, domain):
    from app.signals.research import _self_hosted_firecrawl
    from app.core import local_secrets

    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "http://127.0.0.1:3002")
    monkeypatch.delenv("FIRECRAWL_SELF_HOSTED_TOKEN", raising=False)
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    assert _self_hosted_firecrawl() is None
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local_secrets.read_settings("search")["values"]["firecrawl_url"] = "http://127.0.0.1:3002"
    assert _self_hosted_firecrawl() == ("http://127.0.0.1:3002", "")
    local_secrets.read_settings("search")["values"]["firecrawl_url"] = "http://" + domain
    assert _self_hosted_firecrawl() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query,domain", [("warehouse stock planning", "warehouse.example"),
                                         ("weekend pottery lessons", "pottery.example")])
async def test_local_relationship_search_uses_saved_provider_and_scoped_metering(explore_mod, monkeypatch, query, domain):
    from app.credits import metered
    from app.gtm.relationships import discover
    from app.research import local_providers
    from app.tenancy import bind, current_or_none

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    class Provider:
        name = "openai"
        async def search(self, received, **kwargs):
            assert received == query and kwargs["limit"] == 3
            return [{"url": f"https://{domain}/{index}", "title": query} for index in range(2)]
    monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Provider())
    calls = []
    async def admitted(callback, **kwargs):
        scope = current_or_none()
        assert scope.workspace_id == "workspace-a" and scope.venture_id == "venture-a"
        assert kwargs["workspace_id"] == scope.workspace_id and kwargs["venture_id"] == scope.venture_id
        assert kwargs["payer"] == "founder" and kwargs["provider"] == "openai"
        assert kwargs["purpose"] == "gtm.discovery.search"
        calls.append(kwargs)
        return await callback()
    monkeypatch.setattr(metered, "call", admitted)
    with bind(workspace_id="workspace-a", venture_id="venture-a", actor="owner-a"):
        with pytest.raises(PermissionError):
            discover._search_web(query, workspace_id="workspace-b", limit=3)
        assert discover._search_web(query, limit=3) == [{"url": f"https://{domain}/0", "title": query}]
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("query,domain", [("warehouse stock planning", "warehouse.example"),
                                         ("weekend pottery lessons", "pottery.example")])
@pytest.mark.parametrize("limit,batches", [(10, 1), (11, 2), (21, 3)])
async def test_local_search_reserves_only_the_requested_result_batches(explore_mod, monkeypatch, query, domain, limit, batches):
    from app.core import local_secrets
    from app.credits import metered
    from app.research import local_providers

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local_secrets.read_settings("search")["values"]["firecrawl_url"] = "https://api.firecrawl.dev"
    provider = local_providers.LocalHttpResearchProvider("firecrawl", "", limit=100)
    async def searched(received, **options):
        assert received == query and options["limit"] == limit
        return [{"url": f"https://{domain}/", "title": query}]
    monkeypatch.setattr(provider, "search", searched)
    async def admitted(callback, **kwargs):
        assert kwargs["estimate_micro"] == metered.price_micro("firecrawl_search", batches)
        assert kwargs["payer"] == "founder"
        return await callback()
    monkeypatch.setattr(metered, "call", admitted)
    assert await local_providers.search(provider, query, limit=limit) == [{"url": f"https://{domain}/", "title": query}]


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["warehouse.example", "pottery.example"])
@pytest.mark.parametrize("attempt", ["builtin", "firecrawl", "search"])
async def test_unbilled_reader_failure_does_not_hide_paid_uncertainty(explore_mod, monkeypatch, domain, attempt):
    from contextlib import asynccontextmanager
    from app.credits import metered as credits_metered
    from app.research import local_providers, metered as research_metered
    from app.signals import local_reader, research
    from app.tools import reader
    from core import ssrf

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_providers, "_research_enabled", lambda: True)
    monkeypatch.setattr(reader, "_target", lambda url: None)
    monkeypatch.setattr(ssrf, "assert_safe_remote_url", lambda url: None)
    endpoint = [("https://reader.example", "fixture-token") if attempt == "firecrawl" else None]
    search = [("tavily", "https://search.example", "fixture-token", "") if attempt == "search" else None]
    monkeypatch.setattr(local_reader, "_firecrawl", lambda: endpoint[0])
    monkeypatch.setattr(local_reader, "_search_provider", lambda: search[0])
    calls = []

    @asynccontextmanager
    async def slot():
        yield

    async def firecrawl(*args, **kwargs):
        calls.append("firecrawl")
        endpoint[0] = None
        raise TimeoutError("fixture transport timeout")

    async def searched(*args, **kwargs):
        calls.append("search")
        search[0] = None
        raise TimeoutError("fixture transport timeout")

    def unreadable(url, main_only):
        assert url == f"https://{domain}/"
        calls.append("builtin")
        raise reader.ReaderError("reader_format_unsupported")

    async def admitted(callback, **kwargs):
        return await callback()

    monkeypatch.setattr(research, "_firecrawl_slot", slot)
    monkeypatch.setattr(local_reader, "_post", firecrawl)
    monkeypatch.setattr(local_reader, "_search", searched)
    monkeypatch.setattr(local_reader, "_basic_read", unreadable)
    monkeypatch.setattr(credits_metered, "call", admitted)
    expected_error = TimeoutError if attempt == "firecrawl" else ValueError
    with pytest.raises(expected_error) as failure:
        await local_reader.request(object(), "scrape", {"url": f"https://{domain}/"})
    if attempt == "builtin":
        assert isinstance(failure.value, local_reader.UnbilledReadError)
        assert research_metered._failure(failure.value) == ("failed", "research_provider_rejected")
        assert calls == ["builtin"]
    else:
        assert not isinstance(failure.value, local_reader.UnbilledReadError)
        assert research_metered._failure(failure.value) == ("uncertain", "research_outcome_uncertain")
        assert calls == (["firecrawl"] if attempt == "firecrawl" else ["builtin", "search"])
