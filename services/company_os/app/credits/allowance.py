"""Monthly allowance bucket. Consume order: allowance → purchased → grants."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.credits.ledger import append
from app.credits.units import credits_to_micro
from app.db.models import CreditAccount, CreditLedgerEntry


def grant_allowance(
    *,
    account_id: str,
    credits: int,
    period_end: datetime,
    actor_id: str = "system",
    idempotency_key: str,
) -> CreditLedgerEntry:
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=account_id)
        account = session.get(CreditAccount, account_id)
        if account is None:
            raise ValueError(f"unknown account {account_id}")
        micro = credits_to_micro(credits)
        account.granted_micro += micro
        account.updated_at = naive_utc()
        entry = append(
            session,
            account=account,
            kind="allowance",
            amount_micro=micro,
            reason="plan_allowance",
            actor_kind="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            expires_at=naive_utc(period_end),
            meta={"bucket": "allowance"},
        )
        session.commit()
        session.refresh(entry)
        return entry


def buckets(account_id: str, *, now: datetime | None = None) -> dict[str, int]:
    """Remaining µc by consume-order bucket."""
    cutoff = naive_utc(now) if now is not None else naive_utc()
    factory = session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(CreditLedgerEntry)
                .where(CreditLedgerEntry.account_id == account_id)
                .order_by(CreditLedgerEntry.seq.asc())
            )
        )
    allowances, purchased, grants = _bucket_balances(rows)
    allowance = sum(allowances[row.id] for row in rows if row.kind == "allowance"
                    and (row.expires_at is None or naive_utc(row.expires_at) > cutoff))
    return {"allowance": allowance, "purchased": purchased, "grants": grants}


def _bucket_balances(rows):
    allowances = {}
    purchased = 0
    grants = 0
    for row in rows:
        meta = {}
        try:
            meta = json.loads(row.meta_json or "{}")
        except json.JSONDecodeError:
            pass
        bucket = meta.get("bucket")
        if row.kind == "allowance":
            allowances[row.id] = row.amount_micro
        elif row.kind == "grant" and bucket == "purchased":
            purchased += row.amount_micro
        elif row.kind == "grant":
            grants += row.amount_micro
        elif row.kind == "spend":
            left = max(0, -row.amount_micro)
            for entry_id, remaining in allowances.items():
                take = min(remaining, left)
                allowances[entry_id] -= take
                left -= take
                if left == 0:
                    break
            take = min(purchased, left)
            purchased -= take
            left -= take
            grants = max(0, grants - left)
        elif row.kind in {"revoke", "adjust"} and bucket == "allowance_expire":
            entry_id = meta.get("allowance_id") or row.idempotency_key.removeprefix("allowance-expire:")
            if entry_id in allowances:
                allowances[entry_id] = max(0, allowances[entry_id] + row.amount_micro)
    return allowances, purchased, grants


def expire_allowances(*, now: datetime | None = None) -> int:
    cutoff = naive_utc(now) if now is not None else naive_utc()
    factory = session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.kind == "allowance",
                    CreditLedgerEntry.expires_at.is_not(None),
                    CreditLedgerEntry.expires_at <= cutoff,
                )
            )
        )
    n = 0
    for row in rows:
        factory = session_factory()
        with factory() as session:
            try:
                serialize_writes(session, key=row.account_id)
                account = session.get(CreditAccount, row.account_id)
                if account is None:
                    continue
                entries = list(session.scalars(select(CreditLedgerEntry)
                    .where(CreditLedgerEntry.account_id == row.account_id)
                    .order_by(CreditLedgerEntry.seq.asc())))
                remaining = _bucket_balances(entries)[0].get(row.id, 0)
                take = min(remaining, account.granted_micro - account.spent_micro - account.reserved_micro)
                if take <= 0:
                    session.commit()
                    continue
                account.granted_micro -= take
                account.updated_at = cutoff
                append(
                    session,
                    account=account,
                    kind="revoke",
                    amount_micro=-take,
                    reason="allowance_expired",
                    actor_kind="system",
                    actor_id="credits.sweep",
                    idempotency_key=f"allowance-expire:{row.id}:{entries[-1].seq}",
                    meta={"bucket": "allowance_expire", "allowance_id": row.id},
                )
                session.commit()
                n += 1
            except Exception:
                session.rollback()
    return n


def sleep_unpaid_apps(*, now: datetime | None = None) -> int:
    """24h grace → App sleeping. No-op until live Apps exist."""
    _ = now or datetime.utcnow() + timedelta(0)
    return 0
