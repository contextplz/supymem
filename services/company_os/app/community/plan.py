"""Community plan dataclasses and serialization helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CommunityVerdict(StrEnum):
    BUILD_OWNED = "build_owned"
    PRE_COMMUNITY = "pre_community"
    PARTICIPATE_EXISTING = "participate_existing"
    CAB_NOT_COMMUNITY = "cab_not_community"
    DO_NOT_BUILD = "do_not_build"


@dataclass
class DimensionScore:
    id: str
    label: str
    weight: float
    score: int
    evidence: list[str] = field(default_factory=list)
    research_paths: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    id: str
    label: str
    passed: bool
    fail_action: str | None = None
    evidence: str = ""


@dataclass
class CommunityThesis:
    thesis_score: int
    verdict: str
    because: str
    gates: list[GateResult]
    dimensions: list[DimensionScore]
    blocking_dimension_ids: list[str] = field(default_factory=list)
    motion: str = "founder_led_content"
    rescore_after_days: int = 60
    primary_segment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis_score": self.thesis_score,
            "verdict": self.verdict,
            "because": self.because,
            "gates": [asdict(g) for g in self.gates],
            "dimensions": [asdict(d) for d in self.dimensions],
            "blocking_dimension_ids": list(self.blocking_dimension_ids),
            "motion": self.motion,
            "rescore_after_days": self.rescore_after_days,
            "primary_segment": dict(self.primary_segment),
            "build_owned": self.verdict == CommunityVerdict.BUILD_OWNED,
        }


@dataclass
class ObjectiveMix:
    education: int
    networking: int
    product_adoption: int
    acquisition: int
    primary: str
    secondary: str
    archetype: str
    signal_trace: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommunityIdentity:
    name: str
    positioning: str
    description: str
    anti_position: str
    rules: list[str]
    member_archetypes: list[dict[str, Any]]
    channels: list[dict[str, Any]]
    roles: list[dict[str, Any]]
    name_rejected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Program30d:
    kpis: dict[str, Any]
    weeks: list[dict[str, Any]]
    rituals: list[dict[str, Any]]
    invites_target: int
    public_launch: bool
    staff_post_share_min: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommunityPlan:
    schema_version: str
    workspace_id: str
    venture_id: str | None
    research_bundle_id: str | None
    thesis: dict[str, Any]
    objectives: dict[str, Any] | None
    identity: dict[str, Any] | None
    program_30d: dict[str, Any] | None
    work_orders: list[dict[str, Any]] = field(default_factory=list)
    status: str = "draft"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "venture_id": self.venture_id,
            "research_bundle_id": self.research_bundle_id,
            "thesis": self.thesis,
            "objectives": self.objectives,
            "identity": self.identity,
            "program_30d": self.program_30d,
            "work_orders": list(self.work_orders),
            "status": self.status,
        }


def research_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize brief / research / free-form lab payload into a ResearchBundle-like dict."""
    raw = dict(payload or {})
    brief = raw.get("brief") if isinstance(raw.get("brief"), dict) else {}
    icp = raw.get("icp") if isinstance(raw.get("icp"), dict) else {}
    if not icp and isinstance(brief.get("icp"), dict):
        icp = brief["icp"]

    segments = raw.get("icp_segments") or icp.get("segments") or []
    if isinstance(segments, dict):
        # Marketing briefs sometimes use {name: {pains: ...}}
        normalized = []
        for name, body in segments.items():
            if isinstance(body, dict):
                normalized.append({"name": name, **body})
            else:
                normalized.append({"name": str(name)})
        segments = normalized

    personas = raw.get("personas") or brief.get("personas") or []
    competitors = raw.get("competitors") or brief.get("competitors") or []

    return {
        "venture_id": raw.get("venture_id") or brief.get("venture_id"),
        "product_name": (
            raw.get("product_name")
            or brief.get("product_name")
            or brief.get("name")
            or raw.get("name")
            or "Product"
        ),
        "one_liner": raw.get("one_liner") or brief.get("one_liner") or brief.get("summary") or "",
        "category": raw.get("category") or brief.get("category") or "",
        "differentiators": list(raw.get("differentiators") or brief.get("differentiators") or []),
        "features": list(raw.get("features") or brief.get("features") or []),
        "icp_segments": list(segments) if isinstance(segments, list) else [],
        "competitors": list(competitors) if isinstance(competitors, list) else [],
        "demand_signals": list(raw.get("demand_signals") or brief.get("demand_signals") or []),
        "personas": list(personas) if isinstance(personas, list) else [],
        "paying_customers": _int_or_none(raw.get("paying_customers")),
        "monthly_active_users": _int_or_none(raw.get("monthly_active_users")),
        "content_audience": _int_or_none(raw.get("content_audience")),
        "founder_hours_per_week": _int_or_none(raw.get("founder_hours_per_week")),
        "pmf_signal": raw.get("pmf_signal"),
        "founder_will_invite_25": bool(raw.get("founder_will_invite_25")),
        "core_job_completions": _int_or_none(raw.get("core_job_completions")),
    }


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
