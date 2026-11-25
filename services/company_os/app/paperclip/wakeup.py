"""Start lock + coalesced wakeups.

Ported from Paperclip ``agent-start-lock.ts`` (serialize starts per agent) and
built-in ``concurrencyPolicy: coalesce_if_active`` (if already running, merge
the wakeup instead of launching a duplicate).

MIT: Paperclip AI.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.state import MEM

T = TypeVar("T")

_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()


async def _lock_for(venture_id: str) -> asyncio.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(venture_id)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[venture_id] = lock
        return lock


async def with_venture_start_lock(venture_id: str, fn: Callable[[], Awaitable[T]]) -> T:
    """Only one autopilot start at a time for this venture."""
    lock = await _lock_for(venture_id)
    async with lock:
        return await fn()


def note_coalesced_wakeup(venture_id: str) -> None:
    MEM[f"paperclip_wakeup:{venture_id}"] = True


def take_coalesced_wakeup(venture_id: str) -> bool:
    return bool(MEM.pop(f"paperclip_wakeup:{venture_id}", None))
