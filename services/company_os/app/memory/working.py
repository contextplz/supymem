"""Tier-1 working memory — durable, dict-compatible, <10ms (doc 72 §1 / §9).

Replaces the process-local `MEM` dict while keeping the same get/set/pop API so
call sites do not change in the mechanical swap (S1.5).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator, MutableMapping

from app.config import data_root
from app.memory.sqlite_store import MemoryDatabase


def _default_db_path() -> Path:
    override = os.environ.get("COMPANY_OS_WORKING_MEMORY_PATH")
    if override:
        return Path(override)
    return data_root() / "working_memory.sqlite3"


class WorkingMemory(MutableMapping[str, Any]):
    """SQLite-backed key/value store with an in-process cache for hot reads."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _default_db_path()
        self._lock = threading.RLock()
        self._db = MemoryDatabase(self.path)
        with self._db.connect(initialize=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS working (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            rows = conn.execute("SELECT key, value FROM working").fetchall()
            self._cache = {k: json.loads(v) for k, v in rows}

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._cache[key]

    def __setitem__(self, key: str, value: Any) -> None:
        payload = json.dumps(value, default=str)
        now = time.time()
        with self._lock:
            with self._db.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO working(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, payload, now),
                )
            self._cache[key] = value

    def update_value(self, key: str, transform, default=None):
        """Apply a local transform to the latest durable value atomically."""
        with self._lock:
            with self._db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM working WHERE key = ?", (key,)).fetchone()
                value = transform(json.loads(row[0]) if row else default)
                payload = json.dumps(value, allow_nan=False)
                conn.execute(
                    "INSERT INTO working(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, payload, time.time()),
                )
            self._cache[key] = json.loads(payload)
            return json.loads(payload)

    def __delitem__(self, key: str) -> None:
        with self._lock:
            if key not in self._cache:
                raise KeyError(key)
            with self._db.connect() as conn:
                conn.execute("DELETE FROM working WHERE key = ?", (key,))
            del self._cache[key]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._cache.keys()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        with self._lock:
            return self._cache.get(key, default)

    def pop(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        with self._lock:
            if key not in self._cache:
                return default
            value = self._cache[key]
            with self._db.connect() as conn:
                conn.execute("DELETE FROM working WHERE key = ?", (key,))
            del self._cache[key]
            return value

    def clear(self) -> None:  # type: ignore[override]
        with self._lock:
            with self._db.connect() as conn:
                conn.execute("DELETE FROM working")
            self._cache.clear()

    def close(self) -> None:
        with self._lock:
            self._db.close()


# Process-wide singleton used as MEM after the swap.
_WORKING: WorkingMemory | None = None
_WORKING_LOCK = threading.Lock()
_DICT_UPDATE_LOCK = threading.RLock()


def update_working_value(store, key: str, transform, default=None):
    """One read/transform/write, including dict-based local test fixtures.

    Callbacks are local and provider-free. Shared hosted stores own their DB
    transaction; this compatibility fallback does not claim cross-process locks.
    """
    method = getattr(store, "update_value", None)
    if callable(method):
        return method(key, transform, default)
    with _DICT_UPDATE_LOCK:
        value = transform(store.get(key, default))
        store[key] = value
        return value


def merge_working_dict(store, key: str, changes: dict):
    return update_working_value(store, key, lambda current: {**(current or {}), **changes}, {})


def get_working_memory() -> WorkingMemory:
    global _WORKING
    with _WORKING_LOCK:
        if _WORKING is None:
            from app.db.session import resolve_database_url
            url = resolve_database_url() or ""
            if url.startswith(("postgres:", "postgresql:", "postgresql+")):
                from app.memory.pg_working import PostgresWorkingMemory
                _WORKING = PostgresWorkingMemory()
            else:
                _WORKING = WorkingMemory()
        return _WORKING


def reset_working_memory_for_tests(path: Path | None = None) -> WorkingMemory:
    """Reopen the test store without invalidating imported singleton aliases."""
    global _WORKING
    with _WORKING_LOCK:
        if _WORKING is not None:
            try:
                _WORKING.close()
            except Exception:
                pass
        if isinstance(_WORKING, WorkingMemory):
            _WORKING.__init__(path=path)
        else:
            _WORKING = WorkingMemory(path=path)
        return _WORKING
