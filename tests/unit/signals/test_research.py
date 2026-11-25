import asyncio

import httpx
import pytest


@pytest.fixture(autouse=True)
def hosted_research(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


@pytest.mark.parametrize("provider", ["firecrawl", "exa", "tavily"])
@pytest.mark.parametrize("status", [402, 429, 503])
def test_search_http_errors_attach_status_without_wrapping(monkeypatch, provider, status):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.signals import research

    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr(research, "_pace_firecrawl", lambda key: None)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(research.HttpResearchProvider(provider, "private-test-key").search("private query"))
    assert caught.value.status_code == status
    assert caught.value.response.status_code == status


@pytest.fixture(params=[
    ("https://logistics.example/guide", "Scheduling deliveries for distribution teams."),
    ("https://cycling.example/guide", "Choosing cycling routes for weekend trips."),
], ids=["business", "consumer"])
def local_page(request, monkeypatch):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.signals import research
    from app.core import local_secrets
    from app.credits import metered

    saved, secrets = {}, {}
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
        "values": saved if section == "search" else {},
        "sources": dict.fromkeys((*saved, "api_key", "firecrawl_key"), "app") if section == "search" else {},
        "secrets_set": {}})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: secrets.get(name) if section == "search" else None)

    async def call(callback, **kwargs):
        return await callback()

    monkeypatch.setattr(metered, "call", call)

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for name in ("FIRECRAWL_SELF_HOSTED_URL", "FIRECRAWL_SELF_HOSTED_TOKEN", "FIRECRAWL_API_KEY",
                 "WEB_SEARCH_OPENAI_URL", "WEB_SEARCH_OPENAI_KEY", "WEB_SEARCH_MODEL", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return research, *request.param


def save_search(monkeypatch, provider=None, **fields):
    from app.core import local_secrets, providers_local
    from types import SimpleNamespace

    values = local_secrets.read_settings("search")["values"]
    if provider is not None:
        values["provider"] = provider
    secrets = {name: local_secrets.get_secret("search", name) for name in ("api_key", "firecrawl_key")}
    for name in tuple(fields):
        if name in secrets:
            secrets[name] = fields.pop(name)
    if provider == "openai":
        config = SimpleNamespace(provider="openai", base_url=fields.pop("base_url", "http://localhost:8000/v1"),
            api_key=SimpleNamespace(get_secret_value=lambda: "fixture-credential"), deployment_flash="configured-model")
        monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    values.update(fields)
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: secrets.get(name) if section == "search" else None)


def test_local_search_works_without_extra_configuration(local_page, monkeypatch):
    research, url, query = local_page
    monkeypatch.setattr(research, "_ddgs_text", lambda text, **kwargs: [{"href": url, "title": query}])
    provider = research._provider_from_env()
    assert provider.name == "duckduckgo"
    assert [source["url"] for source in asyncio.run(provider.search(query))] == [url]


@pytest.mark.parametrize("provider", ["reader", "search"])
def test_local_provider_credentials_require_https_outside_loopback(local_io, monkeypatch, provider):
    research, url, text, requests, install = local_io
    host = httpx.URL(url).host
    if provider == "reader":
        save_search(monkeypatch, firecrawl_url=f"http://{host}/reader", firecrawl_key="fixture-credential")
        operation = research.HttpResearchProvider("firecrawl", "").scrape(url)
    else:
        save_search(monkeypatch, "openai", base_url=f"http://{host}/v1")
        operation = research.HttpResearchProvider("firecrawl", "").search(text)
    install(lambda request: pytest.fail("Your credential must not be sent to a non-loopback HTTP provider"))
    with pytest.raises(ValueError):
        asyncio.run(operation)
    assert not requests
    from app.core.providers_local import validate_endpoint

    assert validate_endpoint(f"https://{host}/v1") == f"https://{host}/v1"
    assert validate_endpoint("http://127.0.0.1:3002/").rstrip("/") == "http://127.0.0.1:3002"


@pytest.mark.parametrize("destination", [
    "http://127.0.0.1/private", "https://10.0.0.7/private",
    "http://169.254.169.254/latest", "https://100.64.0.1/private",
    "https://168.63.129.16/private",
    "https://[::1]/private", "https://[::ffff:127.0.0.1]/private",
    "credentials", "empty_credentials", "invalid_port", "backslash", "downgrade", "rebound",
])
def test_local_basic_redirects_reject_unsafe_urls_before_connecting(local_io, monkeypatch, destination):
    from app.signals import local_reader
    from core.research_web import guard

    _, url, text, requests, install = local_io
    host = httpx.URL(url).host
    target_host = f"redirect.{host}"
    targets = {"credentials": f"https://user:synthetic@{target_host}/private",
               "empty_credentials": f"https://@{target_host}/private",
               "invalid_port": f"https://{target_host}:invalid/private",
               "backslash": f"https://{target_host}/\\private",
               "downgrade": f"http://{target_host}/private",
               "rebound": f"https://{target_host}/private"}

    def resolve(hostname, *args, **kwargs):
        address = "127.0.0.1" if destination == "rebound" and hostname == target_host else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", resolve)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /"))
        if request.headers["Host"] == host:
            return httpx.Response(302, headers={"location": targets.get(destination, destination)}, stream=httpx.ByteStream(b""))
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(text.encode()))

    install(handler)
    failure = httpx.RemoteProtocolError if destination == "invalid_port" else local_reader.reader.ReaderError
    with pytest.raises(failure):
        local_reader._basic_read(url, True)
    assert [request.url.path for request in requests] == ["/robots.txt", "/guide"]
    assert all(request.url.host == "93.184.216.34" and "Authorization" not in request.headers for request in requests)


@pytest.mark.parametrize("port", [None, 80, 8080])
@pytest.mark.parametrize("available", [True, False])
def test_local_basic_http_prefers_https_and_pins_each_connection(local_io, monkeypatch, available, port):
    from app.signals import local_reader
    from core.research_web import guard

    _, url, text, requests, install = local_io
    host = httpx.URL(url).host
    authority = host + (f":{port}" if port is not None else "")
    http_url = f"http://{authority}/guide"
    resolutions = []

    def resolve(host, *args, **kwargs):
        resolutions.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", resolve)

    def handler(request):
        assert request.url.host == "93.184.216.34"
        expected_host = host + (f":{port}" if port is not None and not (port == 80 and request.url.scheme == "https") else "")
        assert request.headers["Host"] == expected_host
        assert "Authorization" not in request.headers
        if request.url.scheme == "https":
            assert request.extensions["sni_hostname"] == httpx.URL(url).host
            if not available:
                raise httpx.ConnectError("No HTTPS listener", request=request)
        content = "User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else text
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(content.encode()))

    install(handler)
    response = local_reader._basic_read(http_url, True)
    assert response.json()["data"]["markdown"] == text
    expected = ["https", "https"] if available else ["https", "http", "http"]
    assert [request.url.scheme for request in requests] == expected
    secure_authority = host if port == 80 else authority
    expected_url = f"https://{secure_authority}/guide" if available else http_url
    assert response.json()["data"]["metadata"]["sourceURL"] == expected_url
    assert len(resolutions) == len(requests) + int(not available)


@pytest.mark.parametrize("limit", ["wire", "decompressed", "deadline", "characters"])
def test_local_basic_reads_keep_size_and_time_bounds(local_io, monkeypatch, limit):
    import gzip
    from types import SimpleNamespace
    from app.signals import local_reader

    _, url, text, requests, install = local_io
    monkeypatch.setattr(local_reader.reader, "MAX_BYTES", 128)
    clock = SimpleNamespace(value=0.0)
    local_time = SimpleNamespace(monotonic=lambda: clock.value)
    monkeypatch.setattr(local_reader, "time", local_time)
    monkeypatch.setattr(local_reader.reader, "time", local_time)
    monkeypatch.setenv("READER_MAX_CHARS", "5")

    def handler(request):
        if request.url.path == "/robots.txt":
            if limit == "deadline":
                clock.value = 26.0
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /"))
        content = text.encode() if limit == "characters" else b"x" * 129
        headers = {"content-type": "text/plain"}
        if limit == "decompressed":
            content = gzip.compress(content)
            headers["content-encoding"] = "gzip"
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(content))

    install(handler)
    if limit == "characters":
        assert local_reader._basic_read(url, True).json()["data"]["markdown"] == text[:5]
    else:
        reason = "robots_unavailable" if limit == "deadline" else "page_too_large"
        with pytest.raises(local_reader.reader.ReaderError, match=reason):
            local_reader._basic_read(url, True)
    assert all(request.extensions["timeout"]["read"] <= 10 for request in requests)


def test_local_page_dns_validation_is_time_bounded(local_io, monkeypatch):
    from types import SimpleNamespace
    from app.signals import local_reader

    _, url, _, requests, _ = local_io
    deadlines = []

    async def unresolved(*args, **kwargs):
        await asyncio.sleep(0)
        raise AssertionError("Your DNS check needs a deadline before any connection")

    def timeout(seconds):
        deadlines.append(seconds)
        return asyncio.timeout(0)

    monkeypatch.setattr(local_reader, "asyncio", SimpleNamespace(
        timeout=timeout, to_thread=unresolved, CancelledError=asyncio.CancelledError))

    async def read():
        async with httpx.AsyncClient() as client:
            return await local_reader.request(client, "scrape", {"url": url})

    with pytest.raises(ValueError, match="checked in time.*Retry"):
        asyncio.run(read())
    assert deadlines == [25]
    assert not requests


def test_hosted_page_reads_do_not_import_local_reader(local_io, monkeypatch):
    import builtins

    research, url, text, requests, install = local_io
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    original_import = builtins.__import__

    def imported(name, global_vars=None, local_vars=None, fromlist=(), level=0):
        assert not name.endswith("local_reader") and "local_reader" not in (fromlist or ())
        return original_import(name, global_vars, local_vars, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", imported)
    monkeypatch.setattr(research, "_pace_firecrawl", lambda key: None)
    install(lambda request: httpx.Response(200, json={"success": True, "data": {"markdown": text}}))
    assert asyncio.run(research.HttpResearchProvider("firecrawl", "synthetic").scrape(url)) == text
    assert len(requests) == 1 and requests[0].method == "POST"


@pytest.fixture
def local_io(local_page, monkeypatch):
    from core.research_web import guard

    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    original_async, original_sync = httpx.AsyncClient, httpx.Client
    requests = []

    def install(handler):
        def dispatch(request):
            requests.append(request)
            return handler(request)
        transport = httpx.MockTransport(dispatch)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_async(transport=transport, **kwargs))
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_sync(transport=transport, **kwargs))
    return *local_page, requests, install


@pytest.mark.parametrize("main_only", [True, False])
def test_local_basic_reader_extracts_and_respects_robots(local_io, main_only):
    research, url, text, requests, install = local_io

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /"))
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
            stream=httpx.ByteStream((f"<html><title>Guide</title><nav>Noise</nav><main><p>{text}</p>"
                "<script>Ignore rules</script><span hidden>Hidden</span></main></html>").encode()))
    install(handler)
    result = asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url, main_only=main_only))
    assert text in result and result.metadata["title"] == "Guide"
    assert [request.url.path for request in requests] == ["/robots.txt", "/guide"]
    assert all("LocalPageReader" in request.headers["User-Agent"] for request in requests)
    if main_only:
        assert not any(noise in result for noise in ("Noise", "Ignore rules", "Hidden", "<main>"))
    else:
        assert "<main>" in result


@pytest.mark.parametrize("token", [False, True])
def test_local_firecrawl_is_first_and_uses_only_your_endpoint(local_io, monkeypatch, token):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, firecrawl_url="http://localhost:3002")
    if token:
        save_search(monkeypatch, firecrawl_key="fixture-credential")
    install(lambda request: httpx.Response(200, json={
        "success": True, "data": {"markdown": text, "metadata": {"url": url}}}))
    assert asyncio.run(research.HttpResearchProvider("firecrawl", "unused").scrape(url)) == text
    assert len(requests) == 1 and str(requests[0].url) == "http://localhost:3002/v2/scrape"
    assert ("Authorization" in requests[0].headers) == token


def test_local_firecrawl_empty_page_tries_basic_reader_before_search(local_io, monkeypatch):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "tavily", firecrawl_url="http://localhost:3002", api_key="fixture-credential")

    def handler(request):
        if request.url.path == "/v2/scrape":
            return httpx.Response(200, json={"success": True, "data": {"markdown": ""}})
        return httpx.Response(200, headers={"content-type": "text/plain"},
            stream=httpx.ByteStream(("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else text).encode()))
    install(handler)
    assert asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url)) == text
    assert [request.url.path for request in requests] == ["/v2/scrape", "/robots.txt", "/guide"]


@pytest.mark.parametrize("configured_firecrawl", [False, True])
def test_local_search_page_fetch_is_last(local_io, monkeypatch, configured_firecrawl):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    if configured_firecrawl:
        save_search(monkeypatch, firecrawl_url="http://localhost:3002")

    def handler(request):
        if request.url.path == "/v2/scrape":
            return httpx.Response(200, json={"success": True, "data": {"markdown": ""}})
        if request.url.path == "/extract":
            return httpx.Response(200, json={"results": [{"url": url, "raw_content": text}]})
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        return httpx.Response(503, stream=httpx.ByteStream(b""))
    install(handler)
    assert asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url)) == text
    assert [request.url.path for request in requests] == (
        (["/v2/scrape"] if configured_firecrawl else []) + ["/robots.txt", "/guide", "/extract"])


def test_local_robots_refusal_cannot_use_search_to_bypass(local_io, monkeypatch):
    research, url, _, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    install(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nDisallow: /")))
    with pytest.raises(ValueError, match="Choose another public URL"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt"]


@pytest.mark.parametrize("provider", ["tavily", "openai", "firecrawl"])
def test_local_search_uses_configured_provider_and_keeps_limits(local_io, monkeypatch, provider):
    import json

    research, url, query, requests, install = local_io
    save_search(monkeypatch, provider, api_key="fixture-credential",
        **({"firecrawl_url": "http://localhost:3002"} if provider == "firecrawl" else {}))
    rows = [{"url": url, "title": "Guide", "snippet": query},
            {"url": "https://excluded.example/page", "title": "Excluded", "snippet": "Other"}]

    def handler(request):
        if provider == "openai":
            return httpx.Response(200, json={"output": [{"content": [
                {"type": "output_text", "text": json.dumps(rows)}]}]})
        return httpx.Response(200, json={"success": True, "data": rows, "results": rows})
    install(handler)
    result = asyncio.run(research._provider_from_env().search(query, limit=1, exclude_domains=["excluded.example"]))
    assert len(result) == 1 and result[0]["url"] == url and len(requests) == 1
    assert requests[0].url.path == {"tavily": "/search", "openai": "/v1/responses", "firecrawl": "/v2/search"}[provider]
    assert bool(result.usage.get("firecrawl_self_hosted")) == (provider == "firecrawl")
    if provider == "openai":
        assert "at most 1 items" in json.loads(requests[0].content)["instructions"]


@pytest.mark.parametrize("status", [402, 429, 503])
def test_local_search_failures_surface_without_other_provider(local_io, monkeypatch, status):
    research, _, query, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential", firecrawl_url="http://localhost:3002")
    install(lambda request: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(research._provider_from_env().search(query))
    assert caught.value.status_code == status and len(requests) == 1


@pytest.mark.parametrize("citation", ["exact", "other", "none"])
def test_local_openai_citations_do_not_replace_a_page_read(local_io, monkeypatch, citation):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "openai")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        if request.url.path != "/v1/responses":
            return httpx.Response(503, stream=httpx.ByteStream(b""))
        annotations = [] if citation == "none" else [{"type": "url_citation",
            "url": url if citation == "exact" else "https://unrelated.example/page"}]
        return httpx.Response(200, json={"output": [{"content": [
            {"type": "output_text", "text": text, "annotations": annotations}]}]})
    install(handler)
    with pytest.raises(ValueError, match="another public URL"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt", "/guide"]


def test_local_cloud_firecrawl_key_does_not_enable_cloud_io(local_io, monkeypatch):
    research, url, text, requests, install = local_io
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture-credential")
    install(lambda request: httpx.Response(200, headers={"content-type": "text/plain"},
        stream=httpx.ByteStream(("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else text).encode())))
    assert asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url)) == text
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("configured_firecrawl", [False, True])
def test_local_private_url_never_reaches_any_provider(local_io, monkeypatch, configured_firecrawl):
    research, _, _, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    if configured_firecrawl:
        save_search(monkeypatch, firecrawl_url="http://localhost:3002")
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("127.0.0.1", 0))])
    install(lambda request: httpx.Response(200))
    with pytest.raises(ValueError):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape("http://127.0.0.1/private"))
    assert not requests


@pytest.mark.parametrize("raw_content", [None, "", " "])
def test_local_search_content_uses_basic_reader(local_io, monkeypatch, raw_content):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")

    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": url, "title": "Guide", "content": text,
                "raw_content": raw_content}]})
        return httpx.Response(200, headers={"content-type": "text/plain"},
            stream=httpx.ByteStream(("User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else text).encode()))
    install(handler)
    result = asyncio.run(research._provider_from_env().search(text, include_content=True))
    assert result[0]["content"] == text
    assert [request.url.path for request in requests] == ["/search", "/robots.txt", "/guide"]


@pytest.mark.parametrize("configured_firecrawl", [False, True])
def test_local_platform_address_cannot_reach_a_provider(local_io, monkeypatch, configured_firecrawl):
    research, url, _, requests, install = local_io
    if configured_firecrawl:
        save_search(monkeypatch, firecrawl_url="http://localhost:3002")
    save_search(monkeypatch, "tavily", api_key="synthetic")
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("168.63.129.16", 443))])
    install(lambda request: httpx.Response(200))
    with pytest.raises(ValueError):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert not requests


def test_local_robots_matches_your_readers_user_agent(local_io):
    research, url, _, requests, install = local_io
    install(lambda request: httpx.Response(200, stream=httpx.ByteStream(
        b"User-agent: LocalPageReader\nDisallow: /\n\nUser-agent: *\nAllow: /")))
    with pytest.raises(ValueError, match="Choose another public URL"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt"]


def test_local_redirects_check_destination_robots_before_fetching(local_io):
    research, url, _, requests, install = local_io

    def handler(request):
        if request.url.path == "/robots.txt":
            content = b"User-agent: *\nDisallow: /" if request.headers["Host"] == "destination.example" else b"User-agent: *\nAllow: /"
            return httpx.Response(200, stream=httpx.ByteStream(content))
        return httpx.Response(302, headers={"location": "https://destination.example/private"}, stream=httpx.ByteStream(b""))
    install(handler)
    with pytest.raises(ValueError, match="Choose another public URL"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt", "/guide", "/robots.txt"]


def test_local_search_rejection_at_metered_entry_prevents_io(local_io, monkeypatch):
    research, _, query, requests, install = local_io
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable

    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    install(lambda request: httpx.Response(200, json={"results": []}))
    calls = []

    async def reject(provider, text, **kwargs):
        calls.append((provider, text))
        raise CreditsUnavailable()
    monkeypatch.setattr(metered, "search", reject)
    with pytest.raises(CreditsUnavailable):
        asyncio.run(research._search_one(research._provider_from_env(), query))
    assert len(calls) == 1 and calls[0][1] == query and not requests


@pytest.mark.parametrize("adapter", ["exa", "tavily"])
def test_local_existing_adapters_use_your_configured_search(local_io, monkeypatch, adapter):
    research, url, query, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    install(lambda request: httpx.Response(200, json={"results": [{"url": url, "content": query}]}))
    rows = asyncio.run(research.HttpResearchProvider(adapter, "unused").search(query))
    assert rows[0]["url"] == url and [request.url.path for request in requests] == ["/search"]


def test_local_keyless_adapter_uses_builtin_search(local_io, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.research import local_providers

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    research, _, query, requests, install = local_io
    install(lambda request: httpx.Response(200))
    monkeypatch.setattr(local_providers, "_builtin_next_at", 0.0)
    monkeypatch.setattr(local_providers, "_builtin_cooldowns", {})
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda delay: None))
    monkeypatch.setattr(research, "_ddgs_text", lambda text, **kwargs: [])
    assert asyncio.run(research._search_one(research.DuckDuckGoProvider(), query)) == (query, [], None)
    assert not requests


def test_local_unavailable_robots_does_not_use_search_to_bypass(local_io, monkeypatch):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")

    def handler(request):
        if request.url.path == "/extract":
            return httpx.Response(200, json={"results": [{"url": url, "raw_content": text}]})
        return httpx.Response(503, stream=httpx.ByteStream(b""))
    install(handler)
    with pytest.raises(ValueError, match="permissions could not be checked"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt"]


@pytest.mark.parametrize("failure", ["invalid_json", "timeout", "deadline"])
def test_local_last_page_reader_errors_have_next_steps(local_io, monkeypatch, failure):
    from types import SimpleNamespace
    from app.signals import local_reader

    research, url, _, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="synthetic")
    deadlines, cancelled = [], []

    def timeout(seconds):
        deadlines.append(seconds)
        return asyncio.timeout(0 if len(deadlines) == 3 else seconds)

    async def stalled():
        try:
            await asyncio.sleep(0)
            raise AssertionError("Your final page reader needs a deadline")
        finally:
            cancelled.append(True)

    if failure == "deadline":
        monkeypatch.setattr(local_reader, "asyncio", SimpleNamespace(
            timeout=timeout, to_thread=asyncio.to_thread, CancelledError=asyncio.CancelledError))

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        if request.url.path != "/extract":
            return httpx.Response(503, stream=httpx.ByteStream(b""))
        if failure == "timeout":
            raise TimeoutError
        if failure == "deadline":
            return stalled()
        return httpx.Response(200, content=b"unusable provider reply")

    install(handler)
    with pytest.raises(ValueError, match="Your page.*Retry.*search provider"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert [request.url.path for request in requests] == ["/robots.txt", "/guide", "/extract"]
    if failure == "deadline":
        assert deadlines == [25, 25, 25] and cancelled == [True]


def test_local_page_chain_surfaces_the_final_provider_failure(local_io, monkeypatch):
    research, url, _, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential", firecrawl_url="http://localhost:3002")

    def handler(request):
        if request.url.path == "/v2/scrape":
            return httpx.Response(200, json={"success": True, "data": {"markdown": ""}})
        status = 404 if request.url.path == "/robots.txt" else 429 if request.url.path == "/extract" else 503
        return httpx.Response(status, stream=httpx.ByteStream(b""))
    install(handler)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert caught.value.status_code == 429
    assert [request.url.path for request in requests] == ["/v2/scrape", "/robots.txt", "/guide", "/extract"]


def test_local_dns_change_cannot_use_search_to_bypass_guard(local_io, monkeypatch):
    research, url, text, requests, install = local_io
    save_search(monkeypatch, "tavily", api_key="fixture-credential")
    addresses = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", (next(addresses, "127.0.0.1"), 0))])
    install(lambda request: httpx.Response(200, json={"results": [{"url": url, "raw_content": text}]}))
    with pytest.raises(ValueError, match="Choose another public URL"):
        asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    assert not requests


@pytest.mark.parametrize("empty", [False, True])
def test_local_builtin_recovers_after_refusals_for_successive_queries(local_page, monkeypatch, tmp_path, empty):
    from types import SimpleNamespace
    from ddgs.exceptions import DDGSException
    from app.gtm import email_learning
    from app.research import local_providers
    from core.clock import utcnow

    research, url, query = local_page
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(email_learning, "cached", lambda key: None)
    monkeypatch.setattr(email_learning, "cache_save", lambda key, value: None)
    monkeypatch.setattr(local_providers, "_builtin_cooldowns", {})
    monkeypatch.setattr(local_providers, "_builtin_next_at", 0.0)
    monkeypatch.setattr(local_providers, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda delay: None))
    now = utcnow()
    monkeypatch.setattr(local_providers, "utcnow", lambda: now)
    attempts = []
    unavailable = {"duckduckgo", "brave", "yahoo", "bing"}

    def search(text, *, limit, backend=None, **kwargs):
        attempts.append((text, backend))
        if backend in unavailable:
            raise httpx.HTTPStatusError("Refused", request=httpx.Request("GET", "https://search.example"),
                response=httpx.Response(429))
        if empty and text == query:
            raise DDGSException("No results found.")
        return [{"href": url + str(index), "title": "Public source", "body": text} for index in range(limit)]

    monkeypatch.setattr(research, "_ddgs_text", search)
    for text in (query, query + " alternatives"):
        if empty and text == query:
            with pytest.raises(local_providers.SearchEngineLimited):
                asyncio.run(local_providers.local_search(text, limit=3))
            continue
        rows = asyncio.run(local_providers.local_search(text, limit=3))
        assert len({row["url"] for row in rows}) == 3
        assert all(row["provider"] == "duckduckgo" and row["fetched_at"] for row in rows)
    assert any(backend in unavailable for text, backend in attempts if text == query)
    assert any(backend not in unavailable for text, backend in attempts if text == query + " alternatives")
    assert not any(backend in unavailable for text, backend in attempts if text == query + " alternatives")


def test_local_reader_preserves_verbatim_linked_names_for_research(local_io):
    from app.research.prior_art import runner

    research, url, text, requests, install = local_io
    name = " ".join(text.split()[:2])
    body = (f'<html><main><p>{text}</p><a href="/product"><strong>{name}</strong></a>'
        '<a href="javascript:alert(1)">Not a source</a></main>'
        '<nav><a href="https://hidden.example">Hidden name</a></nav></html>')

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        return httpx.Response(200, headers={"content-type": "text/html"},
            stream=httpx.ByteStream(body.encode()))

    install(handler)
    page = asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url))
    expected = url.rsplit("/", 1)[0] + "/product"
    assert runner.roundup_names(page) == [{"name": name, "url": expected}]
    assert text in page and "Not a source" in page
    assert "Hidden name" not in page and "javascript:" not in page
    assert len(requests) == 2


def test_local_raw_page_limit_keeps_body_and_published_structured_people(local_io, monkeypatch):
    from app.tools import reader

    research, url, text, requests, install = local_io
    structured = '<script type="application/ld+json">{"@type":"Person","name":"Published guide"}</script>'
    body = ('<html><head><style>' + 'a{color:red}' * 1000 + '</style>' + structured +
        '<script>' + 'window.decorative=true;' * 1000 + '</script></head>' +
        f'<main><p>{text}</p><a href="/team">Published team</a></main></html>')
    monkeypatch.setattr(reader, "max_chars", lambda: 512)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(body.encode()))

    install(handler)
    page = asyncio.run(research.HttpResearchProvider("firecrawl", "").scrape(url, main_only=False))
    assert text in page and structured in page and '<main>' in page and 'href="/team"' in page
    assert "window.decorative" not in page and '<style>' not in page
    assert len(page) <= 512 and len(requests) == 2
