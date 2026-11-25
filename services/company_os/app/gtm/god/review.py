"""Judge is not the author. Deterministic review gate (gtm-cofounder review-the-work)."""

from __future__ import annotations

import re
from typing import Any

PUFFERY = (
    "powerful",
    "best-in-class",
    "seamless",
    "revolutionary",
    "ai-powered",
    "game-changing",
    "#1",
    "trusted by",
)

HARVEST_HINTS = (
    "discover_leads",
    "buy a list",
    "scrape linkedin",
    "unipile",
    "auto-send on linkedin",
)


def review_artifact(artifact: dict[str, Any] | str | None) -> dict[str, Any]:
    blob = artifact if isinstance(artifact, str) else _flatten(artifact or {})
    lower = blob.lower()
    flags: list[str] = []
    for word in PUFFERY:
        if word in lower:
            flags.append(f"puffery:{word}")
    for hint in HARVEST_HINTS:
        if hint in lower:
            flags.append(f"forbidden:{hint}")
    if re.search(r"\bwe will send (this|it) on linkedin\b", lower):
        flags.append("forbidden:linkedin_send")
    return {
        "ok": not flags,
        "flags": flags,
        "judge": "reviewer",
    }


def _flatten(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_flatten(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten(v) for v in obj)
    return str(obj)
