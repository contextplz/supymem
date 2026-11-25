"""existing_company onboarding steps (38 §2). Wired via registry PHASES_BY_MODE."""

from __future__ import annotations

from app.onboarding.registry import StepDef, StepResult
from app.onboarding.state import Ctx


async def orient(ctx: Ctx) -> StepResult:
    return StepResult(
        output={
            "observe_ladder": ["O0", "O1", "O2", "O3", "O4"],
            "promise": "observe first, prove, then act",
            "floor": "We never merge, deploy, delete, or email customers without you.",
        },
        tokens={"company": ctx.company},
        detail=[
            "Observe only by default.",
            "Write access is earned per scope through a proof gate.",
        ],
    )


async def connect(ctx: Ctx) -> StepResult:
    from app.connectors.service import list_connections

    ws_id = getattr(ctx.venture, "workspace_id", None)
    items = list_connections(ws_id) if ws_id else []
    return StepResult(
        output={
            "connections": [c.to_dict() for c in items],
            "hint": "Connect one read-only system when you are ready.",
        },
        detail=[f"{len(items)} connection(s) ready"] if items else ["No systems connected yet."],
    )


async def discover(ctx: Ctx) -> StepResult:
    from app.discovery.scan import scan_workspace

    ws_id = getattr(ctx.venture, "workspace_id", None)
    if not ws_id:
        return StepResult(output={"skipped": True, "reason": "no workspace"})
    inventory = await scan_workspace(ws_id)
    return StepResult(
        output={"inventory": inventory},
        tokens={"count": inventory.get("connection_count", 0)},
        detail=[f"Scanned {inventory.get('connection_count', 0)} connection(s)."],
        simulated=True,
    )


async def map_company(ctx: Ctx) -> StepResult:
    from app.discovery import map as map_mod

    ws_id = getattr(ctx.venture, "workspace_id", None)
    if not ws_id:
        return StepResult(output={"skipped": True, "reason": "no workspace"})
    view = await map_mod.discover_and_map(ws_id)
    return StepResult(
        output={"map": view.to_dict()},
        tokens={"version": view.version},
        detail=[f"Company map v{view.version} ready for your corrections."],
    )


async def propose(ctx: Ctx) -> StepResult:
    """Stub: narrate three small, boring proposals."""
    proposals = [
        {
            "title": "Stabilize one flaky test",
            "why": "Cheap, reviewable, proves house style.",
            "risk": "low",
        },
        {
            "title": "Add a missing index called out in slow queries",
            "why": "Small blast radius with a clear revert.",
            "risk": "low",
        },
        {
            "title": "Bump a patch dependency with a green suite",
            "why": "Obvious correctness over impressive refactors.",
            "risk": "low",
        },
    ]
    return StepResult(
        output={"proposals": proposals},
        tokens={"count": len(proposals)},
        detail=[p["title"] for p in proposals],
    )


async def prove(ctx: Ctx) -> StepResult:
    """Stub: narrate a sandbox proof without touching their default branch."""
    return StepResult(
        output={
            "status": "awaiting_pick",
            "note": "Pick one proposal; I will open a sandbox branch and a draft PR only.",
        },
        detail=["Sandbox only until you accept a proof."],
    )


STEPS: list[StepDef] = [
    StepDef("byoc.orient", "orient", orient, "byoc.orient", timeout_s=5),
    StepDef("byoc.connect", "connect", connect, "byoc.connect", timeout_s=5),
    StepDef("byoc.discover", "discover", discover, "byoc.discover", timeout_s=30),
    StepDef("byoc.map", "map", map_company, "byoc.map", timeout_s=30),
    StepDef("byoc.propose", "propose", propose, "byoc.propose", timeout_s=10),
    StepDef("byoc.prove", "prove", prove, "byoc.prove", timeout_s=10),
]
