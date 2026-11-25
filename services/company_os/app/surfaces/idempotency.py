"""Idempotency-Key handling (41 §4).

Clients time out and retry; without this, one retry is one extra engagement.
Keys are held 24 h with their response, so the same key returns the first
answer and creates nothing new.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from app.db.models import IdempotencyRecord
from app.state import db
from core.clock import as_utc, utcnow

RETENTION = timedelta(hours=24)
ENTRY_VOICE_NOTE_SCOPE_PREFIX = "entry-voice-note:"
# Reserved while the first request is still in flight, so two concurrent
# retries cannot both proceed.
IN_FLIGHT = "__in_flight__"


@dataclass(frozen=True)
class Replay:
    found: bool
    status_code: int = 200
    body: dict[str, Any] | None = None
    in_flight: bool = False


def _scoped(scope: str, key: str) -> str:
    return f"{scope}:{key}"


def replay_or_reserve(scope: str, key: str) -> Replay:
    """Return a stored response, or reserve the key for this caller."""
    purge_expired()
    stored = _scoped(scope, key)
    factory = db()
    with factory() as session:
        row = session.get(IdempotencyRecord, stored)
        if row is not None:
            if row.response_json == IN_FLIGHT:
                return Replay(found=False, in_flight=True)
            try:
                body = json.loads(row.response_json)
            except json.JSONDecodeError:
                body = None
            return Replay(found=True, status_code=row.status_code, body=body)
        session.add(
            IdempotencyRecord(
                key=stored,
                scope=scope,
                response_json=IN_FLIGHT,
                status_code=0,
                created_at=utcnow(),
            )
        )
        session.commit()
    return Replay(found=False)


def store_response(
    scope: str, key: str, body: dict[str, Any], *, status_code: int = 200
) -> None:
    stored = _scoped(scope, key)
    factory = db()
    with factory() as session:
        row = session.get(IdempotencyRecord, stored)
        payload = json.dumps(body, default=str)
        if row is None:
            session.add(
                IdempotencyRecord(
                    key=stored,
                    scope=scope,
                    response_json=payload,
                    status_code=status_code,
                    created_at=utcnow(),
                )
            )
        else:
            row.response_json = payload
            row.status_code = status_code
        session.commit()


def release(scope: str, key: str) -> None:
    """Drop a reservation when the request failed, so a retry can proceed."""
    stored = _scoped(scope, key)
    factory = db()
    with factory() as session:
        row = session.get(IdempotencyRecord, stored)
        if row is not None and row.response_json == IN_FLIGHT:
            session.delete(row)
            session.commit()


def purge_expired(now: Any = None) -> int:
    moment = now or utcnow()
    cutoff = moment - RETENTION
    factory = db()
    with factory() as session:
        rows = session.scalars(select(IdempotencyRecord)).all()
        stale = [
            r.key
            for r in rows
            if not r.scope.startswith(ENTRY_VOICE_NOTE_SCOPE_PREFIX)
            and (as_utc(r.created_at) or moment) < cutoff
        ]
        if stale:
            session.execute(
                delete(IdempotencyRecord).where(IdempotencyRecord.key.in_(stale))
            )
            session.commit()
        return len(stale)
