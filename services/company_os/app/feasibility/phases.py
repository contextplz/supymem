"""Evidence-oriented phase planning."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.feasibility.model import RequirementNode, RequirementVerdict
from app.feasibility.novelty import phase_one_days


class Phase(BaseModel):
    n: int
    name: str
    proves: str
    proof_metric: str
    proof_threshold: float
    includes: list[str] = Field(default_factory=list)
    excludes_deferred: list[str] = Field(default_factory=list)
    human_tasks: list[str] = Field(default_factory=list)
    estimated_days: int
    blocked_by: list[str] = Field(default_factory=list)
    kill_criteria: str


def plan_phases(nodes: list[RequirementNode], novelty: float) -> list[Phase]:
    buildable = [node for node in nodes if node.verdict in {RequirementVerdict.CAN_FULLY, RequirementVerdict.CAN_WITH_HELP}]
    first = buildable[: max(1, min(3, len(buildable)))]
    deferred = [node.id for node in buildable if node not in first]
    human = [node.id for node in first if node.human_minutes]
    blockers = [blocker for node in first for blocker in node.blockers]
    return [
        Phase(
            n=1,
            name="Signal",
            proves="A defined audience will leave a measurable intent signal before a full build.",
            proof_metric="landing_conversion_rate",
            proof_threshold=0.05,
            includes=[node.id for node in first],
            excludes_deferred=deferred,
            human_tasks=human,
            estimated_days=phase_one_days(novelty),
            blocked_by=blockers,
            kill_criteria="Stop or reframe if fewer than 5% of qualified visitors convert after two positioning tests.",
        ),
        Phase(
            n=2,
            name="Thin slice",
            proves="Early users activate the core workflow.",
            proof_metric="engagement_rate",
            proof_threshold=0.4,
            includes=deferred,
            estimated_days=5,
            kill_criteria="Stop expansion if fewer than 40% of recruited users complete the core action.",
        ),
    ]
