"""Supymem HTTP adapter stub — full wiring in S1.7 when sidecar is available.

Refuses to start when MNEMOS is required but disabled (doc 72 §4.2).
"""

from __future__ import annotations

import os
from datetime import datetime

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


class SupymemAdapter:
    name = "supymem"

    def __init__(self) -> None:
        self.base_url = (os.environ.get("SUPYMEM_BASE_URL") or "").rstrip("/")
        self.api_key = os.environ.get("SUPYMEM_API_KEY") or ""
        self.org_id = os.environ.get("SUPYMEM_ORG_ID") or ""
        self.require_mnemos = os.environ.get("SUPYMEM_REQUIRE_MNEMOS", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        if not self.base_url:
            raise RuntimeError(
                "SUPYMEM_BASE_URL is required for the supymem adapter. "
                "Use tier2: local (default) or set the sidecar URL."
            )

    def _scoped_headers(self, venture_id: str) -> dict[str, str]:
        """Every request must be project-scoped (doc 72 §4.1)."""
        if not venture_id:
            raise PermissionError("supymem adapter refuses unscoped requests")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Supymem-Org": self.org_id,
            "X-Supymem-Project": venture_id,
            "Content-Type": "application/json",
        }

    async def health(self) -> MemoryHealth:
        # Without a live sidecar, report not-ok rather than pretending.
        if self.require_mnemos and os.environ.get("MNEMOS_ENABLED", "false").lower() not in {
            "1",
            "true",
            "yes",
        }:
            return MemoryHealth(
                ok=False,
                adapter=self.name,
                detail="MNEMOS_ENABLED must be true; refusing HELIX-only path (doc 72 §4.2)",
            )
        if not self.base_url:
            return MemoryHealth(ok=False, adapter=self.name, detail="no base_url")
        return MemoryHealth(
            ok=False,
            adapter=self.name,
            detail="sidecar not probed in this build — use local adapter until S1.7 compose is up",
        )

    async def remember(self, m: MemoryWrite) -> str:
        raise NotImplementedError("supymem remember — enable sidecar (S1.7)")

    async def supersede(self, mem_id: str, *, by: MemoryWrite, reason: str) -> str:
        raise NotImplementedError("supymem supersede — enable sidecar (S1.7)")

    async def recall(self, q: RecallQuery) -> list[MemoryHit]:
        raise NotImplementedError("supymem recall — enable sidecar (S1.7)")

    async def context_pack(
        self, *, venture_id: str, task: TaskRef, token_budget: int = 4000
    ) -> ContextPack:
        raise NotImplementedError("supymem context_pack — enable sidecar (S1.7)")

    async def answer(self, *, venture_id: str, question: str) -> GroundedAnswer:
        raise NotImplementedError("supymem answer — enable sidecar (S1.7)")

    async def timeline(
        self, *, venture_id: str, subject: str, since: datetime | None = None
    ) -> list[MemoryHit]:
        raise NotImplementedError("supymem timeline — enable sidecar (S1.7)")

    async def forget(self, *, venture_id: str, mem_id: str, reason: str) -> None:
        raise NotImplementedError("supymem forget — enable sidecar (S1.7)")

    async def stats(self, venture_id: str) -> MemoryStats:
        raise NotImplementedError("supymem stats — enable sidecar (S1.7)")
