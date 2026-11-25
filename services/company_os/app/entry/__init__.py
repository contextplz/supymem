"""Front-door entry-mode routing (35 §5, 44)."""

from __future__ import annotations

# Do not bind `route` on this package — it would shadow `app.entry.route` the module.
from app.entry.route import MODES, EntryDecision
from app.entry.service import EntryError, enter

__all__ = ["MODES", "EntryDecision", "EntryError", "enter"]
