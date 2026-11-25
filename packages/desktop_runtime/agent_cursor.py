"""Event feed for the Supymem agent cursor overlay (drawn by the Tauri app).

The computer-use engine calls ``emit`` BEFORE it performs an action (with the
screen-global point it is about to act on) and once more after it (``done`` or
``fail``). Delivery is one fire-and-forget Unix datagram to the Tauri app, so
this never blocks, never raises, and is a no-op when the app is not listening
(tests, source runs, cursor turned off).

    from desktop_runtime import agent_cursor
    agent_cursor.emit("begin")                         # task starts
    agent_cursor.emit("click", x, y, "Clicking Save")  # before the click
    agent_cursor.emit("done")                          # action succeeded
    agent_cursor.emit("end")                           # task finished

Coordinates are screen-global points, top-left origin of the primary display
(the same space as AX frames): window frame origin + element frame center.
"""

from __future__ import annotations

import json
import contextvars
import secrets
from contextlib import contextmanager
import math
import os
import re
import socket
import threading

SCHEMA_VERSION = 1
# Intent kinds carry a target; "read" may omit it (cursor stays put).
INTENTS = frozenset({"move", "click", "type", "scroll", "read"})
KINDS = INTENTS | {"begin", "done", "fail", "end"}
MAX_LABEL = 40
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SOCKET_ENV = "SUPYMEM_AGENT_CURSOR_SOCK"
_COORD_LIMIT = 100_000.0  # no real display setup is larger; rejects garbage

_sock: socket.socket | None = None
_socket_lock = threading.Lock()
_owner = contextvars.ContextVar("supymem_cursor_execution_owner", default=None)
_active_owner: str | None = None
_latest_owner: str | None = None
_published_owner: str | None = None
_listener_identity: tuple | None = None


@contextmanager
def execution():
    """One fresh visual lease; superseded executions cannot repaint the next task.

    This is overlay custody, never authorization for native input. The receiver
    must also implement owner admission for protection across runtime processes.
    """
    global _latest_owner
    owner = secrets.token_hex(16)
    lease = {"id": owner, "started": False, "retired": False}
    # Reserve before a best-effort begin can be dropped by writer backpressure.
    # Socket writes still serialize: an in-flight old hint precedes the new begin.
    _latest_owner = owner
    token = _owner.set(lease)
    try:
        emit("begin")
        yield owner
    finally:
        try:
            emit("end")
        finally:
            lease["retired"] = True
            _owner.reset(token)



def clean_label(label: object) -> str | None:
    if not isinstance(label, str):
        return None
    text = _CONTROL.sub(" ", label).strip()
    if len(text) > MAX_LABEL:
        text = text[: MAX_LABEL - 1].rstrip() + "…"
    return text or None


def build_event(kind: str, x: float | None = None, y: float | None = None,
                label: str | None = None) -> dict | None:
    """The wire event, or None when it is not valid (never raises)."""
    if kind not in KINDS:
        return None
    event: dict = {"v": SCHEMA_VERSION, "kind": kind}
    if x is not None or y is not None:
        if (isinstance(x, bool) or isinstance(y, bool)
                or not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                or not math.isfinite(x) or not math.isfinite(y)
                or abs(x) > _COORD_LIMIT or abs(y) > _COORD_LIMIT):
            return None
        event["x"], event["y"] = round(float(x), 1), round(float(y), 1)
    elif kind in {"move", "click", "type", "scroll"}:
        return None  # these need somewhere to act
    text = clean_label(label)
    if text:
        event["label"] = text
    return event


def emit(kind: str, x: float | None = None, y: float | None = None,
         label: str | None = None) -> bool:
    """Send one event; True if it was handed to the overlay."""
    global _sock, _active_owner, _published_owner, _listener_identity
    path = os.environ.get(SOCKET_ENV)
    event = build_event(kind, x, y, label)
    if event is None:
        return False
    # Visual hints must never wait behind another task or close its socket.
    if not _socket_lock.acquire(blocking=False):
        return False
    owned = None
    try:
        lease = _owner.get()
        owner = lease["id"] if lease is not None else None
        if owner is None:
            if _active_owner is not None:
                return False  # Unowned legacy hints cannot repaint an owned task.
        elif owner != _latest_owner or lease["retired"]:
            return False  # Superseded or ended contexts can never reacquire.
        elif kind == "begin" or (not lease["started"] and kind != "end"):
            if lease["started"] and _active_owner != owner:
                return False
            lease["started"] = True
            _active_owner = owner
        elif _active_owner != owner:
            return False
        if owner is not None:
            event["owner"] = owner
            if kind == "end":
                _active_owner = None
        if not path:
            _published_owner = None
            return False
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        owned = _sock
        owned.setblocking(False)
        if owner is not None:
            metadata = os.stat(path)
            listener = (path, metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)
            if _listener_identity != listener:
                _published_owner = None
                _listener_identity = listener
            if kind not in {"begin", "end"} and _published_owner != owner:
                owned.sendto(json.dumps({"v": SCHEMA_VERSION, "kind": "begin", "owner": owner},
                                        separators=(",", ":")).encode(), path)
                _published_owner = owner
        owned.sendto(json.dumps(event, separators=(",", ":")).encode(), path)
        if owner is not None:
            _published_owner = None if kind == "end" else owner
        return True
    except OSError:
        if owned is not None:
            try:
                owned.close()
            except OSError:
                pass
        _published_owner = None
        _listener_identity = None
        _sock = None  # overlay not up (or restarted): retry with a fresh socket next time
        return False
    finally:
        _socket_lock.release()


if __name__ == "__main__":  # python -m desktop_runtime.agent_cursor: move the cursor around, sends no input
    import time

    emit("begin")
    for kind, x, y, label in [("move", 300, 300, None), ("click", 700, 400, "Clicking Save"),
                              ("type", 700, 400, "Typing…"), ("scroll", 500, 650, None), ("read", None, None, None)]:
        emit(kind, x, y, label)
        time.sleep(2)
        emit("done")
    emit("end")
