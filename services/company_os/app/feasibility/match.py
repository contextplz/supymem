"""Deterministic capability and blocker matching."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.feasibility.blockers import blocker_verdict, match_blocker
from app.feasibility.model import Domain, RequirementNode, RequirementVerdict

_CONFIG = Path(__file__).resolve().parents[4] / "config" / "capabilities.yaml"


@lru_cache
def load_capabilities() -> list[dict[str, Any]]:
    data = yaml.safe_load(_CONFIG.read_text()) or {}
    return [row for row in data.get("capabilities", []) if isinstance(row, dict)]


def match_capabilities(label: str) -> list[dict[str, Any]]:
    text = label.lower()
    return [
        capability
        for capability in load_capabilities()
        if any(str(term).lower() in text for term in capability.get("matches", []))
    ]


def assess_requirement(node: RequirementNode) -> RequirementNode:
    """Assess a leaf with the ordered rules from the capability contract."""
    if node.children:
        node.children = [assess_requirement(child) for child in node.children]
        return node
    blocker = match_blocker(node.label)
    if blocker:
        node.blockers = [str(blocker["id"])]
        node.verdict = blocker_verdict(blocker)
        node.human_minutes = int(blocker.get("human_minutes") or 0)
        node.human_task_template = blocker.get("human_task_template")
        node.alternative = blocker.get("we_do_instead")
        return node
    if node.domain in {Domain.HARDWARE, Domain.PHYSICAL_OPS, Domain.HUMAN_SERVICE, Domain.REGULATED}:
        node.verdict = RequirementVerdict.CANNOT
        node.alternative = "We can build the software and coordination layer around this external requirement."
        return node
    if node.domain in {Domain.IDENTITY, Domain.LEGAL, Domain.FINANCIAL}:
        node.verdict = RequirementVerdict.CAN_WITH_HELP
        node.human_minutes = 10
        node.alternative = "We prepare everything; you complete the one-time identity or legal action."
        return node
    capabilities = match_capabilities(node.label)
    if not capabilities:
        node.verdict = RequirementVerdict.UNKNOWN
        node.novelty = 1.0
        return node
    node.capability_ids = [str(cap["id"]) for cap in capabilities]
    node.tool_ids = [str(tool) for cap in capabilities for tool in cap.get("tools", [])]
    if all(bool(cap.get("live")) for cap in capabilities):
        node.verdict = RequirementVerdict.CAN_FULLY
    else:
        node.verdict = RequirementVerdict.CAN_PARTIALLY
        node.alternative = "A required tool is not live; we can provide the supported subset."
    return node


def assess_tree(tree: RequirementNode) -> RequirementNode:
    return assess_requirement(tree)
