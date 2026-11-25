"""Shared sliding windows and password lockouts; Redis is the hosted authority."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import threading
import time
import uuid
from collections import deque

from fastapi import HTTPException


WINDOW_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local retry = 0
for index, key in ipairs(KEYS) do
    local limit = tonumber(ARGV[index * 2])
    local window = tonumber(ARGV[index * 2 + 1])
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
    if redis.call('ZCARD', key) >= limit then
        local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
        retry = math.max(retry, tonumber(oldest[2]) + window - now)
    end
end
if retry > 0 then return math.ceil(retry) end
for index, key in ipairs(KEYS) do
    redis.call('ZADD', key, now, ARGV[1])
    redis.call('EXPIRE', key, math.ceil(tonumber(ARGV[index * 2 + 1])))
end
return 0
"""

FAILURE_SCRIPT = """
local now = tonumber(redis.call('TIME')[1])
local count = tonumber(redis.call('HGET', KEYS[1], 'count') or '0')
local start = tonumber(redis.call('HGET', KEYS[1], 'start') or '0')
local until_at = tonumber(redis.call('HGET', KEYS[1], 'until') or '0')
local level = tonumber(redis.call('HGET', KEYS[1], 'level') or '0')
if ARGV[1] == 'check' then return math.max(0, until_at - now) end
if now < until_at then return until_at - now end
if now - start >= tonumber(ARGV[3]) then count = 0; start = now end
count = count + 1
if count >= tonumber(ARGV[2]) then
    until_at = now + math.min(86400, tonumber(ARGV[3]) * 2 ^ level)
    level = math.min(tonumber(ARGV[4]), level + 1)
    count = 0
end
redis.call('HSET', KEYS[1], 'count', count, 'start', start, 'until', until_at, 'level', level)
redis.call('EXPIRE', KEYS[1], 172800)
return math.max(0, until_at - now)
"""


def identity_key(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def client_ip(request, *, proxy_hops: int = 0) -> str:
    """Count from the right in ALB append mode; direct requests never trust XFF."""
    peer = request.client.host if request.client else "unknown"
    forwarded = ",".join(request.headers.getlist("x-forwarded-for")).split(",")
    candidate = forwarded[-proxy_hops].strip() if proxy_hops > 0 and len(forwarded) >= proxy_hops else peer
    for address in (candidate, peer):
        if "%" in address:
            continue
        try:
            return str(ipaddress.ip_address(address))
        except ValueError:
            match = re.fullmatch(r"(?:\[([^\]]+)\]|([0-9.]+)):([0-9]{1,5})", address)
            if match and 0 < int(match.group(3)) <= 65535:
                try:
                    return str(ipaddress.ip_address(match.group(1) or match.group(2)))
                except ValueError:
                    pass
    return peer


def reject(retry: float) -> None:
    seconds = max(1, math.ceil(retry))
    raise HTTPException(429, detail={"code": "rate_limited", "retry_after": seconds,
                                   "message": f"You're going a bit fast - try again in {seconds} seconds"},
                        headers={"Retry-After": str(seconds)})


def limits(name: str, *defaults: tuple[int, int]) -> list[tuple[int, int]]:
    return [(max(1, int(os.getenv(f"RATE_LIMIT_{name}_{seconds}", str(count)))), seconds)
            for count, seconds in defaults]


def expensive_action(path: str, method: str) -> bool:
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if re.fullmatch(r"(?:/api)?/ventures/[^/]+/chat/proposals(?:/[^/]+(?:/(?:confirm|cancel|reconcile))?)?/?", path):
        return False
    parts = set(path.strip("/").split("/"))
    if path.rstrip("/").rsplit("/", 1)[-1] in {"telemetry", "cancel", "pause", "dismiss", "revoke", "retry-source"}:
        return False
    return bool(parts & {"chat", "research", "pagebuilder", "builder", "build", "builds", "corrections",
                         "outreach", "social", "content", "ai-gtm", "idealab", "idea", "plans",
                         "simulation", "handoffs", "discover", "iterate", "debug", "marketing", "community",
                         "coding", "gstack", "worldmodel", "charter", "concierge", "agents", "onboarding",
                         "greeting", "clarify", "validation", "experiments", "evolution", "org"}
                or "voice" in parts and parts & {"session", "tools", "summary"}
                or "gtm" in parts and parts & {"runs", "send", "prepare", "plan", "tick", "start"}
                or parts & {"tasks", "workflows"} and parts & {"run", "plan", "replan", "summary"}
                or path.rstrip("/").endswith(("/entry", "/chat/completions", "/responses")))


class RateLimiter:
    def __init__(self, *, redis_url: str | None = None, clock=time.time) -> None:
        self._url = os.getenv("REDIS_URL", "").strip() if redis_url is None else redis_url
        self._redis = None
        self._clock = clock
        self._windows: dict[str, deque] = {}
        self._failures: dict[str, tuple[int, float, float, int]] = {}
        self._lock = threading.RLock()
        self._sweep_at = 0.0
        self._down_until = 0.0

    def _redis_up(self) -> bool:
        return bool(self._url) and self._clock() >= self._down_until

    def _redis_failed(self) -> None:
        # ponytail: 5s breaker so a Redis outage costs one timeout, not one per request.
        self._down_until = self._clock() + 5

    def _client(self):
        if self._redis is None:
            import redis

            from redis.backoff import NoBackoff
            from redis.retry import Retry

            self._redis = redis.Redis.from_url(self._url, socket_connect_timeout=0.2, socket_timeout=0.2,
                                              retry=Retry(NoBackoff(), 0))
        return self._redis

    def _key(self, key: str) -> str:
        # ponytail: one Redis hash slot for atomic multi-key admission, shard by principal if throughput outgrows it.
        return "cos:rl:{limits}:" + identity_key(key)

    def _local(self, specs) -> int:
        now = self._clock()
        with self._lock:
            # ponytail: bounded process-local outage budget, Redis restores cross-replica enforcement.
            if now >= self._sweep_at:
                self._windows = {key: values for key, values in self._windows.items() if values and values[-1][1] > now}
                self._failures = {key: value for key, value in self._failures.items() if value[1] + 172800 > now}
                self._sweep_at = now + 60
            if len(self._windows) + len(self._failures) + len(specs) > 100000:
                return 60
            retry = 0.0
            for key, count, window in specs:
                values = self._windows.setdefault(key, deque())
                while values and values[0][0] <= now - window:
                    values.popleft()
                if len(values) >= count:
                    retry = max(retry, values[0][0] + window - now)
            if not retry:
                for key, _count, window in specs:
                    self._windows[key].append((now, now + window))
            return math.ceil(retry)

    def check(self, key: str, windows, *, auth: bool = False) -> None:
        self.check_many([(key, windows)], auth=auth)

    def check_many(self, groups, *, auth: bool = False) -> None:
        specs = [(f"{key}:{seconds}", count, seconds) for key, windows in groups for count, seconds in windows]
        if self._redis_up():
            try:
                args = [uuid.uuid4().hex]
                for _key, count, seconds in specs:
                    args.extend((count, seconds))
                retry = int(self._client().eval(WINDOW_SCRIPT, len(specs), *[self._key(key) for key, _, _ in specs], *args))
            except Exception:
                self._redis_failed()
                if not auth:
                    return
                retry = self._local(specs)
            else:
                if auth and not retry:
                    self._local(specs)
        elif self._url and not auth:
            return
        else:
            retry = self._local(specs)
        if retry:
            reject(retry)

    def check_or_raise(self, key: str, *, rate: float, capacity: float, code: str = "rate_limited") -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("invalid rate limit")
        self.check(key, [(int(capacity), math.ceil(capacity / rate))], auth=key.startswith(("auth", "admin", "device", "cognito")))

    def _failure(self, key: str, operation: str, max_fails: int, window_s: int) -> int:
        # Only IP keys escalate; account keys stay at one window so nobody can pin a victim out for a day.
        max_level = 6 if "-ip:" in key else 0
        if self._redis_up():
            try:
                if operation == "clear":
                    self._client().delete(self._key("failure:" + key))
                    retry = 0
                else:
                    retry = int(self._client().eval(FAILURE_SCRIPT, 1, self._key("failure:" + key), operation, max_fails, window_s, max_level))
            except Exception:
                self._redis_failed()
            else:
                self._local_failure(key, operation, max_fails, window_s)
                return retry
        return self._local_failure(key, operation, max_fails, window_s)

    def _local_failure(self, key: str, operation: str, max_fails: int, window_s: int) -> int:
        now = self._clock()
        with self._lock:
            if operation == "clear":
                self._failures.pop(key, None)
                return 0
            count, start, until, level = self._failures.get(key, (0, now, 0, 0))
            if operation == "check" and len(self._failures) >= 100000 and key not in self._failures:
                return window_s
            if now < until or operation == "check":
                return max(0, math.ceil(until - now))
            if len(self._failures) >= 100000 and key not in self._failures:
                return window_s
            if now - start >= window_s:
                count, start = 0, now
            count += 1
            if count >= max_fails:
                until, level, count = now + min(86400, window_s * 2 ** level), min(6 if "-ip:" in key else 0, level + 1), 0
            self._failures[key] = (count, start, until, level)
            return max(0, math.ceil(until - now))

    def attempt_allowed(self, key: str, *, max_fails: int = 5, window_s: int = 900) -> bool:
        max_fails, window_s = limits("PASSWORD", (max_fails, window_s))[0]
        retry = self._failure(key, "check", max_fails, window_s)
        if retry:
            reject(retry)
        return True

    def record_attempt_failure(self, key: str, *, max_fails: int = 5, window_s: int = 900) -> None:
        max_fails, window_s = limits("PASSWORD", (max_fails, window_s))[0]
        self._failure(key, "record", max_fails, window_s)

    def clear_attempt_failures(self, key: str) -> None:
        self._failure(key, "clear", 5, 900)

    login_allowed = attempt_allowed
    record_login_failure = record_attempt_failure
    clear_login_failures = clear_attempt_failures

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
            self._failures.clear()

    def check_ai(self, key: str) -> None:
        self.check(key, limits("AI", (60, 60), (200, 3600), (1000, 86400)), auth=True)

