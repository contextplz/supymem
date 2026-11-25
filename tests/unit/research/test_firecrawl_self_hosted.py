"""Offline regression checks for opt-in self-hosted Firecrawl routing."""

import asyncio
import json

import httpx
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def transport(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    load_company_os_app()
    from app.signals import research

    for name in list(research.os.environ):
        if name.startswith("FIRECRAWL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_INDEX", {})
    requests, paced = [], []
    monkeypatch.setattr(research, "_pace_firecrawl", paced.append)
    original, original_sync = httpx.AsyncClient, httpx.Client

    def install(handler):
        def respond(request):
            requests.append(request)
            return handler(request)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_sync(transport=httpx.MockTransport(respond), **kwargs))

    return research, requests, paced, install


def response(path):
    data = {"markdown": "# Real page", "rawHtml": "<p>Real page</p>"} if path.endswith("scrape") else {"web": [{"url": "https://example.org", "title": "Example"}]}
    return httpx.Response(200, json={"success": True, "creditsUsed": 2, "data": data})


def activate(monkeypatch):
    from app.core import local_secrets
    values = {"provider": "firecrawl", "firecrawl_url": "https://crawl.example/"}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
        "values": values if section == "search" else {},
        "sources": dict.fromkeys((*values, "firecrawl_key"), "app") if section == "search" else {},
        "secrets_set": {}})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: "private-self-token" if name == "firecrawl_key" else None)
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example/")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "private-self-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("base,token", [(None, None), ("https://crawl.example", None), (None, "token"), ("http://crawl.example", "token"), ("https://", "token"), ("https://user:pass@crawl.example", "token")])
async def test_disabled_config_preserves_hosted_request_bytes(transport, monkeypatch, base, token):
    research, requests, paced, install = transport
    if base is not None:
        monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", base)
    if token is not None:
        monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", token)
    install(lambda request: response(request.url.path))
    provider = research.HttpResearchProvider("firecrawl", "hosted-key")
    rows = await provider.search("market demand")
    text = await provider.scrape("https://example.org")
    from app.gtm.quality import EXCLUDE_TAGS
    expected = [
        {"query": "market demand", "limit": 6, "sources": ["web"], "excludeDomains": ["linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com"]},
        {"url": "https://example.org", "formats": ["markdown"], "onlyMainContent": True, "timeout": 20000, "excludeTags": EXCLUDE_TAGS},
    ]
    assert [str(request.url) for request in requests] == ["https://api.firecrawl.dev/v2/search", "https://api.firecrawl.dev/v2/scrape"]
    assert [request.headers["Authorization"] for request in requests] == ["Bearer hosted-key"] * 2
    assert [request.content for request in requests] == [json.dumps(body, separators=(",", ":")).encode() for body in expected]
    assert paced == ["hosted-key"] * 2
    assert rows.usage == text.usage == {"creditsUsed": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("hosted", [True, False])
async def test_self_hosted_first_and_only_never_paces_or_rotates(transport, monkeypatch, hosted):
    research, requests, paced, install = transport
    activate(monkeypatch)
    if hosted:
        monkeypatch.setenv("FIRECRAWL_API_KEY", "hosted-key")
    monkeypatch.setattr(research, "_firecrawl_key", lambda _: pytest.fail("self-hosted rotated a key"))
    install(lambda request: response(request.url.path))
    provider = research._provider_from_env()
    assert provider.name == "firecrawl"
    rows = await provider.search("market demand", include_content=True)
    text = await provider.scrape("https://example.org")
    raw = await provider.scrape("https://example.org", main_only=False)
    assert [str(request.url) for request in requests] == ["https://crawl.example/v2/search", "https://crawl.example/v2/scrape", "https://crawl.example/v2/scrape"]
    assert all(request.headers["Authorization"] == "Bearer private-self-token" for request in requests)
    assert [request.extensions["timeout"]["read"] for request in requests] == [65.0, 25.0, 25.0]
    assert paced == []
    assert rows.usage == text.usage == raw.usage == {"creditsUsed": 2, "firecrawl_self_hosted": True}
    assert "private-self-token" not in str(rows) + text + raw


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search", "scrape"])
@pytest.mark.parametrize("failure", [403, 429, 500, 503, "timeout", "connection", "empty", "unsuccessful"])
@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("url,page_text", [
    ("https://freight.example/contact", "Our dispatch team handles freight bookings."),
    ("https://garden.example/community", "We share seasonal planting advice."),
])
async def test_self_hosted_failures_raise_and_never_call_hosted(transport, monkeypatch, path, failure, mode, url, page_text):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "hosted-key")
    for name in ("WEB_SEARCH_OPENAI_URL", "WEB_SEARCH_OPENAI_KEY", "WEB_SEARCH_MODEL", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(
                ("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else page_text).encode()))
        if failure == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("offline connection", request=request)
        if isinstance(failure, int):
            return httpx.Response(failure)
        return httpx.Response(200, json={"success": failure != "unsuccessful", "data": {"web": [], "markdown": "   "}})

    install(respond)
    provider = research._provider_from_env()
    fallback = path == "scrape" and mode == "local" and failure == "empty"
    if fallback:
        assert await provider.scrape(url) == page_text
    elif path == "search" and failure == "empty":
        assert await provider.search("market demand") == []
    else:
        with pytest.raises((httpx.HTTPError, ValueError)):
            await (provider.search("market demand") if path == "search" else provider.scrape(url))
    expected = ["crawl.example"]
    if fallback:
        expected += [httpx.URL(url).host] * 2
    assert [request.headers["Host"] for request in requests] == expected
    assert paced == []


@pytest.mark.asyncio
async def test_self_hosted_failure_never_reaches_exa_or_tavily(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "hosted-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    install(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await research._provider_from_env().search("market demand")
    assert [request.url.host for request in requests] == ["crawl.example"] and paced == []


@pytest.mark.asyncio
async def test_self_hosted_does_not_bypass_input_validation(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    install(lambda _: pytest.fail("invalid input reached HTTP"))
    with pytest.raises(ValueError, match="invalid excluded domain"):
        await research._provider_from_env().search("market demand", exclude_domains=["bad/path"])
    with pytest.raises(ValueError, match="linkedin_fetch_forbidden"):
        await research._provider_from_env().scrape("https://linkedin.com/in/person")
    assert requests == paced == []


@pytest.mark.asyncio
async def test_keyless_searches_route_through_self_hosted(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    install(lambda request: response(request.url.path))
    assert await research.DuckDuckGoProvider().search("market demand")
    install(lambda request: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await research.DuckDuckGoProvider().search("market demand")
    assert {request.url.host for request in requests} == {"crawl.example"} and paced == []


@pytest.mark.asyncio
async def test_self_hosted_only_scrape_failure_never_calls_hosted(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    install(lambda _: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError) as error:
        await research._provider_from_env().scrape("https://example.org")
    assert error.value.response.status_code == 403
    assert [request.url.host for request in requests] == ["crawl.example"] and paced == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("domain", ["freight.example", "garden.example"])
async def test_self_hosted_total_timeout_cancels_hung_page_and_raises(transport, monkeypatch, mode, domain):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "hosted-key")
    deadline, cancelled, local_deadlines = [], [], []
    original_timeout = asyncio.timeout
    def timeout(seconds):
        if mode == "local":
            local_deadlines.append(seconds)
            if len(local_deadlines) != 2:
                return original_timeout(seconds)
        deadline.append(seconds)
        return original_timeout(0.01)
    monkeypatch.setattr(research.asyncio, "timeout", timeout)
    async def respond(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    install(respond)
    url = f"https://{domain}/page"
    if mode == "local":
        from app.signals import local_reader
        monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
            (0, 0, 0, "", ("93.184.216.34", 0))])
        def basic_read(target, main_only):
            pytest.fail("A timed-out Firecrawl request must not trigger a built-in read")
        monkeypatch.setattr(local_reader, "_basic_read", basic_read)
    with pytest.raises(TimeoutError):
        await research._provider_from_env().scrape(url)
    assert deadline == [25.0] and cancelled == [True]
    if mode == "local":
        assert local_deadlines == [25, 25]
    assert [request.url.host for request in requests] == ["crawl.example"] and paced == []


def test_deep_research_selects_self_hosted_without_hosted_key(transport, monkeypatch):
    activate(monkeypatch)
    from app.research import deep
    provider = deep._web_provider()
    assert provider.name == deep._scrape_provider(provider).name == "firecrawl"


@pytest.mark.parametrize("status,expected", [(503, "retryable"), (429, "retryable"), (403, "failed"), (500, "uncertain")])
def test_self_hosted_http_failure_classification(transport, monkeypatch, status, expected):
    activate(monkeypatch)
    from app.research.metered import _failure
    reply = httpx.Response(status, headers={"Retry-After": "8"}, request=httpx.Request("POST", "https://crawl.example/v2/search"))
    with pytest.raises(httpx.HTTPStatusError) as error:
        reply.raise_for_status()
    assert _failure(error.value)[0] == expected


def web_search_reply(rows):
    text = json.dumps(rows)
    return httpx.Response(200, json={"output": [{"type": "web_search_call"}, {"type": "message", "content": [{"type": "output_text", "text": text}]}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("url,page_text", [
    ("https://freight.example/contact", "Our dispatch team handles freight bookings."),
    ("https://garden.example/community", "We share seasonal planting advice."),
])
@pytest.mark.parametrize("main_only", [True, False])
@pytest.mark.parametrize("case", [
    "success", "empty", "other_citations", "mixed_citations", "uncited", "sources_only",
    "non_refusal_500", "timeout", "connection", "404", "wrong_code", "missing_code",
    "azure_error", "azure_timeout", "unconfigured", "admission_guard", "search_refusal", "deadline", "malformed", "private_url",
])
async def test_self_hosted_antibot_read_exception(transport, monkeypatch, caplog, url, page_text, main_only, case):
    research, requests, paced, install = transport
    activate(monkeypatch)
    from app.credits import policy
    from app.tools import reader
    monkeypatch.setattr(policy, "get_policy", lambda _: {})
    monkeypatch.setattr(reader, "enabled", lambda: case == "admission_guard")
    monkeypatch.delenv("LLM_ADMISSION_V1", raising=False)
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs:
        [(2, 1, 6, "", ("127.0.0.1" if case == "private_url" else "8.8.4.4", 443))])
    if case in {"unconfigured", "search_refusal"}:
        monkeypatch.delenv("WEB_SEARCH_OPENAI_KEY")
    refusal = {"success": False, "code": "SCRAPE_RETRY_LIMIT", "error": "Scrape refused: document_antibot"}
    citations = [{"type": "url_citation", "url": url}]
    if case in {"other_citations", "mixed_citations"}:
        other = [{"type": "url_citation", "url": url + "/other"}]
        citations = other if case == "other_citations" else citations + other
    if case in {"uncited", "sources_only"}:
        citations = []
    azure_body = {"output": [
        {"type": "web_search_call", "action": {"type": "open_page", "sources": [{"url": url}]}},
        {"type": "message", "content": [{"type": "output_text", "text": "" if case == "empty" else page_text,
            "annotations": citations}]},
    ]}
    if case == "malformed":
        azure_body["output"] = None
    deadlines, cancelled = [], []
    if case == "deadline":
        original_timeout = asyncio.timeout
        def timeout(seconds):
            deadlines.append(seconds)
            return original_timeout(0.05 if seconds == 25.0 else seconds)
        monkeypatch.setattr(research.asyncio, "timeout", timeout)
    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    def respond(request):
        if request.url.host == "crawl.example":
            if case == "timeout":
                raise httpx.ReadTimeout("offline timeout", request=request)
            if case == "connection":
                raise httpx.ConnectError("offline connection", request=request)
            if case == "404":
                return httpx.Response(404)
            body = dict(refusal)
            if case == "non_refusal_500":
                body["error"] = "worker unavailable"
            if case == "wrong_code":
                body["code"] = "INTERNAL_SERVER_ERROR"
            if case == "missing_code":
                body.pop("code")
            return httpx.Response(500, json=body)
        assert request.url.host == "ai.example" and request.url.path.endswith("/responses")
        if case == "azure_error":
            return httpx.Response(500)
        if case == "azure_timeout":
            raise httpx.ReadTimeout("offline Azure timeout", request=request)
        if case == "deadline":
            return hang()
        return httpx.Response(200, json=azure_body)

    install(respond)
    provider = research.HttpResearchProvider("firecrawl", "hosted-key")
    with caplog.at_level("INFO", logger=research.__name__):
        if case == "success":
            result = await provider.scrape(url, main_only=main_only)
            assert result == page_text and isinstance(result, research.ScrapedText)
            assert result.metadata["sourceURL"] == url
            assert result.usage["firecrawl_self_hosted"] is True
            assert "reader=azure_web_search" in caplog.text
        else:
            with pytest.raises((httpx.HTTPError, TimeoutError)) as error:
                if case == "search_refusal":
                    await provider.search("public page")
                else:
                    await provider.scrape(url, main_only=main_only)
            if case not in {"timeout", "connection", "deadline"}:
                assert error.value.response.status_code == (404 if case == "404" else 500)
            assert "firecrawl_self_hosted_error" in caplog.text
    fallback = case not in {"non_refusal_500", "timeout", "connection", "404", "wrong_code", "missing_code", "unconfigured", "admission_guard", "search_refusal", "private_url"}
    assert [request.url.host for request in requests] == ["crawl.example"] + (["ai.example"] if fallback else [])
    if fallback:
        sent = json.loads(requests[1].content)
        assert sent["input"] == url and sent["max_tool_calls"] == 1
        assert sent["tools"] == [{"type": "web_search"}]
        assert "never infer" in sent["instructions"].lower()
    assert paced == []
    if case == "deadline":
        assert deadlines == [25.0, 30] and cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("audience,url", [
    ("Logistics dispatchers managing freight bookings", "https://freight.example/contact"),
    ("Home gardeners sharing seasonal planting advice", "https://garden.example/community"),
])
@pytest.mark.parametrize("shape", ["empty", "json", "sources", "citations", "split_text", "firecrawl_empty", "firecrawl_list", "firecrawl_web", "firecrawl_metadata"])
async def test_self_hosted_search_preserves_index_shapes_and_empty_results(transport, monkeypatch, audience, url, shape):
    research, requests, paced, install = transport
    activate(monkeypatch)
    row = {"url": url, "title": audience, "snippet": "Published contact and community information"}
    if shape.startswith("firecrawl"):
        if shape == "firecrawl_empty":
            data = {}
        elif shape == "firecrawl_metadata":
            data = [{"metadata": {"sourceURL": url, "title": audience}, "markdown": "Published page"}]
        else:
            data = [row, {"title": "Missing URL"}, None, {"url": "http://[broken"}]
            if shape == "firecrawl_web":
                data = {"web": data}
        body = {"success": True, "data": data}
    else:
        monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
        monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
        if shape == "sources":
            body = {"output": [{"type": "web_search_call", "action": {"type": "search", "sources": [row]}},
                {"type": "message", "content": [{"type": "output_text", "text": "[]"}]}]}
        elif shape == "citations":
            body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "A public page.",
                "annotations": [{"type": "url_citation", "url": url, "title": audience}]}]}]}
        elif shape == "split_text":
            body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Here are the results:"},
                {"type": "output_text", "text": json.dumps([row])}]}]}
        elif shape == "json":
            body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps([row])}]}]}
        else:
            body = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "[]"}]}]}

    def respond(request):
        if request.url.path.endswith("/scrape"):
            return httpx.Response(500, json={"success": False, "error": "document_antibot"})
        return httpx.Response(200, json=body)

    install(respond)
    rows = await research.HttpResearchProvider("firecrawl", "").search(audience, include_content=True)
    assert [item["url"] for item in rows] == ([] if shape in {"empty", "firecrawl_empty"} else [url])
    assert rows.usage["firecrawl_self_hosted"] is True
    if not shape.startswith("firecrawl"):
        assert json.loads(requests[0].content)["include"] == ["web_search_call.action.sources"]
    if shape in {"sources", "citations", "split_text"}:
        assert rows[0]["title"] == audience and "content" not in rows[0]
        assert sum(request.url.path.endswith("/scrape") for request in requests) == 1
    assert all(request.url.host in {"ai.example", "crawl.example"} for request in requests)
    assert paced == []


@pytest.mark.asyncio
async def test_openai_web_search_finds_and_self_hosted_reads(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
    found = [{"title": "Clinic", "url": "https://clinic.example/contact", "snippet": "Phone"},
             {"title": "Clinic", "url": "https://clinic.example/contact", "snippet": "dupe"},
             {"title": "Social", "url": "https://www.facebook.com/clinic", "snippet": "excluded"},
             {"title": "Bad", "url": "javascript:alert(1)"}]
    install(lambda request: web_search_reply(found) if request.url.host == "ai.example" else response(request.url.path))
    rows = await research.HttpResearchProvider("firecrawl", "").search("physio clinics Bangalore", include_content=True, tbs="qdr:w")
    assert [r["url"] for r in rows] == ["https://clinic.example/contact"]
    assert rows[0]["content"] == "# Real page" and rows.usage["firecrawl_self_hosted"] is True
    sent = json.loads(requests[0].content)
    assert requests[0].headers["api-key"] == "search-key" and sent["tools"] == [{"type": "web_search"}]
    assert sent["input"] == "physio clinics Bangalore (published in the past week)" and sent["model"] == "gpt-6-luna"
    assert [str(r.url) for r in requests[1:]] == ["https://crawl.example/v2/scrape"]  # SearXNG search is never called
    assert paced == []


@pytest.mark.asyncio
async def test_openai_web_search_failure_raises_without_fallback(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
    install(lambda request: httpx.Response(500) if request.url.host == "ai.example" else response(request.url.path))
    with pytest.raises(httpx.HTTPStatusError):
        await research.HttpResearchProvider("firecrawl", "").search("anything")
    install(lambda request: web_search_reply([]) if request.url.host == "ai.example" else response(request.url.path))
    assert await research.HttpResearchProvider("firecrawl", "").search("anything") == []
    assert all(r.url.host == "ai.example" for r in requests)


@pytest.mark.asyncio
async def test_openai_web_search_rewrites_site_operators_to_brand_words(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
    install(lambda request: web_search_reply([{"title": "Owner", "url": "https://www.linkedin.com/in/owner"}, {"title": "Thread", "url": "https://forum.example/t"}]))
    rows = await research.HttpResearchProvider("firecrawl", "").search('site:linkedin.com/in "gym owner" Bangalore')
    assert [r["url"] for r in rows][0] == "https://www.linkedin.com/in/owner"
    assert json.loads(requests[0].content)["input"] == 'LinkedIn "gym owner" Bangalore'
    await research.HttpResearchProvider("firecrawl", "").search("(site:reddit.com/r/gyms OR site:news.ycombinator.com) churn")
    assert json.loads(requests[1].content)["input"] == "(Reddit OR Hacker News) churn"


@pytest.mark.asyncio
async def test_empty_web_search_returns_empty_without_repeating_query(transport, monkeypatch):
    research, requests, paced, install = transport
    activate(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_OPENAI_URL", "https://ai.example/openai/v1")
    monkeypatch.setenv("WEB_SEARCH_OPENAI_KEY", "search-key")
    install(lambda request: web_search_reply([]) if request.url.host == "ai.example" else response(request.url.path))
    rows = await research.HttpResearchProvider("firecrawl", "").search('gym "Pune" contact')
    assert rows == []
    asks = [json.loads(r.content)["input"] for r in requests]
    assert asks == ['gym "Pune" contact']
