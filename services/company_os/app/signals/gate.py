"""Deterministic statistical-honesty gate (R28)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    person_key: str
    collector_id: str
    effect: float | None = None
    window: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateClaim:
    statement: str
    min_sample: int | None = None
    min_effect_size: float | None = None
    min_days_stable: int | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    have_n: int
    need_n: int
    effect: float | None
    confidence: float
    reason: str
    eta_hours: float | None = None


def _config(path: Path | None = None) -> dict[str, float | int]:
    default = {"min_sample": 30, "min_collectors": 2, "min_effect_size": 0.2, "min_days_stable": 2}
    config_path = path or Path(__file__).resolve().parents[4] / "config/gate.yaml"
    if config_path.exists():
        default.update(yaml.safe_load(config_path.read_text(encoding="utf-8")) or {})
    return default


def evaluate(claim: CandidateClaim, signals: Iterable[Signal], *, config_path: Path | None = None) -> GateResult:
    config = _config(config_path)
    rows = list(signals)
    distinct = {signal.person_key for signal in rows if signal.person_key}
    have_n = len(distinct)
    min_sample = claim.min_sample or int(config["min_sample"])
    if have_n < min_sample:
        return GateResult(False, have_n, min_sample, None, 0.0, f"{have_n} of {min_sample} distinct persons required")
    collectors = {signal.collector_id for signal in rows if signal.collector_id}
    min_collectors = int(config["min_collectors"])
    if len(collectors) < min_collectors:
        return GateResult(False, have_n, min_sample, None, 0.0, f"{min_collectors} independent collectors required")
    effects = [signal.effect for signal in rows if signal.effect is not None]
    effect = sum(effects) / len(effects) if effects else None
    min_effect = claim.min_effect_size if claim.min_effect_size is not None else float(config["min_effect_size"])
    if effect is None or effect < min_effect:
        return GateResult(False, have_n, min_sample, effect, 0.0, f"effect {effect or 0:.3f} below minimum {min_effect:.3f}")
    windows = {signal.window for signal in rows if signal.window}
    min_days = claim.min_days_stable or int(config["min_days_stable"])
    if len(windows) < min_days:
        return GateResult(False, have_n, min_sample, effect, 0.0, f"effect must hold across {min_days} observation windows")
    confidence = min(1.0, 0.5 + 0.25 * min(1.0, have_n / min_sample) + 0.25 * min(1.0, len(collectors) / min_collectors))
    return GateResult(True, have_n, min_sample, effect, confidence, "all statistical gate checks passed")
