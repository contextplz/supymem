"""Experiment ledger for the console (recording results goes through chat)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.experiments import MAX_ACTION_LENGTH
from app.experiments.ledger import (
    has_venture_access,
    list_for_venture,
    loop_state,
    start_proposed,
    suggested_tests,
)

router = APIRouter(tags=["experiments"])


class ProposedTest(BaseModel):
    hypothesis: str = Field(..., min_length=1, max_length=1500)
    action: str = Field("", max_length=MAX_ACTION_LENGTH)
    success_measure: str = Field("", max_length=600)
    direction: str | None = Field(None, max_length=200)


@router.get("/ventures/{venture_id}/market-map")
def market_map(venture_id: str):
    from app.experiments.beliefs import load

    if not has_venture_access(venture_id):
        raise HTTPException(404, "not_found")
    context = load(venture_id)
    return {"beliefs": context["beliefs"], "plan": context.get("plan")}


@router.get("/ventures/{venture_id}/experiments")
def list_venture_experiments(venture_id: str):
    if not has_venture_access(venture_id):
        raise HTTPException(404, "not_found")
    state = loop_state(venture_id)
    items = list_for_venture(venture_id, include_system=True)
    for item in items:
        if item.get("source") == "Sample":
            item["hypothesis"] = "Sample: " + item["hypothesis"]
    return {"items": items, "current_direction": state["direction"],
            "review_ready": state["ready"],
            "suggested": suggested_tests(venture_id)}


@router.post("/ventures/{venture_id}/experiments/start")
def start_proposed_experiment(venture_id: str, body: ProposedTest):
    """"Start this test" on a validation review: records the proposed test and marks it running."""
    if not has_venture_access(venture_id):
        raise HTTPException(404, "not_found")
    item = start_proposed(venture_id, body.model_dump())
    if item is None:
        raise HTTPException(422, "This suggestion may be out of date. Reopen Experiments and choose a test for your current idea.")
    return item


@router.post("/ventures/{venture_id}/measurement/counts")
async def record_page_counts(venture_id: str, request: Request):
    from app.chat.execute import execute_action
    from core.bounded_body import events_body

    try:
        body = json.loads(await events_body(request))
        if not isinstance(body, dict):
            raise ValueError
    except ValueError as error:
        raise HTTPException(getattr(error, "status", 422), detail="In chat, use Record your page counts on your test readout, fill in the counts and date range, then send that message.") from None

    try:
        result = await execute_action(venture_id, "experiment.measurement.record", payload=body)
    except ValueError as error:
        raise HTTPException(422, detail=str(error)) from None
    if "counts" not in result:
        raise HTTPException(422, detail=result["body_md"])
    return result
