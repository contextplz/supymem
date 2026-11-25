"""Compact project state for the planner."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.state import MEM


def compact_state(ctx: Any) -> dict[str, Any]:
    charter = (ctx.out.get("commit.lock_charter") or {}).get("content") or {}
    website = MEM.get(f"website:{ctx.venture.id}") or {}
    return {
        "venture_id": ctx.venture.id,
        "name": ctx.company,
        "one_liner": charter.get("one_liner"),
        "audience": charter.get("audience"),
        "has_landing": bool(website.get("url")),
        "published": bool(website.get("published")),
        "autonomy": ctx.venture.autonomy_level,
    }


def state_hash(state: dict[str, Any]) -> str:
    blob = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
