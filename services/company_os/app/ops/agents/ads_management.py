"""Ads management recommendations — no live writes in sandbox."""

from __future__ import annotations

from typing import Any

from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent
from app.ops.sandbox import SandboxBlocked, assert_ads_write


class AdsManagementAgent(BaseOpsAgent):
    agent_type = "ads_management"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        parsed = self.call_claude_json(
            "Recommend an ad campaign adjustment. Return JSON with summary and recommendation."
        )
        wrote = False
        if not ops_config.sandbox_mode():
            try:
                assert_ads_write()
                wrote = True
            except SandboxBlocked:
                wrote = False
        return {
            "summary": parsed.get("summary") or "Ad campaign recommendation ready",
            "recommendation": parsed.get("recommendation")
            or "Pause underperforming keywords; raise budget on top CTR ad set.",
            "applied": wrote,
            "sandbox": ops_config.sandbox_mode(),
        }
