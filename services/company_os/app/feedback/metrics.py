"""Metric rollups from feedback/events."""

from __future__ import annotations

from typing import Any


def rollup(events: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "n": float(len(events)),
        "injection_rate": (
            sum(1 for e in events if e.get("injection_attempt")) / max(1, len(events))
        ),
    }
