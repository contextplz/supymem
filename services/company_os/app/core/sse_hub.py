"""SSE publish + 10-minute replay. Redis pub/sub when REDIS_URL is set."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from app.core.local_worker import redis_url

RING_SECONDS = 600
CHANNEL = "sse:events"


class SSEHub:
    def __init__(self) -> None:
        from desktop_runtime.hosted import validate_local_state

        validate_local_state()
        self._seq = 0
        self._ring: deque[tuple[float, dict[str, Any]]] = deque()

    def _prune(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - RING_SECONDS
        while self._ring and self._ring[0][0] < cutoff:
            self._ring.popleft()

    def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        venture_id: str | None = None,
    ) -> dict[str, Any]:
        self._seq += 1
        ev = {
            "id": str(self._seq),
            "seq": self._seq,
            "topic": topic,
            "venture_id": venture_id,
            "payload": payload,
        }
        self._ring.append((time.time(), ev))
        self._prune()
        url = redis_url()
        if url:
            try:
                import redis

                client = redis.Redis.from_url(url)
                client.publish(CHANNEL, json.dumps(ev, default=str))
                client.zadd(
                    "sse:ring",
                    {json.dumps(ev, default=str): time.time()},
                )
                client.zremrangebyscore("sse:ring", 0, time.time() - RING_SECONDS)
            except Exception:
                pass
        return ev

    def replay(self, after_seq: int | None, topics: set[str], venture_id: str | None) -> list[dict[str, Any]]:
        self._prune()
        out = []
        for _ts, ev in self._ring:
            if after_seq is not None and ev["seq"] <= after_seq:
                continue
            if topics and ev["topic"] not in topics:
                continue
            if venture_id and ev.get("venture_id") != venture_id:
                continue
            out.append(ev)
        return out


sse_hub = SSEHub()
