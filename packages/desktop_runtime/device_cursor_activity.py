"""Best-effort activity preview for claimed browser and coding workers.

This carries no input, target, task text, or completion claim. The visual lease
fences task supersession; the caller's live bridge predicate is not an account
generation/ABA proof. A founder pause parks the same lease without heartbeats;
resuming never replaces that lease or supersedes a newer task.
"""
from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable

from desktop_runtime import agent_cursor

_LABELS = {
    "browser": {
        "opening_browser": "Opening page",
        "observing": "Reading page",
        "deciding": "Waiting",
        "acting": "Working in browser",
    },
    "coding": {"running_coding": "Working on code"},
}
_RETIRE = frozenset({"attention_required", "foreground_handoff", "closing",
                     "stopped", "not_started", "reconciliation_required"})


class DeviceCursorActivity:
    def __init__(self, current: Callable[[], bool | None], *, heartbeat_seconds: float = 30.0):
        self._current = current
        self._heartbeat_seconds = heartbeat_seconds
        self._retired = threading.Event()
        self._lock = threading.Lock()
        self._context = contextvars.Context()
        self._execution = None
        self._source_thread = None
        self._label = None
        self._paused = False

    def _live(self) -> bool | None:
        try:
            if self._retired.is_set():
                return False
            current = self._current()
            return False if self._retired.is_set() else current
        except Exception:
            return False

    def _finish_locked(self):
        execution, self._execution = self._execution, None
        if execution is not None:
            try:
                self._context.run(execution.__exit__, None, None, None)
            except Exception:
                pass  # Visual failures cannot alter the executor's outcome.

    def close(self):
        self._retired.set()  # Racing writers must also finish before releasing.
        if self._lock.acquire(blocking=False):
            try:
                self._finish_locked()
            finally:
                self._lock.release()

    def _begin_locked(self):
        if self._execution is None:
            self._execution = agent_cursor.execution()
            self._context.run(self._execution.__enter__)

    def update(self, route: str, state: str):
        if state in _RETIRE:
            self.close()
            return
        paused = state == "needs_you" and route in _LABELS
        label = "Waiting" if paused else _LABELS.get(route, {}).get(state)
        if label is None or self._retired.is_set():
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if paused and self._execution is None:
                return
            self._label = label
            self._paused = paused
            if self._source_thread is None:
                self._source_thread = threading.current_thread()
                threading.Thread(target=self._heartbeat, name="supymem-cursor-activity",
                                 daemon=True).start()
            live = self._live()
            if live is None:
                return  # A busy local account lock is not retirement evidence.
            if not live:
                self._retired.set()
                return
            self._begin_locked()
            self._context.run(agent_cursor.emit, "read", label=label)
        except Exception:
            self._retired.set()
        finally:
            if self._retired.is_set():
                self._finish_locked()
            self._lock.release()

    def _heartbeat(self):
        while not self._retired.wait(self._heartbeat_seconds):
            if not self._lock.acquire(blocking=False):
                continue
            try:
                live = self._live()
                if (live is False or self._source_thread is None
                        or not self._source_thread.is_alive()):
                    self._retired.set()
                elif live is True and not self._paused and self._label is not None:
                    self._begin_locked()
                    self._context.run(agent_cursor.emit, "read", label=self._label)
            except Exception:
                self._retired.set()
            finally:
                if self._retired.is_set():
                    self._finish_locked()
                self._lock.release()
