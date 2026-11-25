"""External blocker registry lookup."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.feasibility.model import RequirementVerdict

_CONFIG = Path(__file__).resolve().parents[4] / "config" / "blockers.yaml"


@lru_cache
def load_blockers() -> list[dict[str, Any]]:
    data = yaml.safe_load(_CONFIG.read_text()) or {}
    return [row for row in data.get("blockers", []) if isinstance(row, dict)]


def match_blocker(label: str) -> dict[str, Any] | None:
    lowered = label.lower()
    for blocker in load_blockers():
        if any(str(term).lower() in lowered for term in blocker.get("matches", [])):
            return blocker
    return None


def blocker_verdict(blocker: dict[str, Any]) -> RequirementVerdict:
    return RequirementVerdict(str(blocker["verdict"]))
