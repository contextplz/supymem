from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import subprocess
import tempfile
import time

import pytest
from fastapi import HTTPException, Request

from core.ratelimit import RateLimiter, client_ip, expensive_action, identity_key


def test_sliding_windows_and_atomic_groups():
    now = [1000.0]
    limiter = RateLimiter(redis_url="", clock=lambda: now[0])
    groups = [("ip:a", [(2, 60), (3, 3600)]), ("account:a", [(2, 60)])]
    limiter.check_many(groups)
    now[0] += 30
    limiter.check_many(groups)
    with pytest.raises(HTTPException) as denied:
        limiter.check_many(groups)
    assert denied.value.headers == {"Retry-After": "30"}
    assert denied.value.detail["code"] == "rate_limited"
    now[0] += 30
    limiter.check_many(groups)
    now[0] += 60
    with pytest.raises(HTTPException) as hourly:
        limiter.check_many(groups)
    assert hourly.value.headers == {"Retry-After": "3480"}
    assert not limiter._windows["account:a:60"]
    now[0] += 3480
    limiter.check_many(groups)


def test_keys_are_normalized_private_and_independent():
    assert identity_key(" Founder@EXAMPLE.com ") == identity_key("founder@example.com")
    limiter = RateLimiter(redis_url="")
    assert "founder" not in limiter._key("founder@example.com")
    limiter.check("account:a", [(1, 60)])
    limiter.check("account:b", [(1, 60)])
    with pytest.raises(HTTPException):
        limiter.check("account:a", [(1, 60)])


@pytest.mark.parametrize("hops,forwarded,expected", [
    (0, "198.51.100.8", "127.0.0.1"),
    (1, "forged, 203.0.113.7", "203.0.113.7"),
    (2, "forged, 198.51.100.8, 203.0.113.7", "198.51.100.8"),
    (3, "198.51.100.8", "127.0.0.1"),
    (1, "203.0.113.7:4321", "203.0.113.7"),
    (1, "[2001:0DB8::1]:4321", "2001:db8::1"),
    (1, "forged, not-an-ip", "127.0.0.1"),
    (1, "forged,", "127.0.0.1"),
    (1, "fe80::1%en0", "127.0.0.1"),
])
def test_proxy_hops(hops, forwarded, expected):
    request = Request({"type": "http", "client": ("127.0.0.1", 1000),
                       "headers": [(b"x-forwarded-for", forwarded.encode())]})
    assert client_ip(request, proxy_hops=hops) == expected


class OfflineRedis:
    def eval(self, *args):
        raise ConnectionError("redis unavailable")


def test_redis_outage_is_conservative_for_auth_only():
    limiter = RateLimiter(redis_url="redis://unused")
    limiter._redis = OfflineRedis()
    for _attempt in range(10):
        limiter.check("public:a", [(1, 60)])
    limiter.check("auth:a", [(1, 60)], auth=True)
    with pytest.raises(HTTPException):
        limiter.check("auth:a", [(1, 60)], auth=True)


def test_auth_shadow_survives_redis_outage():
    limiter = RateLimiter(redis_url="redis://unused")
    limiter._redis = type("Healthy", (), {"eval": lambda *args: 0})()
    limiter.check("auth:a", [(1, 60)], auth=True)
    limiter._redis = OfflineRedis()
    with pytest.raises(HTTPException):
        limiter.check("auth:a", [(1, 60)], auth=True)


def test_password_lockout_exponent_and_success_reset():
    now = [1000.0]
    limiter = RateLimiter(redis_url="", clock=lambda: now[0])
    for key, windows in (("password-ip:a", (900, 1800)), ("password-account:a", (900, 900))):
        for seconds in windows:
            for _attempt in range(5):
                assert limiter.attempt_allowed(key)
                limiter.record_attempt_failure(key)
            with pytest.raises(HTTPException) as denied:
                limiter.attempt_allowed(key)
            assert denied.value.headers == {"Retry-After": str(seconds)}
            now[0] += seconds
    limiter.clear_attempt_failures("password-account:a")
    assert limiter.attempt_allowed("password-account:a")


def test_local_counter_is_thread_safe():
    limiter = RateLimiter(redis_url="")

    def attempt(_number):
        try:
            limiter.check("same", [(10, 60)])
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(100))) == 10


@pytest.mark.parametrize("path", ["/api/ventures/ven_a/chat", "/api/entry/chat/preview",
    "/api/ventures/ven_a/research/dossier/regenerate", "/api/ventures/ven_a/pagebuilder/build",
    "/api/ventures/ven_a/gtm/journey/send", "/api/entry/voice/session", "/api/v1/content/social/post",
    "/openai/v1/chat/completions"])
def test_expensive_routes_cannot_bypass_caps(path):
    assert expensive_action(path, "POST")
    assert not expensive_action(path, "GET")
    assert not expensive_action(path + "/cancel", "POST")


def test_resource_name_cannot_disable_ai_cap():
    assert expensive_action("/api/ventures/cancel/chat", "POST")
    assert expensive_action("/api/workspaces/telemetry/pagebuilder/build", "POST")


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("suffix", ["", "/cpr_a", "/cpr_a/confirm", "/cpr_a/cancel", "/cpr_a/reconcile"])
def test_plan_approval_cards_do_not_spend_ai_admission(prefix, suffix):
    assert not expensive_action(f"{prefix}/ventures/ven_a/chat/proposals{suffix}", "POST")
    assert expensive_action(f"{prefix}/ventures/ven_a/chat/proposals/cpr_a/build", "POST")
    assert expensive_action(f"{prefix}/entry/chat/proposals/cpr_a/name", "POST")


@pytest.mark.asyncio
async def test_three_same_ip_signup_and_founder_journeys_fit_default_limits(monkeypatch):
    """Hosted signup/device flow plus run_founder_live_journeys.py's preview/cancel/confirm/readback."""
    import json

    from tests.integration.test_postgres_auth_security import service_modules

    for name in tuple(os.environ):
        if name.startswith("RATE_LIMIT_"):
            monkeypatch.delenv(name)
    with service_modules("gateway"):
        from app import ratelimit

        edge = RateLimiter(redis_url="", clock=lambda: 1000)
        core = RateLimiter(redis_url="", clock=lambda: 1000)
        monkeypatch.setattr(ratelimit, "limiter", edge)

        async def admit(method, path, body=None, subject=None):
            async def receive():
                return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}

            request = Request({"type": "http", "method": method, "path": path,
                               "headers": [(b"content-type", b"application/json")],
                               "client": ("203.0.113.7", 1234)}, receive)
            await ratelimit.check_edge(request)
            if subject:
                ratelimit.check_subject(request, subject)
                if expensive_action(path, method):
                    core.check_ai(f"ai-workspace:{subject}")

        for journey in range(3):
            subject = f"founder-{journey}"
            email = f"founder-{journey}@example.test"
            token = f"verification-{journey}"
            await admit("POST", "/auth/signup", {"email": email})
            await admit("POST", "/auth/verify-email", {"token": token})
            await admit("POST", "/auth/login", {"email": email})
            await admit("GET", "/auth/device/approve", subject=subject)
            await admit("POST", "/auth/device/approve", {"code": token}, subject)
            await admit("POST", "/auth/device/token", {"code": token})
            await admit("POST", "/auth/login", {"passphrase": "operator"})
            for _read in range(3):
                await admit("GET", "/auth/session", subject=subject)
                await admit("GET", "/api/ventures", subject=subject)
                await admit("GET", "/api/entry/chat/history", subject=subject)
            await admit("POST", "/api/entry/chat/preview", subject=subject)
            await admit("POST", "/api/entry/chat/proposals/hello/cancel", subject=subject)
            await admit("POST", "/api/entry/chat/preview", subject=subject)
            await admit("POST", "/api/entry/chat/proposals/idea/name", subject=subject)
            await admit("POST", "/api/entry/chat/confirm", subject=subject)
            await admit("GET", "/api/ventures/ven_a", subject=subject)
            await admit("GET", "/api/entry/chat/history", subject=subject)
            await admit("GET", "/public/waitlists/ven_a")
            await admit("POST", "/public/waitlists/ven_a", {"email": email})


@pytest.fixture
def redis_server():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is required for the real Lua check")
    import redis

    directory = tempfile.TemporaryDirectory(prefix="rl-", dir="/tmp")
    socket = directory.name + "/redis.sock"
    log = tempfile.TemporaryFile()
    process = subprocess.Popen([executable, "--port", "0", "--unixsocket", socket,
                                "--save", "", "--appendonly", "no"], stdout=log, stderr=log)
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    client = redis.Redis(unix_socket_path=socket, socket_timeout=0.1, socket_connect_timeout=0.1,
                         retry=Retry(NoBackoff(), 0))
    try:
        for _attempt in range(100):
            if process.poll() is not None:
                log.seek(0)
                output = log.read().decode()
                if "Operation not permitted" in output:
                    pytest.skip("sandbox prohibits Redis socket binding")
                pytest.fail(output)
            try:
                if client.ping():
                    break
            except redis.ConnectionError:
                time.sleep(0.02)
        else:
            pytest.fail("test Redis did not start")
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
        log.close()
        directory.cleanup()


def test_real_redis_lua_is_atomic_across_instances_and_expires(redis_server):
    instances = [RateLimiter(redis_url="redis://test") for _number in range(8)]
    for limiter in instances:
        limiter._redis = redis_server

    def attempt(number):
        try:
            instances[number % 8].check_many([("ip:a", [(10, 60)]), ("account:a", [(10, 60)])], auth=True)
            return True
        except HTTPException:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(100))) == 10
    key = instances[0]._key("account:a:60")
    assert redis_server.zcard(key) == 10
    assert 0 < redis_server.ttl(key) <= 60
    now = float(redis_server.time()[0])
    for member in redis_server.zrange(key, 0, -1):
        redis_server.zadd(key, {member: now - 61})
    other_key = instances[0]._key("ip:a:60")
    for member in redis_server.zrange(other_key, 0, -1):
        redis_server.zadd(other_key, {member: now - 61})
    instances[0].check_many([("ip:a", [(10, 60)]), ("account:a", [(10, 60)])], auth=True)
    assert redis_server.zcard(key) == 1


def test_real_redis_password_lockout_is_shared(redis_server):
    first, second = RateLimiter(redis_url="redis://test"), RateLimiter(redis_url="redis://test")
    first._redis = second._redis = redis_server
    for _attempt in range(5):
        first.record_attempt_failure("password-ip:a")
    with pytest.raises(HTTPException) as denied:
        second.attempt_allowed("password-ip:a")
    assert denied.value.detail["retry_after"] >= 899
    key = first._key("failure:password-ip:a")
    redis_server.hset(key, mapping={"until": 0, "start": 0})
    for _attempt in range(5):
        second.record_attempt_failure("password-ip:a")
    with pytest.raises(HTTPException) as denied:
        first.attempt_allowed("password-ip:a")
    assert denied.value.detail["retry_after"] >= 1799
    second.clear_attempt_failures("password-ip:a")
    assert first.attempt_allowed("password-ip:a")


def test_redis_outage_trips_breaker_instead_of_timing_out_every_request():
    calls = []

    class Slow:
        def eval(self, *args):
            calls.append(1)
            raise TimeoutError

    limiter = RateLimiter(redis_url="redis://unused")
    limiter._redis = Slow()
    for _attempt in range(5):
        limiter.check("public:a", [(1, 60)])
    assert len(calls) == 1
