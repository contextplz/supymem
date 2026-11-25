"""Organization and account admission before any Composio HTTP execution."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from app.core.local_worker import redis_url
from app.core.ratelimit import RateLimitedJob


class RateLimited(RateLimitedJob):
    def __init__(self, seconds):
        super().__init__(max(1, math.ceil(seconds)))
        self.args = ("composio_rate_limited",)


class AdmissionLimited(RateLimited):
    """Admission refused before any provider request, so retrying is safe."""


BACKGROUND = ContextVar("composio_background", default=False)
_LOCK = threading.Lock()
_BUCKETS = {}
_REDIS_DOWN_UNTIL = 0.0
_REDIS_WARNING_AT = float("-inf")
_SCRIPT = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) + tonumber(stamp[2]) / 1000000
local wait, values = 0, {}
for index, key in ipairs(KEYS) do
    local rate = tonumber(ARGV[index]) / 60
    local cap = math.max(index == 1 and 1 or 2, index == 1 and rate or tonumber(ARGV[index]))
    local required = 1 + (index == 2 and tonumber(ARGV[#KEYS+1]) or 0)
    local at = tonumber(redis.call('HGET', key, 'at')) or now
    local tokens = math.min(cap, (tonumber(redis.call('HGET', key, 'tokens')) or cap) + math.max(0, now-at)*rate)
    local blocked = tonumber(redis.call('HGET', key, 'blocked')) or 0
    wait = math.max(wait, (required-tokens)/rate, blocked-now)
    values[index] = tokens
end
for index, key in ipairs(KEYS) do
    redis.call('HSET', key, 'tokens', values[index] - (wait <= 0 and 1 or 0), 'at', now)
    redis.call('EXPIRE', key, 86400)
end
return math.ceil(math.max(0, wait)*1000)
"""


@lru_cache(maxsize=1)
def _client(url):
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    return redis.Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0))


def _keys(account):
    org = hashlib.sha256(os.getenv("COMPOSIO_ORG_ID", "platform").encode()).hexdigest()[:24]
    base = "cos:composio:{" + org + "}:"
    return [base + "org"] + ([base + hashlib.sha256(account.encode()).hexdigest()] if account else [])


def _redis_failed():
    global _REDIS_DOWN_UNTIL, _REDIS_WARNING_AT
    with _LOCK:
        now = time.monotonic()
        _REDIS_DOWN_UNTIL = now + 30
        if now >= _REDIS_WARNING_AT:
            _REDIS_WARNING_AT = now + 60
            logging.getLogger(__name__).warning("composio_pacing_redis_unavailable")


def acquire(account="", *, background=False):
    background = background or BACKGROUND.get()
    keys = _keys(account)
    rates = [max(1, int(os.getenv("COMPOSIO_ORG_RPM", "2000")))]
    if account:
        rates.append(max(1, int(os.getenv("COMPOSIO_ACCOUNT_RPM", "60"))))
    url = redis_url()
    wait = None
    if url and time.monotonic() >= _REDIS_DOWN_UNTIL:
        try:
            wait = _client(url).eval(_SCRIPT, len(keys), *keys, *rates, int(background and bool(account))) / 1000
        except Exception:
            _redis_failed()
    if wait is None:
        # shortcut: local fallback has per-replica budgets; upgrade by fixing Redis reachability.
        with _LOCK:
            now, wait, values = time.monotonic(), 0, []
            for index, (key, rpm) in enumerate(zip(keys, rates)):
                rate = rpm / 60
                cap = max(1, rate) if index == 0 else max(2, rpm)
                required = 1 + (int(background) if index == 1 else 0)
                tokens, at, blocked = _BUCKETS.get(key, (cap, now, 0))
                tokens = min(cap, tokens + max(0, now - at) * rate)
                values.append((key, tokens, blocked))
                wait = max(wait, (required - tokens) / rate, blocked - now)
            for key, tokens, blocked in values:
                _BUCKETS[key] = (tokens - (1 if wait <= 0 else 0), now, blocked)
    if wait > 0:
        raise AdmissionLimited(wait)


def reject(account, retry_after):
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 30
    seconds = max(1, seconds) if math.isfinite(seconds) else 30
    key = _keys(account)[-1]
    url = redis_url()
    recorded = False
    if url and time.monotonic() >= _REDIS_DOWN_UNTIL:
        try:
            _client(url).eval("""
local stamp = redis.call('TIME')
local until_time = tonumber(stamp[1]) + tonumber(ARGV[1])
local previous = tonumber(redis.call('HGET', KEYS[1], 'blocked')) or 0
redis.call('HSET', KEYS[1], 'blocked', math.max(previous, until_time))
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[1])) + 120)
""", 1, key, seconds)
        except Exception:
            _redis_failed()
        else:
            recorded = True
    if not recorded:
        with _LOCK:
            now = time.monotonic()
            tokens, at, blocked = _BUCKETS.get(key, (0, now, 0))
            _BUCKETS[key] = (tokens, at, max(blocked, now + seconds))
    raise RateLimited(seconds)


def account_key(path, body=None, params=None):
    return str((body or {}).get("connected_account_id") or
        (path.rsplit("/", 1)[-1] if path.startswith("/connected_accounts/") else "") or
        (params or {}).get("user_ids") or "")


def check_response(account, data, retry_after=None):
    if not isinstance(data, dict) or data.get("successful") is not False:
        return
    error = data.get("error")
    code = (error.get("status_code") or error.get("status") or error.get("code")) if isinstance(error, dict) else None
    if str(code) == "429" or re.search(r"\b(?:HTTP|status(?:\s+code)?)[\s:=]+429\b", str(error or ""), re.I):
        reject(account, retry_after or (error.get("retry_after") if isinstance(error, dict) else None))
