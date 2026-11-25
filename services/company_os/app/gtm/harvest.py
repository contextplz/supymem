"""Harvest channel metrics into feedback events."""

from __future__ import annotations

from typing import Any

from app.feedback.ingest import ingest


def harvest_comments(comments: list[str], *, venture_id: str) -> list[dict[str, Any]]:
    out = []
    for c in comments:
        r = ingest(c, source="public", venture_id=venture_id)
        out.append({"id": r.id, "trust": r.trust, "injection": r.security_logged})
    return out
