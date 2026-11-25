"""Venture world-model state snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorldState:
    venture_id: str
    metrics: dict[str, float] = field(default_factory=dict)
    budget_remaining_usd: float = 100.0
    capacity: dict[str, float] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


def snapshot(venture_id: str, metrics: dict[str, float] | None = None) -> WorldState:
    return WorldState(venture_id=venture_id, metrics=dict(metrics or {}))
