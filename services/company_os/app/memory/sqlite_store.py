"""One SQLite checkout at a time per memory file, with no idle connections."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from weakref import WeakValueDictionary

from app.db.session import _apply_sqlite_pragmas

_TIMEOUT_S = 5
_BUSY = "Saved memory is busy. Wait for the other operation to finish, then try again."
_SLOTS: WeakValueDictionary[Path, threading.Lock] = WeakValueDictionary()
_SLOTS_LOCK = threading.Lock()


class MemoryDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        with _SLOTS_LOCK:
            slot = _SLOTS.get(self.path)
            if slot is None:
                slot = _SLOTS[self.path] = threading.Lock()
            self._slot = slot

    @contextmanager
    def connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        if not self._slot.acquire(timeout=_TIMEOUT_S):
            raise sqlite3.OperationalError(_BUSY)
        try:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            with closing(sqlite3.connect(self.path, timeout=_TIMEOUT_S)) as conn, conn:
                conn.row_factory = sqlite3.Row
                if initialize:
                    _apply_sqlite_pragmas(conn)
                    conn.execute(f"PRAGMA busy_timeout={_TIMEOUT_S * 1000}")
                else:
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA synchronous=FULL")
                yield conn
        except sqlite3.OperationalError as error:
            if (getattr(error, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise sqlite3.OperationalError(_BUSY) from error
            raise
        finally:
            self._slot.release()

    def close(self) -> None:
        self._closed = True
