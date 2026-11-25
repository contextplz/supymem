"""Shared session + dialect-aware write serialisation for credit mutations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import begin_immediate, serialize_writes
from app.state import db
from core.clock import utcnow  # type: ignore[import-not-found]

__all__ = [
    "begin_immediate",
    "naive_utc",
    "rowcount",
    "serialize_writes",
    "session_factory",
]


def session_factory() -> sessionmaker[Session]:
    return db()  # type: ignore[no-untyped-call, no-any-return]


def rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def naive_utc(dt: datetime | None = None) -> datetime:
    """Credits store naive UTC; comparisons against ``utcnow`` must match.

    SQLite has no timezone. Postgres columns are ``TIMESTAMP`` without time
    zone (see the alembic DDL), so the same naive values round-trip on both.
    """
    value = dt if dt is not None else utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value
