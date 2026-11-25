"""Business planning recommendations."""

from __future__ import annotations

from typing import Any

from app.ops.base_agent import BaseOpsAgent


class BusinessPlanningAgent(BaseOpsAgent):
    agent_type = "business_planning"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        company = (context.get("company") or {}).get("name") or "the company"
        mission = (context.get("company") or {}).get("mission") or ""
        parsed = self.call_claude_json(
            f"Recommend next business moves for {company}. Mission: {mission}. "
            "Return JSON with summary and recommendations list."
        )
        return {
            "summary": parsed.get("summary")
            or f"Planning recommendations for {company}",
            "recommendations": parsed.get("recommendations")
            or ["Clarify ICP", "Ship one growth experiment", "Tighten pricing page"],
        }
