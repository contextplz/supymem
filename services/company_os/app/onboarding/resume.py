"""Idempotency, output replay, crash sweep (26 §5)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db.models import OnboardingRun, OnboardingStep
from app.onboarding.failures import INTERRUPTED
from app.onboarding.state import persist_run
from app.state import db
from core.clock import as_utc, utcnow


def idempotency_key(run_id: str, step_id: str, input_digest: str) -> str:
    raw = f"{run_id}:{step_id}:{input_digest}"
    return hashlib.sha256(raw.encode()).hexdigest()


def replay_output(prior: OnboardingStep) -> dict[str, Any]:
    if not prior.output_json:
        return {}
    try:
        return json.loads(prior.output_json)
    except json.JSONDecodeError:
        return {}


def crash_sweep(*, stale_after_s: int = 90) -> list[str]:
    """Mark stale running runs as paused(interrupted).

    Boot calls this with ``stale_after_s=0``: every leftover ``running`` row is
    orphaned because this process has no inherited workers. A positive window
    is only for in-process watchdog use.
    """
    factory = db()
    cutoff = utcnow() - timedelta(seconds=stale_after_s)
    touched: list[str] = []
    with factory() as session:
        rows = session.scalars(
            select(OnboardingRun).where(OnboardingRun.status == "running")
        ).all()
        for run in rows:
            updated_at = as_utc(run.updated_at)
            # stale_after_s=0 means "all running rows are orphans" — do not skip
            # a just-written row because its updated_at equals or slightly
            # post-dates cutoff (SQLite naive vs aware, same-second boot).
            if stale_after_s > 0 and updated_at and updated_at > cutoff:
                continue
            # Fail in-flight step
            step = session.scalars(
                select(OnboardingStep).where(
                    OnboardingStep.run_id == run.id,
                    OnboardingStep.status == "running",
                )
            ).first()
            if step:
                step.status = "failed"
                step.error_class = INTERRUPTED.name
                step.error_detail = "process interrupted"
                step.finished_at = utcnow()
            run.status = "paused"
            run.paused_reason = "interrupted"
            run.resume_token = run.resume_token or "interrupted"
            run.updated_at = utcnow()
            touched.append(run.id)
        # A completed/failed run can still have a leftover running step after a
        # crash (the compost demand_read ghost). Fail those without rewriting
        # terminal run status — auto-resume only applies to swept running runs.
        leftover_steps = session.scalars(
            select(OnboardingStep).where(OnboardingStep.status == "running")
        ).all()
        for step in leftover_steps:
            started = as_utc(step.started_at)
            if stale_after_s > 0 and started and started > cutoff:
                continue
            step.status = "failed"
            step.error_class = INTERRUPTED.name
            step.error_detail = "process interrupted"
            step.finished_at = utcnow()
            if step.run_id not in touched:
                parent = session.get(OnboardingRun, step.run_id)
                if parent is not None and parent.status == "running":
                    parent.status = "paused"
                    parent.paused_reason = "interrupted"
                    parent.resume_token = parent.resume_token or "interrupted"
                    parent.updated_at = utcnow()
                    touched.append(parent.id)
        session.commit()
    return touched


def mark_interrupted(run: OnboardingRun) -> OnboardingRun:
    run.status = "paused"
    run.paused_reason = "interrupted"
    run.resume_token = run.resume_token or "interrupted"
    run.updated_at = utcnow()
    persist_run(run)
    return run


async def auto_resume_runs(run_ids: list[str]) -> None:
    """Continue crash-swept runs so the founder does not have to click Resume."""
    import logging

    from app.onboarding.spawn import start_onboarding
    from app.onboarding.state import load_run

    log = logging.getLogger(__name__)
    for rid in run_ids:
        run = load_run(rid)
        if not run or run.status not in {"paused", "failed"}:
            continue
        try:
            await start_onboarding(
                run.venture_id,
                mode=run.mode or "guided",
                resume=True,
                run_id=run.id,
                entry_mode=getattr(run, "entry_mode", None) or "idea",
                workspace_id=getattr(run, "workspace_id", None),
            )
        except Exception:  # noqa: BLE001 — one run must not block the others
            log.exception("auto-resume after crash_sweep failed for %s", rid)
