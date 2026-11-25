"""ICP filter for GTM signals (config/gtm_icp.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[5]  # repo root
_DEFAULT = _ROOT / "config" / "gtm_icp.yaml"


@lru_cache(maxsize=4)
def load_icp(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT
    if not p.is_file():
        return {
            "scoring": {"min_icp_score": 0.6, "yc_admit": 1.0, "launch_hn": 0.9},
            "include": {"signal_kinds": ["yc_admit", "launch_hn", "ef_admit"]},
            "exclude": {"email_confidence_min": "risky"},
        }
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    return data


def score_signal(kind: str, *, icp: dict[str, Any] | None = None) -> float:
    cfg = icp or load_icp()
    scoring = cfg.get("scoring") or {}
    allowed = (cfg.get("include") or {}).get("signal_kinds") or []
    if allowed and kind not in allowed:
        return 0.0
    return float(scoring.get(kind, 0.0))


def passes_icp(kind: str, *, icp: dict[str, Any] | None = None) -> bool:
    cfg = icp or load_icp()
    scoring = cfg.get("scoring") or {}
    min_score = float(scoring.get("min_icp_score", 0.6))
    return score_signal(kind, icp=cfg) >= min_score


def email_confidence_allowed(confidence: str, *, icp: dict[str, Any] | None = None) -> bool:
    """Relationship track never accepts ``guess`` emails."""
    cfg = icp or load_icp()
    floor = ((cfg.get("exclude") or {}).get("email_confidence_min") or "risky").lower()
    order = {"none": 0, "guess": 1, "risky": 2, "safe": 3}
    return order.get(confidence, 0) >= order.get(floor, 2) and confidence != "guess"
