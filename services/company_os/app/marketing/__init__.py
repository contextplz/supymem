"""Marketing strategist → plan → jobs → verified execution.

The execution manager (`executor.run_tick`) runs compiled jobs through adapters
(Composio API, content engine, visual renderer, gtm outreach, ads). Hermes
research/browser adapters are operator fallbacks only — not the brain.
"""

from __future__ import annotations

from app.marketing.executor import (
    resume_blocked,
    run_tick,
    status as execution_status,
)
from app.marketing.handoff import create_plan_from_research
from app.marketing.strategist import activate_plan, create_plan_from_brief

__all__ = [
    "activate_plan",
    "create_plan_from_brief",
    "create_plan_from_research",
    "execution_status",
    "resume_blocked",
    "run_tick",
]
