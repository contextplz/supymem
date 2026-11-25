"""Collector for outbound email replies — fills from gtm_touches (doc 115)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.models import GtmTouch, Venture, Workspace
from app.signals.collectors.base import RawSignal
from app.state import db


class OutboundRepliesCollector:
    id = "outbound_replies"
    cadence_s = 600
    cost_per_run_usd = Decimal("0")

    def _workspace_id(self, venture_id: str) -> str | None:
        try:
            with db()() as session:
                ven = session.get(Venture, venture_id)
                if ven and ven.workspace_id:
                    return ven.workspace_id
                if session.get(Workspace, venture_id):
                    return venture_id
        except Exception:
            return None
        return None

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]:
        workspace_id = self._workspace_id(venture_id)
        if not workspace_id:
            return []
        try:
            with db()() as session:
                rows = session.scalars(
                    select(GtmTouch).where(
                        GtmTouch.direction == "in",
                        GtmTouch.sent_at.is_not(None),
                        GtmTouch.sent_at >= since,
                    )
                ).all()
                # Scope to workspace via contact join would be better; filter in Python
                from app.db.models import GtmContact

                contact_ids = {r.contact_id for r in rows}
                contacts = {
                    c.id: c
                    for c in session.scalars(
                        select(GtmContact).where(
                            GtmContact.id.in_(contact_ids or ["__none__"]),
                            GtmContact.workspace_id == workspace_id,
                        )
                    )
                }
        except Exception:
            return []
        out: list[RawSignal] = []
        for row in rows:
            if row.contact_id not in contacts:
                continue
            out.append(
                RawSignal(
                    id=row.id,
                    person_key=hashlib.sha256(row.contact_id.encode()).hexdigest(),
                    collector_id=self.id,
                    occurred_at=row.sent_at or row.created_at,
                    effect=1.0,
                    window=(row.classification or "reply"),
                    payload={
                        "contact_id": row.contact_id,
                        "classification": row.classification,
                        "body": (row.body_text or "")[:500],
                    },
                )
            )
        return out
