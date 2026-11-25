"""In-memory quality-diversity archive for evolution proposals."""

from __future__ import annotations

from math import sqrt
from typing import Any

from core.canonical import digest

_ARCHIVE: list[dict[str, Any]] = []


def _descriptor_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    """A small deterministic distance for mixed categorical descriptors."""
    keys = sorted(set(left) | set(right))
    if not keys:
        return 0.0
    differing = sum(left.get(key) != right.get(key) for key in keys)
    return differing / sqrt(len(keys))


def _novelty(entry: dict[str, Any]) -> float:
    descriptors = entry.get("descriptors") or {}
    neighbours = [
        _descriptor_distance(descriptors, existing.get("descriptors") or {})
        for existing in _ARCHIVE
        if existing.get("venture_id") == entry.get("venture_id")
    ]
    return min(neighbours) if neighbours else 1.0


def archive(entry: dict[str, Any]) -> None:
    """Store a complete, restorable variant plus quality-diversity metadata."""
    entry = dict(entry)
    entry.setdefault("descriptors", {})
    entry.setdefault("fitness", {})
    entry.setdefault("fitness_scalar", None)
    entry.setdefault("sample_n", 0)
    entry.setdefault("generation", 0)
    entry.setdefault("parent_id", None)
    entry.setdefault("evidence", [])
    entry.setdefault("outcome", "never_promoted")
    entry.setdefault("novelty", _novelty(entry))
    entry["fingerprint"] = digest(
        {
            "hypothesis": entry.get("hypothesis"),
            "predicted_metric": entry.get("predicted_metric"),
            "tier": entry.get("tier"),
        }
    )
    _ARCHIVE.append(entry)


def has_similar_failure(hyp: dict[str, Any] | str) -> bool:
    fp = digest(
        {
            "hypothesis": str(hyp),
            "predicted_metric": "engagement_rate",
            "tier": "org" if "org" in str(hyp) else "config",
        }
    )
    return any(a.get("fingerprint") == fp and a.get("outcome") == "regressed" for a in _ARCHIVE)


def list_archive() -> list[dict[str, Any]]:
    return list(_ARCHIVE)


def clear_archive() -> None:
    _ARCHIVE.clear()
