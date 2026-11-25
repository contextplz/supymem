"""Rate limits, backoff and poll intervals (39 §6).

On 429 we honour ``Retry-After`` and halve that provider's concurrency for ten
minutes; auth failures are never retried, because retrying a 401 in a loop is
how an integration gets an account locked.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, TypeVar

from app.connectors.protocol import AuthExpired, RateLimited
from core.clock import utcnow

THROTTLE_WINDOW = timedelta(minutes=10)
MIN_POLL_S = 60
MAX_POLL_S = 30 * 60
DEFAULT_ATTEMPTS = 3
MAX_BACKOFF_S = 60.0

T = TypeVar("T")


@dataclass
class ProviderState:
    provider: str
    base_concurrency: int = 4
    throttled_until: datetime | None = None
    last_retry_after_s: float = 0.0
    rate_limit_hits: int = 0
    waits: list[float] = field(default_factory=list)

    def concurrency(self, *, now: datetime | None = None) -> int:
        moment = now or utcnow()
        if self.throttled_until and moment < self.throttled_until:
            return max(1, self.base_concurrency // 2)
        return self.base_concurrency

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "concurrency": self.concurrency(),
            "base_concurrency": self.base_concurrency,
            "throttled_until": (
                self.throttled_until.isoformat() if self.throttled_until else None
            ),
            "rate_limit_hits": self.rate_limit_hits,
        }


_STATES: dict[str, ProviderState] = {}


def state(provider: str, *, base_concurrency: int | None = None) -> ProviderState:
    st = _STATES.get(provider)
    if st is None:
        st = ProviderState(provider=provider)
        _STATES[provider] = st
    if base_concurrency is not None:
        st.base_concurrency = base_concurrency
    return st


def reset(provider: str | None = None) -> None:
    if provider is None:
        _STATES.clear()
    else:
        _STATES.pop(provider, None)


def note_rate_limited(provider: str, retry_after_s: float) -> ProviderState:
    st = state(provider)
    st.rate_limit_hits += 1
    st.last_retry_after_s = retry_after_s
    st.throttled_until = utcnow() + THROTTLE_WINDOW
    return st


def concurrency(provider: str) -> int:
    return state(provider).concurrency()


def backoff_delay(attempt: int, *, retry_after_s: float | None = None) -> float:
    """``Retry-After`` wins; otherwise exponential with a small jitter."""
    if retry_after_s is not None and retry_after_s > 0:
        return min(retry_after_s, MAX_BACKOFF_S)
    base = min(2.0 ** max(0, attempt - 1), MAX_BACKOFF_S)
    return base + random.uniform(0, 0.05)


def next_poll_interval_s(base_s: int, empty_streak: int) -> int:
    """Webhook-first; polling backs off to 30 min on repeated empty results."""
    floor = max(MIN_POLL_S, base_s)
    if empty_streak <= 0:
        return floor
    return int(min(MAX_POLL_S, floor * (2 ** min(empty_streak, 10))))


async def with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    provider: str,
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    if sleep is None:
        import asyncio

        sleep = asyncio.sleep
    last: RateLimited | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await call()
        except AuthExpired:
            # An expired credential will not un-expire on retry (39 §6).
            raise
        except RateLimited as exc:
            last = exc
            st = note_rate_limited(provider, exc.retry_after_s)
            if attempt >= attempts:
                break
            delay = backoff_delay(attempt, retry_after_s=exc.retry_after_s)
            st.waits.append(delay)
            await sleep(delay)
    assert last is not None
    raise last
