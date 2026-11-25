"""Nonblocking per-venture admission for outreach delivery.

Keep this context open through the final provider receipt and persisted send
state. PostgreSQL commits a token fence and holds the production advisory lock
on a connection detached from the pool. SQLite uses a process-local fence.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import ExitStack, contextmanager
from uuid import uuid4

from sqlalchemy import text

from app.state import db

_ACTIVE: set[str] = set()
_GUARD = threading.Lock()


@contextmanager
def try_send_admission(venture_id: str):
    """Yield False before effects when this venture has an active sender."""
    if not isinstance(venture_id, str) or not venture_id:
        raise ValueError("outreach_send_venture_required")
    token = uuid4().hex
    with ExitStack() as ownership:
        with db()() as session:
            engine = session.get_bind()
            dialect = engine.dialect.name
            if dialect == "postgresql":
                lock = ownership.enter_context(engine.connect())
                lock.detach()
                key = int.from_bytes(hashlib.sha256(("outreach-send:" + venture_id).encode()).digest()[:8],
                                     "big", signed=True)
                admitted = bool(lock.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar())
                if admitted:
                    # The shared lock proves any previous fence is orphaned; draft states still forbid uncertain resends.
                    admitted = session.execute(text("INSERT INTO provider_admission_fences (scope,token) "
                        "VALUES (:scope,:token) ON CONFLICT (scope) DO UPDATE SET token=EXCLUDED.token, "
                        "acquired_at=CURRENT_TIMESTAMP RETURNING token"),
                        {"scope": venture_id, "token": token}).scalar() == token
                session.commit()
            elif dialect != "sqlite":
                raise RuntimeError("outreach_send_database_unsupported")
        if dialect == "postgresql":
            try:
                yield admitted
            finally:
                if admitted:
                    with db()() as session:
                        session.execute(text("DELETE FROM provider_admission_fences WHERE scope=:scope AND token=:token"),
                            {"scope": venture_id, "token": token})
                        session.commit()
            return
    with _GUARD:
        admitted = venture_id not in _ACTIVE
        if admitted:
            _ACTIVE.add(venture_id)
    try:
        yield admitted
    finally:
        if admitted:
            with _GUARD:
                _ACTIVE.discard(venture_id)
