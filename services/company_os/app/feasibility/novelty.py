"""Mechanical novelty and confidence calculations."""
from __future__ import annotations

from app.feasibility.model import Domain, RequirementNode, RequirementVerdict


def score_novelty(
    nodes: list[RequirementNode], *, competitor_count: int = 0, source_count: int = 0
) -> float:
    if not nodes:
        return 1.0
    scores: list[float] = []
    for node in nodes:
        scores.append(
            0.30 * (1 - min(competitor_count / 5, 1))
            + 0.25 * (1 if not node.capability_ids else 0)
            + 0.20 * (1 - min(source_count / 5, 1))
            + 0.15 * (1 if node.domain in {Domain.HARDWARE, Domain.REGULATED, Domain.HUMAN_SERVICE} else 0)
            + 0.10 * (1 if node.verdict == RequirementVerdict.UNKNOWN else 0)
        )
    return round(max(0.0, min(1.0, sum(scores) / len(scores))), 3)


def confidence_for(novelty: float, nodes: list[RequirementNode]) -> float:
    unknown_fraction = (
        sum(node.verdict == RequirementVerdict.UNKNOWN for node in nodes) / len(nodes) if nodes else 1.0
    )
    return round(max(0.15, min(0.95, 1 - 0.7 * novelty - 0.3 * unknown_fraction)), 3)


def phase_one_days(novelty: float) -> int:
    """High novelty gets a smaller probe, never a larger initial build."""
    return 2 + round(4 * (1 - max(0.0, min(1.0, novelty))))
