"""Server time for prompts, with an optional founder timezone binding."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.clock import as_utc, utcnow

_founder_timezone: ContextVar[ZoneInfo | None] = ContextVar("founder_timezone", default=None)


def _zone(timezone_name: str | None) -> ZoneInfo | None:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        return None
    try:
        return ZoneInfo(timezone_name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


@contextmanager
def timezone_context(timezone_name: str | None) -> Iterator[None]:
    """Bind the founder's IANA zone for this request or worker execution."""
    token = _founder_timezone.set(_zone(timezone_name))
    try:
        yield
    finally:
        _founder_timezone.reset(token)


def time_context(timezone_name: str | None = None) -> str:
    """Render the injectable clock; missing or invalid zones use unconfirmed UTC."""
    zone = _founder_timezone.get() if timezone_name is None else _zone(timezone_name)
    now = as_utc(utcnow()).astimezone(zone or timezone.utc)
    founder_zone = zone.key if zone else "unconfirmed, using UTC"
    return (
        f"Now (server clock): {now.isoformat(timespec='seconds')} "
        f"({now.strftime('%A')}, {now.tzname()}); founder timezone: {founder_zone}."
    )
