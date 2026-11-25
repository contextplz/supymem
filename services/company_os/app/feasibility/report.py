"""Feasibility report assembly and deterministic rollups."""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.feasibility.match import assess_tree
from app.feasibility.model import Domain, HardwareTrack, RequirementNode, RequirementVerdict
from app.feasibility.novelty import confidence_for, score_novelty
from app.feasibility.phases import Phase, plan_phases


class FeasibilityReport(BaseModel):
    venture_id: str
    tree: RequirementNode
    can_fully: list[RequirementNode] = Field(default_factory=list)
    can_with_help: list[RequirementNode] = Field(default_factory=list)
    can_partially: list[RequirementNode] = Field(default_factory=list)
    cannot: list[RequirementNode] = Field(default_factory=list)
    unknown: list[RequirementNode] = Field(default_factory=list)
    software_coverage_pct: float
    total_coverage_pct: float
    human_minutes_total: int
    hardware_track: HardwareTrack | None = None
    novelty: float
    confidence: float
    phases: list[Phase] = Field(default_factory=list)
    generated_at: datetime
    registry_version: str = "capabilities-v1"

    @property
    def I_CAN_DO_FULLY(self) -> list[RequirementNode]:  # noqa: N802
        return self.can_fully

    @property
    def I_CAN_DO_WITH_YOUR_ONE_TIME_HELP(self) -> list[RequirementNode]:  # noqa: N802
        return self.can_with_help

    @property
    def I_CANNOT_DO(self) -> list[RequirementNode]:  # noqa: N802
        return self.cannot


def _leaves(node: RequirementNode) -> list[RequirementNode]:
    if not node.children:
        return [node]
    return [leaf for child in node.children for leaf in _leaves(child)]


def build_report(
    venture_id: str, tree: RequirementNode, *, competitor_count: int = 0, source_count: int = 0
) -> FeasibilityReport:
    assessed = assess_tree(tree)
    leaves = _leaves(assessed)
    buckets = {verdict: [node for node in leaves if node.verdict == verdict] for verdict in RequirementVerdict}
    supported = buckets[RequirementVerdict.CAN_FULLY] + buckets[RequirementVerdict.CAN_WITH_HELP]
    total = len(leaves) or 1
    software = [node for node in leaves if node.domain in {Domain.SOFTWARE, Domain.CONTENT, Domain.GTM}] or []
    software_supported = [node for node in software if node in supported]
    novelty = score_novelty(leaves, competitor_count=competitor_count, source_count=source_count)
    return FeasibilityReport(
        venture_id=venture_id,
        tree=assessed,
        can_fully=buckets[RequirementVerdict.CAN_FULLY],
        can_with_help=buckets[RequirementVerdict.CAN_WITH_HELP],
        can_partially=buckets[RequirementVerdict.CAN_PARTIALLY],
        cannot=buckets[RequirementVerdict.CANNOT],
        unknown=buckets[RequirementVerdict.UNKNOWN],
        software_coverage_pct=round(100 * len(software_supported) / len(software), 1) if software else 0.0,
        total_coverage_pct=round(100 * len(supported) / total, 1),
        human_minutes_total=sum(node.human_minutes for node in leaves),
        hardware_track=HardwareTrack(required=[node for node in leaves if node.domain == Domain.HARDWARE])
        if any(node.domain == Domain.HARDWARE for node in leaves)
        else None,
        novelty=novelty,
        confidence=confidence_for(novelty, leaves),
        phases=plan_phases(leaves, novelty),
        generated_at=datetime.now(timezone.utc),
    )
