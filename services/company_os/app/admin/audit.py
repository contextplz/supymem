"""Append-only, hash-chained admin audit log (doc 99 §6.6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.append_only import install_append_only_triggers
from app.db.models import AdminAuditLogEntry
from app.db.session import allocate_seq, serialize_writes
from app.state import db
from core.canonical import canonical_json, sha256_hex
from core.clock import as_utc, utcnow
from core.ids import new_id

GENESIS_HASH = "0" * 64
_MAX_APPEND_ATTEMPTS = 4
_TRIGGERS_READY = False
ADMIN_AUDIT_LOCK_KEY = "admin_audit_log"


def _created_stamp(dt: datetime) -> str:
    aware = as_utc(dt) or dt
    return aware.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AuditPage:
    items: list[AdminAuditLogEntry]
    next_cursor: int | None


def _session_factory() -> sessionmaker[Session]:
    return db()  # type: ignore[no-untyped-call,no-any-return]


def ensure_append_only_triggers() -> None:
    """Install dialect-aware triggers when create_all was used instead of alembic."""
    global _TRIGGERS_READY
    if _TRIGGERS_READY:
        return
    factory = _session_factory()
    bind = factory.kw.get("bind")
    if bind is None:
        session = factory()
        try:
            bind = session.get_bind()
        finally:
            session.close()
    install_append_only_triggers(bind)
    _TRIGGERS_READY = True


def _json(value: Any | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return canonical_json(value)


def _hash_fields(
    *,
    id: str,
    actor_admin_id: str | None,
    actor_email: str | None,
    action: str,
    target_kind: str | None,
    target_id: str | None,
    before_json: str | None,
    after_json: str | None,
    amount_micro: int | None,
    ip: str | None,
    user_agent: str | None,
    request_id: str | None,
    outcome: str,
    reason: str | None,
    created_at: str,
    prev_hash: str,
) -> str:
    body = canonical_json(
        {
            "action": action,
            "actor_admin_id": actor_admin_id,
            "actor_email": actor_email,
            "after_json": after_json,
            "amount_micro": amount_micro,
            "before_json": before_json,
            "created_at": created_at,
            "id": id,
            "ip": ip,
            "outcome": outcome,
            "reason": reason,
            "request_id": request_id,
            "target_id": target_id,
            "target_kind": target_kind,
            "user_agent": user_agent,
        }
    )
    return sha256_hex(prev_hash + body)


def record(
    *,
    actor_admin_id: str | None = None,
    actor_email: str | None = None,
    action: str,
    target_kind: str | None = None,
    target_id: str | None = None,
    before: Any | None = None,
    after: Any | None = None,
    amount_micro: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    outcome: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> AdminAuditLogEntry:
    if outcome not in {"allow", "deny", "error"}:
        raise ValueError("invalid_outcome")
    ensure_append_only_triggers()
    ts = now or utcnow()
    created = _created_stamp(ts)
    entry_id = new_id("aal")
    before_json = _json(before)
    after_json = _json(after)
    factory = _session_factory()
    # prev_hash is the global chain tip; serialize_writes on ADMIN_AUDIT_LOCK_KEY
    # serialises writers so two records cannot fork the chain. seq comes from
    # the database sequence on Postgres (gaps after rollback are expected).
    # Retry on IntegrityError is belt-and-suspenders: a dropped audit row is
    # worse than a slow one.
    for attempt in range(_MAX_APPEND_ATTEMPTS):
        try:
            return _append(
                session_factory=factory,
                entry_id=entry_id,
                actor_admin_id=actor_admin_id,
                actor_email=actor_email,
                action=action,
                target_kind=target_kind,
                target_id=target_id,
                before_json=before_json,
                after_json=after_json,
                amount_micro=amount_micro,
                ip=ip,
                user_agent=user_agent,
                request_id=request_id,
                outcome=outcome,
                reason=reason,
                ts=ts,
                created=created,
            )
        except IntegrityError:
            if attempt == _MAX_APPEND_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


def _append(
    *,
    session_factory: sessionmaker[Session],
    entry_id: str,
    actor_admin_id: str | None,
    actor_email: str | None,
    action: str,
    target_kind: str | None,
    target_id: str | None,
    before_json: str | None,
    after_json: str | None,
    amount_micro: int | None,
    ip: str | None,
    user_agent: str | None,
    request_id: str | None,
    outcome: str,
    reason: str | None,
    ts: datetime,
    created: str,
) -> AdminAuditLogEntry:
    with session_factory() as session:
        try:
            serialize_writes(session, key=ADMIN_AUDIT_LOCK_KEY)
            last = session.scalar(
                select(AdminAuditLogEntry)
                .order_by(AdminAuditLogEntry.seq.desc())
                .limit(1)
            )
            prev_hash = last.entry_hash if last is not None else GENESIS_HASH
            next_seq = allocate_seq(session, "admin_audit_log")
            entry_hash = _hash_fields(
                id=entry_id,
                actor_admin_id=actor_admin_id,
                actor_email=actor_email,
                action=action,
                target_kind=target_kind,
                target_id=target_id,
                before_json=before_json,
                after_json=after_json,
                amount_micro=amount_micro,
                ip=ip,
                user_agent=user_agent,
                request_id=request_id,
                outcome=outcome,
                reason=reason,
                created_at=created,
                prev_hash=prev_hash,
            )
            row = AdminAuditLogEntry(
                id=entry_id,
                seq=next_seq,
                actor_admin_id=actor_admin_id,
                actor_email=actor_email,
                action=action,
                target_kind=target_kind,
                target_id=target_id,
                before_json=before_json,
                after_json=after_json,
                amount_micro=amount_micro,
                ip=ip,
                user_agent=user_agent,
                request_id=request_id,
                outcome=outcome,
                reason=reason,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
                created_at=ts,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row
        except Exception:
            session.rollback()
            raise


def verify_chain() -> tuple[bool, str | None]:
    factory = _session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(AdminAuditLogEntry).order_by(AdminAuditLogEntry.seq)
            ).all()
        )
        prev = GENESIS_HASH
        last_seq: int | None = None
        for row in rows:
            if last_seq is not None and row.seq <= last_seq:
                return False, f"seq not strictly increasing at {row.id}"
            last_seq = row.seq
            if row.prev_hash != prev:
                return False, f"prev_hash mismatch at {row.id}"
            created = _created_stamp(row.created_at) if row.created_at else ""
            expected = _hash_fields(
                id=row.id,
                actor_admin_id=row.actor_admin_id,
                actor_email=row.actor_email,
                action=row.action,
                target_kind=row.target_kind,
                target_id=row.target_id,
                before_json=row.before_json,
                after_json=row.after_json,
                amount_micro=row.amount_micro,
                ip=row.ip,
                user_agent=row.user_agent,
                request_id=row.request_id,
                outcome=row.outcome,
                reason=row.reason,
                created_at=created,
                prev_hash=row.prev_hash,
            )
            if expected != row.entry_hash:
                return False, f"entry_hash mismatch at {row.id}"
            prev = row.entry_hash
        return True, None


def search(
    *,
    actor: str | None = None,
    action: str | None = None,
    target: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> AuditPage:
    limit = max(1, min(int(limit), 200))
    factory = _session_factory()
    with factory() as session:
        stmt = select(AdminAuditLogEntry).order_by(AdminAuditLogEntry.seq.desc())
        if actor:
            stmt = stmt.where(
                (AdminAuditLogEntry.actor_email == actor.strip().lower())
                | (AdminAuditLogEntry.actor_admin_id == actor)
            )
        if action:
            stmt = stmt.where(AdminAuditLogEntry.action == action)
        if target:
            stmt = stmt.where(
                (AdminAuditLogEntry.target_id == target)
                | (AdminAuditLogEntry.target_kind == target)
            )
        if outcome:
            stmt = stmt.where(AdminAuditLogEntry.outcome == outcome)
        if since is not None:
            stmt = stmt.where(AdminAuditLogEntry.created_at >= since)
        if until is not None:
            stmt = stmt.where(AdminAuditLogEntry.created_at <= until)
        if cursor is not None:
            stmt = stmt.where(AdminAuditLogEntry.seq < int(cursor))
        rows = list(session.scalars(stmt.limit(limit + 1)).all())
        next_cursor = None
        if len(rows) > limit:
            next_cursor = int(rows[limit - 1].seq)
            rows = rows[:limit]
        for row in rows:
            session.expunge(row)
        return AuditPage(items=rows, next_cursor=next_cursor)


def reset_trigger_flag() -> None:
    global _TRIGGERS_READY
    _TRIGGERS_READY = False
