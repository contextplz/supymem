import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openai import APIConnectionError, BadRequestError, RateLimitError

from llm_gateway import limiter
from llm_gateway.provider_foundry import ProviderConfig
from tests.unit.test_shared_ratelimit import redis_server as redis_server


def test_limiter_lua_token_and_request_refill_without_sockets():
    lua = pytest.importorskip("lupa").LuaRuntime()
    lua.execute("""
        now = 1000
        state = {}
        KEYS = {'test'}
        redis = {call = function(command, ...)
            local args = {...}
            if command == 'TIME' then return {now, 0} end
            if command == 'HGET' then return state[args[2]] end
            if command == 'HSET' then
                for index = 2, #args, 2 do state[args[index]] = args[index + 1] end
            end
        end}
    """)
    lua.globals().ARGV = lua.table_from([6000, 1000, 100, 0.1])
    assert [lua.execute(limiter._BUCKET) for _attempt in range(9)] == [0] * 9
    assert lua.execute(limiter._BUCKET) == 6000
    lua.globals().ARGV[4] = 0
    assert lua.execute(limiter._BUCKET) == 0
    assert lua.execute(limiter._BUCKET) == 6000
    lua.globals().now += 6
    assert lua.execute(limiter._BUCKET) == 0
    lua.globals().state = lua.table()
    lua.globals().ARGV = lua.table_from([60, 10000, 1, 0])
    assert lua.execute(limiter._BUCKET) == 0
    assert lua.execute(limiter._BUCKET) == 1000
    lua.globals().now += 1
    assert lua.execute(limiter._BUCKET) == 0


def test_limiter_local_defaults_off_and_requires_complete_configuration(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("LLM_ADMISSION_V1", raising=False)
    monkeypatch.delenv("LLM_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("LLM_RATE_LIMIT_TPM", raising=False)
    assert limiter.request_hooks(ProviderConfig()) == []
    monkeypatch.setenv("LLM_RATE_LIMIT_RPM", "60")
    with pytest.raises(ValueError):
        limiter.request_hooks(ProviderConfig())
    assert limiter.request_hooks(ProviderConfig(source="founder_override")) == []


def test_explicit_admission_off_preserves_configured_quotas(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://stub")
    monkeypatch.setenv("LLM_ADMISSION_V1", "0")
    monkeypatch.setenv("LLM_RATE_LIMIT_RPM", "75")
    monkeypatch.setenv("LLM_RATE_LIMIT_TPM", "25000")
    calls = []

    class Quota:
        def eval(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(limiter, "_client", lambda _url: Quota())

    async def run():
        hook, = limiter.request_hooks(ProviderConfig())
        for domain in ("Warehouse inspections", "Family meal planning"):
            await hook(httpx.Request("POST", "https://stub.invalid/chat/completions", json={
                "messages": [{"role": "user", "content": domain}], "max_tokens": 100}))

    asyncio.run(run())
    assert [call[3:5] for call in calls] == [(75, 25000)] * 2
    monkeypatch.setenv("LLM_RATE_LIMIT_TPM", "0")
    with pytest.raises(ValueError):
        limiter.request_hooks(ProviderConfig())


@pytest.mark.parametrize("domain", ["Factory maintenance", "Family travel"])
def test_admission_flag_generous_defaults_and_request_reservation(monkeypatch, domain):
    monkeypatch.setenv("REDIS_URL", "redis://stub")
    monkeypatch.setenv("LLM_ADMISSION_V1", "1")
    monkeypatch.delenv("LLM_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("LLM_RATE_LIMIT_TPM", raising=False)
    calls = []

    class StubRedis:
        def eval(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(limiter, "_client", lambda url: StubRedis())
    hook = limiter.request_hooks(ProviderConfig())[0]

    async def run():
        request = httpx.Request("POST", "https://stub.invalid/v1/chat/completions",
            json={"messages": [{"role": "user", "content": domain}], "max_tokens": 32})
        with limiter.request_class("bulk"):
            await hook(request)
        await hook(request)

    asyncio.run(run())
    assert [call[3:5] for call in calls] == [(6000, 10000000)] * 2
    assert [call[-1] for call in calls] == [0.1, 0.0]
    monkeypatch.setenv("LLM_ADMISSION_V1", "0")
    assert limiter.request_hooks(ProviderConfig()) == []
    monkeypatch.setenv("LLM_ADMISSION_V1", "1")
    monkeypatch.setenv("LLM_INTERACTIVE_RESERVE", "1")
    with pytest.raises(ValueError):
        limiter.request_hooks(ProviderConfig())

    lua = pytest.importorskip("lupa").LuaRuntime()
    lua.execute("now = 1000; state = {}; KEYS = {'test'}; redis = {call = function(command, ...) "
        "local args = {...}; if command == 'TIME' then return {now, 0} end; "
        "if command == 'HGET' then return state[args[2]] end; "
        "if command == 'HSET' then for index = 2, #args, 2 do state[args[index]] = args[index + 1] end end end}")
    lua.globals().ARGV = lua.table_from([600, 1000000, 1, 0.1])
    assert [lua.execute(limiter._BUCKET) for attempt in range(9)] == [0] * 9
    assert lua.execute(limiter._BUCKET) > 0
    lua.globals().ARGV[4] = 0
    assert lua.execute(limiter._BUCKET) == 0


def test_limiter_atomic_across_workers_and_reserves_interactive_tokens(redis_server):
    def admit(_number):
        return redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 6000, 1000, 100, 0.1)

    with ThreadPoolExecutor(max_workers=16) as pool:
        waits = list(pool.map(admit, range(16)))
    assert waits.count(0) == 9
    assert redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 6000, 1000, 100, 0) == 0
    assert redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 6000, 1000, 100, 0) > 0
    assert 0 < redis_server.ttl(limiter._KEY) <= 120


def test_limiter_paces_requests_and_refills(redis_server):
    assert redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 60, 10000, 1, 0) == 0
    assert redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 60, 10000, 1, 0) > 0
    stamp = redis_server.time()
    redis_server.hset(limiter._KEY, "at", stamp[0] + stamp[1] / 1_000_000 - 2)
    assert redis_server.eval(limiter._BUCKET, 1, limiter._KEY, 60, 10000, 1, 0) == 0


def test_limiter_counts_requested_output_and_fails_closed(monkeypatch):
    monkeypatch.setenv("LLM_RATE_LIMIT_RPM", "60")
    monkeypatch.setenv("LLM_RATE_LIMIT_TPM", "10000")
    monkeypatch.setenv("REDIS_URL", "redis://test")
    calls = []

    class FakeRedis:
        def eval(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(limiter, "_client", lambda _url: FakeRedis())
    hook = limiter.request_hooks(ProviderConfig(timeout_seconds=1))[0]

    async def run():
        request = httpx.Request("POST", "https://provider.test/chat/completions", json={
            "model": "deployment", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 500})
        with limiter.request_class("bulk"):
            await hook(request)
        assert calls[0][-2] >= 500 and calls[0][-1] == 0.1
        await hook(request)
        assert calls[1][-1] == 0.0
        with pytest.raises(BadRequestError):
            await hook(httpx.Request("POST", "https://provider.test/responses", json={"input": "hi", "max_output_tokens": 10000}))
        for invalid_output in (-100, 0, True, "500", None):
            with pytest.raises(BadRequestError):
                await hook(httpx.Request("POST", "https://provider.test/responses", json={"input": "hi", "max_output_tokens": invalid_output}))

        class DownRedis:
            def eval(self, *args):
                raise ConnectionError("unavailable")

        monkeypatch.setattr(limiter, "_client", lambda _url: DownRedis())
        assert await hook(request) is None  # limiter outage admits the call (fail-open)

        class BusyRedis:
            def eval(self, *args):
                return 2000

        monkeypatch.setattr(limiter, "_client", lambda _url: BusyRedis())
        with pytest.raises(RateLimitError) as blocked:
            await hook(request)
        assert blocked.value.response.headers["Retry-After"] == "2"

    asyncio.run(run())


@pytest.mark.parametrize("factory", ["create_async_client", "create_async_client_from_config"])
def test_limiter_is_on_real_sdk_chat_and_responses_requests(monkeypatch, factory):
    from openai import DefaultAsyncHttpxClient, _base_client
    from llm_gateway import provider_foundry

    sdk_http = getattr(_base_client, "httpx", None) or _base_client.httpx2
    monkeypatch.setenv("LLM_API_KEY", "offline")
    monkeypatch.setenv("LLM_BASE_URL", "https://offline.invalid/openai/v1")
    monkeypatch.setenv("LLM_RATE_LIMIT_RPM", "60")
    monkeypatch.setenv("LLM_RATE_LIMIT_TPM", "10000")
    monkeypatch.setenv("REDIS_URL", "redis://test")
    charges = []

    class Quota:
        def eval(self, *args):
            charges.append(args)
            return 0

    async def reply(request):
        assert len(charges) == 1
        return sdk_http.Response(200, json={"id": "offline", "choices": [], "output": []})

    def transport(**kwargs):
        return DefaultAsyncHttpxClient(**{**kwargs, "transport": sdk_http.MockTransport(reply)})

    monkeypatch.setattr(limiter, "_client", lambda _url: Quota())
    monkeypatch.setattr(provider_foundry, "DefaultAsyncHttpxClient", transport)

    async def run():
        config = ProviderConfig(api_key="offline", base_url="https://offline.invalid/openai/v1")
        client, _config = getattr(provider_foundry, factory)(config)
        try:
            await client.chat.completions.create(model="offline", messages=[{"role": "user", "content": "hello"}], max_tokens=123)
            assert charges[0][-2] >= 123
            charges.clear()
            await client.responses.create(model="offline", input="hi", max_output_tokens=456)
            assert charges[0][-2] >= 456
        finally:
            await client.close()

    asyncio.run(run())
