"""Deterministic parent selection from the quality-diversity archive."""
from __future__ import annotations

from typing import Any

from app.evolution.archive import list_archive


def _allocation(k: int) -> tuple[int, int, int]:
    """Allocate slots by largest remainder for the 60/25/15 policy."""
    weights = (0.60, 0.25, 0.15)
    raw = [k * weight for weight in weights]
    counts = [int(value) for value in raw]
    for _, index in sorted(
        ((raw[index] - counts[index], index) for index in range(3)),
        reverse=True,
    )[: k - sum(counts)]:
        counts[index] += 1
    return tuple(counts)  # type: ignore[return-value]


def _take(entries: list[dict[str, Any]], n: int, *, key: str) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (entry.get(key) is None, -(entry.get(key) or 0), str(entry.get("id", ""))),
    )[:n]


def sample_parents(venture_id: str, k: int = 3, *, min_sample: int = 1) -> list[dict[str, Any]]:
    """Return elite, novel, then resurrected parents in 60/25/15 proportions.

    A resurrection candidate must be explicitly unblocked (or have no recorded
    blocker); regressed variants are never selected.
    """
    if k < 1:
        return []
    entries = [
        entry
        for entry in list_archive()
        if entry.get("venture_id") == venture_id and entry.get("outcome") != "regressed"
    ]
    elite_n, novel_n, resurrect_n = _allocation(k)
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    pools = (
        (
            _take(
                [
                    entry
                    for entry in entries
                    if entry.get("fitness_scalar") is not None
                    and int(entry.get("sample_n") or 0) >= min_sample
                ],
                elite_n,
                key="fitness_scalar",
            ),
            elite_n,
        ),
        (_take(entries, novel_n, key="novelty"), novel_n),
        (
            _take(
                [
                    entry
                    for entry in entries
                    if entry.get("outcome") in {"inconclusive", "never_promoted"}
                    and entry.get("blocker_cleared", True)
                ],
                resurrect_n,
                key="novelty",
            ),
            resurrect_n,
        ),
    )
    for candidates, wanted in pools:
        for entry in candidates:
            identifier = str(entry.get("id", entry.get("fingerprint")))
            if identifier not in selected_ids and len(selected) < k:
                selected.append(entry)
                selected_ids.add(identifier)
        if len(selected) >= k:
            break

    # Sparse archives should still return useful, non-regressed parents.
    for entry in sorted(entries, key=lambda entry: str(entry.get("id", ""))):
        identifier = str(entry.get("id", entry.get("fingerprint")))
        if len(selected) >= k:
            break
        if identifier not in selected_ids:
            selected.append(entry)
            selected_ids.add(identifier)
    return selected
