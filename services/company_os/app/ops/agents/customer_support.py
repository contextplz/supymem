"""Customer support reply drafts."""

from __future__ import annotations

from typing import Any

from app.ops.base_agent import BaseOpsAgent


class CustomerSupportAgent(BaseOpsAgent):
    agent_type = "customer_support"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        inbox_item = task.get("description") or task.get("title") or "general inquiry"
        parsed = self.call_claude_json(
            f"Draft a helpful support reply for: {inbox_item}. "
            "Return JSON with summary and reply."
        )
        reply = parsed.get("reply") or (
            "Thanks for writing in, I looked into this and here's what I found."
        )
        return {
            "summary": parsed.get("summary") or "Support reply draft ready",
            "reply": reply,
        }
