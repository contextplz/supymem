"""Deterministic write keys (39 §6).

A replayed engagement must not open a second pull request in someone's
repository, so the key is derived from the request rather than generated, and
the resulting external id is stored beside it in ``connection_events``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(payload: dict[str, Any] | None) -> str:
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def key_for(
    *,
    engagement_id: str | None,
    operation: str,
    target: str | None,
    content_hash: str,
) -> str:
    raw = "|".join([engagement_id or "-", operation, target or "-", content_hash])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"idem_{digest}"
