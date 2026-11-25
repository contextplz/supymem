"""Simple in-process agent run scheduler."""

from __future__ import annotations

import asyncio
from typing import Any

from app.runtime.agent_runtime import AgentRuntime, AgentRunResult


class Scheduler:
    def __init__(self) -> None:
        self._runtime = AgentRuntime()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue(self, job: dict[str, Any]) -> None:
        await self._queue.put(job)

    async def run_one(self) -> AgentRunResult | None:
        if self._queue.empty():
            return None
        job = await self._queue.get()
        return await self._runtime.run(**job)


scheduler = Scheduler()
