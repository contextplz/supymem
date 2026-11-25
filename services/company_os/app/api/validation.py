"""Validation-gate read and decision endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.validation import board
from app.validation.board import _persist_research_step, _validation_from_persisted_research, validation_from_market

router = APIRouter(tags=["validation"])


class ValidationDecisionBody(BaseModel):
    action: Literal["review_full", "review_summary", "proceed", "dont_proceed", "build_reframe"]
    note: str | None = None


@router.get("/ventures/{venture_id}/validation")
def get_validation(venture_id: str):
    return board.get_validation(venture_id)


@router.post("/ventures/{venture_id}/validation/retry-research")
async def retry_research(venture_id: str):
    return await board.retry_research(venture_id)


@router.post("/ventures/{venture_id}/validation/decision")
async def decide_validation(venture_id: str, payload: ValidationDecisionBody):
    return await board.decide_validation(venture_id, payload)
