import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(params=[
    ("warehouse dispatch tools", "https://dispatch.example/guide"),
    ("weekend cycling routes", "https://cycling.example/guide"),
], ids=["business", "consumer"])
def local(request, monkeypatch, tmp_path):
    from ddgs.engines import ENGINES

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/local.db")
    load_company_os_app()
    from app.core import local_secrets
    from app.research import local_providers
    from app.credits import metered

    monkeypatch.setattr(local_providers, "time", SimpleNamespace(
        monotonic=local_providers.time.monotonic, sleep=lambda delay: None))
    monkeypatch.setattr(local_providers, "_builtin_next_at", 0)
    monkeypatch.setattr(local_providers, "_builtin_cooldowns", {})
    values, secrets, calls = {}, {}, []
    actual_call = metered.call
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
        "values": values, "sources": {field: "app" for field in values},
    })
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, name: secrets.get(name))

    async def call(callback, **kwargs):
        calls.append(kwargs)
        return await callback()

    monkeypatch.setattr(metered, "call", call)
    backends = [name for name in dict.fromkeys(
        ("bing", "yahoo", "duckduckgo", "brave", "mojeek", *ENGINES["text"])) if name == "bing" or name in ENGINES["text"]]
    return SimpleNamespace(api=local_providers, values=values, secrets=secrets,
        calls=calls, actual_call=actual_call, query=request.param[0], url=request.param[1], backends=backends)


@pytest.mark.parametrize("provider", ["builtin", "tavily", "exa", "openai", "firecrawl"])
def test_saved_provider_resolves_and_keeps_source_receipts(local, monkeypatch, provider):
    from app.core import providers_local
    from app.signals import research

    local.values.update(provider=provider, firecrawl_url="http://localhost:3002")
    local.secrets.update(api_key="fixture-search-key", firecrawl_key="fixture-reader-key")
    config = SimpleNamespace(provider="openai", base_url="https://api.openai.com/v1",
        api_key=SimpleNamespace(get_secret_value=lambda: "fixture-model-key"), deployment_flash="saved-model",
        _local_role_models={"chat": "saved-chat-model"})
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    monkeypatch.setattr(research, "_ddgs_text", lambda query, **kwargs: [
        {"href": local.url, "title": "Guide", "body": query}])
    requests = []

    def handle(request):
        requests.append(request)
        rows = [{"url": local.url, "title": "Guide", "snippet": local.query}]
        return httpx.Response(200, json={"success": True, "data": rows, "results": rows,
            "output": [{"content": [{"type": "output_text", "text": json.dumps(rows)}]}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    rows = asyncio.run(local.api.local_search(local.query, limit=1))
    assert local.api.search_ready()
    assert [(row["url"], row["title"]) for row in rows] == [(local.url, "Guide")]
    assert all(row["fetched_at"] for row in rows)
    assert len(local.calls) == 1
    assert len(requests) == (0 if provider == "builtin" else 1)
    if provider != "builtin":
        assert str(requests[0].url) == {
            "tavily": "https://api.tavily.com/search", "exa": "https://api.exa.ai/search",
            "openai": "https://api.openai.com/v1/responses", "firecrawl": "http://localhost:3002/v2/search",
        }[provider]
    if provider == "openai":
        assert requests[0].headers["Authorization"] == "Bearer fixture-model-key"
        assert json.loads(requests[0].content)["model"] == "saved-chat-model"


def test_builtin_search_and_read_need_no_search_key_or_firecrawl(local, monkeypatch):
    from app.research import deep
    from app.signals import local_reader, research

    searches = []

    def search(query, **kwargs):
        searches.append((query, kwargs))
        return [{"href": local.url, "title": "Guide", "body": query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)
    reads = []

    def read(url, main_only):
        reads.append((url, main_only))
        return local_reader._page_response(url, local.query, "Guide")

    monkeypatch.setattr(local_reader, "_basic_read", read)
    rows = asyncio.run(local.api.local_search(local.query, limit=2))
    page = asyncio.run(local.api.local_read(rows[0]["url"]))
    assert page == local.query
    assert reads == [(local.url, True)]
    assert page.metadata["sourceURL"] == local.url and page.metadata["title"] == "Guide"
    assert page.metadata["fetched_at"]
    assert deep._web_provider() is not None and deep._scrape_provider(deep._web_provider()) is not None
    assert local.api.setup_reply() is None
    assert len(local.calls) == 2
    assert searches == [(local.query, {"limit": 2, "backend": backend, "timeout": 3}) for backend in local.backends]


@pytest.mark.parametrize("recovers", [False, True])
def test_builtin_falls_through_once_per_engine_and_gives_settings_next_step(local, monkeypatch, recovers, builtin_clock):
    from ddgs.exceptions import RatelimitException
    from app.signals import research

    attempts = []

    def search(query, **kwargs):
        attempts.append(query)
        if not recovers or len(attempts) < 3:
            raise RatelimitException("rate limit")
        return [{"href": local.url, "title": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    if recovers:
        assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url
    else:
        with pytest.raises(local.api.SearchEngineLimited, match="add.*search.*key.*Keys and connections"):
            asyncio.run(local.api.local_search(local.query, limit=1))
    assert len(attempts) == (3 if recovers else len(local.backends))
    assert builtin_clock[1] == [1] * len(attempts)


@pytest.fixture
def builtin_clock(local, monkeypatch):
    from datetime import timedelta

    clock, delays = [0.0], []
    now = local.api.utcnow()

    def sleep(delay):
        delays.append(delay)
        clock[0] += delay

    monkeypatch.setattr(local.api, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep), raising=False)
    monkeypatch.setattr(local.api, "utcnow", lambda: now + timedelta(seconds=clock[0]))
    return clock, delays


def test_builtin_walks_supported_engines_and_cools_failures(local, monkeypatch, builtin_clock, caplog):
    import logging
    from ddgs import engines
    from ddgs.exceptions import DDGSException
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": dict.fromkeys(
        ["startpage", "mojeek", "brave", "duckduckgo", "bing", "yahoo"])})
    attempts = []

    def search(query, *, backend, **kwargs):
        attempts.append(backend)
        if backend == "bing":
            raise TimeoutError(query)
        if backend == "duckduckgo":
            raise DDGSException("No results found.")
        if backend != "startpage":
            return []
        return [{"href": local.url, "title": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    with caplog.at_level(logging.INFO, logger=research.log.name):
        assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url
        expected = ["bing", "yahoo", "duckduckgo", "brave", "mojeek", "startpage"]
        assert attempts == expected
        assert asyncio.run(local.api.local_search(local.query + " alternatives", limit=1))[0]["url"] == local.url
        assert attempts == expected + expected[1:]
        builtin_clock[0][0] += 30 * 60
        assert asyncio.run(local.api.local_search(local.query + " comparisons", limit=1))[0]["url"] == local.url
        assert attempts == expected + expected[1:] + expected
    assert "engine=yahoo outcome=empty results=0" in caplog.text
    assert "engine=bing outcome=error results=0 kind=TimeoutError" in caplog.text
    assert "engine=duckduckgo outcome=empty results=0" in caplog.text
    assert "engine=bing outcome=cooldown results=0" in caplog.text
    assert "engine=startpage outcome=success results=1" in caplog.text
    assert local.query not in caplog.text and local.url not in caplog.text


def test_builtin_persists_refusals_and_challenges_but_not_errors(local, monkeypatch, builtin_clock):
    from ddgs import engines
    from app.gtm.email_learning import cache_key, cache_save, cached
    from app.signals import research

    order = ["bing", "yahoo", "duckduckgo", "brave", "mojeek", "google"]
    monkeypatch.setattr(engines, "ENGINES", {"text": dict.fromkeys(reversed(order))})
    clock, _ = builtin_clock
    calls = []

    def fake_ddgs(query, *, limit, backend, **kwargs):
        calls.append(backend)
        if backend in {"bing", "yahoo"}:
            return [{"href": f"{local.url}/{backend}", "title": local.query}]
        if backend in {"duckduckgo", "google"}:
            raise httpx.HTTPStatusError("blocked", request=httpx.Request("GET", local.url),
                response=httpx.Response({"duckduckgo": 202, "google": 403}[backend]))
        if backend == "mojeek":
            raise local.api.SearchEngineLimited
        if backend == "brave":
            raise TimeoutError

    monkeypatch.setattr(research, "_ddgs_text", fake_ddgs)
    asyncio.run(local.api.local_search(local.query, limit=9))
    assert calls == order
    cooldown_key = cache_key("builtin", "engine-cooldown-v2", "text")
    persisted = cached(cooldown_key)
    assert {backend: health[1] for backend, health in persisted.items()} == {
        "duckduckgo": "refused", "mojeek": "challenge", "google": "refused"}
    cache_save(cooldown_key, persisted | {"brave": [local.api.utcnow().timestamp() + 1800, "error"]})

    monkeypatch.setattr(local.api, "_builtin_cooldowns", {})
    calls.clear()
    with local.api.collect_search_diagnostics() as diagnostics:
        asyncio.run(local.api.local_search(local.query + " alternatives", limit=9))
    assert calls == ["bing", "yahoo", "brave"]
    [query] = diagnostics.values()
    assert query["engines"]["duckduckgo"] == {"outcome": "cooldown", "results": 0, "cause": "refused"}
    assert query["engines"]["mojeek"] == {"outcome": "cooldown", "results": 0, "cause": "challenge"}

    clock[0] += 29 * 60
    calls.clear()
    asyncio.run(local.api.local_search(local.query + " comparisons", limit=9))
    assert calls == ["bing", "yahoo", "brave"]
    clock[0] += 2 * 60
    calls.clear()
    asyncio.run(local.api.local_search(local.query + " guides", limit=9))
    assert calls == order


def test_builtin_transient_backoff_doubles_caps_and_resets(local, monkeypatch, builtin_clock):
    from ddgs import engines
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": {"bing": object}})
    clock, _ = builtin_clock
    calls = []
    failure = httpx.TimeoutException

    def fake_ddgs(query, **kwargs):
        calls.append(query)
        if failure is not None:
            raise failure("fixture network error")
        return [{"href": local.url, "title": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", fake_ddgs)

    def search(query):
        return asyncio.run(local.api.local_search(query, limit=1))

    with local.api.collect_search_diagnostics(customer_run_id="backoff") as diagnostics:
        with pytest.raises(local.api.BuiltinSearchUnavailable):
            search(local.query)
        for count, delay in enumerate((60, 120, 240, 480, 960, 1800, 1800), start=1):
            clock[0] += delay - 1
            with pytest.raises(local.api.BuiltinSearchUnavailable):
                search(local.query)
            assert len(calls) == count
            [skipped] = diagnostics.values()
            clock[0] += 1
            if count == 7:
                failure = None
                assert search(local.query)[0]["url"] == local.url
            else:
                with pytest.raises(local.api.BuiltinSearchUnavailable):
                    search(local.query)
            assert len(calls) == count + 1
            assert skipped["engines"]["bing"] == {"outcome": "cooldown", "results": 0, "cause": "backoff"}
            assert skipped["reason"] == "search_unavailable"

        failure = ConnectionResetError
        with pytest.raises(local.api.BuiltinSearchUnavailable):
            search(local.query + " alternatives")
        clock[0] += 60
        failure = None
        assert search(local.query + " alternatives")[0]["url"] == local.url


def test_builtin_tries_installed_bing_before_yahoo_even_when_ddgs_disables_it(local, monkeypatch, builtin_clock):
    from ddgs import engines
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": {"yahoo": object}})
    attempts = []

    def search(query, *, backend, **kwargs):
        attempts.append(backend)
        return [{"href": local.url, "title": "Published contact", "body": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url
    assert attempts == ["bing"]


def test_builtin_bing_reads_published_results_when_disabled_in_ddgs(local, monkeypatch):
    import base64
    from ddgs.http_client import HttpClient

    destination = base64.urlsafe_b64encode(local.url.encode()).decode().rstrip("=")
    page = (f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{destination}">'
        f'{local.query}</a></h2><p>Published contact page</p></li>')
    monkeypatch.setattr(HttpClient, "request", lambda *args, **kwargs: SimpleNamespace(status_code=200, text=page))
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url


@pytest.mark.parametrize("found,requested,provider,expected", [
    (0, 3, "builtin", True), (2, 3, "builtin", True), (3, 3, "builtin", False),
    (1, 1, "builtin", False), (0, 3, "tavily", False),
])
def test_thin_keyless_contacts_name_search_key_screen_without_blame(local, found, requested, provider, expected):
    local.values["provider"] = provider
    notice = local.api.customer_search_notice(found, requested)
    assert bool(notice) == expected
    if expected:
        assert "machine" not in notice and "laptop" not in notice
        assert "Keys and connections > Search" in notice
        assert "Tavily or Exa" in notice and "provider" not in notice
        assert len(notice.split(". ")) == 2


@pytest.mark.parametrize("raises_empty", [False, True], ids=["empty-list", "empty-exception"])
def test_a_valid_empty_query_does_not_cool_down_other_queries(local, monkeypatch, builtin_clock, caplog, raises_empty):
    import logging
    from ddgs import engines
    from ddgs.exceptions import DDGSException
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": dict.fromkeys(["startpage", "bing", "yahoo"])})
    attempts = []

    def search(query, *, backend, **kwargs):
        attempts.append(backend)
        if query != local.query:
            return [{"href": local.url, "title": local.query}]
        if raises_empty:
            raise DDGSException("No results found.")
        return []

    monkeypatch.setattr(research, "_ddgs_text", search)
    with caplog.at_level(logging.INFO, logger=research.log.name):
        assert asyncio.run(local.api.local_search(local.query, limit=1)) == []
        monkeypatch.setattr(local.api, "_builtin_cooldowns", {})
        assert asyncio.run(local.api.local_search(local.query + " alternatives", limit=1))[0]["url"] == local.url
    assert attempts == ["bing", "yahoo", "startpage", "bing"]
    assert all(f"engine={backend} outcome=empty results=0" in caplog.text for backend in attempts)
    assert local.query not in caplog.text


def test_builtin_empty_query_leaves_untried_engines_available(local, monkeypatch, builtin_clock):
    from ddgs import engines
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": dict.fromkeys(["yahoo", "brave"])})
    attempts = []

    def search(query, *, backend, **kwargs):
        attempts.append((query, backend))
        if query == local.query:
            return []
        return [{"href": local.url, "title": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    assert asyncio.run(local.api.local_search(local.query, limit=1)) == []
    engines.ENGINES["text"]["startpage"] = object
    broader = local.query + " alternatives"
    assert asyncio.run(local.api.local_search(broader, limit=1))[0]["url"] == local.url
    assert attempts == [(local.query, "bing"), (local.query, "yahoo"), (local.query, "brave"), (broader, "bing")]


def test_builtin_refusal_falls_back_and_cools_down_across_six_searches(local, monkeypatch, builtin_clock, caplog):
    import logging
    from urllib.parse import urlsplit
    from ddgs import engines
    from ddgs.http_client import HttpClient
    from app.signals import research

    attempts = []
    monkeypatch.setattr(engines, "ENGINES", {"text": {
        name: engines.ENGINES["text"][name] for name in ("yahoo", "brave")}})

    def request(self, method, url, **kwargs):
        backend = {"search.brave.com": "brave", "www.bing.com": "bing", "search.yahoo.com": "yahoo"}[urlsplit(url).hostname]
        attempts.append((backend, builtin_clock[0][0]))
        return SimpleNamespace(status_code=200 if backend == "brave" else 429, text=(
            f'<div data-type="web"><a href="{local.url}"><div class="title">Guide</div></a>'
            f'<div class="snippet"><div class="content">{local.query}</div></div></div>'))

    monkeypatch.setattr(HttpClient, "request", request)
    with caplog.at_level(logging.INFO, logger=research.log.name):
        for number in range(6):
            rows = asyncio.run(local.api.local_search(local.query + str(number), limit=1))
            assert rows[0]["url"] == local.url
    assert [backend for backend, _ in attempts] == ["bing", "yahoo"] + ["brave"] * 6
    assert all(later[1] - earlier[1] >= 1 for earlier, later in zip(attempts, attempts[1:]))
    assert caplog.text.count("engine=yahoo status=429") == 1
    assert caplog.text.count("engine=bing status=429") == 1
    assert caplog.text.count("engine=brave status=200") == 6


def test_builtin_identical_query_cache_survives_reopen_and_expires(local, monkeypatch, tmp_path, builtin_clock):
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.gtm import email_learning
    from app.gtm.shared_models import PublicResearchCache
    from app.signals import research

    database = f"sqlite:///{tmp_path}/search-cache.db"
    engine = create_engine(database)
    PublicResearchCache.__table__.create(engine)
    monkeypatch.setattr(email_learning, "db", lambda: sessionmaker(bind=engine))
    attempts = []

    def search(query, **kwargs):
        attempts.append(query)
        return [{"href": local.url, "title": "Guide", "body": query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    first = asyncio.run(local.api.local_search(local.query, limit=1))
    engine.dispose()
    engine = create_engine(database)
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == first[0]["url"]
    assert len(attempts) == 1
    now = local.api.utcnow()
    monkeypatch.setattr(local.api, "utcnow", lambda: now + timedelta(minutes=16))
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url
    assert len(attempts) == 2
    engine.dispose()


def test_builtin_cache_failure_does_not_stop_search(local, monkeypatch, builtin_clock):
    from sqlalchemy.exc import OperationalError
    from app.gtm import email_learning
    from app.signals import research

    def unavailable(*args):
        raise OperationalError("cache busy", {}, RuntimeError("database busy"))

    monkeypatch.setattr(email_learning, "cached", unavailable)
    monkeypatch.setattr(email_learning, "cache_save", unavailable)
    monkeypatch.setattr(research, "_ddgs_text", lambda query, **kwargs: [
        {"href": local.url, "title": "Guide", "body": query}])
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url


def test_builtin_thin_search_adds_unique_fallback_sources(local, monkeypatch, builtin_clock):
    from app.signals import research

    backends = []

    def search(query, *, backend=None, **kwargs):
        backends.append(backend)
        paths = ("", "/details") if backend != "brave" else ("/details", "/alternatives")
        return [{"href": local.url + path, "title": "Guide", "body": query} for path in paths]

    monkeypatch.setattr(research, "_ddgs_text", search)
    rows = asyncio.run(local.api.local_search(local.query, limit=3))
    assert [row["url"] for row in rows] == [local.url, local.url + "/details", local.url + "/alternatives"]
    assert backends == ["bing", "yahoo", "duckduckgo", "brave"]


def test_builtin_serializes_searches_from_different_event_loops(local, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app.signals import research

    release, entered = threading.Event(), threading.Event()
    attempts = []
    monkeypatch.setattr(local.api, "time", time)

    def search(query, **kwargs):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
        return [{"href": local.url, "title": "Guide", "body": query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(asyncio.run, local.api.local_search(local.query, limit=1))
        assert entered.wait(5)
        second = executor.submit(asyncio.run, local.api.local_search(local.query + " alternatives", limit=1))
        release.set()
        assert first.result()[0]["url"] == second.result()[0]["url"] == local.url
    assert len(attempts) == 2 and attempts[1] - attempts[0] >= 1


def test_builtin_gate_is_shared_between_api_and_worker_processes(local):
    import subprocess
    import sys
    import threading

    script = ("from app.research.local_providers import _builtin_gate\n"
        "print('attempting', flush=True)\n"
        "with _builtin_gate():\n"
        "    print('entered', flush=True)\n")
    entered = threading.Event()
    with local.api._builtin_gate():
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        try:
            assert process.stdout.readline().strip() == "attempting"

            def read():
                if process.stdout.readline().strip() == "entered":
                    entered.set()

            reader = threading.Thread(target=read)
            reader.start()
            assert not entered.wait(0.2)
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
    try:
        assert entered.wait(5)
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize("status", [200, 202, 403, 429])
def test_explicit_keyless_engine_logs_http_status_and_retains_refusals(local, monkeypatch, caplog, status):
    import logging
    from ddgs.http_client import HttpClient
    from app.signals import research

    def request(self, *args, **kwargs):
        return SimpleNamespace(status_code=status, text=(
            f'<div class="body"><h2>Guide</h2><a href="{local.url}">{local.query}</a></div>'))

    monkeypatch.setattr(HttpClient, "request", request)
    with caplog.at_level(logging.INFO, logger=research.log.name):
        if status == 200:
            assert research._ddgs_text(local.query, limit=1, backend="duckduckgo")[0]["href"] == local.url
        else:
            with pytest.raises(httpx.HTTPStatusError) as refusal:
                research._ddgs_text(local.query, limit=1, backend="duckduckgo")
            assert refusal.value.response.status_code == status
    assert f"engine=duckduckgo status={status}" in caplog.text
    assert local.query not in caplog.text and local.url not in caplog.text


@pytest.mark.parametrize("page", ["captcha", "empty", "blank", "normal"])
def test_builtin_serp_reports_limits_without_hiding_partial_results(local, monkeypatch, builtin_clock, page):
    from ddgs import engines
    from ddgs.http_client import HttpClient
    from app.signals import research

    duckduckgo = engines.ENGINES["text"]["duckduckgo"]
    monkeypatch.setattr(engines, "ENGINES", {"text": {"bing": duckduckgo, "duckduckgo": duckduckgo}})
    normal = f'<div class="body"><h2>Guide</h2><a href="{local.url}">{local.query}</a></div>'
    partial = f'<div class="body"><h2>Other</h2><a href="https://other.example/guide">{local.query}</a></div>'
    pages = iter([partial, {
        "captcha": '<form id="challenge-form" action="/anomaly.js"><p>Please complete the CAPTCHA</p></form>' + normal,
        "empty": '<main><h1>No results found</h1></main>',
        "blank": '  ',
        "normal": normal,
    }[page]])
    monkeypatch.setattr(HttpClient, "request", lambda *args, **kwargs: SimpleNamespace(status_code=200, text=next(pages)))

    async def search():
        with local.api.collect_search_diagnostics() as queries:
            rows = await local.api.local_search(local.query, limit=2)
        return rows, list(queries.values())

    rows, [diagnostics] = asyncio.run(search())
    assert diagnostics["results"] == len(rows) == (2 if page == "normal" else 1)
    assert diagnostics["reason"] == ("search_engine_limited" if page == "captcha" else "")
    assert diagnostics["engines"]["duckduckgo"] == {
        "outcome": {"captcha": "challenge", "empty": "empty", "blank": "empty", "normal": "success"}[page],
        "results": int(page == "normal"),
    }
    assert local.query not in str(diagnostics)


@pytest.mark.parametrize("layout", ["section", "related"])
def test_builtin_yahoo_reads_product_results_without_navigation_text(local, monkeypatch, caplog, layout):
    import logging
    from ddgs.http_client import HttpClient
    from app.signals import research

    tag, classes = ("section", "dd lst algo s-algo") if layout == "section" else ("div", "relsrch")
    page = (f'<{tag} class="{classes}"><div class="compTitle"><h3>'
        f'<a class="s-title" href="{local.url}"><span class="title-url">Navigation</span>{local.query}</a>'
        f'</h3></div><div class="compText">Published description</div></{tag}>'
        '<section class="alsoTry"><h3>Related searches</h3><a href="https://search.yahoo.com/">Other query</a></section>')
    monkeypatch.setattr(HttpClient, "request", lambda *args, **kwargs: SimpleNamespace(status_code=200, text=page))
    with caplog.at_level(logging.INFO, logger=research.log.name):
        rows = research._ddgs_text(local.query, limit=6, backend="yahoo")
    assert [(row["href"], row["title"], row["body"]) for row in rows] == [
        (local.url, local.query, "Published description")]
    assert "engine=yahoo status=200" in caplog.text
    assert local.query not in caplog.text and local.url not in caplog.text


def test_builtin_search_filters_and_ranks_recorded_engine_rows(local, monkeypatch):
    from html import escape
    from urllib.parse import quote, urlsplit

    from ddgs import engines
    from ddgs.engines.bing import Bing
    from ddgs.http_client import HttpClient

    query = local.query + " alternatives"
    first_word = local.query.split()[0]
    alternate = local.url + "/alternative"
    bing_rows = [
        (f"https://{first_word}.example/", first_word, "A large but unrelated portal"),
        ("https://en.wikipedia.org/wiki/Overview", local.query, local.query),
        ("https://www.bing.com/aclick?ld=opaque", local.query, local.query),
        ("https://careers.example/jobs", local.query, local.query),
        ("https://www.investopedia.com/terms/overview", local.query, local.query),
        ("https://www.britannica.com/topic/Overview", local.query, local.query),
    ]
    redirect = f"https://r.search.yahoo.com/_ylt=test/RU={quote(alternate, safe='')}/RK=2/RS=test"
    wrapped = f"https://www.google.com/url?q={quote(local.url + '?id=7&utm_source=engine', safe='')}"
    ddg_target = "//duckduckgo.com/l/?uddg=" + quote(wrapped, safe="")
    pages = {
        "www.bing.com": "<ul>" + "".join(
            f'<li class="b_algo"><h2><a href="{escape(url)}">{title}</a></h2><p>{body}</p></li>'
            for url, title, body in bing_rows) + "</ul>",
        "search.yahoo.com": (
            '<section class="algo"><div class="compTitle"><h3>'
            '<a href="https://r.search.yahoo.com/opaque">'
            f'{local.query}</a></h3></div><div class="compText">{local.query}</div></section>'),
        "duckduckgo.com": (
            f'<div class="body"><h2>Public guide</h2><a href="{redirect}">{local.query}</a></div>'
            f'<div class="body"><h2>{local.query}</h2><a href="{escape(ddg_target)}">{local.query}</a></div>'
            f'<div class="body"><h2>{local.query}</h2><a href="//duckduckgo.com/y.js?ad=1">{local.query}</a></div>'
            f'<div class="body"><h2>{local.query}</h2><a href="https://www.google.com/url?q=javascript:test">'
            f'{local.query}</a></div>'),
    }
    monkeypatch.setitem(engines.ENGINES, "text", {
        "bing": Bing, "yahoo": engines.ENGINES["text"]["yahoo"], "duckduckgo": engines.ENGINES["text"]["duckduckgo"],
    })
    requests = []

    def request(self, method, url, *, params=None, data=None, **kwargs):
        host = urlsplit(url).hostname
        requests.append((host, method, params or data))
        return SimpleNamespace(status_code=200, text=pages[host])

    monkeypatch.setattr(HttpClient, "request", request)
    with local.api.collect_search_diagnostics(customer_run_id="recorded-search") as diagnostics:
        rows = asyncio.run(local.api.local_search(query, limit=6))
    assert [row["url"] for row in rows] == [local.url + "?id=7", alternate]
    assert [host for host, _, _ in requests] == list(pages)
    assert [method for _, method, _ in requests] == ["GET"] * 3
    assert [payload.get("q") or payload.get("p") for _, _, payload in requests] == [query] * 3
    assert requests[0][2] == {"q": query, "adlt": "strict"}
    assert requests[2][2] == {"q": query, "kl": "wt-wt", "kp": "1"}
    outcomes = next(iter(diagnostics.values()))["engines"]
    assert outcomes["bing"] == outcomes["yahoo"] == {"outcome": "irrelevant", "results": 0}


def test_hosted_ddgs_keeps_automatic_backend_and_closes_client(local, monkeypatch):
    import ddgs
    from app.signals import research

    calls = []

    class Search:
        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [{"href": local.url, "title": "Guide"}]

        def close(self):
            calls.append("closed")

    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setattr(ddgs, "DDGS", Search)
    assert research._ddgs_text(local.query, limit=2, backend="brave")[0]["href"] == local.url
    assert calls == [(local.query, {"max_results": 2}), "closed"]


def test_builtin_backoff_caches_success_and_serializes_queries(local, monkeypatch):
    import time
    from ddgs.exceptions import RatelimitException
    from app.signals import research

    attempts, active, peak = [], 0, 0
    monkeypatch.setattr(local.api, "time", time)

    def search(query, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            attempts.append(query)
            time.sleep(0.01)
            if len(attempts) == 1:
                raise RatelimitException("rate limited")
            return [{"href": local.url, "title": "Public guide", "body": query}]
        finally:
            active -= 1

    monkeypatch.setattr(research, "_ddgs_text", search)

    async def run():
        await local.api.local_search(local.query, limit=1)
        rows = await asyncio.gather(local.api.local_search(local.query, limit=1),
            local.api.local_search(local.query + " contact", limit=1))
        assert all(result[0]["url"] == local.url for result in rows)

    asyncio.run(run())
    assert attempts.count(local.query) == 2 and peak == 1


@pytest.mark.parametrize("empty", [False, True])
def test_builtin_later_supported_backend_recovers_when_earlier_engines_are_unavailable(local, monkeypatch, builtin_clock, empty):
    from ddgs.exceptions import RatelimitException
    from app.signals import research

    backends = []

    def search(query, *, backend, **kwargs):
        backends.append(backend)
        if backend != local.backends[-1]:
            if empty:
                return []
            raise RatelimitException("rate limited")
        return [{"href": local.url, "title": "Public guide", "body": query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    assert asyncio.run(local.api.local_search(local.query, limit=1))[0]["url"] == local.url
    assert backends == local.backends


def test_builtin_failed_query_recovers_after_engine_cooldown(local, monkeypatch, builtin_clock):
    from ddgs.exceptions import RatelimitException
    from app.signals import research

    queries = []

    def search(query, **kwargs):
        queries.append(query)
        if query == local.query:
            raise RatelimitException("rate limited")
        return [{"href": local.url, "title": "Public guide", "body": query}]

    monkeypatch.setattr(research, "_ddgs_text", search)
    with pytest.raises(ValueError, match="Keys and connections"):
        asyncio.run(local.api.local_search(local.query, limit=1))
    builtin_clock[0][0] += 30 * 60
    assert asyncio.run(local.api.local_search(local.query + " contact", limit=1))[0]["url"] == local.url
    assert queries == [local.query] * len(local.backends) + [local.query + " contact"]


@pytest.mark.parametrize("provider", ["tavily", "exa", "openai", "firecrawl"])
def test_selected_provider_missing_settings_never_uses_another(local, monkeypatch, provider):
    from app.core import providers_local
    from app.signals import research

    local.values["provider"] = provider
    monkeypatch.setenv("TAVILY_API_KEY", "ignored-environment-key")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: SimpleNamespace(provider="anthropic"))
    monkeypatch.setattr(research, "_ddgs_text", lambda *args, **kwargs: pytest.fail("unexpected fallback"))
    assert not local.api.search_ready()
    with pytest.raises(ValueError, match="Keys and connections"):
        asyncio.run(local.api.local_search(local.query, limit=1))


def test_dossier_recon_uses_builtin_sources_instead_of_requiring_paid_search(local, monkeypatch):
    from app.signals import research

    monkeypatch.setattr(research, "_ddgs_text", lambda query, **kwargs: [
        {"href": local.url, "title": local.query, "body": local.query}])
    result = asyncio.run(local.api.research_recon(local.query))
    assert result["mode"] == "live" and result["sources"][0]["url"] == local.url
    assert result["sources"][0]["fetched_at"]


def test_local_bridges_and_reader_share_the_saved_resolver(local, monkeypatch):
    from app.research import openworker_bridge, agent_reach_bridge
    from app.signals import research, local_reader
    from app.tools import reader

    monkeypatch.setattr(research, "_ddgs_text", lambda query, **kwargs: [
        {"href": local.url, "title": local.query, "body": local.query}])
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)
    monkeypatch.setattr(local_reader, "_basic_read", lambda url, main_only: local_reader._page_response(
        url, local.query, "Guide", [url + "/details"]))
    assert openworker_bridge.search_web(local.query)[0]["url"] == local.url
    assert agent_reach_bridge.search_platform("reddit", local.query)["hits"][0]["url"] == local.url
    assert openworker_bridge.fetch_url(local.url)["fetched_at"]
    assert agent_reach_bridge.read_url(local.url) == local.query
    assert reader.enabled()
    page = reader.read_page(local.url, limit=5)
    assert page.text == local.query[:5] and page.truncated
    assert page.links == [local.url + "/details"]
    assert asyncio.run(research.HttpResearchProvider("exa", "unused").scrape(local.url)) == local.query


def test_optional_page_failure_keeps_the_real_search_source(local, monkeypatch):
    from app.signals import research

    monkeypatch.setattr(research, "_ddgs_text", lambda query, **kwargs: [
        {"href": local.url, "title": local.query, "body": local.query}])
    provider = local.api.search_provider()

    async def forbidden(url, **kwargs):
        raise ValueError("Your page does not allow this read. Choose another public URL.")

    monkeypatch.setattr(provider, "scrape", forbidden)
    rows = asyncio.run(provider.search(local.query, include_content=True))
    assert rows[0]["url"] == local.url and rows[0]["snippet"] == local.query
    assert "content" not in rows[0] and rows[0]["fetched_at"]


@pytest.mark.parametrize("endpoint,self_hosted", [("http://localhost:3002", True), ("https://api.firecrawl.dev", False)])
def test_firecrawl_cloud_and_local_use_saved_keys_without_free_cloud_receipts(local, monkeypatch, endpoint, self_hosted):
    from app.credits.metered import firecrawl_actual_micro

    local.values.update(provider="firecrawl", firecrawl_url=endpoint)
    local.secrets["firecrawl_key"] = "fixture-reader-key"
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-reader-key"
        return httpx.Response(200, json={"success": True, "data": [{"url": local.url, "title": local.query}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    rows = asyncio.run(local.api.local_search(local.query, limit=20))
    assert rows[0]["url"] == local.url and len(requests) == 1
    assert bool(rows.usage.get("firecrawl_self_hosted")) == self_hosted
    assert firecrawl_actual_micro(rows, 20000) == (0 if self_hosted else 20000)
    assert local.calls[0]["estimate_micro"] == (0 if self_hosted else 20000)


def test_owner_research_switch_blocks_all_local_entry_points(local, monkeypatch):
    import sys
    from types import ModuleType
    from app.research import deep
    from app.tools import reader
    from app.signals.research import HttpResearchProvider

    provider = local.api.search_provider()
    capabilities = ModuleType("app.core.local_capabilities")
    capabilities.enabled = lambda name: False
    capabilities.blocked_message = lambda name: "Turn on research in Settings > Features, then retry."
    monkeypatch.setitem(sys.modules, capabilities.__name__, capabilities)
    assert local.api.search_provider() is None and deep._web_provider() is None
    assert not reader.enabled()
    for callback in (local.api.local_search(local.query, limit=1), local.api.local_read(local.url),
            HttpResearchProvider("firecrawl", "").scrape(local.url), provider.search(local.query)):
        with pytest.raises(ValueError, match="Turn on research.*Settings > Features"):
            asyncio.run(callback)
    assert not local.calls


@pytest.mark.parametrize("reason", ["SEARCH_LIMITED", "SEARCH_UNAVAILABLE"])
def test_deep_builtin_rate_limit_retains_a_plain_gap_and_zero_spend(local, monkeypatch, reason):
    from contextlib import nullcontext
    from app.experiments import activity
    from app.research import deep

    provider = local.api.search_provider()
    message = getattr(local.api, reason)

    async def limited(query):
        error = local.api.SearchEngineLimited if reason == "SEARCH_LIMITED" else local.api.BuiltinSearchUnavailable
        raise error(message)

    monkeypatch.setattr(provider, "search", limited)
    monkeypatch.setattr(activity, "track", lambda *args, **kwargs: nullcontext({}))
    estimates = []

    def retrieve(dossier, key, payload, **kwargs):
        estimates.append(kwargs["estimate"])
        return kwargs["callback"]()

    monkeypatch.setattr(deep, "_retrieve", retrieve)
    result = deep._search("dossier", {}, provider, "demand", local.query, local.query)
    assert result["items"] == [] and result["coverage_gap"]
    assert message in str(result["coverage_gap"])
    assert estimates == [0]


def test_builtin_reader_reuses_pdf_extraction_and_checks_robots(local, monkeypatch):
    from app.tools import reader
    from core.research_web import guard

    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /"))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(b"%PDF-fixture"))

    monkeypatch.setattr(reader, "_pdf", lambda content, limit, deadline: (local.query, "Guide"))
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    page = asyncio.run(local.api.local_read(local.url))
    assert page == local.query and page.metadata["title"] == "Guide"
    assert requests == ["/robots.txt", "/guide"]


def test_builtin_customer_reader_preserves_published_structure(local, monkeypatch):
    import brotli
    import gzip
    from app.gtm import enrich
    from app.gtm.discovery import LocalDiscoveryProvider
    from core.research_web import guard

    fixtures = Path(__file__).resolve().parents[1] / "gtm" / "fixtures"
    business = "dispatch" in local.url
    host = "cedar-vale.example" if business else "ride-network.example"
    cases = {
        "/leadership": ("hospital_leadership.html", "text/html", "gzip", "Naomi Bennett", "Director of Pharmacy", "naomi.bennett"),
        "/services/pharmacy": ("hospital_pharmacy.html", "text/html; charset=ISO-8859-1", "br", "Inès Laurent", "Pharmacy Director", "ines.laurent"),
    } if business else {
        "/directory": ("cycling_directory.html", "application/octet-stream", "br", "Samir Patel", "Lead Coach", "samir.patel"),
    }
    requests = []
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))])

    def handle(request):
        path = request.url.path
        requests.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, stream=httpx.ByteStream(b"User-agent: *\nAllow: /"))
        fixture, mime, encoding, *_ = cases[path]
        body = (fixtures / fixture).read_text(encoding="utf-8").encode(
            "iso-8859-1" if "ISO-8859-1" in mime else "utf-8")
        if request.url.params.get("charset") == "meta":
            mime = mime.split(";", 1)[0]
        compressed = gzip.compress(body) if encoding == "gzip" else brotli.compress(body)
        return httpx.Response(200, headers={"content-type": mime, "content-encoding": encoding},
            stream=httpx.ByteStream(compressed))

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    provider = LocalDiscoveryProvider(local.api.search_provider())
    expected_requests = []
    for path, (_, _, _, name, role, mailbox) in cases.items():
        url = f"https://{host}{path}"
        raw = asyncio.run(provider.scrape(url, main_only=False))
        expected_requests += ["/robots.txt", path]
        facts = enrich.extract(host, [(url, raw)], published_mailboxes=True)
        email = f"{mailbox}@{host}"
        assert [(person["name"], person["role"], person["email"]) for person in facts["people"]] == [(name, role, email)]
        assert [(contact["email"], contact["kind"], contact["source_url"]) for contact in facts["contacts"]] == [(email, "named_person", url)]
        if path == "/services/pharmacy":
            assert facts["phone"]["value"] == "+15415550141"
        if not business:
            assert facts["contact_form"]["source_url"] == url
            assert enrich.directory_links(raw, url) == ["https://birch-riders.example/", "https://canal-cycling.example/"]
        if path != "/leadership":
            text = asyncio.run(provider.scrape(url + "?charset=meta"))
            assert f"{name} {role}" in text
            expected_requests += ["/robots.txt", path]
    assert requests == expected_requests


@pytest.mark.parametrize("problem,reason", [
    ("script", "page needs JavaScript"),
    ("compression", "unreadable compression"),
    ("oversize", "page too large to read"),
])
def test_builtin_reader_reports_why_public_text_cannot_be_read(local, monkeypatch, problem, reason):
    from app.gtm.enrich import source_failure_reason
    from app.signals.local_reader import UnbilledReadError
    from app.tools import reader
    from core.research_web import guard

    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(reader, "MAX_BYTES", 1024)
    body = {
        "script": b"<main>Loading...</main><script src='/app.js'></script>",
        "compression": b"This body is not actually Brotli-encoded.",
        "oversize": b"<main>" + b"long page " * 150 + b"</main>",
    }[problem]

    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        headers = {"content-type": "text/html", **({"content-encoding": "br"} if problem == "compression" else {})}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(UnbilledReadError, match="choose another public URL|Retry with another public URL") as caught:
        asyncio.run(local.api.local_read(local.url))
    assert source_failure_reason(caught.value) == reason


@pytest.mark.parametrize("kind,value,reason", [
    ("status", 401, "blocked access"), ("status", 403, "blocked access"),
    ("status", 404, "page not found"), ("status", 410, "page not found"),
    ("status", 429, "temporarily busy"), ("status", 502, "server error"),
    ("status", 418, "page request rejected"),
    ("reader", "empty_page", "empty page"), ("reader", "robots_disallowed", "site permissions (automated reads blocked)"),
    ("reader", "unsupported_content_type", "unsupported page format"),
    ("reader", "text_extraction_failed", "unreadable text or format"),
    ("reader", "reader_busy", "reader busy"),
    ("redirect", None, "too many redirects"), ("tls", None, "secure connection failed"),
    ("dns", None, "site address not found"), ("refused", None, "connection refused"),
    ("reset", None, "connection reset"), ("unknown", None, "page read failed"),
])
def test_customer_source_failure_classification(kind, value, reason):
    import socket
    import ssl

    load_company_os_app()
    from app.gtm.enrich import source_content_reason, source_failure_reason
    from app.signals.research import ScrapedText
    from app.tools.reader import ReaderError

    request = httpx.Request("GET", "https://source.example/")
    if kind == "status":
        error = httpx.HTTPStatusError("HTTP request failed", request=request, response=httpx.Response(value))
        assert source_content_reason(ScrapedText("", metadata={"statusCode": value}, usage={})) == reason
    elif kind == "reader":
        error = ReaderError(value)
    else:
        error = {"redirect": RuntimeError("too many redirects (>5)"), "tls": ssl.SSLCertVerificationError(),
            "dns": socket.gaierror(), "refused": ConnectionRefusedError(), "reset": ConnectionResetError(),
            "unknown": ValueError("the provider included untrusted detail")}[kind]
    wrapper = ValueError("choose another public URL")
    wrapper.__cause__ = error
    assert source_failure_reason(wrapper) == reason


def test_builtin_reader_handles_redirects_and_reports_http_metadata(local, monkeypatch):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import urlsplit

    import brotli
    from app.gtm import enrich
    from core.research_web import guard

    received = []
    html = f"<section><p>{local.query}</p></section>".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            status, media_type, body = {
                "/robots.txt": (404, "text/plain", b""),
                "/redirect": (302, "text/plain", b""),
                "/brotli": (200, "application/octet-stream", brotli.compress(html)),
                "/missing": (404, "text/html; charset=utf-8", b"<p>Gone</p>"),
                "/empty": (200, "text/html", b""),
                "/binary": (200, "image/png", b"\x89PNG"),
            }[self.path]
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            if self.path == "/brotli":
                assert "br" in self.headers["Accept-Encoding"] and "zstd" not in self.headers["Accept-Encoding"]
                self.send_header("Content-Encoding", "br")
            if self.path == "/redirect":
                self.send_header("Location", "/brotli")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    original = httpx.Client
    resolve = socket.getaddrinfo
    monkeypatch.setenv("READER_FOUNDER_CONCURRENCY", "1")
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda host, *args, **kwargs:
        resolve("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34", *args, **kwargs))

    async def read_all():
        origin = "https://" + urlsplit(local.url).hostname
        return await asyncio.gather(*(local.api.local_read(origin + path)
            for path in ("/redirect", "/missing", "/empty", "/binary")), return_exceptions=True)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        class LoopbackTransport(httpx.HTTPTransport):
            def handle_request(self, request):
                assert request.url.host == "93.184.216.34"
                request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=server.server_port)
                return super().handle_request(request)

        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=LoopbackTransport(), **kwargs))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page, missing, empty, binary = asyncio.run(read_all())
        finally:
            server.shutdown()
            thread.join()

    assert page == local.query and page.metadata["sourceURL"].endswith("/brotli")
    assert received.count("/robots.txt") == 5
    for error, reason, status, content_type, size in ((missing, "page not found", 404, "text/html", 11),
            (empty, "empty page", 200, "text/html", 0), (binary, "unsupported page format", 200, "image/png", 4)):
        assert isinstance(error, Exception)
        failure = enrich.source_read_failure(local.url, error=error)
        assert failure["reason"] == reason
        diagnostics = failure["diagnostics"]
        assert (diagnostics["http_status"], diagnostics["content_type"], diagnostics["byte_length"]) == (status, content_type, size)


@pytest.mark.parametrize("metadata_kind", ["source_only", "invalid", "unsafe_url", "verified"])
def test_firecrawl_without_final_url_can_still_try_builtin_reader(local, monkeypatch, metadata_kind):
    from app.signals import local_reader

    local.values["firecrawl_url"] = "http://localhost:3002"
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)
    builtin_url = local.url + "/public"
    firecrawl_url = local.url + "/canonical"
    metadata = {
        "source_only": {"sourceURL": local.url},
        "invalid": "invalid",
        "unsafe_url": {"sourceURL": local.url, "url": "file://private.test/guide"},
        "verified": {"sourceURL": local.url, "url": firecrawl_url},
    }[metadata_kind]
    reads = []

    def read(url, main_only):
        reads.append((url, main_only))
        return local_reader._page_response(builtin_url, local.query, "Guide")

    monkeypatch.setattr(local_reader, "_basic_read", read)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "success": True, "data": {"markdown": "Firecrawl excerpt", "metadata": metadata}}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    page = asyncio.run(local.api.local_read(local.url))
    if metadata_kind == "verified":
        assert not reads
        assert page == "Firecrawl excerpt" and page.metadata["url"] == firecrawl_url
    else:
        assert reads == [(local.url, True)]
        assert page == local.query and page.metadata["url"] == builtin_url
    assert page.metadata["title"] and page.metadata["fetched_at"]


@pytest.mark.parametrize("provider", ["tavily", "exa", "openai", "firecrawl"])
def test_provider_failure_names_the_saved_key_screen_without_exposing_keys(local, monkeypatch, provider):
    from app.core import providers_local

    local.values.update(provider=provider, firecrawl_url="http://localhost:3002")
    local.secrets.update(api_key="fixture-search-key", firecrawl_key="fixture-reader-key")
    config = SimpleNamespace(provider="openai", base_url="https://api.openai.com/v1",
        api_key=SimpleNamespace(get_secret_value=lambda: "fixture-model-key"), deployment_flash="saved-model")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "private-provider-response"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(local.api.local_search(local.query, limit=1))
    assert "Keys and connections" in str(caught.value) and "key" in str(caught.value)
    assert not any(secret in str(caught.value) for secret in [*local.secrets.values(), "fixture-model-key", "private-provider-response"])
    assert caught.value.response.status_code == caught.value.status_code == 401


@pytest.mark.parametrize("failure", [ValueError, TimeoutError])
@pytest.mark.parametrize("firecrawl", [False, True])
def test_page_preflight_failure_is_unbilled_before_any_provider_request(local, monkeypatch, failure, firecrawl):
    from app.research.metered import _failure
    from app.signals import local_reader

    if firecrawl:
        local.values["firecrawl_url"] = "https://reader.example"
        local.secrets["firecrawl_key"] = "fixture-reader-key"

    def rejected(url):
        raise failure("private-validation-error")

    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", rejected)
    monkeypatch.setattr(local_reader, "_basic_read", lambda *args: pytest.fail("preflight must stop before reading"))
    monkeypatch.setattr(local_reader, "_post", lambda *args, **kwargs: pytest.fail("preflight must stop before provider I/O"))
    with pytest.raises(local_reader.UnbilledReadError) as caught:
        asyncio.run(local.api.scrape_provider().scrape(local.url))
    assert _failure(caught.value) == ("failed", "research_provider_rejected")
    assert "private-validation-error" not in str(caught.value)
    assert not local.calls


@pytest.mark.parametrize("provider", ["builtin", "exa"])
def test_unreadable_builtin_page_suggests_another_public_url_not_a_key(local, monkeypatch, provider):
    from app.signals import local_reader

    local.values["provider"] = provider
    local.secrets["api_key"] = "fixture-search-key"
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)

    def unreadable(*args):
        raise ValueError("private-provider-response")

    monkeypatch.setattr(local_reader, "_basic_read", unreadable)
    with pytest.raises(ValueError) as caught:
        asyncio.run(local.api.local_read(local.url))
    assert "Retry with another public URL" in str(caught.value)
    assert "Firecrawl" not in str(caught.value) and "search key" not in str(caught.value)
    assert "private-provider-response" not in str(caught.value)


def test_search_prose_cannot_become_verified_page_text(local, monkeypatch):
    from app.core import providers_local
    from app.signals import local_reader

    local.values["provider"] = "openai"
    local.secrets["api_key"] = "fixture-search-key"
    config = SimpleNamespace(provider="openai", base_url="https://api.openai.com/v1",
        api_key=SimpleNamespace(get_secret_value=lambda: "fixture-model-key"), deployment_flash="saved-model")
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)

    def unavailable(*args):
        raise ValueError("page unavailable")

    monkeypatch.setattr(local_reader, "_basic_read", unavailable)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"url": local.url, "raw_content": local.query}],
            "output": [{"content": [{"type": "output_text", "text": local.query,
                "annotations": [{"type": "url_citation", "url": local.url}]}]}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(local_reader.UnbilledReadError, match="another public URL"):
        asyncio.run(local.api.local_read(local.url))
    assert not requests
    assert [(call["provider"], call["estimate_micro"]) for call in local.calls] == [("duckduckgo", 0)]


def test_dossier_history_does_not_present_quote_text_without_its_source(local, monkeypatch):
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from app.research import service

    now = datetime.now(timezone.utc)
    row = SimpleNamespace(id="report", venture_id="venture", workspace_id="workspace", revision_id="revision",
        kind="deep", state="complete", created_at=now, completed_at=now, budget_calls=1, calls_used=1,
        summary=json.dumps({"review": {"observations": [{"source_id": "source", "quote": local.query}]}}))
    session = SimpleNamespace(scalars=lambda query: SimpleNamespace(all=lambda: [row]), scalar=lambda query: None)
    monkeypatch.setattr(service, "db", lambda: lambda: nullcontext(session))
    report, = service.list_dossiers(row.venture_id)
    assert report["id"] == row.id and local.query not in json.dumps(report)


@pytest.mark.parametrize("allowed", [True, False])
def test_tavily_extract_keeps_spend_admission_but_does_not_verify_the_final_url(local, monkeypatch, allowed):
    from app.core import local_secrets
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    from app.signals import local_reader

    local.values["provider"] = "tavily"
    local.secrets["api_key"] = "fixture-search-key"
    monkeypatch.setattr("core.ssrf.assert_safe_remote_url", lambda url: None)

    def unavailable(*args):
        raise ValueError("page unavailable")

    monkeypatch.setattr(local_reader, "_basic_read", unavailable)
    requests = []

    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"results": [{"url": local.url, "raw_content": local.query}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    if allowed:
        page = asyncio.run(local.api.local_read(local.url))
        assert page == local.query and page.metadata["url"] is None
        assert requests == ["/extract"]
        assert [(call["provider"], call["estimate_micro"]) for call in local.calls] == [("duckduckgo", 0), ("tavily", 8000)]
    else:
        monkeypatch.setattr(metered, "call", local.actual_call)
        monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
            "values": {"monthly_spend_cap": 0} if section == "spend" else local.values,
            "sources": {field: "app" for field in (["monthly_spend_cap"] if section == "spend" else local.values)},
        })
        with pytest.raises(CreditsUnavailable) as caught:
            asyncio.run(local.api.local_read(local.url))
        assert caught.value.code == "local_spend_limit" and not requests


def test_builtin_deadline_budgets_fallbacks_and_gate_wait(local, monkeypatch, builtin_clock):
    from ddgs import engines
    from app.signals import research

    clock, _ = builtin_clock
    monkeypatch.setattr(engines, "ENGINES", {"text": dict.fromkeys(["bing", "yahoo", "brave", "mojeek"])})
    dispatched = []

    def find(query, *, backend, timeout=4, **kwargs):
        dispatched.append(backend)
        clock[0] += timeout
        if backend in {"bing", "yahoo"}:
            raise httpx.HTTPStatusError("blocked", request=httpx.Request("GET", local.url),
                response=httpx.Response(202 if backend == "bing" else 403))
        return [{"href": local.url, "title": local.query}]

    monkeypatch.setattr(research, "_ddgs_text", find)
    assert asyncio.run(local.api.local_search(local.query, limit=8))[0]["url"] == local.url
    assert clock[0] <= 10 and dispatched == ["bing", "yahoo", "brave"]
    with local.api._builtin_gate():
        with pytest.raises(ValueError, match="Keys and connections"):
            asyncio.run(local.api.local_search(local.query + " other", limit=1))
    assert dispatched == ["bing", "yahoo", "brave"]
    with local.api._builtin_gate():
        pass


@pytest.mark.parametrize("provider,status", [("tavily", 401), ("exa", 403), ("firecrawl", 401)])
def test_rejected_search_key_stops_retrieval_with_a_safe_next_step(local, monkeypatch, provider, status):
    from contextlib import nullcontext
    from app.core.jobs import FatalError
    from app.experiments import activity
    from app.research import deep, metered, presentation

    local.values.update(provider=provider, firecrawl_url="https://reader.example")
    local.secrets.update(api_key="fixture-search-key", firecrawl_key="fixture-reader-key")
    monkeypatch.setattr(deep.ex, "operation", lambda *args: {})
    monkeypatch.setattr(activity, "track", lambda *args, **kwargs: nullcontext({}))
    requests = []

    def reject(request):
        requests.append(request.url.path)
        return httpx.Response(status, json={"error": "private-provider-response"})

    def perform(*args, **kwargs):
        try:
            kwargs["callback"]()
        except Exception as exc:
            state, code = metered._failure(exc)
            assert state == "failed"
            raise FatalError(code) from None

    monkeypatch.setattr(deep, "perform", perform)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(reject), **kwargs))
    with pytest.raises(FatalError) as stopped:
        deep._search("dossier", {}, local.api.search_provider(), "demand", local.query, local.query)
    notice = presentation.search_notice({"error_code": str(stopped.value)})
    assert len(requests) == 1 and provider.title() in notice["next_step"]
    assert "Keys and connections" in notice["next_step"] and "key" in notice["next_step"].lower()
    assert "fixture-" not in str(notice) and "private-provider-response" not in str(notice)


@pytest.mark.parametrize("outcome", ["limited", "unavailable", "no_matches"])
def test_deep_builtin_outcomes_survive_founder_projection(local, monkeypatch, builtin_clock, outcome):
    from contextlib import nullcontext
    from ddgs import engines
    from ddgs.exceptions import RatelimitException
    from app.experiments import activity
    from app.research import deep, presentation
    from app.signals import research

    monkeypatch.setattr(engines, "ENGINES", {"text": {"bing": object}})

    def find(*args, **kwargs):
        if outcome == "limited":
            raise RatelimitException("fixture refusal")
        if outcome == "unavailable":
            raise TimeoutError
        return []

    monkeypatch.setattr(research, "_ddgs_text", find)
    monkeypatch.setattr(activity, "track", lambda *args, **kwargs: nullcontext({}))
    monkeypatch.setattr(deep, "_retrieve", lambda *args, **kwargs: kwargs["callback"]())
    result = deep._search("dossier", {}, local.api.search_provider(), "demand", local.query, local.query)
    assert not result["items"] and result["search_outcome"] == outcome
    document = {"_runtime": {"operations": {"demand": {"response": result}}}}
    summary = presentation.founder_payload({"search_notice": presentation.search_notice(document)})
    notice = summary["search_notice"]
    assert notice["state"] == outcome and bool(notice.get("settings_href")) == (outcome != "no_matches")
    if outcome == "no_matches":
        assert "On Idea" in notice["next_step"] and "Run research again" in notice["next_step"]
    else:
        assert "Keys and connections" in notice["next_step"]


def test_saved_review_validates_its_digest_without_requiring_live_search(local, monkeypatch):
    from contextlib import nullcontext
    from app.core import local_capabilities, local_secrets
    from app.research import service

    local.values.update(provider="tavily")
    monkeypatch.setattr(local_capabilities, "read_settings", local_secrets.read_settings)
    monkeypatch.setattr(local_capabilities, "get_secret", local_secrets.get_secret)
    previous = SimpleNamespace(kind="deep", id=local.url, venture_id=local.query)
    session = SimpleNamespace(get=lambda *args: previous)
    monkeypatch.setattr(service, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr("app.db.session.serialize_writes", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "review_quote", lambda did: {"source_digest": local.url, "sufficient_now": True})
    with pytest.raises(RuntimeError, match="research_inputs_changed"):
        service.start(local.query, previous_dossier_id=local.url, phase="review", source_digest="stale")
    assert service.start(local.query)["kind"] == "text"
    local.values["research"] = False
    assert "Settings > Capabilities" in service.start(local.query, previous_dossier_id=local.url, phase="review")["body_md"]


@pytest.mark.parametrize("cross_host", [True, False])
def test_verified_excerpt_uses_the_page_that_was_read(local, monkeypatch, cross_host):
    from contextlib import nullcontext
    from app.experiments import activity
    from app.research import categorize, deep
    from core.research_web import guard

    final = "https://destination.example/final" if cross_host else local.url + "/final"
    finding = SimpleNamespace(url=local.url, title="Guide", summary="Original lead", evidence=json.dumps({"evidence_kind": "search_excerpt"}))
    session = SimpleNamespace(get=lambda *args, **kwargs: finding)
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 0))])

    def page(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        if request.url.path == "/guide":
            return httpx.Response(302, headers={"location": final}, stream=httpx.ByteStream(b""))
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(local.query.encode()))

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: original(*args, transport=httpx.MockTransport(page), **kwargs))
    monkeypatch.setattr(activity, "track", lambda *args, **kwargs: nullcontext({}))
    monkeypatch.setattr(deep, "_retrieve", lambda *args, **kwargs: kwargs["callback"]())
    monkeypatch.setattr(deep.ex, "mutate", lambda did, payload, update: update(None, {}, session))
    result = deep._scrape("dossier", {}, local.api.scrape_provider(), {"url": local.url, "id": "source"})
    assert result["page_url"] == final
    deep._store_page("dossier", {}, "source", result)
    source, evidence = deep.finding_page(finding, json.loads(finding.evidence))
    source.update(evidence)
    review = {"competitor_checks": [{"source_id": "source", "product_quote": local.query, "positioning_quote": local.query}]}
    grid = categorize.competitor_grid({"source": source}, review, {})
    assert finding.url == local.url
    if cross_host:
        assert not source.get("page_verified") and not grid["rows"]
    else:
        assert source["additional_excerpts"][0]["url"] == final
        assert source["search_lead"]["url"] == local.url
        assert grid["rows"][0]["field_sources"]["positioning_line"]["url"] == final


@pytest.mark.parametrize("redirected", [False, True])
def test_research_does_not_rebind_unattributed_older_verified_excerpts(local, redirected):
    from app.research.deep import finding_page
    from app.research.service import dossier_evidence_dimensions

    final_url = local.url + "/pricing" if redirected else local.url
    old_url = local.url if redirected else local.url + "/old-pricing"
    finding = SimpleNamespace(url=local.url, title="Guide", summary=local.query)
    excerpts = [
        {"verified_page": True, "url": final_url, "text": "Plans start at $12/month."},
        {"verified_page": True, "url": final_url, "text": "Setup fee: $30."},
        {"verified_page": True, "url": old_url, "text": "Plans start at $99/month."},
        {"verified_page": True, "text": "Plans start at $200/month."},
    ]
    source, evidence = finding_page(finding, {"page_verified": True, "additional_excerpts": excerpts})
    assert evidence["additional_excerpts"] == excerpts[:2]
    report = dossier_evidence_dimensions({"web": [{"id": "source", **source, **evidence}]}, None)
    assert {(lead["url"], lead["finding"]) for lead in report["pricing"]["support"]} == {
        (final_url, entry["text"]) for entry in excerpts[:2]
    }
