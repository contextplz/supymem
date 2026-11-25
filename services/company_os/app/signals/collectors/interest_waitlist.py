"""Collector for first-look waitlist captures."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.models import InterestSignal, Venture, Workspace
from app.signals.collectors.base import RawSignal
from app.state import db


class InterestWaitlistCollector:
    id = "interest_waitlist"
    cadence_s = 3600
    cost_per_run_usd = Decimal("0")

    def _workspace_id(self, venture_id: str) -> str | None:
        """Resolve venture → workspace; never treat venture_id as workspace_id blindly."""
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
                    select(InterestSignal).where(
                        InterestSignal.workspace_id == workspace_id,
                        InterestSignal.created_at >= since,
                    )
                ).all()
        except Exception:
            return []
        return [
            RawSignal(
                id=row.id,
                person_key=hashlib.sha256((row.email or row.id).encode()).hexdigest(),
                collector_id=self.id,
                occurred_at=row.created_at,
                effect=1.0,
                window=row.created_at.date().isoformat(),
            )
            for row in rows
        ]
