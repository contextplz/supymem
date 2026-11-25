"""Platform capability catalogue (doc 12 §1)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).resolve().parents[4] / "config" / "platform_capabilities.yaml"


@lru_cache(maxsize=1)
def load_catalogue() -> dict[str, Any]:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}


def get(cap_id: str) -> dict[str, Any] | None:
    for row in load_catalogue().get("capabilities") or []:
        if row.get("id") == cap_id:
            return row
    return None


def by_maturity(*levels: str) -> list[dict[str, Any]]:
    want = set(levels)
    return [c for c in load_catalogue().get("capabilities") or [] if c.get("maturity") in want]
