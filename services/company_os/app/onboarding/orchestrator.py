"""Onboarding run loop (26 §6)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core.eventbus import bus
from app.db.models import OnboardingRun
from app.onboarding.failures import StepFailed, classify
from app.onboarding.narrator import Narrator
from app.onboarding.registry import (
    StepDef,
    StepResult,
    phase_index,
    phases_for_mode,
    steps_for,
)
from app.onboarding.resume import idempotency_key, replay_output
from app.onboarding.state import (
    Ctx,
    active_run,
    create_run,
    find_completed_step,
    load_run,
    persist_run,
    resumable_run,
    record_done,
    record_failed,
    record_running,
    record_skipped,
)
from core.clock import utcnow

log = logging.getLogger(__name__)

# Cancel flags keyed by run_id
_CANCEL: dict[str, asyncio.Event] = {}


def cancel_flag(run_id: str) -> asyncio.Event:
    if run_id not in _CANCEL:
        _CANCEL[run_id] = asyncio.Event()
    return _CANCEL[run_id]


async def cancel_run(run_id: str) -> OnboardingRun | None:
    cancel_flag(run_id).set()
    run = load_run(run_id)
    if run and run.status in {"running", "paused"}:
        from app.core.local_mode import is_local

        if is_local():
            from app.onboarding.steps.build import builder_cancel_device_order

            builder_cancel_device_order(run.venture_id, run)
        # Forward abort to a live QM turn when one was recorded (doc 55 §4.4 / 56 QM-P3).
        try:
            flags = json.loads(run.flags_json or "{}")
        except json.JSONDecodeError:
            flags = {}
        qm_run_id = flags.get("qm_run_id")
        if isinstance(qm_run_id, str) and qm_run_id.strip():
            try:
                from app.qm.client import signal_run

                await signal_run(qm_run_id.strip(), kind="abort")
            except Exception as exc:  # noqa: BLE001 — cancel must still succeed locally
                log.warning("QM abort for run %s (qm_run_id=%s) failed: %s", run_id, qm_run_id, exc)
        run.status = "cancelled"
        run.finished_at = utcnow()
        persist_run(run)
    return run


async def execute(ctx: Ctx, step: StepDef) -> StepResult:
    digest = ctx.input_digest(step.id)
    key = idempotency_key(ctx.run.id, step.id, digest)
    prior = find_completed_step(ctx.run.id, step.id, key)
    if prior:
        if prior.status == "skipped":
            ctx.out[step.id] = {}
            return StepResult(output={})
        out = replay_output(prior)
        ctx.out[step.id] = out
        return StepResult(output=out)

    if step.skip_if and step.skip_if(ctx):
        record_skipped(ctx.run.id, step.id, step.phase, key)
        # Narrate skip for founder research
        if step.id == "understand.research_founder" and ctx.narrator:
            await ctx.narrator.done(
                step.id,
                variant_key="understand.search_skipped",
                tokens={"company": ctx.company},
            )
        ctx.out[step.id] = {}
        return StepResult(output={})

    rec = record_running(ctx.run.id, step.id, step.phase, key)
    if ctx.narrator:
        await ctx.narrator.begin(step.narrate, kind=step.kind)

    try:
        res = await asyncio.wait_for(step.fn(ctx), timeout=step.timeout_s)
    except Exception as e:  # noqa: BLE001
        cls = classify(e)
        detail = str(e)
        try:
            from llm_gateway.errors import ProviderCallError

            if isinstance(e, ProviderCallError):
                detail = e.message
        except Exception:
            pass
        record_failed(rec, cls.name, detail)
        if ctx.narrator:
            await ctx.narrator.fail(
                step.id,
                cls,
                tokens={"level": ctx.autonomy_word, "detail": detail[:200]},
            )
        if step.required:
            raise StepFailed(step.id, cls, detail) from e
        ctx.out[step.id] = {}
        return StepResult(output={})

    # Founder-gate pauses must re-run until the founder actually finishes.
    # Recording "done" on first pause made confirm-plan skip office hours and
    # bounce back to the product quiz after a retry.
    if res.pause:
        ctx.out[step.id] = res.output
        persist_run(ctx.run)
        return res

    record_done(rec, res.output, cost_usd=res.cost_usd)
    ctx.out[step.id] = res.output
    ctx.budget.charge(res.cost_usd)
    ctx.run.cost_usd = ctx.budget.cost_usd
    persist_run(ctx.run)

    if ctx.narrator:
        await ctx.narrator.done(
            step.narrate,
            kind=res.kind or step.kind,
            simulated=res.simulated,
            downgraded=res.downgraded or ctx.downgraded,
            partial=res.partial,
            detail=res.detail,
            query=res.query,
            evidence_url=res.evidence_url,
            undo=res.undo,
            cost_usd=res.cost_usd or None,
            tokens=res.tokens,
            demand=res.demand,
            variant_key=res.variant_key,
        )
    return res


def _pause(run: OnboardingRun, reason: str, token: str | None = None) -> OnboardingRun:
    run.status = "paused"
    run.paused_reason = reason
    run.resume_token = token
    run.updated_at = utcnow()
    persist_run(run)
    return run


def _finish(run: OnboardingRun, status: str, error: str | None = None) -> OnboardingRun:
    run.status = status
    run.error = error
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    if status == "done":
        run.phases_done = run.phases_total
        run.paused_reason = None
    persist_run(run)
    if status == "failed":
        from app.onboarding.gates import clear_founder_gates

        clear_founder_gates(run.venture_id, run.id)
    return run


async def run_onboarding(
    venture_id: str,
    *,
    mode: str = "guided",
    resume: bool = False,
    research_founder: bool = False,
    skip_research: bool = False,
    run_id: str | None = None,
    entry_mode: str = "idea",
    workspace_id: str | None = None,
) -> OnboardingRun:
    existing = active_run(venture_id)
    if existing and not resume and existing.status == "running":
        return existing

    if resume:
        run = load_run(run_id) if run_id else existing or resumable_run(venture_id)
        if not run:
            run = create_run(
                venture_id,
                mode=mode,
                research_founder=research_founder,
                skip_research=skip_research,
                entry_mode=entry_mode,
                workspace_id=workspace_id,
            )
        else:
            if run.status == "failed":
                from app.onboarding.gates import prepare_failed_resume

                prepare_failed_resume(run)
            run.status = "running"
            run.paused_reason = None
            persist_run(run)
    else:
        if existing and existing.status == "paused":
            run = existing
            run.status = "running"
            run.paused_reason = None
            persist_run(run)
        elif existing and existing.status == "running":
            run = existing
        else:
            run = create_run(
                venture_id,
                mode=mode,
                research_founder=research_founder,
                skip_research=skip_research,
                entry_mode=entry_mode,
                workspace_id=workspace_id,
            )

    narrator = Narrator(venture_id, run.id)
    narrator.set_tokens(company=run.venture_id)  # overwritten below
    ctx = Ctx.build(run, narrator=narrator)
    narrator.set_tokens(company=ctx.company, level=ctx.autonomy_word)
    ctx.cancel = cancel_flag(run.id)
    ctx.cancel.clear()

    active_entry_mode = getattr(run, "entry_mode", None) or entry_mode or "idea"
    wid = (
        workspace_id
        or getattr(run, "workspace_id", None)
        or getattr(ctx.venture, "workspace_id", None)
    )

    from llm_gateway.sole import workspace_llm_context

    with workspace_llm_context(wid):
        return await _drive_phases(
            ctx,
            run,
            narrator,
            venture_id=venture_id,
            entry_mode=active_entry_mode,
        )


async def _drive_phases(
    ctx: Ctx,
    run: OnboardingRun,
    narrator: Narrator,
    *,
    venture_id: str,
    entry_mode: str,
) -> OnboardingRun:
    phases = phases_for_mode(entry_mode)
    if run.phases_total != len(phases):
        run.phases_total = len(phases)
        persist_run(run)
    start_phase_i = phase_index(run.phase, entry_mode)

    for phase in phases:
        if phase_index(phase, entry_mode) < start_phase_i:
            continue
        run.phase = phase
        persist_run(run)
        narrator.set_phase(phase)
        try:
            await bus.publish(
                "onboarding.phase",
                {
                    "run_id": run.id,
                    "phase": phase,
                    "phases_done": run.phases_done,
                    "phases_total": run.phases_total,
                },
                venture_id=venture_id,
            )
        except Exception:
            pass

        for step in steps_for(phase, entry_mode):
            if ctx.cancel.is_set():
                return _finish(run, "cancelled")
            if ctx.budget.exhausted():
                return _pause(run, "budget_exhausted")

            try:
                res = await execute(ctx, step)
            except StepFailed as f:
                try:
                    await bus.publish(
                        "onboarding.failed",
                        {
                            "run_id": run.id,
                            "step_id": f.step_id,
                            "error_class": f.error_class.name,
                            "message": str(f),
                        },
                        venture_id=venture_id,
                    )
                except Exception:
                    pass
                return _finish(run, "failed", error=f.error_class.name)

            if res.pause:
                paused = _pause(run, res.pause.reason, res.pause.resume_token)
                try:
                    await bus.publish(
                        "onboarding.paused",
                        {
                            "run_id": run.id,
                            "reason": res.pause.reason,
                            "founder_action": res.pause.founder_action,
                        },
                        venture_id=venture_id,
                    )
                except Exception:
                    pass
                return paused

        run.phases_done = phase_index(phase, entry_mode) + 1
        nxt_i = phase_index(phase, entry_mode) + 1
        if nxt_i < len(phases):
            run.phase = phases[nxt_i]
        persist_run(run)
        start_phase_i = phase_index(run.phase, entry_mode)

    finished = _finish(run, "done")
    try:
        await bus.publish(
            "onboarding.done",
            {"run_id": run.id, "cost_usd": run.cost_usd},
            venture_id=venture_id,
        )
        await bus.publish(
            "dashboard.invalidate",
            {"tiles": ["tasks", "website", "team", "documents", "next_up", "validation"]},
            venture_id=venture_id,
        )
    except Exception:
        pass
    return finished
