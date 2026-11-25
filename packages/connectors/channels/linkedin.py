"""LinkedIn channel adapter — dry-run by default."""

from __future__ import annotations

import os
from typing import Any


class LinkedInAdapter:
    name = "linkedin"
    dry_run_default = True

    def __init__(self, *, dry_run: bool | None = None) -> None:
        env = os.environ.get("CHANNELS_DRY_RUN", "true").lower() != "false"
        self.dry_run = env if dry_run is None else dry_run

    async def publish(self, payload: dict[str, Any], *, warden_allow: bool = False) -> dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "channel": self.name, "payload": payload}
        if not warden_allow:
            return {"ok": False, "error": "warden_tool_post_public_required"}
        return {"ok": True, "dry_run": False, "channel": self.name, "live": True}
