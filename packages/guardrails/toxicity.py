"""Toxicity — skipped in deterministic_only. ONNX toxic-bert only when mode=full."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToxicityHit:
    score: float
    skipped: bool


def detect_toxicity(text: str) -> ToxicityHit:
    from guardrails.classifiers import score_toxicity
    from guardrails.config import resolve_mode

    if resolve_mode() != "full":
        return ToxicityHit(score=0.0, skipped=True)
    score = score_toxicity(text)
    if score is None:
        return ToxicityHit(score=0.0, skipped=True)
    return ToxicityHit(score=score, skipped=False)
