"""Email channel adapter — dry-run by default."""

from __future__ import annotations

import os
from typing import Any


class EmailAdapter:
    name = "email"
    dry_run_default = True

    def __init__(self, *, dry_run: bool | None = None) -> None:
        env = os.environ.get("CHANNELS_DRY_RUN", "true").lower() != "false"
        self.dry_run = env if dry_run is None else dry_run

    async def publish(self, payload: dict[str, Any], *, warden_allow: bool = False) -> dict[str, Any]:
        if self.dry_run:
            return {
                "ok": True, "mode": "simulated", "reason": "dry_run_env",
                "dry_run": True, "channel": self.name, "tool_id": "email.send",
                "external_ref": None, "external_url": None, "payload": payload,
            }
        if not warden_allow:
            return {
                "ok": False, "mode": "refused", "reason": "warden_tool_post_public_required",
                "dry_run": False, "channel": self.name, "tool_id": "email.send",
                "external_ref": None, "external_url": None,
            }
        return {
            "ok": True, "mode": "simulated", "reason": "provider_unavailable",
            "dry_run": False, "channel": self.name, "tool_id": "email.send",
            "external_ref": None, "external_url": None, "payload": payload,
        }
