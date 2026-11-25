"""Gateway subject → Person resolution (doc 99 §6.1)."""

from __future__ import annotations

from app.identity.resolve import identities_for_email, person_for_email, person_for_subject

__all__ = [
    "identities_for_email",
    "person_for_email",
    "person_for_subject",
]
