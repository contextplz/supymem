"""Candidate selection and compatibility names for the real durable workflows."""
from __future__ import annotations

from typing import Any



def generate_candidates(payload: dict[str, Any]):
    from app.core.jobs import FatalError
    raise FatalError("candidate_generation_requires_clarifier_job")


def run_validate(payload: dict[str, Any]):
    from app.idea.validation import run_validation
    return run_validation(payload)


def decide_validation(session_id: str, run_id: str, decision: str, *, note=None, patch=None):
    from app.idea.validation import decide
    return decide(session_id, run_id, decision, note=note, patch=patch)
