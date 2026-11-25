"""Firecrawl v2 web results remain evidence-bearing search hits."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[3] / "services" / "company_os"))


@pytest.fixture(autouse=True)
def hosted_provider_contract(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)

from app.signals.research import HttpResearchProvider, _sources_from_response  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["procurement.example", "family-meals.example"])
@pytest.mark.parametrize("failure", [None, 401, 429, 503, "invalid", "timeout", "empty", "unsuccessful"])
async def test_local_page_fallback_only_on_unusable_success_and_outside_slot(monkeypatch, caplog, domain, failure):
    from app.signals import local_reader, research
    from core import ssrf

    for name in ("FIRECRAWL_SELF_HOSTED_URL", "WEB_SEARCH_OPENAI_URL", "WEB_SEARCH_OPENAI_KEY",
                 "WEB_SEARCH_MODEL", "TAVILY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.core.local_secrets import update_settings
    if failure is not None:
        monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "http://127.0.0.1:3102")
        update_settings("search", {"provider": "firecrawl", "firecrawl_url": "http://127.0.0.1:3102"}, {})
    url = f"https://{domain}/contact?session=private-value"
    checked, requests, reads = [], [], []
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(research, "_FIRECRAWL_PROCESS_SLOTS", slots)
    monkeypatch.setattr(ssrf, "assert_safe_remote_url", lambda url: checked.append(url))

    def basic_read(target, main_only):
        assert checked == [url]
        assert slots.acquire(blocking=False), "fallback held the Firecrawl slot"
        slots.release()
        reads.append((target, main_only))
        return local_reader._page_response(target, "Public page text")

    monkeypatch.setattr(local_reader, "_basic_read", basic_read)

    def respond(request):
        assert checked == [url]
        assert not slots.acquire(blocking=False), "Firecrawl made a request without a slot"
        requests.append(request)
        if failure == "timeout":
            raise TimeoutError("offline timeout")
        if failure == "invalid":
            return httpx.Response(200, content=b"invalid")
        if isinstance(failure, int):
            return httpx.Response(failure)
        return httpx.Response(200, json={"success": failure != "unsuccessful", "data": {"markdown": " "}})

    fallback = failure in {None, "empty"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if fallback:
            response = await local_reader.request(client, "scrape", {"url": url})
            assert response.json()["data"]["markdown"] == "Public page text"
        else:
            error_type = httpx.HTTPStatusError if isinstance(failure, int) else TimeoutError if failure == "timeout" else ValueError
            with pytest.raises(error_type, match="Keys and connections") as caught:
                await local_reader.request(client, "scrape", {"url": url})
            if isinstance(failure, int):
                assert caught.value.response.status_code == failure
    assert checked == [url] and reads == ([(url, True)] if fallback else [])
    assert [str(request.url) for request in requests] == (
        ["http://127.0.0.1:3102/v2/scrape"] if failure is not None else [])
    failures = [record for record in caplog.records if record.message.startswith("firecrawl_self_hosted_error")]
    assert len(failures) == (not fallback)
    if failures:
        assert failures[0].source_url == f"https://{domain}/contact"
    assert "private-value" not in caplog.text


@pytest.mark.asyncio
async def test_contacts_request_raw_html_and_business_search_excludes_aggregators(monkeypatch):
    import json
    from app.signals import research
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"rawHtml": '<a href="mailto:hello@clinic.example">Email</a>', "web": []}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(research, "_pace_firecrawl", lambda *args: None)
    provider = research.HttpResearchProvider("firecrawl", "synthetic")
    assert "mailto:" in await provider.scrape("https://clinic.example/contact", main_only=False)
    await provider.search('site:uk.linkedin.com/in "designer"', exclude_domains=["yelp.com"])
    assert bodies[0]["formats"] == ["rawHtml"] and not bodies[0]["onlyMainContent"]
    assert "yelp.com" in bodies[1]["excludeDomains"] and "linkedin.com" not in bodies[1]["excludeDomains"]


@pytest.mark.asyncio
async def test_quality_scrape_requests_main_content_without_navigation(monkeypatch):
    from app.signals import research
    bodies = []
    async def post(self, client, path, body):
        bodies.append(body)
        return httpx.Response(200, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/scrape"),
            json={"success": True, "data": {"markdown": "We struggle to cover last-minute staff shifts."}})
    monkeypatch.setattr(research.HttpResearchProvider, "_firecrawl_post", post)
    result = await research.HttpResearchProvider("firecrawl", "synthetic").scrape("https://restaurant.example/")
    assert result == "We struggle to cover last-minute staff shifts."
    assert bodies[0]["onlyMainContent"] is True and bodies[0]["formats"] == ["markdown"]
    assert bodies[0]["excludeTags"] == ["nav", "header", "footer", "aside"]


@pytest.mark.asyncio
async def test_structured_registries_are_bounded_and_do_not_guess_websites(monkeypatch):
    from app.signals import research
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == "overpass-api.de":
            return httpx.Response(200, json={"elements": [
                {"type": "node", "id": 1, "tags": {"name": "Austin Dental", "website": "austin.example", "contact:email": "office@austin.example", "phone": "5125550100"}},
                {"type": "node", "id": 2, "tags": {"name": "Chain", "website": "chain.example", "brand": "Chain"}}]})
        if request.url.host == "npiregistry.cms.hhs.gov":
            return httpx.Response(200, json={"results": [{"enumeration_type": "NPI-2", "number": "1234567890", "basic": {"organization_name": "Austin Dental LLC"}, "addresses": [{"address_purpose": "LOCATION", "city": "Austin", "state": "TX", "telephone_number": "5125550100"}], "taxonomies": [{"desc": "Dentist"}]}]})
        return httpx.Response(200, json={"items": [{"title": "MY STUDIO LTD", "company_number": "01234567", "company_status": "active", "company_type": "ltd"}, {"title": "MY STUDIO SERVICES LTD", "company_number": "other", "company_status": "active", "company_type": "ltd"}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(research, "_pace_structured", lambda *args: None)
    osm = await research.StructuredBusinessProvider("overpass", state="TX").search("Austin")
    assert len(osm) == 1 and osm[0]["url"] == "https://austin.example" and osm[0]["attribution"] == "© OpenStreetMap contributors"
    assert b'%5B%21%22brand%22%5D' in requests[0].content and "CompanyOS/" in requests[0].headers["User-Agent"]
    npi = await research.StructuredBusinessProvider("npi", state="TX").search("Austin")
    assert npi[0]["phone"] == "5125550100" and "provider-view/1234567890" in npi[0]["url"]
    assert "enumeration_type=NPI-2" in str(requests[1].url) and "version=2.1" in str(requests[1].url)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "synthetic")
    companies = await research.CompaniesHouseProvider().search("My Studio Ltd")
    assert len(companies) == 1 and companies[0]["subscriber"] == "corporate"


def test_search_preserves_dates_without_markdown_and_avoids_image_truncation():
    evidence = "Our swimming pools host classes for children in Bangalore."
    rows = _sources_from_response("firecrawl", {"success": True, "data": {"web": [
        {"url": "https://forum.example/need", "publishedTime": "2026-10-01", "metadata": {"og:published_time": "2026-10-01"}},
        {"url": "https://academy.example/", "markdown": "![map](data:image/png;base64," + "a" * 25000 + ")\n" + evidence}]}})
    assert rows[0]["metadata"] == {"publishedTime": "2026-10-01", "og:published_time": "2026-10-01"}
    assert rows[0]["observed_at"] and "content" not in rows[0]
    assert rows[1]["content"].strip() == evidence


@pytest.fixture(autouse=True)
def current_provider(monkeypatch):
    from app.signals import research
    from app.signals.research import HttpResearchProvider
    monkeypatch.setitem(globals(), "HttpResearchProvider", HttpResearchProvider)
    monkeypatch.setattr(research, "_FIRECRAWL_NEXT_CALL", {})
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_LOCKS", {})
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_INDEX", {})
    monkeypatch.delenv("FIRECRAWL_SELF_HOSTED_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_SELF_HOSTED_TOKEN", raising=False)
    for name in list(research.os.environ):
        if name.startswith("FIRECRAWL_API_KEY_"):
            monkeypatch.delenv(name)


@pytest.fixture()
def firecrawl_clock(monkeypatch):
    from app.signals import research
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setenv("FIRECRAWL_MIN_INTERVAL_SECONDS", "1.2")
    monkeypatch.delenv("REDIS_URL", raising=False)
    now, waits = [100.0], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(research, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0], sleep=sleep))
    monkeypatch.setattr(research, "_shared_firecrawl_wait", lambda *args: False)
    return now, waits


@pytest.mark.asyncio
async def test_search_inline_markdown_metadata_and_usage_are_bounded(monkeypatch, firecrawl_clock):
    requests = []
    timeouts = []
    def respond(request):
        import json
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "creditsUsed": 3, "data": {"web": [{
            "url": "https://forum.example/need", "title": "Need", "description": "Snippet", "markdown": "x" * 15000,
            "metadata": {"publishedTime": "2026-10-01"}}]}})
    original = httpx.AsyncClient
    def client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    rows = await HttpResearchProvider("firecrawl", "synthetic").search("dentist missed appointments", include_content=True, tbs="qdr:m3", limit=2)
    assert requests[0]["scrapeOptions"] == {"formats": ["markdown"], "onlyMainContent": True}
    assert "linkedin.com" in requests[0]["excludeDomains"]
    assert requests[0]["limit"] == 2 and requests[0]["timeout"] == 60000 and timeouts[0] == 65
    assert len(rows[0]["content"]) == 15000 and rows[0]["metadata"]["publishedTime"] == "2026-10-01"
    assert rows.usage == {"creditsUsed": 3} and rows[0]["snippet"] == "Snippet"
    await HttpResearchProvider("firecrawl", "synthetic").search("legacy search")
    assert "scrapeOptions" not in requests[1]
    assert "timeout" not in requests[1] and timeouts[1] == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 6, True, "2"])
async def test_inline_search_limit_cannot_escape_the_reserved_read_budget(limit):
    with pytest.raises(ValueError, match="invalid search limit"):
        await HttpResearchProvider("firecrawl", "synthetic", limit=5).search("community", include_content=True, limit=limit)


@pytest.mark.asyncio
async def test_inline_response_is_capped_even_if_server_ignores_limit(monkeypatch, firecrawl_clock):
    original = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, json={"success": True, "creditsUsed": 3, "data": {"web": [
            {"url": f"https://forum.example/{index}", "markdown": "Fetched source"} for index in range(5)]}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    rows = await HttpResearchProvider("firecrawl", "synthetic").search("community", include_content=True, limit=2)
    assert len(rows) == 2 and all(row["content"] == "Fetched source" for row in rows)
    assert rows.usage == {"creditsUsed": 3}


@pytest.mark.asyncio
async def test_firecrawl_search_and_scrape_share_pacing_across_instances(monkeypatch, firecrawl_clock):
    now, waits = firecrawl_clock
    calls = []
    def handler(request):
        calls.append(now[0])
        assert len([t for t in calls if now[0] - t < 10]) <= 10
        return httpx.Response(200, json={"success": True, "data": {"web": [], "markdown": "Page"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    for index in range(12):
        provider = HttpResearchProvider("firecrawl", "k", limit=10)
        if index % 2:
            await provider.scrape("https://example.org")
        else:
            await provider.search("customer conversations", include_content=True, tbs="qdr:y", limit=2)
    assert calls == pytest.approx([100 + i * 1.2 for i in range(12)])
    assert len(waits) == 11


@pytest.mark.asyncio
async def test_firecrawl_rotates_search_and_scrape_with_independent_pacing(monkeypatch, firecrawl_clock, caplog):
    import json
    now, waits = firecrawl_clock
    monkeypatch.setenv("FIRECRAWL_API_KEY_2", "second-private-key")
    calls = []
    def handler(request):
        calls.append((now[0], request.headers["Authorization"]))
        if request.url.path.endswith("/search"):
            assert json.loads(request.content)["scrapeOptions"] == {"formats": ["markdown"], "onlyMainContent": True}
        return httpx.Response(200, json={"success": True, "data": {"web": [], "markdown": "Page"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    for index in range(6):
        provider = HttpResearchProvider("firecrawl", "first-private-key")
        if index % 2:
            await provider.scrape("https://example.org")
        else:
            await provider.search("customer conversations", include_content=True, tbs="qdr:y", limit=2)
    assert [timestamp for timestamp, _ in calls] == pytest.approx([100, 100, 101.2, 101.2, 102.4, 102.4])
    assert [key for _, key in calls] == ["Bearer first-private-key", "Bearer second-private-key"] * 3
    assert waits == pytest.approx([1.2, 1.2])
    assert "first-private-key" not in caplog.text and "second-private-key" not in caplog.text


def test_unspent_pacing_is_retryable_not_uncertain():
    from app.research.metered import _failure
    from app.signals.research import FirecrawlLimiterUnavailable
    assert _failure(FirecrawlLimiterUnavailable()) == ("retryable", "research_provider_slot_unavailable")


def test_firecrawl_numbered_keys_are_sorted_nonempty_and_deduplicated(monkeypatch):
    from app.signals import research
    for name, value in {"FIRECRAWL_API_KEY_10": "tenth", "FIRECRAWL_API_KEY_3": "third",
                        "FIRECRAWL_API_KEY_2": "second", "FIRECRAWL_API_KEY_4": " ",
                        "FIRECRAWL_API_KEY_5": "second", "FIRECRAWL_API_KEY_1": "ignored",
                        "FIRECRAWL_API_KEY_BAD": "ignored"}.items():
        monkeypatch.setenv(name, value)
    assert [research._firecrawl_key("first") for _ in range(8)] == ["first", "second", "third", "tenth"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retry_after,delay", [
    (429, "4", 4), (429, "Thu, 01 Jan 1970 00:01:44 GMT", 4),
    (429, None, 6.5), (429, "invalid", 6.5), (429, "900", 60), (503, "4", 4),
])
async def test_firecrawl_backoff_only_delays_the_rejected_key(monkeypatch, firecrawl_clock, status, retry_after, delay):
    now, waits = firecrawl_clock
    monkeypatch.setenv("FIRECRAWL_API_KEY_2", "second")
    calls = []
    def handler(request):
        calls.append((now[0], request.headers["Authorization"]))
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": retry_after} if retry_after else {})
        return httpx.Response(200, json={"success": True, "data": {"web": [], "markdown": "Page"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(httpx.HTTPStatusError):
        await HttpResearchProvider("firecrawl", "first").search("bakery owners")
    await HttpResearchProvider("firecrawl", "first").scrape("https://example.org")
    assert calls == [(100, "Bearer first"), (100, "Bearer second")]
    assert waits == []
    await HttpResearchProvider("firecrawl", "first").search("bakery owners")
    assert calls[-1] == (100 + delay, "Bearer first")
    assert waits == [delay]


@pytest.mark.asyncio
async def test_firecrawl_both_keys_keep_their_own_backoff(monkeypatch, firecrawl_clock):
    now, waits = firecrawl_clock
    monkeypatch.setenv("FIRECRAWL_API_KEY_2", "second")
    calls = []
    def handler(request):
        calls.append((now[0], request.headers["Authorization"]))
        if len(calls) <= 2:
            return httpx.Response(429, headers={"Retry-After": "4" if len(calls) == 1 else "9"})
        return httpx.Response(200, json={"success": True, "data": {"web": []}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    provider = HttpResearchProvider("firecrawl", "first")
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await provider.search("bakery owners")
    await provider.search("bakery owners")
    await provider.search("bakery owners")
    assert calls == [(100, "Bearer first"), (100, "Bearer second"),
                     (104, "Bearer first"), (109, "Bearer second")]
    assert waits == [4, 5]


@pytest.mark.asyncio
async def test_firecrawl_waiting_key_does_not_lock_out_the_other(monkeypatch, firecrawl_clock):
    from app.signals import research
    monkeypatch.setenv("FIRECRAWL_API_KEY_2", "second")
    entered, release = threading.Event(), threading.Event()
    calls, slots = [], []
    first_slot = research._firecrawl_slot_key("first")
    def wait(delay, slot_key):
        slots.append(slot_key)
        if slot_key == first_slot:
            entered.set()
            assert release.wait(5)
        return True
    def handler(request):
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json={"success": True, "data": {"web": []}})
    original = httpx.AsyncClient
    monkeypatch.setattr(research, "_shared_firecrawl_wait", wait)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    provider = HttpResearchProvider("firecrawl", "first")
    first = asyncio.create_task(provider.search("first query"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.wait_for(provider.search("second query"), timeout=2)
        assert calls == ["Bearer second"] and not first.done()
    finally:
        release.set()
        await first
    assert slots == [first_slot, research._firecrawl_slot_key("second")]
    assert calls == ["Bearer second", "Bearer first"]


@pytest.mark.asyncio
async def test_firecrawl_429_escapes_immediately_without_retry_or_sleep(monkeypatch, firecrawl_clock):
    now, waits = firecrawl_clock
    calls = []
    def handler(request):
        calls.append((now[0], request.content.decode()))
        return httpx.Response(429, headers={"Retry-After": "4"}) if len(calls) == 1 else httpx.Response(200, json={"success": True, "data": {"web": []}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(httpx.HTTPStatusError) as error:
        await HttpResearchProvider("firecrawl", "k", limit=10).search("startups series A")
    assert error.value.response.headers["Retry-After"] == "4"
    assert [t for t, _ in calls] == [100]
    assert '"limit":10' in calls[0][1]
    assert waits == []


@pytest.mark.asyncio
async def test_firecrawl_429_is_one_dial(monkeypatch, firecrawl_clock):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(httpx.HTTPStatusError):
        await HttpResearchProvider("firecrawl", "k").search("bakery owners")
    assert len(calls) == 1


def test_firecrawl_retry_after_accepts_http_date_and_bounds_invalid_delay(monkeypatch, firecrawl_clock):
    from app.signals import research
    assert research._firecrawl_retry_after("Thu, 01 Jan 1970 00:01:44 GMT") == 4
    for value, expected in [("inf", 6.5), ("nan", 6.5), ("bad", 6.5), (None, 6.5), ("-1", 1), ("900", 60)]:
        assert research._firecrawl_retry_after(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["search", "scrape"])
@pytest.mark.parametrize("retry_after,success,expected_calls", [("2", True, 3), ("1", False, 3), ("60", True, 1)])
async def test_discovery_firecrawl_429_retries_are_bounded(monkeypatch, firecrawl_clock, operation, retry_after, success, expected_calls):
    from app.signals import research
    now, waits = firecrawl_clock
    calls = []
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    def handler(request):
        calls.append(request)
        if len(calls) < 3 or not success:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"success": True, "data": {"web": [], "markdown": "Published contact"}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(research.asyncio, "sleep", sleep)
    provider = HttpResearchProvider("firecrawl", "synthetic", rate_limit_retries=2)
    callback = provider.search("dental practice contact") if operation == "search" else provider.scrape("https://practice.example/contact")
    if success and expected_calls == 3:
        await callback
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await callback
    assert len(calls) == expected_calls
    assert len({request.headers["Authorization"] for request in calls}) == 1
    assert sum(waits) <= 15 and len(waits) <= 2
    if expected_calls == 3:
        assert waits == pytest.approx([max(1.2, float(retry_after)), max(2, float(retry_after))])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_firecrawl_rejection_headers_precede_body_timeout(monkeypatch, firecrawl_clock, status):
    from app.research.metered import _failure
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadTimeout("rejected response body timed out")
            yield b""
    original = httpx.AsyncClient
    with monkeypatch.context() as transport_patch:
        transport_patch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(
                status, headers={"Retry-After": "4"}, stream=SlowBody())), **kwargs))
        with pytest.raises(httpx.HTTPStatusError) as error:
            await HttpResearchProvider("firecrawl", "k").search("bakery owners")
    assert _failure(error.value) == ("retryable", "research_provider_rate_limited")


@pytest.mark.asyncio
async def test_firecrawl_pacing_deadline_is_unspent_before_any_http(monkeypatch):
    from app.research.metered import _failure
    from app.signals import research
    async def wait_for_slot(*args):
        await asyncio.Event().wait()
    requests = []
    original = httpx.AsyncClient
    with monkeypatch.context() as transport_patch:
        transport_patch.setattr(research.asyncio, "to_thread", wait_for_slot)
        transport_patch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: requests.append(request)), **kwargs))
        with pytest.raises(research.FirecrawlLimiterUnavailable) as error:
            await asyncio.wait_for(HttpResearchProvider("firecrawl", "k").search("bakery"), timeout=0.01)
    assert _failure(error.value)[0] == "retryable" and requests == []


@pytest.mark.parametrize("url,headers", [
    ("https://api.firecrawl.dev/v2/search", {}),
    ("https://api.exa.ai/search", {"Retry-After": "4"}),
])
def test_only_firecrawl_503_with_retry_after_is_definitive(url, headers):
    from app.research.metered import _failure
    response = httpx.Response(503, headers=headers, request=httpx.Request("POST", url))
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()
    assert _failure(error.value)[0] == "uncertain"


@pytest.mark.parametrize("value,expected", [(None, 6.5), ("2.5", 2.5), ("0", 6.5), ("nan", 6.5), ("invalid", 6.5)])
def test_firecrawl_pacing_env_and_safe_default(monkeypatch, firecrawl_clock, value, expected):
    from app.signals import research
    if value is None:
        monkeypatch.delenv("FIRECRAWL_MIN_INTERVAL_SECONDS")
    else:
        monkeypatch.setenv("FIRECRAWL_MIN_INTERVAL_SECONDS", value)
    research._pace_firecrawl()
    research._pace_firecrawl()
    assert firecrawl_clock[1] == [expected]


def test_firecrawl_v2_web_results_are_normalized():
    result = _sources_from_response("firecrawl", {"success": True, "data": {"web": [
        {"url": "https://example.org/product", "title": "Example", "description": "Observed result"},
        {"url": "", "title": "Missing URL"},
    ]}})
    assert [{key: value for key, value in row.items() if key != "observed_at"} for row in result] == [{"url": "https://example.org/product", "title": "Example",
                       "snippet": "Observed result", "provider": "firecrawl", "metadata": {}}]
    assert result.provider_rows == 2
    assert result.rejected_missing_url == 1
    assert result.rejected_shape == 0
    assert result[0]["observed_at"]
    with pytest.raises(ValueError, match="not successful"):
        _sources_from_response("firecrawl", {"success": False, "data": {"web": []}})


@pytest.mark.asyncio
async def test_search_time_filter_and_usage_preserve_list_and_string_callers(monkeypatch):
    import json
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"success": True, "creditsUsed": 2, "data": {"web": [
                {"url": "https://example.org/post", "published_at": "2026-10-01", "description": "Published need"}]}})
        return httpx.Response(200, json={"success": True, "creditsUsed": 1, "data": {
            "markdown": "Published text", "metadata": {"datePublished": "2026-10-01"}}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("app.signals.research._pace_firecrawl", lambda *args: None)
    provider = HttpResearchProvider("firecrawl", "synthetic-key", limit=5)
    results = await provider.search("need", tbs="cdr:1,cd_min:07/11/2026,cd_max:10/09/2026")
    text = await provider.scrape("https://example.org/post")
    assert captured[0]["tbs"].startswith("cdr:1,") and "scrapeOptions" not in captured[0]
    assert isinstance(results, list) and results.usage == {"creditsUsed": 2} and results[0]["published_at"] == "2026-10-01"
    assert isinstance(text, str) and text == "Published text" and text.metadata["datePublished"] == "2026-10-01"
    assert text.usage == {"creditsUsed": 1}
    with pytest.raises(ValueError, match="time filter"):
        await provider.search("need", tbs="bad filter\n")


@pytest.mark.asyncio
async def test_firecrawl_search_uses_v2_and_keeps_key_server_side(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["authorization"] = request.headers["Authorization"]
        return httpx.Response(200, json={"success": True, "data": {"web": [
            {"url": "https://example.org", "title": "Example", "description": "Real result"}
        ]}})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    result = await HttpResearchProvider("firecrawl", "test-private-key").search("market demand")
    assert captured["url"] == "https://api.firecrawl.dev/v2/search"
    assert '"sources":["web"]' in captured["body"]
    assert captured["authorization"] == "Bearer test-private-key"
    assert result[0]["url"] == "https://example.org"
    assert "test-private-key" not in str(result)


def test_firecrawl_parser_counts_rejected_rows_without_retaining_their_text():
    result = _sources_from_response("firecrawl", {"success": True, "data": {"web": [
        {"title": "private customer text without URL"},
        "private malformed row",
        {"url": "https://example.org/valid", "title": "Valid"},
    ]}})
    assert (result.provider_rows, result.rejected_missing_url, result.rejected_shape) == (3, 1, 1)
    assert "private" not in str(result)


@pytest.mark.asyncio
async def test_firecrawl_scrape_uses_v2_markdown(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"success": True, "data": {"markdown": "# Pricing $12"}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await HttpResearchProvider("firecrawl", "k").scrape("https://example.org/p") == "# Pricing $12"
    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape" and '"markdown"' in captured["body"]
    with pytest.raises(ValueError):
        await HttpResearchProvider("exa", "k").scrape("https://example.org/p")
