"""Worker registration target; no provider I/O on the event loop."""
import asyncio
from .runtime import run


async def handle(payload: dict):
    return await asyncio.to_thread(run, payload)
