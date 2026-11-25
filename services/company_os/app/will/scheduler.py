"""Single-process scheduler for Will maintenance jobs.

This scheduler is deliberately in-process and must have only one service instance;
multi-instance deployment needs a DB advisory lock before enabling these jobs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[None] | None]


async def pocket_5m() -> None:
    """Credit reservation sweep + product-analytics rollup."""
    try:
        from app.credits.reserve import sweep_expired

        sweep_expired()
    except Exception:
        log.exception("credit reservation sweep failed")
    try:
        from app.analytics.rollup import rollup_today

        rollup_today()
    except Exception:
        log.exception("analytics rollup failed")


async def slate_staleness() -> None:
    """Placeholder for six-hour slate freshness checks."""


async def signal_sweep() -> None:
    """Huginn watches: scrape, RSS, waitlist peaks, founder digest."""
    from app.watch.bridge import sweep_all

    sweep_all()


async def drive_slate() -> None:
    """Claim the next open task on every live, unpaused venture."""
    from app.will.driver import drive_open_work

    await drive_open_work()


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    interval_seconds: float
    func: JobFunc


class WillScheduler:
    """Coalescing, in-process registry; one running instance per Company OS process."""

    def __init__(self) -> None:
        self.jobs: list[ScheduledJob] = []
        self._tasks: list[asyncio.Task[None]] = []
        self.running = False

    def register(self, name: str, interval_seconds: float, func: JobFunc) -> None:
        self.jobs.append(ScheduledJob(name, interval_seconds, func))

    async def _run_job(self, job: ScheduledJob) -> None:
        while self.running:
            try:
                result = job.func()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — one crash must not kill the loop
                log.exception("scheduler job %s failed", job.name)
            await asyncio.sleep(job.interval_seconds)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._tasks = [asyncio.create_task(self._run_job(job), name=job.name) for job in self.jobs]

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


async def gtm_send_tick() -> None:
    """AI GTM department: find, research, send, replies, bounces, LinkedIn."""
    from app.gtm.supymem.workers import tick_department
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    tick_department(desk.id)


async def content_tick() -> None:
    """Legacy content publish tick — kept for CONTENT_ENGINE_ENABLED-only mode."""
    import os

    if os.environ.get("CONTENT_ENGINE_ENABLED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    # When the social agent is on, it owns publishing inside its own tick.
    if os.environ.get("CONTENT_AGENT_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    } and os.environ.get("COMPOSIO_API_KEY", "").strip():
        return

    from app.content.engine import purge_expired_states, tick
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    await tick(workspace_id=desk.id)
    purge_expired_states()


async def social_agent_tick() -> None:
    """24×7 social agent: keep-alive, sense, decide, act, learn."""
    import os

    # Default: run whenever Composio is present unless explicitly disabled.
    enabled = os.environ.get("CONTENT_AGENT_ENABLED")
    if enabled is not None and enabled.strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if not os.environ.get("COMPOSIO_API_KEY", "").strip() and (
        enabled is None or enabled.strip().lower() not in {"1", "true", "yes", "on"}
    ):
        return

    from app.content.agent import run_agent_tick
    from app.content.engine import purge_expired_states
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    await run_agent_tick(workspace_id=desk.id)
    purge_expired_states()


async def marketing_tick() -> None:
    """Execute due marketing jobs compiled from the active plan."""
    import os

    if os.environ.get("MARKETING_EXECUTION_ENABLED", "true").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return

    from app.marketing.executor import run_tick
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    desk = ensure_desk(LOCAL_PERSON_ID)
    await run_tick(workspace_id=desk.id)


def default_scheduler() -> WillScheduler:
    scheduler = WillScheduler()
    scheduler.register("pocket_5m", 300, pocket_5m)
    scheduler.register("slate_staleness", 900, slate_staleness)
    scheduler.register("signal_sweep", 300, signal_sweep)
    scheduler.register("gtm_send_tick", 180, gtm_send_tick)
    scheduler.register("content_tick", 240, content_tick)
    scheduler.register("social_agent_tick", 120, social_agent_tick)
    scheduler.register("marketing_tick", 90, marketing_tick)
    scheduler.register("drive_slate", 8, drive_slate)
    scheduler.register("brain_cycle", 21600, _brain_cycle)
    return scheduler


async def _brain_cycle() -> None:
    from app.gbrain.cycle import brain_cycle

    await brain_cycle()
