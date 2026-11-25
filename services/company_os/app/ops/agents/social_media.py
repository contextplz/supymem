"""Social media agent — drafts locally, publishes through social.post when armed."""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from app.actuation.contract import ActuationMode
from app.ops import config as ops_config
from app.ops.base_agent import BaseOpsAgent
from app.ops.sandbox import SandboxBlocked, assert_twitter_write


class SocialMediaAgent(BaseOpsAgent):
    agent_type = "social_media"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        company = (context.get("company") or {}).get("name") or "our product"
        parsed = self.call_claude_json(
            f"Draft a short social post for {company}. Return JSON with summary and content."
        )
        content = parsed.get("content") or (
            f"Shipping updates from {company}, what should we build next?"
        )
        published = False
        external_ref = None
        external_url = None
        mode = "draft"
        if not ops_config.sandbox_mode():
            try:
                assert_twitter_write()
                result = self._publish(content, context)
                published = result.mode is ActuationMode.LIVE
                external_ref = result.external_ref
                external_url = result.external_url
                mode = result.mode.value
            except SandboxBlocked:
                published = False
                mode = "sandbox"

        workspace_id = context.get("workspace_id")
        if workspace_id:
            from app.ops.services import social as social_svc

            social_svc.create_post(
                workspace_id,
                content=content,
                status="published" if published else "draft",
                platform="twitter",
                tweet_id=external_ref if published else None,
            )
        return {
            "summary": parsed.get("summary") or "Draft social post ready",
            "content": content,
            "status": "published" if published else "draft",
            "mode": mode,
            "external_ref": external_ref,
            "external_url": external_url,
            "sandbox": ops_config.sandbox_mode(),
        }

    def _publish(self, content: str, context: dict[str, Any]):
        from app.actuation.execute import execute

        venture_id = str(context.get("venture_id") or context.get("workspace_id") or "ops")

        async def _run():
            return await execute(
                "social.post",
                venture_id,
                {"content": content, "publishNow": True},
                allow_live=True,
            )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(lambda: asyncio.run(_run())).result()
            return loop.run_until_complete(_run())
        except RuntimeError:
            return asyncio.run(_run())
