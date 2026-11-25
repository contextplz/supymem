"""Start onboarding without blocking the HTTP response (product UX).

Tests set ONBOARDING_SYNC=1 so they still await a full pause/done in-request.
Local product defaults to fire-and-forget: create the run row immediately, then
continue the loop in a background task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.db.models import OnboardingRun
from app.onboarding.orchestrator import run_onboarding
from app.onboarding.state import create_run, load_run, persist_run, resumable_run

log = logging.getLogger(__name__)


def onboarding_sync() -> bool:
    return os.environ.get("ONBOARDING_SYNC", "1") == "1"


async def start_onboarding(
    venture_id: str,
    *,
    mode: str = "guided",
    resume: bool = False,
    research_founder: bool = False,
    skip_research: bool = False,
    run_id: str | None = None,
    entry_mode: str = "idea",
    workspace_id: str | None = None,
    background: bool | None = None,
) -> OnboardingRun:
    """Return a run handle quickly when async; await fully when sync."""
    sync = onboarding_sync() if background is None else (not background)
    if sync:
        return await run_onboarding(
            venture_id,
            mode=mode,
            resume=resume,
            research_founder=research_founder,
            skip_research=skip_research,
            run_id=run_id,
            entry_mode=entry_mode,
            workspace_id=workspace_id,
        )

    if resume:
        target_id = run_id
        if not target_id:
            found = resumable_run(venture_id)
            target_id = found.id if found else None
        if target_id:
            existing = load_run(target_id)
            if existing:
                if existing.status == "failed":
                    from app.onboarding.gates import prepare_failed_resume

                    prepare_failed_resume(existing)
                existing.status = "running"
                existing.paused_reason = None
                persist_run(existing)

                async def _resume() -> None:
                    try:
                        await run_onboarding(
                            venture_id,
                            mode=mode,
                            resume=True,
                            research_founder=research_founder,
                            skip_research=skip_research,
                            run_id=existing.id,
                            entry_mode=entry_mode,
                            workspace_id=workspace_id,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("background onboarding resume failed for %s", venture_id)

                asyncio.get_running_loop().create_task(_resume())
                return existing
            return await run_onboarding(
                venture_id,
                mode=mode,
                resume=True,
                research_founder=research_founder,
                skip_research=skip_research,
                run_id=target_id,
                entry_mode=entry_mode,
                workspace_id=workspace_id,
            )

    # Fresh start: persist a running row so GET /onboarding never 404s, then continue.
    run = create_run(
        venture_id,
        mode=mode,
        research_founder=research_founder,
        skip_research=skip_research,
        entry_mode=entry_mode,
        workspace_id=workspace_id,
    )

    async def _go() -> None:
        try:
            await run_onboarding(
                venture_id,
                mode=mode,
                resume=True,
                research_founder=research_founder,
                skip_research=skip_research,
                run_id=run.id,
                entry_mode=entry_mode,
                workspace_id=workspace_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("background onboarding failed for %s", venture_id)

    asyncio.get_running_loop().create_task(_go())
    return run


def run_payload_stub(run: OnboardingRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "status": run.status,
        "phase": run.phase,
        "phases_done": run.phases_done,
        "phases_total": run.phases_total,
    }
