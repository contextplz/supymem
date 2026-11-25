"""Autopilot mode and policy-derived pause conditions (doc 79 §6)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class AutopilotMode(StrEnum):
    OFF = "off"
    ASSISTED = "assisted"
    ON = "on"
    FULL = "full"


class AutopilotPolicy(BaseModel):
    mode: AutopilotMode = AutopilotMode.ASSISTED
    pause_on: list[str] = Field(default_factory=list)
    max_items_in_flight: int = 3
    resume: str = "requires_founder_ack"


_DEFAULT_PAUSES = [
    "dispute_created",
    "error_rate_above: 0.05",
    "founder_rejection_count: 2",
    "unattributed_revenue_above_usd: 5",
    "warden_drift_detected",
]


def load_autopilot_policy(path: Path | None = None) -> AutopilotPolicy:
    """Read policy baseline when PyYAML is installed, otherwise retain safe defaults."""
    path = path or Path(__file__).resolve().parents[4] / "config" / "policy.baseline.yaml"
    try:
        import yaml  # type: ignore[import-not-found]

        body = yaml.safe_load(path.read_text()) or {}
        autopilot = body.get("autopilot") or {}
        caps = body.get("caps") or {}
        return AutopilotPolicy(
            mode=autopilot.get("default", "assisted"),
            pause_on=autopilot.get("pause_on", _DEFAULT_PAUSES),
            max_items_in_flight=caps.get("max_items_in_flight", 3),
            resume=autopilot.get("resume", "requires_founder_ack"),
        )
    except (ImportError, OSError, ValueError):
        return AutopilotPolicy(pause_on=_DEFAULT_PAUSES)
