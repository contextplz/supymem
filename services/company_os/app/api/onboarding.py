"""Onboarding API (29 §1)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.models import Venture
from app.core.local_mode import is_local
from app.onboarding.orchestrator import cancel_run
from app.onboarding.state import active_run, latest_run, list_steps, load_run, resumable_run
from app.state import db
from core.clock import utcnow

router = APIRouter(tags=["onboarding"])


class OnboardBody(BaseModel):
    resume: bool = False
    mode: str | None = None
    research_founder: bool = False


def _elapsed_ms(started, finished=None) -> int | None:
    if not started:
        return None
    end = finished or utcnow()
    # SQLite often returns naive; utcnow() is aware
    if getattr(started, "tzinfo", None) is not None and getattr(end, "tzinfo", None) is None:
        started = started.replace(tzinfo=None)
    if getattr(started, "tzinfo", None) is None and getattr(end, "tzinfo", None) is not None:
        end = end.replace(tzinfo=None)
    return int((end - started).total_seconds() * 1000)


def _run_payload(run) -> dict:
    flags = {}
    try:
        flags = json.loads(run.flags_json or "{}")
    except json.JSONDecodeError:
        flags = {}
    elapsed_ms = _elapsed_ms(run.started_at, run.finished_at)
    founder_action = None
    if run.paused_reason == "awaiting_product_understanding":
        founder_action = {
            "label": "Answer the product questions",
            "href": f"/v/{run.venture_id}/#clarify",
        }
    elif run.paused_reason == "awaiting_product_brief":
        founder_action = {
            "label": "Check what I understood",
            "href": f"/v/{run.venture_id}/#product-brief",
        }
    elif run.paused_reason == "awaiting_clarification":
        founder_action = {
            "label": "Continue GStack office hours",
            "href": f"/v/{run.venture_id}/#clarify",
        }
    elif run.paused_reason == "awaiting_builder_choice":
        founder_action = {
            "label": "Choose how to build the landing page",
            "href": f"/v/{run.venture_id}/#builder-choice",
        }
    elif run.paused_reason == "awaiting_builder_session":
        founder_action = {
            "label": "Watch the local build",
            "href": f"/v/{run.venture_id}/#builder-session",
        }
    elif run.paused_reason == "awaiting_gstack_plan":
        founder_action = {
            "label": "Review the GStack plan",
            "href": f"/v/{run.venture_id}/#gstack-plan",
        }
    elif run.paused_reason == "awaiting_validation_review":
        founder_action = {
            "label": "Review validation",
            "href": f"/v/{run.venture_id}/#validation",
        }
    elif run.paused_reason == "awaiting_validation_decision":
        founder_action = {
            "label": "Decide whether to proceed",
            "href": f"/v/{run.venture_id}/#validation",
        }
    elif run.paused_reason == "parked":
        founder_action = {
            "label": "Reopen validation",
            "href": f"/v/{run.venture_id}/#validation",
        }
    elif run.paused_reason == "interrupted":
        founder_action = {"label": "Resume", "href": f"/v/{run.venture_id}"}
    steps = [
        {
            "step_id": s.step_id,
            "phase": s.phase,
            "status": s.status,
            "duration_ms": s.duration_ms,
        }
        for s in list_steps(run.id)
    ]
    current_step = None
    for s in reversed(steps):
        if s["status"] == "running":
            current_step = {"step_id": s["step_id"], "phase": s["phase"], "status": s["status"]}
            break
    return {
        "run_id": run.id,
        "status": run.status,
        "mode": run.mode,
        "phase": run.phase,
        "phases_done": run.phases_done,
        "phases_total": run.phases_total,
        "paused_reason": run.paused_reason,
        "founder_action": founder_action,
        "cost_usd": round(run.cost_usd or 0.0, 4),
        "elapsed_ms": elapsed_ms,
        "steps": steps,
        "current_step": current_step,
        "flags": flags,
        "next_up_headline": run.next_up_headline,
    }


@router.post("/ventures/{venture_id}/onboard")
async def start_onboard(venture_id: str, payload: OnboardBody | None = None):
    payload = payload or OnboardBody()
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "Venture not found."}})

    existing = resumable_run(venture_id) if payload.resume else active_run(venture_id)
    if existing and existing.status == "running" and not payload.resume:
        raise HTTPException(
            409,
            detail={
                "error": {
                    "code": "onboarding_already_running",
                    "message": "Onboarding is already running.",
                    "detail": {"run_id": existing.id},
                }
            },
        )

    from app.onboarding.spawn import onboarding_sync, start_onboarding

    run = await start_onboarding(
        venture_id,
        mode=payload.mode or (existing.mode if existing else "guided"),
        resume=payload.resume,
        research_founder=payload.research_founder,
        run_id=existing.id if existing and payload.resume else None,
    )
    body = {"run_id": run.id, "status": run.status, "phase": run.phase}
    if onboarding_sync():
        return body
    from fastapi.responses import JSONResponse

    return JSONResponse(body, status_code=202)


@router.get("/ventures/{venture_id}/onboarding")
async def get_onboarding(venture_id: str, request: Request):
    if is_local():
        from app.admin.authn import verify_caller
        from app.authz.workspace import assert_venture_access

        assert_venture_access(venture_id, verify_caller(request, required=True).sub)
    run = active_run(venture_id) or latest_run(venture_id)
    if not run:
        if is_local():
            return {"status": "idle", "steps": [], "current_step": None}
        raise HTTPException(404, detail={"error": {"code": "no_onboarding", "message": "No onboarding run yet."}})
    if run.status == "paused" and run.paused_reason in {
        "awaiting_builder_choice",
        "awaiting_builder_session",
    }:
        from app.api.builder import complete_builder_because_page_exists
        from app.onboarding.state import load_run

        if await complete_builder_because_page_exists(venture_id, run):
            run = load_run(run.id) or run
    return _run_payload(run)


@router.post("/ventures/{venture_id}/onboarding/cancel")
async def cancel_onboarding(venture_id: str):
    run = active_run(venture_id)
    if not run:
        raise HTTPException(404, detail={"error": {"code": "no_onboarding", "message": "No active run."}})
    await cancel_run(run.id)
    return {"status": "cancelled"}
