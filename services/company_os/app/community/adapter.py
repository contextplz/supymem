"""Marketing-spine adapter for community.* WorkOrder kinds.

Separate from Reddit ``CommunityAdapter`` (find_threads / draft_reply).
"""

from __future__ import annotations

from typing import Any

from core.clock import utcnow


def _proof(*, mode: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Local proof shape — avoid importing marketing package (circular)."""
    out: dict[str, Any] = {"mode": mode, "timestamp": utcnow().isoformat()}
    if extra:
        out.update(extra)
    return out


class CommunityDepartmentAdapter:
    name = "community_department"

    KINDS = {
        "community.evaluate_thesis",
        "community.generate_identity",
        "community.generate_program_30d",
        "community.pre_community_plan",
        "community.provision_discord",
        "community.host_ritual",
        "community.rescore",
    }

    def supports(self, kind: str) -> bool:
        return kind in self.KINDS or kind.startswith("community.")

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job.get("kind") or ""
        payload = job.get("payload") or {}
        workspace_id = str(job.get("workspace_id") or payload.get("workspace_id") or "")
        from app.community import service

        try:
            if kind == "community.evaluate_thesis":
                result = service.run_thesis(workspace_id, payload, persist=True)
            elif kind in {
                "community.generate_identity",
                "community.generate_program_30d",
                "community.pre_community_plan",
            }:
                result = service.create_plan(workspace_id, payload)
            elif kind == "community.provision_discord":
                result = service.provision_discord(
                    workspace_id,
                    guild_id=str(payload.get("guild_id") or "") or None,
                    plan_id=str(payload.get("plan_id") or "") or None,
                    approval_id=str(payload.get("approval_id") or "") or None,
                )
            elif kind == "community.host_ritual":
                result = service.discord_action(
                    workspace_id,
                    action="send_message",
                    params={
                        "channel_id": payload.get("channel_id"),
                        "content": payload.get("content") or payload.get("prompt"),
                        "confirm": bool(payload.get("confirm")),
                        "approval_id": payload.get("approval_id"),
                    },
                )
            elif kind == "community.rescore":
                result = service.run_thesis(workspace_id, payload, persist=True)
            else:
                return {
                    "ok": False,
                    "status": "failed",
                    "error": f"unsupported:{kind}",
                    "result": {},
                    "proof": {},
                }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": f"NeedsYou: {exc}",
                "error": str(exc),
                "result": {},
                "proof": _proof(mode="error", extra={"kind": kind}),
            }

        ok = bool(result.get("ok", True)) and result.get("status") != "blocked"
        if result.get("block_reason") or result.get("status") == "blocked":
            ok = False
        return {
            "ok": ok,
            "status": "done" if ok else "blocked",
            "block_reason": result.get("block_reason"),
            "error": result.get("error"),
            "result": result,
            "proof": _proof(
                mode="live" if ok else "blocked",
                extra={"kind": kind, "adapter": self.name},
            ),
        }
