"""Guarded adjacent-idea reframing."""
from __future__ import annotations

from typing import Any


def eligible_reframe(
    original: dict[str, Any], candidate: dict[str, Any] | None, *, confidence_delta: float | None = None
) -> dict[str, Any] | None:
    """Return one evidence-superior adjacent reframe, otherwise no suggestion."""
    if not candidate:
        return None
    original_confidence = float(original.get("confidence") or 0)
    candidate_confidence = float(candidate.get("confidence") or 0)
    delta = confidence_delta if confidence_delta is not None else candidate_confidence - original_confidence
    original_audience = set(original.get("audience") or original.get("audiences") or [])
    candidate_audience = set(candidate.get("audience") or candidate.get("audiences") or [])
    original_jobs = set(original.get("jobs") or original.get("job_to_be_done") or [])
    candidate_jobs = set(candidate.get("jobs") or candidate.get("job_to_be_done") or [])
    if delta <= 0.15 or not (original_audience & candidate_audience) or not (original_jobs & candidate_jobs):
        return None
    return {**candidate, "confidence_delta": round(delta, 3)}
