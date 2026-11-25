"""Live harness inboxes so founder chat can steer a run already in flight."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.inbox import Inbox

_LIVE: dict[str, Inbox] = {}


def register(venture_id: str, inbox: Inbox) -> None:
    if venture_id:
        _LIVE[venture_id] = inbox


def unregister(venture_id: str) -> None:
    _LIVE.pop(venture_id, None)


def current(venture_id: str) -> Inbox | None:
    return _LIVE.get(venture_id)


def is_live(venture_id: str) -> bool:
    return venture_id in _LIVE


def steer_live(venture_id: str, text: str) -> bool:
    """Push instruction into the live turn. Returns True if a run absorbed it."""
    inbox = _LIVE.get(venture_id)
    note = (text or "").strip()
    if inbox is None or not note:
        return False
    inbox.steer(note)
    return True
