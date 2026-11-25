import asyncio
import json
import threading
from contextlib import asynccontextmanager

import httpx
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["inventory.example", "trails.example"])
async def test_local_page_dns_cancellation_is_definitely_unspent(monkeypatch, tmp_path, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.research import local_providers, metered
    from app.signals import local_reader, research
    monkeypatch.setattr(local_providers, "_research_enabled", lambda: True)
    checking = asyncio.Event()
    calls = []

    async def queued_dns(callback, *args):
        checking.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(local_reader.asyncio, "to_thread", queued_dns)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
        pending = asyncio.create_task(local_reader.request(client, "scrape", {"url": f"https://{domain}/"}))
        await asyncio.wait_for(checking.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(research.FirecrawlLimiterUnavailable) as error:
            await pending
    assert metered._failure(error.value) == ("retryable", "research_provider_slot_unavailable")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("domain,audience", [
    ("inventory.example", "Warehouse purchasing teams"),
    ("trails.example", "Weekend trail runners"),
])
@pytest.mark.parametrize("limit", [2, 3])
async def test_firecrawl_process_limit_excludes_queued_time(monkeypatch, tmp_path, domain, audience, limit):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_URL", "https://crawl.example")
    monkeypatch.setenv("FIRECRAWL_SELF_HOSTED_TOKEN", "synthetic")
    monkeypatch.delenv("WEB_SEARCH_OPENAI_URL", raising=False)
    load_company_os_app()
    from app.signals import research
    from app.core.local_mode import is_local
    if is_local():
        from app.core import local_capabilities, local_secrets
        from app.signals import local_reader
        sections = {"search": {"provider": "firecrawl", "firecrawl_url": "https://crawl.example"}}
        monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": sections.get(section, {}), "sources": {}})
        monkeypatch.setattr(local_secrets, "get_secret", lambda section, name:
            "synthetic" if (section, name) == ("search", "firecrawl_key") else None)
        monkeypatch.setattr(local_capabilities, "read_settings", local_secrets.read_settings)
        monkeypatch.setattr(local_capabilities, "get_secret", local_secrets.get_secret)
        def forbidden(*args, **kwargs):
            pytest.fail("The Firecrawl concurrency test attempted the built-in transport")
        monkeypatch.setattr(local_reader, "_basic_read", forbidden)
        from core.ssrf import assert_safe_remote_url
        real_to_thread = asyncio.to_thread
        async def deterministic_dns(callback, *args, **kwargs):
            if callback is assert_safe_remote_url:
                return callback(*args, **kwargs)
            return await real_to_thread(callback, *args, **kwargs)
        monkeypatch.setattr(asyncio, "to_thread", deterministic_dns)
    monkeypatch.setattr(research, "_FIRECRAWL_PROCESS_SLOTS", threading.BoundedSemaphore(limit))

    lock = threading.Lock()
    active = peak = 0
    requests, queued = [], []
    real_timeout = asyncio.timeout
    monkeypatch.setattr(research.asyncio, "timeout", lambda seconds: real_timeout(0.5))

    async def respond(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            requests.append(request)
        try:
            await asyncio.sleep(0.15)
            data = {"markdown": audience, "metadata": {"url": json.loads(request.content)["url"]}} if request.url.path.endswith("scrape") else {
                "web": [{"url": f"https://{domain}/", "title": audience}]}
            return httpx.Response(200, json={"success": True, "data": data})
        finally:
            with lock:
                active -= 1

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        real_client(transport=httpx.MockTransport(respond), **kwargs))

    async def journey(start, count):
        provider = research.HttpResearchProvider("firecrawl", "")
        gate = asyncio.Semaphore(6)

        async def scrape(index):
            async with gate:
                began = asyncio.get_running_loop().time()
                result = await provider.scrape(f"https://{domain}/{index}")
                with lock:
                    queued.append(asyncio.get_running_loop().time() - began)
                assert result == audience
                return result

        return await asyncio.gather(*(scrape(index) for index in range(start, start + count)),
            provider.search(audience))

    async def run_journey(start, count):
        return await asyncio.to_thread(lambda: asyncio.run(journey(start, count)))

    results = await asyncio.gather(run_journey(0, 4), run_journey(4, 3), run_journey(7, 3))
    assert sum(len(batch) for batch in results) == 13
    assert peak == limit and active == 0
    assert max(queued) > 0.5
    assert sum(request.url.path.endswith("scrape") for request in requests) == 10
    assert all(request.url.host == "crawl.example" for request in requests)
    assert {json.loads(request.content).get("query") for request in requests
        if request.url.path.endswith("search")} == {audience}

    slots = research._FIRECRAWL_PROCESS_SLOTS
    waiting = asyncio.Event()
    original_slot = research._firecrawl_slot

    @asynccontextmanager
    async def queued_slot():
        waiting.set()
        async with original_slot():
            yield

    monkeypatch.setattr(research, "_firecrawl_slot", queued_slot)
    acquired = 0
    try:
        for _ in range(limit):
            assert slots.acquire(blocking=False)
            acquired += 1
        pending = asyncio.create_task(research.HttpResearchProvider("firecrawl", "").scrape(
            f"https://{domain}/queued"))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(research.FirecrawlLimiterUnavailable) as error:
            await pending
        from app.research.metered import _failure
        assert _failure(error.value) == ("retryable", "research_provider_slot_unavailable")
        assert len(requests) == 13 and not slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            slots.release()
