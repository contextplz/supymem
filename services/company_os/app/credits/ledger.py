"""Append-only hash-chained credit ledger. Never UPDATE or DELETE a row."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, session_factory
from app.credits.errors import DuplicateLedgerEntry
from app.db.models import CreditAccount, CreditLedgerEntry
from app.db.session import allocate_seq
from core.ids import new_id  # type: ignore[import-not-found]

GENESIS_HASH = "0" * 64
_LEDGER_KINDS = frozenset(
    {"grant", "spend", "refund", "revoke", "release", "adjust", "allowance"}
)


def _stamp(dt: datetime) -> str:
    return naive_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _canonical_json(fields: dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_fields(
    *,
    entry_id: str,
    account_id: str,
    kind: str,
    amount_micro: int,
    available_after_micro: int,
    reason: str,
    actor_kind: str,
    actor_id: str | None,
    purpose: str | None,
    model: str | None,
    provider: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    reservation_id: str | None,
    request_id: str | None,
    idempotency_key: str,
    meta_json: str,
    prev_hash: str,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "account_id": account_id,
        "kind": kind,
        "amount_micro": amount_micro,
        "available_after_micro": available_after_micro,
        "reason": reason,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "purpose": purpose,
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reservation_id": reservation_id,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "meta_json": meta_json,
        "prev_hash": prev_hash,
        "created_at": _stamp(created_at),
    }


def compute_entry_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    payload = {k: v for k, v in fields.items() if k != "entry_hash"}
    canonical = _canonical_json(payload)
    return hashlib.sha256(f"{prev_hash}{canonical}".encode("utf-8")).hexdigest()


def _is_idempotency_collision(exc: IntegrityError) -> bool:
    message = str(exc.orig if exc.orig is not None else exc).lower()
    return "idempotency_key" in message


def append(
    session: Session,
    *,
    account: CreditAccount,
    kind: str,
    amount_micro: int,
    reason: str,
    actor_kind: str,
    actor_id: str | None,
    idempotency_key: str,
    purpose: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reservation_id: str | None = None,
    request_id: str | None = None,
    meta: dict[str, Any] | None = None,
    meta_json: str | None = None,
    expires_at: datetime | None = None,
) -> CreditLedgerEntry:
    """Write one ledger row inside the caller's transaction. Do not open a session.

    Raises ``DuplicateLedgerEntry`` on UNIQUE ``idempotency_key`` collision so
    the caller can treat a replay as success.
    """
    if kind not in _LEDGER_KINDS:
        raise ValueError(f"unknown ledger kind: {kind!r}")
    if type(amount_micro) is not int:
        raise TypeError("amount_micro must be int")

    now = naive_utc()
    entry_id = new_id("cle")
    payload_meta = meta_json if meta_json is not None else json.dumps(meta or {}, sort_keys=True)

    prev = session.scalars(
        select(CreditLedgerEntry)
        .where(CreditLedgerEntry.account_id == account.id)
        .order_by(CreditLedgerEntry.seq.desc())
        .limit(1)
    ).first()
    prev_hash = prev.entry_hash if prev is not None else GENESIS_HASH

    # ``seq`` is a globally unique total order. It is NOT an input to the entry
    # hash (see ``_hash_fields``). Callers must hold ``serialize_writes`` for
    # ``account.id`` so two writers cannot read the same chain tip.
    #
    # SQLite: ``max(seq)+1`` under BEGIN IMMEDIATE. create_all does not
    # AUTOINCREMENT this column (it is not the ORM primary key).
    #
    # PostgreSQL: ``nextval`` of the column's owned sequence
    # (``pg_get_serial_sequence``). ``nextval`` is non-transactional, so a
    # rolled-back append burns a value and ``seq`` may contain gaps. The
    # invariant is strictly increasing per account (and globally UNIQUE), not
    # contiguous. ``verify_chain`` walks ``prev_hash``, not ``seq - 1``.
    #
    # A ``seq`` UNIQUE collision is deliberately not classified as a duplicate
    # idempotency key, so it fails loudly rather than dropping a charge.
    seq = allocate_seq(session, "credit_ledger")

    available_after = (
        account.granted_micro - account.spent_micro - account.reserved_micro
    )
    fields = _hash_fields(
        entry_id=entry_id,
        account_id=account.id,
        kind=kind,
        amount_micro=amount_micro,
        available_after_micro=available_after,
        reason=reason,
        actor_kind=actor_kind,
        actor_id=actor_id,
        purpose=purpose,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reservation_id=reservation_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        meta_json=payload_meta,
        prev_hash=prev_hash,
        created_at=now,
    )
    entry_hash = compute_entry_hash(prev_hash, fields)

    entry = CreditLedgerEntry(
        id=entry_id,
        seq=seq,
        account_id=account.id,
        kind=kind,
        amount_micro=amount_micro,
        available_after_micro=available_after,
        reason=reason,
        actor_kind=actor_kind,
        actor_id=actor_id,
        purpose=purpose,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reservation_id=reservation_id,
        request_id=request_id,
        idempotency_key=idempotency_key,
        meta_json=payload_meta,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        created_at=now,
        expires_at=expires_at,
    )
    session.add(entry)
    try:
        session.flush()
    except IntegrityError as exc:
        if _is_idempotency_collision(exc):
            raise DuplicateLedgerEntry(idempotency_key) from exc
        raise
    if kind == "spend" and amount_micro < 0:
        # A referral bookkeeping failure must never block a real spend: isolate it in a savepoint.
        from app.consumer.referrals import qualify_spend
        try:
            with session.begin_nested():
                qualify_spend(session, account)
        except Exception:  # noqa: BLE001 - the reward sweep re-checks qualification later
            import logging
            logging.getLogger(__name__).warning("referral_qualify_failed", exc_info=True)
    return entry


def find_by_idempotency_key(
    key: str, session: Session | None = None
) -> CreditLedgerEntry | None:
    def _find(s: Session) -> CreditLedgerEntry | None:
        return s.scalars(
            select(CreditLedgerEntry).where(CreditLedgerEntry.idempotency_key == key)
        ).first()

    if session is not None:
        return _find(session)
    factory = session_factory()
    with factory() as owned:
        row = _find(owned)
        if row is not None:
            owned.expunge(row)
        return row


def verify_chain(account_id: str, *, transaction: Session | None = None) -> tuple[bool, str | None]:
    """Walk ``prev_hash``. ``seq`` may have gaps; it must only be monotonic."""
    if transaction is None:
        with session_factory()() as session:
            return verify_chain(account_id, transaction=session)
    entries = list(transaction.scalars(select(CreditLedgerEntry)
        .where(CreditLedgerEntry.account_id == account_id).order_by(CreditLedgerEntry.seq.asc())))
    expected_prev = GENESIS_HASH
    last_seq: int | None = None
    for entry in entries:
        if last_seq is not None and entry.seq <= last_seq:
            return (
                False,
                f"seq {entry.seq} is not strictly increasing (prev {last_seq})",
            )
        last_seq = entry.seq
        if entry.prev_hash != expected_prev:
            return (
                False,
                f"seq {entry.seq} prev_hash mismatch: "
                f"expected {expected_prev}, got {entry.prev_hash}",
            )
        fields = _hash_fields(
            entry_id=entry.id,
            account_id=entry.account_id,
            kind=entry.kind,
            amount_micro=entry.amount_micro,
            available_after_micro=entry.available_after_micro,
            reason=entry.reason,
            actor_kind=entry.actor_kind,
            actor_id=entry.actor_id,
            purpose=entry.purpose,
            model=entry.model,
            provider=entry.provider,
            prompt_tokens=entry.prompt_tokens,
            completion_tokens=entry.completion_tokens,
            reservation_id=entry.reservation_id,
            request_id=entry.request_id,
            idempotency_key=entry.idempotency_key,
            meta_json=entry.meta_json,
            prev_hash=entry.prev_hash,
            created_at=entry.created_at,
        )
        recomputed = compute_entry_hash(entry.prev_hash, fields)
        if entry.entry_hash != recomputed:
            return (
                False,
                f"seq {entry.seq} entry_hash mismatch: "
                f"stored {entry.entry_hash}, recomputed {recomputed}",
            )
        expected_prev = entry.entry_hash
    return True, None


def _page(
    session: Session,
    stmt: Any,
    *,
    limit: int,
    cursor: str | None,
) -> dict[str, object]:
    limit = min(max(int(limit), 1), 200)
    # Cursor is a seq value, not a row offset. Gaps in seq skip no rows:
    # ``seq < cursor`` still returns every earlier entry.
    query = stmt.order_by(CreditLedgerEntry.seq.desc())
    if cursor:
        query = query.where(CreditLedgerEntry.seq < int(cursor))
    rows = list(session.scalars(query.limit(limit + 1)).all())
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = str(rows[-1].seq)
    for row in rows:
        session.expunge(row)
    return {"items": rows, "next_cursor": next_cursor}


def entries_for_account(
    account_id: str, *, limit: int = 50, cursor: str | None = None
) -> dict[str, object]:
    factory = session_factory()
    with factory() as session:
        stmt = select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id == account_id
        )
        return _page(session, stmt, limit=limit, cursor=cursor)


def search(
    *,
    account_id: str | None = None,
    kind: str | None = None,
    actor_id: str | None = None,
    actor_kind: str | None = None,
    purpose: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    factory = session_factory()
    with factory() as session:
        stmt = select(CreditLedgerEntry)
        if account_id:
            stmt = stmt.where(CreditLedgerEntry.account_id == account_id)
        if kind:
            stmt = stmt.where(CreditLedgerEntry.kind == kind)
        if actor_id:
            stmt = stmt.where(CreditLedgerEntry.actor_id == actor_id)
        if actor_kind:
            stmt = stmt.where(CreditLedgerEntry.actor_kind == actor_kind)
        if purpose:
            stmt = stmt.where(CreditLedgerEntry.purpose == purpose)
        if since is not None:
            stmt = stmt.where(CreditLedgerEntry.created_at >= naive_utc(since))
        if until is not None:
            stmt = stmt.where(CreditLedgerEntry.created_at <= naive_utc(until))
        return _page(session, stmt, limit=limit, cursor=cursor)
