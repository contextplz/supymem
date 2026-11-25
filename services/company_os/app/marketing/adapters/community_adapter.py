"""Community / research adapters — Reddit find/draft (watering holes).

Owned Discord Community Department lives in ``app.community`` /
``CommunityDepartmentAdapter``. This adapter is Loop-adjacent research only:
find threads and draft replies. Publish stays blocked until policy allows.
"""

from __future__ import annotations

from typing import Any

from core.clock import utcnow

from app.marketing.schema import proof_shape


class CommunityAdapter:
    name = "community"

    def supports(self, kind: str) -> bool:
        return kind in {"find_threads", "draft_reply", "publish_reply", "research_topic"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        if kind == "find_threads":
            return await self._find(job)
        if kind == "draft_reply":
            return await self._draft(job)
        if kind == "publish_reply":
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "reddit_publish_requires_policy_clearance",
                "error": "publish_reply_blocked",
                "result": {},
                "proof": {},
            }
        if kind == "research_topic":
            themes = (job.get("payload") or {}).get("themes") or []
            return {
                "ok": True,
                "status": "done",
                "result": {"notes": [f"Research angle: {t}" for t in themes[:5]]},
                "proof": proof_shape(
                    mode="simulated",
                    timestamp=utcnow().isoformat(),
                    extra={"kind": "research_topic"},
                ),
            }
        return {
            "ok": False,
            "status": "failed",
            "error": f"unsupported:{kind}",
            "result": {},
            "proof": {},
        }

    async def _find(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        themes = payload.get("themes") or []
        limit = int(payload.get("limit") or 10)
        # Structured intents — real Composio Reddit search wires in later.
        threads = [
            {
                "query": str(theme),
                "platform": job.get("platform") or "reddit",
                "status": "queued_for_search",
            }
            for theme in themes[:limit]
        ]
        if not threads:
            threads = [
                {
                    "query": "AI agent go-to-market",
                    "platform": "reddit",
                    "status": "queued_for_search",
                }
            ]
        proof = proof_shape(
            mode="simulated",
            timestamp=utcnow().isoformat(),
            extra={"thread_count": len(threads)},
        )
        return {
            "ok": True,
            "status": "done",
            "result": {"threads": threads},
            "proof": proof,
        }

    async def _draft(self, job: dict[str, Any]) -> dict[str, Any]:
        limit = int((job.get("payload") or {}).get("limit") or 3)
        drafts = [
            {
                "index": i + 1,
                "body": (
                    "Sharing what worked for us when we hit this: treat the "
                    "workflow as one loop (find → research → send → learn) "
                    "instead of bolting another drafting tool onto a manual process."
                ),
                "status": "local_draft",
                "publish": False,
            }
            for i in range(limit)
        ]
        proof = proof_shape(
            mode="simulated",
            timestamp=utcnow().isoformat(),
            extra={"draft_count": len(drafts), "human_send": True},
        )
        return {
            "ok": True,
            "status": "done",
            "result": {"drafts": drafts},
            "proof": proof,
        }
