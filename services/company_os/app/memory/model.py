"""Memory kinds, writes, hits — the shapes behind MemoryPort (doc 72 §2)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryKind(StrEnum):
    INSIGHT = "insight"
    DECISION = "decision"
    STYLE_CORRECTION = "style_correction"
    FOUNDER_PREFERENCE = "founder_preference"
    OUTCOME = "outcome"
    FAILURE = "failure"
    ENTITY = "entity"
    ARTIFACT = "artifact"
    CONSTRAINT = "constraint"
    SKILL_RESOLUTION = "skill_resolution"
    POCKET = "pocket"


class MemoryWrite(BaseModel):
    venture_id: str
    kind: MemoryKind
    subject: str
    statement: str
    detail: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float
    valid_from: datetime = Field(default_factory=_utcnow)
    entities: list[str] = Field(default_factory=list)
    author: str = "system"
    dedupe_key: str
    tags: list[str] = Field(default_factory=list)
    half_life_days: float | None = None
    # Provenance of confidence — MUST NOT be "llm_self". Doc 72 M-3.
    confidence_source: str = "rule"

    @field_validator("venture_id")
    @classmethod
    def _venture_required(cls, v: str) -> str:
        if not v or not str(v).strip():
            raise ValueError("venture_id is mandatory")
        return str(v).strip()

    @field_validator("confidence")
    @classmethod
    def _conf_range(cls, v: float) -> float:
        if not 0.0 <= float(v) <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        return float(v)

    @field_validator("confidence_source")
    @classmethod
    def _no_llm_self(cls, v: str) -> str:
        if v == "llm_self":
            raise ValueError("confidence may not be LLM-self-reported (doc 72 M-3)")
        return v


class MemoryHit(BaseModel):
    mem_id: str
    venture_id: str
    kind: MemoryKind
    subject: str
    statement: str
    detail: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float
    decayed_confidence: float
    valid_from: datetime
    valid_to: datetime | None = None
    superseded_by: str | None = None
    entities: list[str] = Field(default_factory=list)
    author: str = "system"
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0


class RecallQuery(BaseModel):
    venture_id: str
    text: str = ""
    kinds: list[MemoryKind] | None = None
    subjects: list[str] | None = None
    as_of: datetime | None = None
    limit: int = 20
    include_decayed: bool = False


class TaskRef(BaseModel):
    capability_id: str | None = None
    title: str = ""
    tags: list[str] = Field(default_factory=list)


class ContextPack(BaseModel):
    venture_id: str
    memories: list[MemoryHit] = Field(default_factory=list)
    token_estimate: int = 0
    truncated_kinds: list[str] = Field(default_factory=list)
    omitted_count: int = 0
    omitted_memory_ids: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    mem_id: str
    statement: str
    when: datetime
    author: str


class GroundedAnswer(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None
    conflicts: list[tuple[str, str]] = Field(default_factory=list)


class MemoryHealth(BaseModel):
    ok: bool
    adapter: str
    detail: str = ""
    latency_ms: float | None = None


class MemoryStats(BaseModel):
    venture_id: str
    total: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    active: int = 0
    superseded: int = 0


class MemoryRecord(BaseModel):
    """Internal row shape shared by adapters."""

    mem_id: str
    venture_id: str
    kind: MemoryKind
    subject: str
    statement: str
    detail: str | None = None
    evidence: list[str] = Field(default_factory=list)
    confidence: float
    confidence_source: str = "rule"
    valid_from: datetime
    valid_to: datetime | None = None
    superseded_by: str | None = None
    entities: list[str] = Field(default_factory=list)
    author: str = "system"
    dedupe_key: str
    tags: list[str] = Field(default_factory=list)
    half_life_days: float | None = None
    tombstoned: bool = False
    tombstone_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
