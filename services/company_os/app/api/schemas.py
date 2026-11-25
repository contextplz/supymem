"""Shared request bodies for Company OS API routers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from core.enums import VentureStage


class CreateVenture(BaseModel):
    name: str | None = None
    raw_idea: str | None = None
    mode: str = "guided"
    research_founder: bool = False
    # None → True for new-style (no name), False for legacy {name, raw_idea}
    start: bool | None = None


class StageBody(BaseModel):
    stage: VentureStage


class AnswerBody(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    dissent: list[dict[str, Any]] = Field(default_factory=list)
    other_verbatim: dict[str, str] = Field(default_factory=dict)


class AdrBody(BaseModel):
    title: str = "MVP ADR"
    stack: list[str] = Field(default_factory=lambda: ["fastapi", "sqlite"])
    protected_routes: list[str] = Field(default_factory=lambda: ["/api/me", "/api/items"])
    components_estimate: int = 12


class GtmPlanBody(BaseModel):
    channels: list[str] = Field(default_factory=lambda: ["landing", "reddit"])
    actions_per_channel: dict[str, int] = Field(default_factory=dict)
    budget_usd: float = 25.0
    gtm_order: str = "product_first"
    include_links_on_x: bool = False


class FeedbackBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    source: Literal["customer", "public", "founder"] = "customer"


class ConciergeChatBody(BaseModel):
    message: str
    step_id: str | None = None


class CredentialBody(BaseModel):
    kind: str = "secret"
    value: str
    catalog: str = "stripe"


class ConnectSubmitBody(BaseModel):
    fields: dict[str, str] = Field(default_factory=dict)
    skipped: bool = False


class SimulateBody(BaseModel):
    kind: str = "config_change"
    cost_usd: float = 0.0


class CycleBody(BaseModel):
    budget_tokens: int = 10_000
