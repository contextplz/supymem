"""Placeholder collector for landing-page events pending analytics storage."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.signals.collectors.base import RawSignal


class LandingEventsCollector:
    id = "landing_events"
    cadence_s = 900
    cost_per_run_usd = Decimal("0")

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]:
        """Landing event persistence is not available yet, so do not fabricate signals."""
        del venture_id, since
        return []
