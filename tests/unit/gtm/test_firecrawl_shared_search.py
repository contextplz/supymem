"""The platform-paid search path does not waste GTM slots on one company."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "company_os"))
sys.path.insert(0, str(Path(__file__).parents[3] / "packages"))

from app.gtm.relationships import discover  # noqa: E402


import pytest

@pytest.mark.parametrize("website", ["https://logistics.example", "https://camping.example"])
def test_self_hosted_only_enrichment_uses_shared_provider(monkeypatch, website):
    import httpx
    from app.credits import metered
    from app.gtm import enrich
    from app.signals import research
    from app.core.local_mode import is_local
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("search", {"firecrawl_url": "https://crawl.example"}, {"firecrawl_key": "self-key"})
        monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "self-key")
    monkeypatch.setenv("GTM_ENRICH_ENABLED", "1")
    monkeypatch.setattr(research, "_pace_firecrawl", lambda _: (_ for _ in ()).throw(AssertionError("self-hosted pacing")))
    monkeypatch.setattr(metered, "call", lambda callback, **kwargs: callback())
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "# Real company", "metadata": {"url": website}}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    async def site(provider, website, wid, vid, **kwargs):
        assert await provider.scrape(website) == "# Real company"
        return {"quality_rejected": True}
    monkeypatch.setattr(enrich, "enrich_site", site)
    assert enrich.enabled()
    result = asyncio.run(enrich.enrich_seeds([{"website": website}], "ws", "ven"))
    assert result == {httpx.URL(website).host: {"quality_rejected": True}}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://crawl.example/v2/scrape"
    assert requests[0].headers["Authorization"] == "Bearer self-key"


@pytest.mark.hosted_only
def test_shared_firecrawl_precedes_keyless_and_deduplicates_domains(monkeypatch):
    import httpx
    from app.credits import metered
    from app.signals import research

    requests = []
    admissions = []
    paced_keys = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/scrape"):
            return httpx.Response(200, json={"success": True, "data": {"markdown": "# Example"}})
        return httpx.Response(200, json={"success": True, "data": {"web": [
                {"url": "https://www.example.org/one", "title": "One"},
                {"url": "https://example.org/two", "title": "Two"},
                {"url": "https://another.org", "title": "Another"},
            ]}})

    def admitted(callback, **kwargs):
        # Synthetic admission for the shared-transport fixture. Real refusal
        # before network I/O is exercised in test_paid_search_paths.
        admissions.append(kwargs)
        return callback()

    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-private-key")
    monkeypatch.setenv("FIRECRAWL_API_KEY_2", "second-private-key")
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_INDEX", {})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(metered, "call_sync", admitted)
    monkeypatch.setattr(research, "_pace_firecrawl", paced_keys.append)
    result = discover._search_web("security companies", limit=3)
    assert [row["url"] for row in result] == [
        "https://www.example.org/one", "https://another.org"]
    assert str(requests[0].url) == "https://api.firecrawl.dev/v2/search"
    assert b'"sources":["web"]' in requests[0].content
    assert len(admissions) == 1 and admissions[0]["estimate_micro"] > 0
    assert "test-private-key" not in str(result)
    asyncio.run(research.HttpResearchProvider("firecrawl", "test-private-key").scrape("https://example.org"))
    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer test-private-key", "Bearer second-private-key"]
    assert paced_keys == ["test-private-key", "second-private-key"]
    assert str(requests[1].url) == "https://api.firecrawl.dev/v2/scrape"
