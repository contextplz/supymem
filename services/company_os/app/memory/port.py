"""MemoryPort — nine methods. Adapters implement exactly these (doc 72 §2)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.memory.model import (
    ContextPack,
    GroundedAnswer,
    MemoryHealth,
    MemoryHit,
    MemoryStats,
    MemoryWrite,
    RecallQuery,
    TaskRef,
)


@runtime_checkable
class MemoryPort(Protocol):
    async def remember(self, m: MemoryWrite) -> str:
        """Store one durable memory. Returns mem_id. Idempotent on dedupe_key."""
        ...

    async def supersede(self, mem_id: str, *, by: MemoryWrite, reason: str) -> str:
        """Close the old validity window and link forward. NEVER deletes."""
        ...

    async def recall(self, q: RecallQuery) -> list[MemoryHit]:
        """Hybrid retrieval. Honours q.as_of for point-in-time reads."""
        ...

    async def context_pack(
        self,
        *,
        venture_id: str,
        task: TaskRef,
        token_budget: int = 4000,
    ) -> ContextPack:
        """The one call an agent makes when starting a task."""
        ...

    async def answer(self, *, venture_id: str, question: str) -> GroundedAnswer:
        """Founder-facing. MUST return citations or abstain."""
        ...

    async def timeline(
        self,
        *,
        venture_id: str,
        subject: str,
        since: datetime | None = None,
    ) -> list[MemoryHit]:
        """Full supersession chain for one subject."""
        ...

    async def forget(self, *, venture_id: str, mem_id: str, reason: str) -> None:
        """Tombstone, not delete. GDPR / founder-requested removal only."""
        ...

    async def health(self) -> MemoryHealth: ...

    async def stats(self, venture_id: str) -> MemoryStats: ...
