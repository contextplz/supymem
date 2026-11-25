"""Injectable UTC clock — never call ``datetime.now()`` / ``utcnow()`` elsewhere."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

_Clock = Callable[[], datetime]
_clock: _Clock | None = None


def utcnow() -> datetime:
    """Timezone-aware UTC now. Prefer this over ``datetime.utcnow()``."""
    if _clock is not None:
        return _clock()
    return datetime.now(timezone.utc)


def set_clock(fn: _Clock | None) -> None:
    """Override the clock (tests). Pass ``None`` to restore system time."""
    global _clock
    _clock = fn


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime.

    SQLite hands back naive datetimes for columns we wrote as aware, so any
    comparison against :func:`utcnow` raises unless both sides are normalised.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
