"""Retry, backoff, circuit breaker, concurrency limiter."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    open_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        # Once the open interval expires, allow a half-open attempt.
        return time.monotonic() - self.opened_at < self.open_seconds


@dataclass
class Reliability:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 20.0
    jitter: bool = True
    max_concurrency: int = 4
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    _sem: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrency)
        return self._sem

    def _delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(float(retry_after), self.max_delay)
        d = min(self.base_delay * (2**attempt), self.max_delay)
        if self.jitter:
            d *= 0.5 + random.random()
        return d

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        if self.breaker.is_open:
            raise RuntimeError("circuit_open: model backend degraded")
        last_exc: BaseException | None = None
        async with self._semaphore():
            for attempt in range(self.max_attempts):
                try:
                    result = await fn()
                    self.breaker.record_success()
                    return result
                except Exception as exc:
                    last_exc = exc
                    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                    if status is None:
                        status = getattr(
                            getattr(exc, "response", None), "status_code", None
                        )
                    retryable = (
                        status in RETRYABLE_STATUS
                        or bool(getattr(exc, "retryable", False))
                        or isinstance(
                            exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)
                        )
                    )
                    if not retryable or attempt + 1 >= self.max_attempts:
                        self.breaker.record_failure()
                        raise
                    retry_after = None
                    headers = getattr(getattr(exc, "response", None), "headers", None)
                    if headers and "retry-after" in {k.lower() for k in headers}:
                        try:
                            retry_after = float(headers.get("Retry-After") or headers.get("retry-after"))
                        except (TypeError, ValueError):
                            retry_after = None
                    await asyncio.sleep(self._delay(attempt, retry_after))
        assert last_exc is not None
        raise last_exc
