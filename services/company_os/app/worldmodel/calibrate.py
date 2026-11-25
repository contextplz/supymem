"""Calibration with automatic authority reduction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalibrationRecord:
    quantity: str
    predicted: float
    actual: float
    error: float
    authority: float = 1.0


_RECORDS: list[CalibrationRecord] = []


def record_outcome(quantity: str, predicted: float, actual: float) -> CalibrationRecord:
    err = abs(predicted - actual)
    authority = max(0.1, 1.0 - err)
    rec = CalibrationRecord(quantity, predicted, actual, err, authority)
    _RECORDS.append(rec)
    return rec


def authority_for(quantity: str) -> float:
    recs = [r for r in _RECORDS if r.quantity == quantity]
    if not recs:
        return 1.0
    return sum(r.authority for r in recs) / len(recs)
