"""Read the exact current locked charter from canonical database rows."""

from __future__ import annotations

import json

from app.db.models import Charter, Venture


def current_locked_charter(session, venture_id: str, *, workspace_id: str | None):
    """Project canonical fields; never synthesize, import, or select a prior version.

    Callers must authorize/bind the source scope before invoking this reader.
    A corrupt or unlocked active pointer is missing, not a cache fallback.
    """
    venture = session.get(Venture, venture_id)
    if (venture is None or venture.workspace_id != workspace_id
            or not venture.active_charter_id):
        return None
    row = session.get(Charter, venture.active_charter_id)
    if row is None or row.venture_id != venture_id or row.locked != 1:
        return None
    try:
        content = json.loads(row.content_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(content, dict):
        return None
    return {
        "id": row.id,
        "venture_id": row.venture_id,
        "version": row.version,
        "parent_version": row.parent_version,
        "locked": row.locked,
        "content_json": row.content_json,
        "checksum": row.checksum,
        "slots_snapshot_json": row.slots_snapshot_json,
        "dissent_json": row.dissent_json,
        "created_at": row.created_at.isoformat(),
        "locked_at": row.locked_at.isoformat() if row.locked_at else None,
        "content": content,
    }
