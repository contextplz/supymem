"""OT-47: 90-day paste PII null job, per workspace.

``gtm_signals`` is append-only, so paste bodies live on ``gtm_paste_bodies``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from sqlalchemy import select

from app.db.models import GtmPasteBody
from app.state import db


def null_stale_paste_pii(
    workspace_id: str,
    *,
    now: datetime | None = None,
    retention_days: int = 90,
) -> dict[str, Any]:
    moment = now or utcnow()
    cutoff = moment - timedelta(days=retention_days)
    nulled = 0
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmPasteBody).where(GtmPasteBody.workspace_id == workspace_id)
            )
        )
        for row in rows:
            if not row.raw_text:
                continue
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=moment.tzinfo)
            if created > cutoff:
                continue
            row.raw_text_sha256 = hashlib.sha256(row.raw_text.encode("utf-8")).hexdigest()
            row.raw_text = None
            row.nulled_at = moment
            nulled += 1
        session.commit()
    return {"workspace_id": workspace_id, "nulled": nulled, "retention_days": retention_days}
