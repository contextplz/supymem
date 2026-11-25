"""Load `config/guardrails.yaml` and resolve GUARDRAILS_MODE."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

Mode = Literal["deterministic_only", "full"]
Action = Literal["block", "flag", "redact", "off"]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO_ROOT / "config" / "guardrails.yaml"


def resolve_mode() -> Mode:
    raw = (os.getenv("GUARDRAILS_MODE") or "deterministic_only").strip().lower()
    if raw in {"full", "classifiers"}:
        return "full"
    return "deterministic_only"


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        return {"surfaces": {}}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def clear_cache() -> None:
    load_config.cache_clear()


def action_for(surface: str, check: str, *, path: str | None = None) -> Action:
    data = load_config(path)
    surfaces = data.get("surfaces") or {}
    spec = surfaces.get(surface) or surfaces.get("agents") or {}
    raw = str(spec.get(check) or "flag").lower()
    if raw not in {"block", "flag", "redact", "off"}:
        return "flag"
    return raw  # type: ignore[return-value]
