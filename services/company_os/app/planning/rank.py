"""Stable ranking for next-up tasks."""

from __future__ import annotations

from typing import Any


def rank_tasks(tasks: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable order: keep input order (already dependency-sorted), assign rank."""
    out = []
    for i, t in enumerate(tasks):
        item = dict(t)
        item["rank"] = i
        if not item.get("why_now"):
            raise ValueError("every task states why it is next")
        out.append(item)
    return out
