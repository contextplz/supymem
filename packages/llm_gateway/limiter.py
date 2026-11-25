"""Optional atomic platform TPM/RPM admission, shared by all Redis-backed callers."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from openai import BadRequestError, RateLimitError, _base_client

httpx = getattr(_base_client, "httpx", None) or _base_client.httpx2

_CLASS = ContextVar("llm_job_class", default="interactive")
# ponytail: one platform quota pool, split buckets only for independently provisioned quotas.
_KEY = "cos:llm:platform_bucket"
_RESERVE = 0.1
_BUCKET = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) + tonumber(stamp[2]) / 1000000
local previous = tonumber(redis.call('HGET', KEYS[1], 'at')) or now
local elapsed = math.max(0, now - previous)
local rpm, tpm, cost, reserve = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
local request_cap = math.max(1, rpm / 60)
local requests = math.min(request_cap, (tonumber(redis.call('HGET', KEYS[1], 'requests')) or request_cap) + elapsed * rpm / 60)
local tokens = math.min(tpm, (tonumber(redis.call('HGET', KEYS[1], 'tokens')) or tpm) + elapsed * tpm / 60)
local reserved_requests = math.min(request_cap - 1, request_cap * reserve)
local wait = math.max(0, (1 + reserved_requests - requests) * 60 / rpm, (cost + tpm * reserve - tokens) * 60 / tpm)
if wait == 0 then
    requests = requests - 1
    tokens = tokens - cost
end
redis.call('HSET', KEYS[1], 'requests', requests, 'tokens', tokens, 'at', now)
redis.call('EXPIRE', KEYS[1], 120)
return math.ceil(wait * 1000)
"""


@contextmanager
def request_class(value: str):
    token = _CLASS.set(value)
    try:
        yield
    finally:
        _CLASS.reset(token)


@lru_cache(maxsize=1)
def _client(url):
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return redis.Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2,
                                retry=Retry(NoBackoff(), 0))


def request_hooks(config):
    if config.source == "founder_override":
        return []
    url = os.environ.get("REDIS_URL", "").strip()
    flag = os.environ.get("LLM_ADMISSION_V1", "1" if url else "0").lower()
    if flag not in {"0", "1", "false", "true"}:
        raise ValueError("LLM_ADMISSION_V1 must be a boolean")
    configured = "LLM_RATE_LIMIT_RPM" in os.environ or "LLM_RATE_LIMIT_TPM" in os.environ
    if flag in {"0", "false"} and not configured:
        return []
    defaults = ("6000", "10000000") if not configured else ("0", "0")
    rpm = int(os.environ.get("LLM_RATE_LIMIT_RPM", defaults[0]))
    tpm = int(os.environ.get("LLM_RATE_LIMIT_TPM", defaults[1]))
    if rpm == tpm == 0:
        return []
    if rpm <= 0 or tpm <= 0 or not url:
        raise ValueError("LLM rate limiting requires positive RPM, TPM and REDIS_URL")
    reservation = float(os.environ.get("LLM_INTERACTIVE_RESERVE", str(_RESERVE)))
    if not 0 <= reservation < 1:
        raise ValueError("LLM_INTERACTIVE_RESERVE must be in [0, 1)")

    async def admit(request):
        if request.method != "POST":
            return
        body = json.loads(await request.aread())
        output = body.get("max_completion_tokens", body.get("max_output_tokens", body.get("max_tokens", config.max_output_tokens)))
        if type(output) is not int or output < 1:
            raise BadRequestError("Request requires a positive output token limit.",
                                  response=httpx.Response(400, request=request), body=None)
        envelope = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        inputs = body.get("messages", body.get("input", []))
        # ponytail: UTF-8 bytes overestimate text tokens; use a tokenizer if conservative throttling hurts throughput.
        cost = len(envelope) + 128 + 16 * (len(inputs) if isinstance(inputs, list) else 1) + output
        reserve = reservation if _CLASS.get() == "bulk" else 0.0
        if cost > tpm * (1 - reserve):
            raise BadRequestError("Request exceeds the configured token bucket capacity.",
                                  response=httpx.Response(400, request=request), body=None)
        deadline = time.monotonic() + config.timeout_seconds
        while True:
            try:
                wait_ms = await asyncio.to_thread(_client(url).eval, _BUCKET, 1, _KEY, rpm, tpm, cost, reserve)
            except (OSError, TimeoutError, __import__('redis').exceptions.RedisError):
                # Limiter outage must not block founders' model calls: admit and log (provider limits still apply).
                import logging
                logging.getLogger(__name__).warning("llm_limiter_unavailable")
                return
            if wait_ms == 0:
                return
            wait = wait_ms / 1000
            if time.monotonic() + wait >= deadline:
                raise RateLimitError("Shared model quota is busy; retry later.",
                    response=httpx.Response(429, request=request, headers={"Retry-After": str(math.ceil(wait))}), body=None)
            await asyncio.sleep(wait)

    return [admit]
