"""Offline paid-search admission and shared-limiter seams; no live API or PG."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing, nullcontext
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/paid-search.db")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_ENV", "local")
    monkeypatch.setenv("FIRECRAWL_MIN_INTERVAL_SECONDS", "1.2")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    for key in ("FIRECRAWL_API_KEY", "EXA_API_KEY", "TAVILY_API_KEY", "REDIS_URL",
                "SUPYMEM_METER_FIRECRAWL_SEARCH_USD_PER_UNIT", "SUPYMEM_METER_EXA_SEARCH_USD_PER_UNIT",
                "SUPYMEM_METER_TAVILY_SEARCH_USD_PER_UNIT"):
        monkeypatch.delenv(key, raising=False)
    load_company_os_app()
    from app.credits import gate, metered
    from app.credits.errors import CreditsUnavailable
    from app.gtm.relationships import discover
    from app.research import explore
    from app.signals import research
    from app.tenancy import bind, reset_scope, set_scope

    token = set_scope(None)
    monkeypatch.setattr(gate, "enforcement_enabled", lambda: True)
    monkeypatch.setattr(research, "_FIRECRAWL_NEXT_CALL", {})
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_LOCKS", {})
    monkeypatch.setattr(research, "_FIRECRAWL_KEY_INDEX", {})
    monkeypatch.setattr(research, "_provider_from_env", lambda: research.LocalStubProvider())
    monkeypatch.setattr(explore, "_provider_from_env", lambda: research.LocalStubProvider())
    async def no_platforms(*args, **kwargs):
        return []
    monkeypatch.setattr(explore, "_platform_lookups", no_platforms)
    yield SimpleNamespace(explore=explore, research=research, discover=discover, metered=metered,
                          CreditsUnavailable=CreditsUnavailable, bind=bind)
    reset_scope(token)


def provider(events, *, name="firecrawl", error=None):
    class Provider:
        _limit = 6

        async def search(self, query, **kwargs):
            events.append("http")
            if error:
                raise error
            return [{"url": "https://example.org/inventory", "title": "Coffee inventory", "snippet": "Coffee inventory software"}]
    result = Provider()
    result.name = name
    return result


@pytest.mark.parametrize("audience", ["Warehouse purchasing teams", "Weekend trail runners"])
@pytest.mark.parametrize("captured", [False, True])
def test_local_model_journal_settles_sequential_calls_and_reuses_own_receipt(paths, monkeypatch, tmp_path, audience, captured):
    from app.core.operation_receipts import OperationJournal
    from app.credits import accounts, estimate, local_meter
    from app.llm.pricing import pricing_table
    from app.research import metered

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(accounts, "get_account", lambda *args: pytest.fail("Local work must not request a hosted wallet"))
    row = SimpleNamespace(workspace_id="workspace-a", venture_id="venture-a")
    journal_path = tmp_path / "journal.sqlite3"
    with closing(sqlite3.connect(journal_path)) as connection, connection:
        connection.execute("CREATE TABLE journal (document TEXT NOT NULL)")
        connection.execute("INSERT INTO journal VALUES (?)", (json.dumps({"_runtime": {"operations": {}}}),))

    def document(_):
        with closing(sqlite3.connect(journal_path)) as connection:
            return json.loads(connection.execute("SELECT document FROM journal").fetchone()[0])

    def mutate(operation_id, payload, update):
        with closing(sqlite3.connect(journal_path)) as connection, connection:
            saved = json.loads(connection.execute("SELECT document FROM journal").fetchone()[0])
            result = update(row, saved, connection)
            connection.execute("UPDATE journal SET document = ?", (json.dumps(saved),))
            return result

    journal = OperationJournal(read=lambda _: row, document=document, mutate=mutate,
        before_call=lambda *args: None, prefix="gtm")
    model = next(iter(pricing_table()))
    cost = estimate.actual_llm_micro(model, prompt_tokens=1000, completion_tokens=100)
    capture_args = {}
    if captured:
        from tests.unit.credits.test_core_model_receipts import DIGEST, binding

        capture = binding()
        capture["purpose"] = "gtm.reachable.enrich"
        model, cost = capture["deployment"], 248
        capture_args = {"model_binding": capture, "request_digest": DIGEST, "token_ceiling": 200}
    calls = []

    def respond():
        calls.append(audience)
        if captured:
            from tests.unit.credits.test_core_model_receipts import sdk

            result = sdk()
            result.choices[0].message.content = json.dumps({"audience": audience})
            return result
        return SimpleNamespace(usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"audience": audience})), finish_reason="stop")])

    with paths.bind(workspace_id=row.workspace_id, venture_id=row.venture_id, actor="person:test"):
        for key, attempt in (("first", 1), ("second", 1), ("first", 2)):
            result = metered.perform("local-run", key, {"_job_attempt": attempt, "_worker_id": "worker"},
                purpose="gtm.reachable.enrich", provider="azure_openai" if captured else "openai", model=model,
                callback=respond, estimate=1200 if captured else cost + 1, parse=json.loads, journal=journal,
                prefix="gtm", **capture_args)
            assert result == {"audience": audience}
    assert calls == [audience, audience]
    for saved in document("local-run")["_runtime"]["operations"].values():
        assert saved["state"] == "completed" and saved["usage"]["settled"] is True
        assert saved["usage"]["credits_micro"] == 0 and saved["usage"]["actual_micro"] == cost
    snapshot = local_meter.snapshot(workspace_id=row.workspace_id, venture_id=row.venture_id)
    assert snapshot["month_cost_usd_micro"] == 2 * cost
    assert sum(task["unconfirmed_calls"] for task in snapshot["tasks"]) == 0


@pytest.mark.hosted_only
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["explore", "recon"])
async def test_real_gate_without_account_refuses_before_search_io(paths, monkeypatch, entry):
    events = []
    candidate = provider(events)
    if entry == "explore":
        monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: candidate)
        callback = paths.explore.explore_web("coffee inventory", extract=0)
    else:
        callback = paths.research._search_one(candidate, "coffee inventory")
    with pytest.raises(paths.CreditsUnavailable):
        await callback
    assert events == []  # Actual S02 gate and pricing ran; no synthetic admission.


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["explore", "recon"])
async def test_async_search_has_one_admission_and_explicit_tenant(paths, monkeypatch, entry):
    events, receipts = [], []
    candidate = provider(events)
    async def admitted(callback, **kwargs):
        # Explicit synthetic reservation; this fixture checks the caller seam.
        receipts.append(kwargs)
        events.append("reserve")
        result = await callback()
        events.append("settle")
        return result
    monkeypatch.setattr(paths.metered, "call", admitted)
    with paths.bind(workspace_id="workspace-a", venture_id="venture-a", actor="person:test"):
        if entry == "explore":
            monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: candidate)
            result = await paths.explore.explore_web("coffee inventory", extract=0)
            assert result["mode"] == "live"
        else:
            monkeypatch.setattr(paths.research, "_provider_from_env", lambda: candidate)
            monkeypatch.setattr(paths.research, "_queries", lambda idea: ["coffee inventory"])
            result = await paths.research.research_recon("coffee inventory")
            assert result["mode"] == "live"
    assert events == ["reserve", "http", "settle"]
    assert len(receipts) == 1
    assert receipts[0]["workspace_id"] == "workspace-a"
    assert receipts[0]["venture_id"] == "venture-a"
    assert receipts[0]["estimate_micro"] == paths.metered.price_micro("firecrawl_search")


@pytest.mark.hosted_only
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["explore", "recon"])
async def test_unknown_provider_refuses_before_http(paths, monkeypatch, entry):
    events = []
    candidate = provider(events, name="unknown-paid-service")
    if entry == "explore":
        monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: candidate)
        callback = paths.explore.explore_web("coffee inventory", extract=0)
    else:
        callback = paths.research._search_one(candidate, "coffee inventory")
    with pytest.raises(paths.CreditsUnavailable):
        await callback
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["explore", "recon"])
async def test_async_uncertain_paid_failure_is_not_replayed_or_replaced(paths, monkeypatch, entry):
    events = []
    candidate = provider(events, error=httpx.ReadTimeout("offline uncertain outcome"))
    async def admitted(callback, **kwargs):
        events.append("reserve")
        return await callback()
    monkeypatch.setattr(paths.metered, "call", admitted)
    async def forbidden(*args, **kwargs):
        raise AssertionError("No keyless/platform fallback after configured provider failure")
    monkeypatch.setattr(paths.explore, "_platform_lookups", forbidden)
    monkeypatch.setattr(paths.research.DuckDuckGoProvider, "search", forbidden)
    with pytest.raises(httpx.ReadTimeout):
        if entry == "explore":
            monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: candidate)
            await paths.explore.explore_web("coffee inventory", extract=0)
        else:
            await paths.research._search_one(candidate, "coffee inventory", attempts=3)
    assert events == ["reserve", "http"]


@pytest.mark.asyncio
async def test_configured_rate_rejection_has_manual_recovery_without_free_fallback(paths, monkeypatch):
    events = []
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.firecrawl.dev/v2/search"))
    candidate = provider(events, error=httpx.HTTPStatusError("offline rate limit", request=response.request, response=response))
    async def admitted(callback, **kwargs):
        return await callback()
    async def forbidden(*args, **kwargs):
        raise AssertionError("No specialist fallback after configured rate rejection")
    monkeypatch.setattr(paths.metered, "call", admitted)
    monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: candidate)
    monkeypatch.setattr(paths.explore, "_platform_lookups", forbidden)
    result = await paths.explore.explore_web("coffee inventory", extract=0)
    assert result["mode"] == "error" and result["error_code"] == "search_rate_limited"
    assert result["hits"] == result["platforms"] == []
    assert events == ["http"]


@pytest.mark.asyncio
async def test_paid_recon_cancels_remaining_queries_after_uncertain_failure(paths, monkeypatch):
    started, finished = [], []
    pending = asyncio.Event()
    class Provider:
        name = "firecrawl"
        _limit = 6
        async def search(self, query, **kwargs):
            started.append(query)
            try:
                if query == "first":
                    await asyncio.sleep(0)
                    raise httpx.ReadTimeout("offline uncertain outcome")
                await pending.wait()
            finally:
                finished.append(query)
    async def admitted(callback, **kwargs):
        return await callback()  # Explicit synthetic paid admission.
    monkeypatch.setattr(paths.metered, "call", admitted)
    monkeypatch.setattr(paths.research, "_provider_from_env", Provider)
    monkeypatch.setattr(paths.research, "_queries", lambda idea: ["first", "second", "third", "fourth"])
    with pytest.raises(httpx.ReadTimeout):
        await paths.research.research_recon("inventory")
    assert set(started) == set(finished)
    assert len(started) < 4
    previous = list(started)
    await asyncio.sleep(0)
    assert started == previous


@pytest.mark.hosted_only
def test_sync_real_gate_no_account_precedes_provider_io(paths, monkeypatch):
    events = []
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: provider(events))
    with pytest.raises(paths.CreditsUnavailable):
        paths.discover._search_web("coffee inventory")
    assert events == []


@pytest.mark.asyncio
async def test_sync_seam_inside_async_caller_has_one_charge_and_dedup(paths, monkeypatch):
    events, receipts = [], []
    candidate = provider(events)
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: candidate)
    def admitted(callback, **kwargs):
        events.append("reserve")
        receipts.append(kwargs)
        result = callback()
        events.append("settle")
        return result
    monkeypatch.setattr(paths.metered, "call_sync", admitted)
    async def admitted_async(callback, **kwargs):
        events.append("reserve")
        receipts.append(kwargs)
        result = await callback()
        events.append("settle")
        return result
    monkeypatch.setattr(paths.metered, "call", admitted_async)
    with paths.bind(workspace_id="workspace-a", venture_id="venture-a", actor="person:test"):
        result = paths.discover._search_web("coffee inventory", workspace_id="workspace-a", venture_id="venture-a", limit=20)
    assert result and events == ["reserve", "http", "settle"]
    assert len(receipts) == 1 and receipts[0]["estimate_micro"] == paths.metered.price_micro("firecrawl_search", 2)
    assert receipts[0]["workspace_id"] == "workspace-a" and receipts[0]["venture_id"] == "venture-a"


def test_sync_configured_failure_never_falls_back_to_ddgs(paths, monkeypatch):
    events = []
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: provider(events, error=httpx.ReadTimeout("offline fixture")))
    monkeypatch.setattr(paths.metered, "call_sync", lambda callback, **kwargs: callback())
    async def admitted(callback, **kwargs):
        return await callback()
    monkeypatch.setattr(paths.metered, "call", admitted)
    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=lambda: pytest.fail("Keyless fallback must not run")))
    with pytest.raises(httpx.ReadTimeout):
        paths.discover._search_web("coffee inventory")
    assert events == ["http"]


def test_sync_cross_tenant_search_is_rejected_before_provider(paths, monkeypatch):
    events = []
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: provider(events))
    with paths.bind(workspace_id="workspace-a", venture_id="venture-a"):
        with pytest.raises(PermissionError):
            paths.discover._search_web("inventory", workspace_id="workspace-b", venture_id="venture-b")
    assert events == []


@pytest.mark.parametrize("limit,quantity", [(0, 1), (10, 1), (11, 2), (999, 2)])
def test_firecrawl_limit_uses_paid_result_batches(paths, monkeypatch, limit, quantity):
    events, receipts = [], []
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: provider(events))
    def admitted(callback, **kwargs):
        receipts.append(kwargs)
        return callback()
    monkeypatch.setattr(paths.metered, "call_sync", admitted)
    async def admitted_async(callback, **kwargs):
        receipts.append(kwargs)
        return await callback()
    monkeypatch.setattr(paths.metered, "call", admitted_async)
    paths.discover._search_web("inventory", limit=limit)
    assert receipts[0]["estimate_micro"] == paths.metered.price_micro("firecrawl_search", quantity)


def test_exa_search_never_exceeds_known_priced_result_limit(paths, monkeypatch):
    calls, receipts = [], []
    candidate = paths.research.HttpResearchProvider("exa", "offline-fixture")
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: candidate)
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_secrets import update_settings
        from app.research import local_providers

        update_settings("search", {"provider": "exa"}, {"api_key": "offline-fixture"})
        monkeypatch.setattr(local_providers, "search_provider", local_providers._configured_provider)
    original = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": []})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    def admitted(callback, **kwargs):
        receipts.append(kwargs)
        return callback()
    monkeypatch.setattr(paths.metered, "call_sync", admitted)
    async def admitted_async(callback, **kwargs):
        receipts.append(kwargs)
        return await callback()
    monkeypatch.setattr(paths.metered, "call", admitted_async)
    paths.discover._search_web("inventory", limit=20)
    assert b'"numResults":10' in calls[0].content
    assert receipts[0]["estimate_micro"] == paths.metered.price_micro("exa_search")


@pytest.mark.parametrize("name,expected", [("firecrawl_search", 10000), ("exa_search", 7000), ("tavily_search", 8000)])
def test_fixed_search_prices_use_primary_source_plan_rates(paths, name, expected):
    assert paths.metered.price_micro(name) == expected


@pytest.mark.hosted_only
@pytest.mark.asyncio
async def test_missing_price_refuses_search_before_admission_or_http(paths, monkeypatch):
    from app.estimate import meters
    events = []
    monkeypatch.setattr(meters, "load_meters", lambda: {"meters": {}})
    monkeypatch.setattr(paths.research, "_provider_from_env", lambda: provider(events))
    monkeypatch.setattr(paths.explore, "_provider_from_env", lambda: provider(events))
    with pytest.raises(paths.CreditsUnavailable):
        await paths.explore.explore_web("inventory", extract=0)
    with pytest.raises(paths.CreditsUnavailable):
        paths.discover._search_web("inventory")
    assert events == []


@pytest.mark.hosted_only
@pytest.mark.parametrize("dialect,broken", [("sqlite", False), ("postgresql", True)])
@pytest.mark.asyncio
async def test_hosted_limiter_non_pg_or_down_refuses_before_http(paths, monkeypatch, dialect, broken):
    calls = []
    monkeypatch.setenv("APP_ENV", "production")
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        def execute(self, *args):
            if broken:
                raise RuntimeError("offline DB failure with sensitive detail")
        def commit(self):
            pass
    monkeypatch.setattr(paths.research, "db", lambda: Session)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: calls.append(request)), **kwargs))
    with pytest.raises(paths.research.FirecrawlLimiterUnavailable) as error:
        await paths.research.HttpResearchProvider("firecrawl", "offline-fixture").search("inventory")
    assert calls == [] and "sensitive" not in str(error.value)
    assert error.value.status == 429


def test_pg_slot_is_shared_between_caller_scopes_and_commits_before_http(paths, monkeypatch):
    events, keys = [], []
    class Session:
        def __enter__(self):
            events.append("transaction")
            return self
        def __exit__(self, *args):
            events.append("closed")
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def execute(self, statement, params=None):
            if "pg_advisory_xact_lock" in str(statement):
                keys.append(params["key"])
                events.append("lock")
        def commit(self):
            events.append("commit")
    monkeypatch.setattr(paths.research, "db", lambda: Session)
    monkeypatch.setattr(paths.research, "time", SimpleNamespace(monotonic=lambda: 100,
        sleep=lambda delay: events.append(("sleep", delay))))
    for workspace, venture in (("workspace-a", "venture-a"), ("workspace-b", "venture-b")):
        with paths.bind(workspace_id=workspace, venture_id=venture):
            paths.research._pace_firecrawl()
            events.append("http")  # Offline seam: provider can start only after commit.
    assert keys == [paths.research._FIRECRAWL_SLOT_KEY] * 2
    assert events == ["transaction", "lock", ("sleep", 1.2), "commit", "closed", "http"] * 2


def test_pg_retry_after_does_not_wait_inside_metered_call(paths, monkeypatch):
    waits = []
    monkeypatch.setattr(paths.research, "_shared_firecrawl_wait", lambda delay, key: waits.append(delay) or True)
    assert paths.research._firecrawl_retry_after("4") == 4
    paths.research._pace_firecrawl()
    assert waits == [1.2]


def test_pg_backoff_transaction_wait_is_bounded(paths, monkeypatch):
    waits = []
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
                              execute=lambda *args: None, commit=lambda: None)
    monkeypatch.setattr(paths.research, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(paths.research, "time", SimpleNamespace(sleep=waits.append))
    assert paths.research._shared_firecrawl_wait(999999) is True
    assert waits == [30]
