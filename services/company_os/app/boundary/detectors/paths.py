"""Layer 1 — path rules (43 §5.1). Cheapest possible refusal: no read needed.

A path rule blocks the whole segment rather than redacting inside it. There is
no safe partial view of `.env` or of a customer export.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from app.boundary import DataClass, Finding, Segment, policy

DETECTOR = "paths"

# Per-workspace additions: gitignored paths and the workspace denylist (43 §5.1).
# Registered rather than read from disk so the rule set is deterministic in a
# test and identical across tenancy modes.
_extra_denylist: tuple[str, ...] = ()


def set_workspace_denylist(patterns: tuple[str, ...] | list[str]) -> None:
    global _extra_denylist
    _extra_denylist = tuple(patterns)


def workspace_denylist() -> tuple[str, ...]:
    return _extra_denylist


def _rules() -> dict[str, list[str]]:
    return {str(k): list(v or []) for k, v in (policy().get("paths") or {}).items()}


def _matches(path: str, pattern: str) -> bool:
    posix = PurePosixPath(path.replace("\\", "/"))
    candidates = [posix.as_posix(), posix.name]
    lowered = pattern.lower()
    return any(fnmatch.fnmatch(c.lower(), lowered) for c in candidates)


def classify_path(path: str | None) -> tuple[DataClass, str] | None:
    """Return the class a path rule forces, with the rule that forced it."""
    if not path:
        return None
    for pattern in _extra_denylist:
        if _matches(path, pattern):
            return DataClass.UNKNOWN, f"denylist:{pattern}"
    for name, patterns in _rules().items():
        for pattern in patterns:
            if _matches(path, pattern):
                return DataClass(name), f"path:{pattern}"
    return None


def scan(segment: Segment) -> list[Finding]:
    hit = classify_path(segment.path)
    if hit is None:
        return []
    data_class, rule = hit
    return [
        Finding(
            detector=DETECTOR,
            rule=rule,
            data_class=data_class,
            confidence=1.0,
            label=segment.display(),
        )
    ]
