"""Idea Lab — evidence-backed opportunity slates (37).

Layering: ``slate``, ``promote`` and ``steps`` own the ORM; ``corpus``,
``generate`` and ``filters`` are pure and never open a session.
"""

from __future__ import annotations

__all__ = ["corpus", "filters", "generate", "promote", "slate", "steps"]
