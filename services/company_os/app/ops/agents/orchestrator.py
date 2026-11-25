"""Morning/evening orchestration agent."""

from __future__ import annotations

from typing import Any

from app.ops.base_agent import BaseOpsAgent


class OrchestratorAgent(BaseOpsAgent):
    agent_type = "orchestrator"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        company = (context.get("company") or {}).get("name") or "the company"
        title = task.get("title") or "planning cycle"
        parsed = self.call_claude_json(
            f"Plan ops for {company}. Task: {title}. Return JSON with summary, "
            "tasks (list of {title, agent_type}), key_focus."
        )
        summary = parsed.get("summary") or f"Ops plan for {company}: {title}"
        tasks = parsed.get("tasks") or [
            {"title": "Daily revenue snapshot", "agent_type": "finance"},
            {"title": "Morning social check", "agent_type": "social_media"},
            {"title": "Support inbox sweep", "agent_type": "customer_support"},
        ]
        return {
            "summary": summary,
            "tasks": tasks,
            "key_focus": parsed.get("key_focus") or "ship and learn",
            "insights": parsed.get("insights") or [],
        }
