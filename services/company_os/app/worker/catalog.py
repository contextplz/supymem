"""Job-kind catalogue and WORKER_KINDS globs."""

from __future__ import annotations

import fnmatch
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[4]
_CATALOGUE_PATH = _REPO / "config" / "job_kinds.yaml"
_POLICY_PATH = _REPO / "config" / "trigger_policy.yaml"


@lru_cache(maxsize=1)
def load_catalogue() -> dict[str, Any]:
    return yaml.safe_load(_CATALOGUE_PATH.read_text()) or {}


@lru_cache(maxsize=1)
def load_trigger_policy() -> dict[str, Any]:
    return yaml.safe_load(_POLICY_PATH.read_text()) or {}


def all_kind_names() -> frozenset[str]:
    kinds = load_catalogue().get("kinds") or {}
    return frozenset(kinds)


def stage2_kinds() -> frozenset[str]:
    kinds = load_catalogue().get("kinds") or {}
    return frozenset(name for name, spec in kinds.items() if isinstance(spec, dict) and spec.get("stage2"))


def default_worker_globs() -> list[str]:
    from app.core.local_mode import is_local
    raw = "" if is_local() else os.environ.get("WORKER_KINDS", "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(load_catalogue().get("default_worker_kinds") or [])


def matches_globs(kind: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(kind, g) for g in globs)


def enabled_kinds() -> frozenset[str]:
    """Kinds this worker image will lease. Subset of the catalogue."""
    globs = default_worker_globs()
    return frozenset(k for k in all_kind_names() if matches_globs(k, globs) and k in stage2_kinds())


def tick_kinds(tick: str) -> list[str]:
    ticks = (load_trigger_policy().get("ticks") or {}).get(tick) or {}
    enabled = enabled_kinds()
    staged = list(ticks.get("stage2_kinds") or [])
    return [k for k in staged if k in enabled]
