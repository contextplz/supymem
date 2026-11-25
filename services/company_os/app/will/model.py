"""Pydantic shapes for the Will organ (doc 79)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SlateItem(BaseModel):
    rank: int = Field(ge=1, le=5)
    title: str = Field(min_length=1, max_length=60)
    why_now: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    expected_metric: str = Field(min_length=1)
    expected_delta: float
    capability_id: str = Field(min_length=1)
    assignee_agent_id: str | None = None
    effort: Literal["minutes", "hours", "days"] = "hours"
    cost_usd_estimate: Decimal = Decimal("0")
    autonomy: Literal["auto", "needs_approval", "needs_founder_action"] = "auto"
    blocked_by: list[str] = Field(default_factory=list)
    phase: int | None = None
    origin: Literal["insight", "phase", "probe", "evolution", "founder", "recovery"] = "phase"
    kill_criteria: str | None = None

    @field_validator("evidence")
    @classmethod
    def _evidence_ids_are_nonempty(cls, value: list[str]) -> list[str]:
        if not all(item.strip() for item in value):
            raise ValueError("evidence ids must be non-empty")
        return value


class Slate(BaseModel):
    id: str = Field(min_length=1)
    venture_id: str = Field(min_length=1)
    items: list[SlateItem] = Field(min_length=5, max_length=5)
    generated_at: datetime = Field(default_factory=_utcnow)
    generated_because: str = Field(min_length=1)
    supersedes: str | None = None
    founder_edited: bool = False
    founder_edit_transcript: str | None = None


class ItemOutcome(BaseModel):
    item_id: str
    status: Literal["worked", "did_not_work", "inconclusive", "blocked", "refused"]
    what_worked: str | None = None
    what_did_not: str | None = None
    surprised_us: str | None = None
    completed_at: datetime = Field(default_factory=_utcnow)
