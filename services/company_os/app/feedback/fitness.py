"""Fitness registry access + soft-metric promotion refusal."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[4]
REG = REPO / "config" / "fitness_registry.yaml"


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Any]:
    with REG.open(encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("metrics") or {}


def resolve_metric(key: str) -> dict[str, Any]:
    reg = load_registry()
    if key not in reg:
        raise KeyError(f"unknown fitness metric: {key}")
    return reg[key]


def soft_only_promotion_allowed(metrics_used: list[str]) -> bool:
    """A soft metric may never be the sole basis for a promotion."""
    reg = load_registry()
    hard = [m for m in metrics_used if reg.get(m, {}).get("hardness") == "hard"]
    return len(hard) > 0


def assert_promotion_metrics(metrics_used: list[str]) -> None:
    if not soft_only_promotion_allowed(metrics_used):
        raise PermissionError("soft-metric-only promotion refused")
