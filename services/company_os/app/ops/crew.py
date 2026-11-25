"""AGENT_MAP + run_agent_for_task — Polsia crew_factory equivalent."""

from __future__ import annotations

import importlib
from typing import Any

from app.ops import VALID_AGENT_TYPES

AGENT_MAP: dict[str, tuple[str, str]] = {
    "orchestrator": ("app.ops.agents.orchestrator", "OrchestratorAgent"),
    "business_planning": ("app.ops.agents.business_planning", "BusinessPlanningAgent"),
    "competitor_research": (
        "app.ops.agents.competitor_research",
        "CompetitorResearchAgent",
    ),
    "social_media": ("app.ops.agents.social_media", "SocialMediaAgent"),
    "email_outreach": ("app.ops.agents.email_outreach", "EmailOutreachAgent"),
    "customer_support": ("app.ops.agents.customer_support", "CustomerSupportAgent"),
    "ads_management": ("app.ops.agents.ads_management", "AdsManagementAgent"),
    "code_generation": ("app.ops.agents.code_generation", "CodeGenerationAgent"),
    "finance": ("app.ops.agents.finance", "FinanceAgent"),
    "deployment": ("app.ops.agents.deployment", "DeploymentAgent"),
}


def get_agent_class(agent_type: str):
    if agent_type not in AGENT_MAP:
        raise ValueError(f"unknown agent_type: {agent_type}")
    module_name, class_name = AGENT_MAP[agent_type]
    mod = importlib.import_module(module_name)
    return getattr(mod, class_name)


def run_agent_for_task(
    agent_type: str,
    task: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Instantiate the agent and run it with timing metadata."""
    if agent_type not in VALID_AGENT_TYPES:
        raise ValueError(f"invalid agent_type: {agent_type}")
    cls = get_agent_class(agent_type)
    agent = cls()
    return agent.timed_run(task or {}, context or {})
