"""In-process pub/sub event bus with bounded queues, coalescing, and monotonic seq."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from core.redact import redact

# High-volume topics coalesce to at most this many frames/second per subscriber.
COALESCE_TOPICS: frozenset[str] = frozenset({"coding.stream", "agent.step"})
COALESCE_HZ = 10.0
DEFAULT_QUEUE_SIZE = 256


@dataclass
class Event:
    id: str
    topic: str
    venture_id: str | None
    ts: Any
    actor: str
    payload: dict[str, Any]
    seq: int


@dataclass
class _Subscriber:
    topics: frozenset[str]
    venture_id: str | None
    queue: asyncio.Queue[Event | None]
    # topic -> (last_emit_monotonic, pending Event)
    coalesce: dict[str, tuple[float, Event | None]] = field(default_factory=dict)
    closed: bool = False


class InProcessEventBus:
    """Asyncio pub/sub. Slow subscribers never block publishers or fast peers."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        coalesce_hz: float = COALESCE_HZ,
        persist: Any | None = None,
    ) -> None:
        self._queue_size = queue_size
        self._coalesce_interval = 1.0 / coalesce_hz if coalesce_hz > 0 else 0.0
        self._subs: list[_Subscriber] = []
        self._seq: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._persist = persist  # optional callable(Event) for durable tail
        self._history: list[Event] = []

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        venture_id: str | None = None,
        actor: str = "system",
    ) -> Event:
        async with self._lock:
            self._seq[topic] += 1
            seq = self._seq[topic]
            ev = Event(
                id=new_id("evt"),
                topic=topic,
                venture_id=venture_id,
                ts=utcnow(),
                actor=actor,
                payload=redact(payload) if isinstance(payload, dict) else payload,
                seq=seq,
            )
            self._history.append(ev)
            if self._persist is not None:
                self._persist(ev)

        # Fan-out outside the lock so a slow put never stalls publishers.
        dead: list[_Subscriber] = []
        for sub in list(self._subs):
            if sub.closed:
                dead.append(sub)
                continue
            if topic not in sub.topics:
                continue
            if sub.venture_id is not None and venture_id != sub.venture_id:
                continue
            await self._deliver(sub, ev)
        for d in dead:
            if d in self._subs:
                self._subs.remove(d)
        return ev

    async def _deliver(self, sub: _Subscriber, ev: Event) -> None:
        if ev.topic in COALESCE_TOPICS and self._coalesce_interval > 0:
            now = time.monotonic()
            last, pending = sub.coalesce.get(ev.topic, (0.0, None))
            if now - last < self._coalesce_interval:
                # Keep latest only; drop older pending under load.
                sub.coalesce[ev.topic] = (last, ev)
                return
            # Flush any pending older coalesced event first.
            if pending is not None:
                await self._put(sub, pending)
            sub.coalesce[ev.topic] = (now, None)
            await self._put(sub, ev)
            return
        await self._put(sub, ev)

    async def _put(self, sub: _Subscriber, ev: Event) -> None:
        if sub.closed:
            return
        try:
            sub.queue.put_nowait(ev)
        except asyncio.QueueFull:
            # Drop oldest to keep publisher non-blocking; coalesce policy.
            try:
                _ = sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def subscribe(
        self,
        topics: list[str],
        *,
        venture_id: str | None = None,
        after_seq: int | None = None,
    ) -> AsyncIterator[Event]:
        sub = _Subscriber(
            topics=frozenset(topics),
            venture_id=venture_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
        )
        self._subs.append(sub)

        async def _gen() -> AsyncIterator[Event]:
            try:
                if after_seq is not None:
                    for past in await self.replay(topics, after_seq, limit=10_000):
                        if venture_id is not None and past.venture_id != venture_id:
                            continue
                        yield past
                while True:
                    # Flush coalesced pending on a timer tick.
                    await self._flush_coalesce(sub)
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=0.05)
                    except TimeoutError:
                        continue
                    if item is None:
                        return
                    yield item
            finally:
                sub.closed = True
                if sub in self._subs:
                    self._subs.remove(sub)

        return _gen()

    async def _flush_coalesce(self, sub: _Subscriber) -> None:
        now = time.monotonic()
        for topic, (last, pending) in list(sub.coalesce.items()):
            if pending is not None and now - last >= self._coalesce_interval:
                sub.coalesce[topic] = (now, None)
                await self._put(sub, pending)

    async def replay(
        self, topics: list[str], after_seq: int, limit: int
    ) -> list[Event]:
        topic_set = set(topics)
        out: list[Event] = []
        for ev in self._history:
            if ev.topic not in topic_set:
                continue
            if ev.seq <= after_seq:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        return out

    def topic_seq(self, topic: str) -> int:
        return self._seq[topic]


# Module-level default bus for simple imports in tests / early bootstrap.
bus = InProcessEventBus()
