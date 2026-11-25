"""Typed, deterministic feasibility domain models."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Domain(str, Enum):
    SOFTWARE = "software"
    CONTENT = "content"
    GTM = "gtm"
    IDENTITY = "identity"
    LEGAL = "legal"
    FINANCIAL = "financial"
    HARDWARE = "hardware"
    PHYSICAL_OPS = "physical_ops"
    HUMAN_SERVICE = "human_service"
    REGULATED = "regulated"


class RequirementVerdict(str, Enum):
    CAN_FULLY = "can_fully"
    CAN_WITH_HELP = "can_with_help"
    CAN_PARTIALLY = "can_partially"
    CANNOT = "cannot"
    UNKNOWN = "unknown"


class RequirementNode(BaseModel):
    id: str
    label: str
    domain: Domain
    children: list["RequirementNode"] = Field(default_factory=list)
    capability_ids: list[str] = Field(default_factory=list)
    tool_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    verdict: RequirementVerdict | None = None
    human_minutes: int = 0
    human_task_template: str | None = None
    alternative: str | None = None
    novelty: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    phase: int | None = None


class HardwareTrack(BaseModel):
    required: list[RequirementNode] = Field(default_factory=list)
    alternative: str | None = None

