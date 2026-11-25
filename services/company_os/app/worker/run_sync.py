"""Run a registered job kind inline (tests and IDEA_SYNC / ONBOARDING_SYNC)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
from typing import Any


def run_kind(kind: str, payload: dict[str, Any]) -> None:
    from app.worker.handlers import HANDLERS, install_default_handlers

    install_default_handlers()
    fn = HANDLERS.get(kind)
    if fn is None:
        raise RuntimeError(f"no handler registered for {kind}")
    result = fn(payload)
    if not inspect.isawaitable(result):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(result)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, result).result()
