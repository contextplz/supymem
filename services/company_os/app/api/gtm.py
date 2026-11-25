"""GTM planning API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import GtmPlanBody
from app.db.models import Venture
from app.gtm.campaign import create_campaign
from app.gtm.planner import plan_campaign
from app.state import MEM, db

router = APIRouter(tags=["gtm"])


@router.post("/ventures/{venture_id}/gtm/plan")
def gtm_plan(venture_id: str, payload: GtmPlanBody):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    actions = payload.actions_per_channel or {c: 1 for c in payload.channels}
    plan = plan_campaign(
        channels=payload.channels,
        actions_per_channel=actions,
        budget_usd=payload.budget_usd,
        gtm_order=payload.gtm_order,
        include_links_on_x=payload.include_links_on_x,
    )
    result = {
        "ok": plan.ok,
        "projected_cost_usd": plan.projected_cost_usd,
        "channels": plan.channels,
        "earliest_start_days": plan.earliest_start_days,
        "human_tasks": plan.human_tasks,
        "line_items": plan.line_items,
        "reason": plan.reason,
    }
    if plan.ok:
        try:
            camp = create_campaign(
                venture_id,
                hypothesis=f"{payload.channels} demand test meets threshold",
                metric="qualified_signups",
                threshold=10.0,
                window_days=14,
                channels=payload.channels,
            )
            result["campaign_id"] = camp.id
        except ValueError:
            pass
    MEM[f"gtm:{venture_id}"] = result
    return result


@router.get("/ventures/{venture_id}/gtm")
def get_gtm(venture_id: str):
    return MEM.get(f"gtm:{venture_id}") or {"ok": None, "channels": []}
