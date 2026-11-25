"""Truthful result contract for actions that may affect external systems."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActuationMode(StrEnum):
    LIVE = "live"
    SIMULATED = "simulated"
    REFUSED = "refused"
    FAILED = "failed"


class ActuationResult(BaseModel):
    """The auditable outcome of one external action."""

    mode: ActuationMode
    tool_id: str
    venture_id: str
    ok: bool
    reason: str | None = None
    external_ref: str | None = None
    external_url: str | None = None
    provider_response: dict[str, Any] | None = None
    dry_run: bool = False
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime = Field(default_factory=_utcnow)


def assert_live_is_honest(result: ActuationResult) -> None:
    """Reject a live claim that lacks a provider-issued reference."""
    if result.mode is ActuationMode.LIVE and not result.external_ref:
        raise ValueError("live actuation requires external_ref")
