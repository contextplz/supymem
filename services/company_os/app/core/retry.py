"""Retry helper."""
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

async def retry(fn: Callable[[], Awaitable[T]], *, attempts: int = 2) -> T:
    last = None
    for _ in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last  # type: ignore[misc]
