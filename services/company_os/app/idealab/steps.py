"""Surprise-mode step modules for PHASES_BY_MODE["surprise"] (35 §6, 37).

Thin wrappers around the Idea Lab service so the doc-26 orchestrator can drive
orient → scan → slate without a second engine (LD-49).
"""

from __future__ import annotations

from app.idealab import corpus as corpus_mod
from app.idealab import slate as slate_mod
from app.onboarding.registry import StepDef, StepResult
from app.onboarding.state import Ctx


def _workspace_id(ctx: Ctx) -> str:
    ws = getattr(ctx.run, "workspace_id", None)
    if ws:
        return str(ws)
    # Fall back through the venture → workspace bridge when present.
    from app.workspaces.resolve import workspace_for_venture

    linked = workspace_for_venture(ctx.venture.id)
    if linked is None:
        raise slate_mod.IdeaLabError(
            "workspace_not_found", "This run has no workspace."
        )
    return linked.id


async def orient(ctx: Ctx) -> StepResult:
    _workspace_id(ctx)  # resolve early so a missing desk fails here
    return StepResult(output={"ready": True})


async def scan(ctx: Ctx) -> StepResult:
    loaded = corpus_mod.load()
    count = len(loaded.items)
    return StepResult(
        output={
            "count": count,
            "fresh": loaded.fresh(),
            "researched": loaded.fresh() and count > 0,
        },
        tokens={"count": count},
        partial=not loaded.fresh() or count == 0,
    )


async def slate(ctx: Ctx) -> StepResult:
    workspace_id = _workspace_id(ctx)
    seed = {}
    flags = ctx.flags()
    if isinstance(flags.get("seed"), dict):
        seed = flags["seed"]
    view = await slate_mod.create_slate(
        workspace_id=workspace_id,
        seed_input=seed,
        actor_id=getattr(ctx.venture, "owner_id", None) or "per_local000000",
        surface="orchestrator",
    )
    return StepResult(
        output=view.to_dict(),
        tokens={"count": len(view.opportunities)},
        partial=len(view.opportunities) < slate_mod.SLATE_SIZE,
        cost_usd=view.cost_usd,
        detail=[o.title for o in view.opportunities],
    )


STEPS: list[StepDef] = [
    StepDef("idealab.orient", "orient", orient, "idealab.orient", timeout_s=5),
    StepDef("idealab.scan", "scan", scan, "idealab.scan", timeout_s=10),
    StepDef(
        "idealab.slate",
        "slate",
        slate,
        "idealab.slate",
        timeout_s=45,
        est_cost_usd=0.05,
    ),
]

__all__ = ["STEPS", "orient", "scan", "slate"]
