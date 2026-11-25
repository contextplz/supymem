"""Polsia-inspired ops agents layer for Company OS (doc 49).

Reimplements the published Polsia surface (schema, API, schedules, sandbox)
from public contracts. No private Polsia backend was vendored.
"""

from __future__ import annotations

VALID_AGENT_TYPES: tuple[str, ...] = (
    "orchestrator",
    "business_planning",
    "competitor_research",
    "social_media",
    "email_outreach",
    "customer_support",
    "ads_management",
    "code_generation",
    "finance",
    "deployment",
)

__all__ = ["VALID_AGENT_TYPES"]
