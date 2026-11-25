"""Clarify rounds — round 2 only under conditions, capped at 4."""
from __future__ import annotations

def needs_round2(answers: dict, contradictions: list) -> bool:
    return bool(contradictions) and len(contradictions) >= 1

def round2_budget() -> int:
    return 4
