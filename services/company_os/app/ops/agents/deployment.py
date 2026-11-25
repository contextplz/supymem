"""Deployment agent — records intent only in sandbox (no Render HTTP)."""

from __future__ import annotations

from typing import Any

from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent
from app.ops.sandbox import SandboxBlocked, assert_render_write


class DeploymentAgent(BaseOpsAgent):
    agent_type = "deployment"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        workspace_id = context.get("workspace_id") or ""
        service = (
            (task.get("metadata") or {}).get("service_name")
            if isinstance(task.get("metadata"), dict)
            else None
        ) or "web"
        parsed = self.call_claude_json(
            f"Summarize deploy intent for service={service}. Return JSON with summary."
        )
        status = "sandbox_intent"
        external_id = None
        if ops_config.sandbox_mode():
            status = "sandbox_intent"
        else:
            try:
                assert_render_write()
                status = "triggered"
                external_id = "render_live"
            except SandboxBlocked:
                status = "sandbox_intent"

        record = None
        if workspace_id:
            from app.ops.services import deployments as deploy_svc

            record = deploy_svc.record_deployment(
                workspace_id,
                service_name=service,
                status=status,
                trigger_source="agent",
                external_id=external_id,
                summary=parsed.get("summary") or f"Deploy intent for {service}",
            )
        return {
            "summary": parsed.get("summary") or f"Deploy intent recorded for {service}",
            "status": status,
            "deployment_id": record.get("id") if record else None,
            "sandbox": ops_config.sandbox_mode(),
        }
