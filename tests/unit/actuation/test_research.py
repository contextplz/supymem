from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

@pytest.fixture(autouse=True)
def research_app(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["equipment maintenance records", "home planting calendars"])
async def test_research_recon_returns_provenance_mode(monkeypatch: pytest.MonkeyPatch, query) -> None:
    from app.signals.research import research_recon
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("OPENWORKER_SEARCH", "0")
    monkeypatch.setenv("RESEARCH_KEYLESS", "0")

    result = await research_recon(query)

    assert result["mode"] == "blocked"
    assert result["reason"] == "no_research_provider"
    assert set(("queries", "competitors", "claims", "sources", "mode")) <= result.keys()


@pytest.mark.asyncio
async def test_openworker_bridge_search_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.openworker_bridge import openworker_available, search_web

    if not openworker_available():
        pytest.skip("vendor/openworker not cloned")

    monkeypatch.setenv("OPENWORKER_SEARCH_PROVIDER", "duckduckgo")
    rows = search_web("SaaS waitlist landing page", max_results=2)
    assert isinstance(rows, list)
    if rows:
        assert rows[0]["url"].startswith("http")
        assert "openworker:" in rows[0]["provider"]


def test_openworker_fetch_and_guard() -> None:
    from app.research.openworker_bridge import check_url_safe, fetch_url, openworker_available

    if not openworker_available():
        pytest.skip("vendor/openworker not cloned")

    assert check_url_safe("http://127.0.0.1/") is not None
    assert check_url_safe("https://example.com/") is None
    blocked = fetch_url("http://127.0.0.1/")
    assert "error" in blocked
    ok = fetch_url("https://example.com/", max_chars=400)
    assert ok.get("text") and len(ok["text"]) > 20
    assert not ok.get("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("query,url", [
    ("equipment maintenance records", "https://maintenance.example/tools"),
    ("home planting calendars", "https://gardening.example/calendars"),
])
async def test_keyless_duckduckgo_is_live_not_simulated(monkeypatch: pytest.MonkeyPatch, query, url) -> None:
    from app.signals.research import research_recon
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("OPENWORKER_SEARCH", "0")
    monkeypatch.setenv("RESEARCH_KEYLESS", "1")

    async def fake_search(self, query: str):
        return [
            {
                "url": url,
                "title": query,
                "snippet": query,
                "provider": "duckduckgo",
            }
        ]

    from app.signals.research import DuckDuckGoProvider

    monkeypatch.setattr(
        "app.signals.research._provider_from_env",
        lambda: DuckDuckGoProvider(),
    )
    monkeypatch.setattr(DuckDuckGoProvider, "search", fake_search)
    result = await research_recon(query)
    assert result["mode"] == "live"
    assert result["provider"] == "duckduckgo"
    assert result["sources"]


@pytest.mark.parametrize("url", ["https://maintenance.example/", "https://gardening.example/"])
def test_web_fetch_prefers_openworker_guard(monkeypatch, url) -> None:
    from app.research import openworker_bridge, agent_reach_bridge
    from app.research.openworker_bridge import fetch_url
    from app.research.web_fetch import fetch_page_markdown

    def fetch(target, **kwargs):
        if target == "http://127.0.0.1/":
            return {"error": "loopback refused"}
        assert target == url
        return {"text": "Your public page has readable text."}

    monkeypatch.setattr(agent_reach_bridge, "vendored", lambda: False)
    monkeypatch.setattr(openworker_bridge, "_fetch_tool", lambda: fetch)
    text = fetch_page_markdown(url)
    assert len(text) > 20
    blocked = fetch_url("http://127.0.0.1/")
    assert blocked.get("error")
    assert "loopback" in str(blocked.get("error")).lower() or "refus" in str(blocked.get("error")).lower()
