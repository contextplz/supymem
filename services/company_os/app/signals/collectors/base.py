"""Shared interface and normalized shape for signal collectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.signals.gate import Signal


@dataclass(frozen=True, slots=True)
class RawSignal:
    id: str
    person_key: str
    collector_id: str
    occurred_at: datetime
    effect: float | None = None
    window: str | None = None
    payload: dict[str, object] | None = None

    def normalized(self) -> Signal:
        return Signal(self.id, self.person_key, self.collector_id, self.effect, self.window)


class Collector(Protocol):
    id: str
    cadence_s: int
    cost_per_run_usd: Decimal

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]: ...
