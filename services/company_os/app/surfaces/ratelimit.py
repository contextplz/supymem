"""Per-token token-bucket rate limiting (41 §4).

In-process on purpose: local tenancy is one process, and a shared limiter would
need infrastructure we deliberately do not require to run this.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RateLimit:
    """Returned to the caller so it can set the X-RateLimit-* headers."""

    allowed: bool
    limit: int
    remaining: int
    reset_after_s: int
    retry_after_s: int = 0


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class Limiter:
    capacity: int = 60
    per_seconds: float = 60.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def check(self, key: str, *, cost: float = 1.0, now: float | None = None) -> RateLimit:
        moment = now if now is not None else time.monotonic()
        rate = self.capacity / self.per_seconds
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.capacity), updated=moment)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, moment - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * rate)
            bucket.updated = moment

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return RateLimit(
                allowed=True,
                limit=self.capacity,
                remaining=int(bucket.tokens),
                reset_after_s=int((self.capacity - bucket.tokens) / rate) + 1,
            )

        deficit = cost - bucket.tokens
        retry_after = max(1, int(deficit / rate) + 1)
        return RateLimit(
            allowed=False,
            limit=self.capacity,
            remaining=0,
            reset_after_s=retry_after,
            retry_after_s=retry_after,
        )

    def reset(self) -> None:
        self._buckets.clear()


_DEFAULT = Limiter()


def check_rate(key: str, *, cost: float = 1.0, now: float | None = None) -> RateLimit:
    return _DEFAULT.check(key, cost=cost, now=now)


def reset_limits() -> None:
    _DEFAULT.reset()
