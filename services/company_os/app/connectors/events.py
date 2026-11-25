"""The trust receipt: everything we touched in their systems (39 §2).

``connection_events`` is append-only in the database. Every row is written by
:func:`record`, and every read, write and denial goes through it — if a call can
reach a provider without a row landing here, the chokepoint is in the wrong
place. Real and fake adapters share this writer, so their event shapes cannot
drift apart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import ConnectionEvent
from app.state import db
from core.clock import utcnow
from core.ids import new_id

HEALTH_WINDOW = 20


def record(
    *,
    connection_id: str,
    workspace_id: str,
    direction: str,
    operation: str,
    actor: str,
    outcome: str,
    target: str | None = None,
    denied_reason: str | None = None,
    engagement_id: str | None = None,
    idempotency_key: str | None = None,
    external_id: str | None = None,
    bytes_in: int = 0,
    bytes_out: int = 0,
    simulated: bool = False,
) -> str:
    row_id = new_id("cev")
    factory = db()
    with factory() as session:
        session.add(
            ConnectionEvent(
                id=row_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
                at=utcnow(),
                direction=direction,
                operation=operation,
                target=target,
                actor=actor,
                engagement_id=engagement_id,
                idempotency_key=idempotency_key,
                external_id=external_id,
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                simulated=1 if simulated else 0,
                outcome=outcome,
                denied_reason=denied_reason,
            )
        )
        session.commit()
    return row_id


def to_dict(row: ConnectionEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "connection_id": row.connection_id,
        "at": row.at.isoformat() if row.at else "",
        "direction": row.direction,
        "operation": row.operation,
        "target": row.target,
        "actor": row.actor,
        "engagement_id": row.engagement_id,
        "external_id": row.external_id,
        "bytes_in": row.bytes_in,
        "bytes_out": row.bytes_out,
        "simulated": bool(row.simulated),
        "outcome": row.outcome,
        "denied_reason": row.denied_reason,
    }


def recent(
    connection_id: str, *, limit: int = 50, before: str | None = None
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        stmt = select(ConnectionEvent).where(
            ConnectionEvent.connection_id == connection_id
        )
        if before:
            stmt = stmt.where(ConnectionEvent.id < before)
        rows = session.scalars(
            stmt.order_by(ConnectionEvent.at.desc(), ConnectionEvent.id.desc()).limit(
                max(1, min(limit, 200))
            )
        ).all()
        return [to_dict(r) for r in rows]


def for_workspace(workspace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ConnectionEvent)
            .where(ConnectionEvent.workspace_id == workspace_id)
            .order_by(ConnectionEvent.at.desc(), ConnectionEvent.id.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return [to_dict(r) for r in rows]


def find_by_idempotency_key(
    connection_id: str, key: str
) -> dict[str, Any] | None:
    """A delivered write, if this exact write already happened (39 §6)."""
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ConnectionEvent)
            .where(
                ConnectionEvent.connection_id == connection_id,
                ConnectionEvent.idempotency_key == key,
                ConnectionEvent.outcome == "ok",
            )
            .order_by(ConnectionEvent.at.asc())
        ).first()
        return to_dict(row) if row else None


def delivery_seen(connection_id: str, delivery_id: str) -> bool:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ConnectionEvent).where(
                ConnectionEvent.connection_id == connection_id,
                ConnectionEvent.direction == "webhook",
                ConnectionEvent.external_id == delivery_id,
            )
        ).first()
        return row is not None


def health(connection_id: str) -> str:
    """Computed from the last 20 events (39 §6). Denials are governance, not health."""
    rows = recent(connection_id, limit=HEALTH_WINDOW)
    if not rows:
        return "unknown"
    errors = [r for r in rows if r["outcome"] == "error"]
    if not errors:
        return "ok"
    newest = [r["outcome"] for r in rows[:3]]
    if len(newest) == 3 and all(o == "error" for o in newest):
        return "down"
    return "degraded"
