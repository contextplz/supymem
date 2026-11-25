"""Gates that run between a generated draft and a live post.

Nothing publishes unless every gate passes. With no human in the loop these
are the review, so they are deterministic and fail closed.
"""

from app.content.safety.cadence import CadenceVerdict, check_cadence, next_slot
from app.content.safety.linter import LintResult, lint
from app.content.safety.originality import is_duplicate, simhash

__all__ = [
    "CadenceVerdict",
    "LintResult",
    "check_cadence",
    "is_duplicate",
    "lint",
    "next_slot",
    "simhash",
]
