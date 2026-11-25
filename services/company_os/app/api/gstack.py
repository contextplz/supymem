"""GStack office-hours plan — inferred after the six forcing questions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.local_mode import is_local
from app.gstack.office_hours import plan_for, product_brief_for, venture_fields
from app.onboarding.gates import get_run_gate, set_run_gate
from app.onboarding.state import active_run, resumable_run

router = APIRouter(tags=["gstack"])


class GStackPlanBody(BaseModel):
    action: Literal["accept"] = "accept"


def _venture_fields(venture_id: str) -> tuple[str, str, str]:
    try:
        return venture_fields(venture_id)
    except KeyError as exc:
        if exc.args != ("not_found",):
            raise
        raise HTTPException(404, exc.args[0]) from exc


def _plan_for(venture_id: str) -> dict:
    try:
        return plan_for(venture_id)
    except KeyError as exc:
        if exc.args not in (("not_found",), ("plan_not_ready",)):
            raise
        raise HTTPException(404, exc.args[0]) from exc


@router.get("/ventures/{venture_id}/gstack/plan")
def get_gstack_plan(venture_id: str):
    _venture_fields(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    try:
        plan = _plan_for(venture_id)
    except HTTPException as exc:
        if not is_local() or exc.status_code != 404 or exc.detail != "plan_not_ready":
            raise
        plan = None
    return {
        "plan": plan,
        "process": "gstack_office_hours",
        "review": get_run_gate("gstack_plan", venture_id, run.id) if run else None,
    }


@router.post("/ventures/{venture_id}/gstack/plan")
async def accept_gstack_plan(venture_id: str, payload: GStackPlanBody | None = None):
    _ = payload or GStackPlanBody()
    _venture_fields(venture_id)
    plan = _plan_for(venture_id)
    run = active_run(venture_id) or resumable_run(venture_id)
    if run:
        set_run_gate("gstack_plan", venture_id, run.id, "accepted")
        if run.status == "paused" and run.paused_reason == "awaiting_gstack_plan":
            from app.onboarding.spawn import start_onboarding

            await start_onboarding(
                venture_id,
                mode=run.mode,
                resume=True,
                run_id=run.id,
                entry_mode=getattr(run, "entry_mode", None) or "idea",
                workspace_id=getattr(run, "workspace_id", None),
            )
            return {"status": "resumed", "plan": plan, "process": "gstack_office_hours"}
    return {"status": "accepted", "plan": plan, "process": "gstack_office_hours"}


@router.get("/ventures/{venture_id}/product/brief")
def get_product_brief(venture_id: str):
    _venture_fields(venture_id)
    brief = product_brief_for(venture_id)
    if not brief and not is_local():
        raise HTTPException(404, "brief_not_ready")
    run = active_run(venture_id) or resumable_run(venture_id)
    return {
        "brief": brief,
        "process": "product_understanding",
        "review": get_run_gate("product_brief", venture_id, run.id) if run else None,
    }


@router.post("/ventures/{venture_id}/product/brief")
async def accept_product_brief(venture_id: str, payload: GStackPlanBody | None = None):
    _ = payload or GStackPlanBody()
    _venture_fields(venture_id)
    brief = product_brief_for(venture_id)
    if not brief:
        raise HTTPException(404, "brief_not_ready")
    run = active_run(venture_id) or resumable_run(venture_id)
    if run:
        set_run_gate("product_brief", venture_id, run.id, "accepted")
        if run.status == "paused" and run.paused_reason == "awaiting_product_brief":
            from app.onboarding.spawn import start_onboarding

            await start_onboarding(
                venture_id,
                mode=run.mode,
                resume=True,
                run_id=run.id,
                entry_mode=getattr(run, "entry_mode", None) or "idea",
                workspace_id=getattr(run, "workspace_id", None),
            )
            return {"status": "resumed", "brief": brief, "process": "product_understanding"}
    return {"status": "accepted", "brief": brief, "process": "product_understanding"}
