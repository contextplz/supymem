"""Redis-backed JobQueue adapter (doc 19 §7 / 96 H1.3).

When REDIS_URL is unset, callers keep using the SQLite/in-process JobQueue.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from app.core.local_worker import redis_url


class RedisJobQueue:
    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL", "").strip()
        self._redis: Any | None = None

    async def _client(self) -> Any:
        if self._redis is None:
            import redis.asyncio as redis  # type: ignore

            self._redis = redis.from_url(self._url, decode_responses=True)
        return self._redis

    async def enqueue(self, queue: str, payload: dict[str, Any]) -> str:
        client = await self._client()
        job_id = "job_" + uuid.uuid4().hex[:12]
        await client.lpush(f"cos:jobs:{queue}", json.dumps({"id": job_id, "payload": payload}))
        return job_id

    async def dequeue(self, queue: str, timeout_s: int = 5) -> dict[str, Any] | None:
        client = await self._client()
        item = await client.brpop(f"cos:jobs:{queue}", timeout=timeout_s)
        if not item:
            return None
        _key, raw = item
        return json.loads(raw)


def redis_jobs_enabled() -> bool:
    return bool(redis_url())
