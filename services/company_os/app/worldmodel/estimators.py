"""Layer-2 empirical estimators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Est:
    quantity: str
    lo: float
    point: float
    hi: float
    confidence: float
    method: str

    @classmethod
    def insufficient(cls, quantity: str) -> "Est":
        return cls(quantity, 0.0, 0.0, 0.0, 0.0, "insufficient_data")


def empirical_rate(quantity: str, state: Any) -> Est:
    metrics = getattr(state, "metrics", {}) or {}
    if quantity in metrics:
        v = float(metrics[quantity])
        return Est(quantity, v * 0.8, v, v * 1.2, 0.5, "empirical")
    return Est.insufficient(quantity)
