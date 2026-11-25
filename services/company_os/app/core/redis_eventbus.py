"""Redis Streams backend for EventBus (doc 19 §7 / 96 H1.2).

Falls back to in-process semantics when REDIS_URL is unset — local tenancy unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from app.core.eventbus import Event, InProcessEventBus

log = logging.getLogger(__name__)


class RedisStreamsEventBus(InProcessEventBus):
    """Publish to Redis Streams *and* local subscribers when REDIS_URL is set."""

    def __init__(self, redis_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._redis_url = redis_url or os.environ.get("REDIS_URL", "").strip()
        self._redis: Any | None = None

    async def _client(self) -> Any | None:
        if not self._redis_url:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis  # type: ignore
        except ImportError:
            return None
        self._redis = redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        venture_id: str | None = None,
        actor: str = "system",
    ) -> Event:
        ev = await super().publish(topic, payload, venture_id=venture_id, actor=actor)
        client = await self._client()
        if client is None:
            return ev
        try:
            await client.xadd(
                f"cos:events:{topic}",
                {
                    "id": ev.id,
                    "venture_id": venture_id or "",
                    "actor": actor,
                    "seq": str(ev.seq),
                    "payload": json.dumps(ev.payload),
                },
                maxlen=10_000,
                approximate=True,
            )
        except Exception:
            log.debug("Redis stream publish skipped", exc_info=True)
        return ev

    async def subscribe_stream(
        self,
        topic: str,
        *,
        last_id: str = "$",
        block_ms: int = 5000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Remote consumer for hosted workers. No-op iterator when Redis absent."""
        client = await self._client()
        if client is None:
            if False:  # pragma: no cover
                yield {}
            return
        cursor = last_id
        while True:
            rows = await client.xread({f"cos:events:{topic}": cursor}, block=block_ms, count=50)
            if not rows:
                await asyncio.sleep(0)
                continue
            for _stream, messages in rows:
                for msg_id, fields in messages:
                    cursor = msg_id
                    yield {"id": msg_id, "topic": topic, **fields}


def build_event_bus() -> InProcessEventBus:
    """Factory used by bootstrap — Redis when configured, else in-process."""
    url = os.environ.get("REDIS_URL", "").strip()
    if url:
        return RedisStreamsEventBus(redis_url=url)
    return InProcessEventBus()
