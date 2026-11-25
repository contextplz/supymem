"""Load agent catalog + fitness registry."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
CATALOG = REPO_ROOT / "config" / "agent_catalog.yaml"
FITNESS = REPO_ROOT / "config" / "fitness_registry.yaml"


@lru_cache(maxsize=2)
def load_catalog(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CATALOG
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=2)
def load_fitness_registry(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else FITNESS
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("metrics") or {}


def fitness_metric_exists(key: str) -> bool:
    return key in load_fitness_registry()


def clear_caches() -> None:
    load_catalog.cache_clear()
    load_fitness_registry.cache_clear()
