"""Human Task planner — curated catalog first; max 5 open."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.ids import new_id

REPO = Path(__file__).resolve().parents[4]
CATALOG = REPO / "config" / "concierge_catalog"

_TASKS: list[dict[str, Any]] = []
MAX_OPEN = 5


def load_catalog_entry(name: str) -> dict[str, Any]:
    path = CATALOG / f"{name}.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def create_task(catalog_id: str, *, venture_id: str) -> dict[str, Any]:
    open_n = sum(1 for t in _TASKS if t.get("status") == "open" and t.get("venture_id") == venture_id)
    if open_n >= MAX_OPEN:
        raise RuntimeError("max 5 open human tasks")
    entry = load_catalog_entry(catalog_id)
    task = {
        "id": new_id("ht"),
        "venture_id": venture_id,
        "catalog_id": catalog_id,
        "title": entry.get("title"),
        "steps": entry.get("steps") or [],
        "status": "open",
        "verified_on": entry.get("verified_on"),
        "secret_fields": [],  # never store secrets here
    }
    _TASKS.append(task)
    return task


def list_open(venture_id: str) -> list[dict[str, Any]]:
    return [t for t in _TASKS if t["venture_id"] == venture_id and t["status"] == "open"]


def clear_tasks() -> None:
    _TASKS.clear()
