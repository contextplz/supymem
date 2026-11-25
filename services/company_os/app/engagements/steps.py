"""Engagement step modules for PHASES_BY_MODE["engagement"] (36 §5, LD-49).

Thin wrappers: the doc-26 orchestrator drives these; the real work lives in
:mod:`app.engagements.service`.
"""

from __future__ import annotations

import json
from typing import Any

from app.engagements import service
from app.onboarding.registry import StepDef, StepResult
from app.onboarding.state import Ctx


def _engagement_id(ctx: Ctx) -> str:
    flags = ctx.flags()
    eng = flags.get("engagement_id")
    if not eng:
        raise service.EngagementError(
            "missing_engagement",
            "This run has no engagement attached.",
        )
    return str(eng)


def _size(ctx: Ctx) -> str:
    eng_id = _engagement_id(ctx)
    view = service.get_engagement(eng_id)
    return view.size if view else "task"


def _skip_errand_plan(ctx: Ctx) -> bool:
    return _size(ctx) == "errand"


def _skip_errand_review(ctx: Ctx) -> bool:
    return _size(ctx) == "errand"


async def orient(ctx: Ctx) -> StepResult:
    out = await service.phase_orient(_engagement_id(ctx))
    return StepResult(
        output=out,
        tokens={"title": out.get("title") or ""},
        detail=[out.get("restated") or ""],
    )


async def triage(ctx: Ctx) -> StepResult:
    out = await service.phase_triage(_engagement_id(ctx))
    return StepResult(
        output=out,
        demand=out.get("size"),
        detail=[out.get("definition_of_done") or "", *(out.get("not_doing") or [])],
    )


async def plan(ctx: Ctx) -> StepResult:
    out = await service.phase_plan(_engagement_id(ctx))
    if out.get("skipped"):
        return StepResult(output=out, partial=True)
    steps = out.get("steps") or []
    return StepResult(
        output=out,
        tokens={"count": len(steps)},
        detail=[s.get("label", "") for s in steps if isinstance(s, dict)],
    )


async def work(ctx: Ctx) -> StepResult:
    out = await service.phase_work(_engagement_id(ctx))
    if out.get("blocked"):
        return StepResult(
            output=out,
            tokens={"reason": out.get("reason") or ""},
        )
    return StepResult(
        output=out,
        tokens={"count": out.get("steps_done") or 1},
        detail=[
            str(d.get("path_or_url") or d.get("label") or "")
            for d in (out.get("deliverables") or [])
            if isinstance(d, dict)
        ],
        simulated=True,
    )


async def review(ctx: Ctx) -> StepResult:
    out = await service.phase_review(_engagement_id(ctx))
    if out.get("skipped"):
        return StepResult(output=out, partial=True)
    gates = out.get("gates") or {}
    return StepResult(
        output=out,
        tokens={
            "gate_pass": gates.get("passed") or 0,
            "gate_total": gates.get("total") or 0,
        },
        partial=not bool(gates.get("clean", True)),
    )


async def deliver(ctx: Ctx) -> StepResult:
    out = await service.phase_deliver(_engagement_id(ctx))
    view = service.get_engagement(_engagement_id(ctx))
    return StepResult(
        output=out,
        tokens={
            "cost": f"${view.cost_usd:.2f}" if view else "$0.00",
            "cap": f"${view.cost_cap_usd:.2f}" if view else "$0.00",
        },
        detail=(out.get("rendered") or "").splitlines() if out.get("rendered") else None,
    )


STEPS: list[StepDef] = [
    StepDef(
        "engagement.orient",
        "orient",
        orient,
        "engagement.orient.restate",
        timeout_s=10,
    ),
    StepDef(
        "engagement.triage",
        "triage",
        triage,
        "engagement.triage.size",
        timeout_s=20,
        est_cost_usd=0.01,
    ),
    StepDef(
        "engagement.plan",
        "plan",
        plan,
        "engagement.plan.steps",
        timeout_s=15,
        skip_if=_skip_errand_plan,
    ),
    StepDef(
        "engagement.work",
        "work",
        work,
        "engagement.work.execute",
        timeout_s=60,
    ),
    StepDef(
        "engagement.review",
        "review",
        review,
        "engagement.review.self",
        timeout_s=20,
        skip_if=_skip_errand_review,
    ),
    StepDef(
        "engagement.deliver",
        "deliver",
        deliver,
        "engagement.deliver.hand_over",
        timeout_s=20,
    ),
]


def stash_engagement_on_run(run: Any, engagement_id: str) -> None:
    """Attach an engagement id to an onboarding run's flags."""
    try:
        flags = json.loads(getattr(run, "flags_json", None) or "{}")
    except json.JSONDecodeError:
        flags = {}
    if not isinstance(flags, dict):
        flags = {}
    flags["engagement_id"] = engagement_id
    run.flags_json = json.dumps(flags)


__all__ = ["STEPS", "deliver", "orient", "plan", "review", "stash_engagement_on_run", "triage", "work"]
