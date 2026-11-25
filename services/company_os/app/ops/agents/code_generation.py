"""Code generation agent — sandbox-safe, no real GitHub writes."""

from __future__ import annotations

from typing import Any

from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent


class CodeGenerationAgent(BaseOpsAgent):
    agent_type = "code_generation"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        title = task.get("title") or "small improvement"
        parsed = self.call_claude_json(
            f"Propose a small code change for: {title}. "
            "Return JSON with summary and commit_message."
        )
        return {
            "summary": parsed.get("summary") or f"Code proposal for {title}",
            "commit_message": parsed.get("commit_message") or f"chore: {title}",
            "pushed": False,
            "sandbox": ops_config.sandbox_mode(),
        }
