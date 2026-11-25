"""Email outreach — drafts only under sandbox."""

from __future__ import annotations

from typing import Any

from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent
from app.ops.sandbox import SandboxBlocked, assert_sendgrid_write


class EmailOutreachAgent(BaseOpsAgent):
    agent_type = "email_outreach"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        company = (context.get("company") or {}).get("name") or "our product"
        parsed = self.call_claude_json(
            f"Draft a personalized outreach email for {company}. "
            "Return JSON with summary, subject, body."
        )
        subject = parsed.get("subject") or f"Quick idea for your team{company}"
        body = parsed.get("body") or (
            f"Hi, noticed you're exploring this space. "
            f"{company} might help. Open to a short chat?"
        )
        sent = False
        if not ops_config.sandbox_mode():
            try:
                assert_sendgrid_write()
                sent = True
            except SandboxBlocked:
                sent = False
        return {
            "summary": parsed.get("summary") or "Outreach draft ready",
            "subject": subject,
            "body": body,
            "status": "sent" if sent else "draft",
            "sandbox": ops_config.sandbox_mode(),
        }
