"""Grant and revoke value movements. Both counters stay monotonic."""

from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any

from sqlalchemy import text

from app.credits._txn import naive_utc, rowcount, serialize_writes, session_factory
from app.credits.errors import CreditsUnavailable, DuplicateLedgerEntry
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.units import validate_amount_micro
from app.db.models import CreditAccount, CreditLedgerEntry

_GRANT_SQL = text(
    """
    UPDATE credit_accounts
       SET granted_micro = granted_micro + :amount,
           version        = version + 1,
           updated_at     = :now
     WHERE id = :account_id
    """
)

_REVOKE_SQL = text(
    """
    UPDATE credit_accounts
       SET spent_micro = spent_micro + :amount,
           version     = version + 1,
           updated_at  = :now
     WHERE id = :account_id
       AND granted_micro - spent_micro - reserved_micro >= :amount
    """
)


def _original_or_raise(key: str) -> CreditLedgerEntry:
    found = find_by_idempotency_key(key)
    if found is None:
        raise DuplicateLedgerEntry(key)
    return found


def grant(
    *,
    account_id: str,
    amount_micro: int,
    reason: str,
    actor_kind: str,
    actor_id: str | None,
    idempotency_key: str,
    meta: dict[str, Any] | None = None,
    transaction: Any | None = None,
) -> CreditLedgerEntry:
    """Increment ``granted_micro`` and append a ``grant`` row. Replay is a no-op."""
    amount_micro = validate_amount_micro(amount_micro)
    factory = session_factory()
    owned = transaction is None
    with (factory() if owned else nullcontext(transaction)) as session:
        try:
            if owned:
                serialize_writes(session, key=account_id)
            prior = find_by_idempotency_key(idempotency_key, session=session)
            if prior is not None:
                return prior
            now = naive_utc()
            updated = session.execute(
                _GRANT_SQL,
                {"amount": amount_micro, "now": now, "account_id": account_id},
            )
            if rowcount(updated) != 1:
                raise CreditsUnavailable(f"unknown credit account {account_id}")
            account = session.get(CreditAccount, account_id)
            if account is None:
                raise CreditsUnavailable(f"unknown credit account {account_id}")
            session.refresh(account)
            entry = append(
                session,
                account=account,
                kind="grant",
                amount_micro=amount_micro,
                reason=reason,
                actor_kind=actor_kind,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                purpose="grant",
                meta=meta,
            )
            if owned:
                session.commit()
                session.refresh(entry)
                session.expunge(entry)
            return entry
        except DuplicateLedgerEntry:
            if not owned:
                raise
            session.rollback()
            return _original_or_raise(idempotency_key)
        except Exception:
            if not owned:
                raise
            session.rollback()
            raise


def revoke(
    *,
    account_id: str,
    amount_micro: int,
    reason: str,
    actor_id: str,
    idempotency_key: str,
    actor_kind: str = "admin",
    meta: dict[str, Any] | None = None,
    transaction: Any | None = None,
) -> CreditLedgerEntry:
    """Claw back credits by incrementing ``spent_micro``. Floors at available.

    Spec §4.1: revocation does not decrement ``granted_micro``. Shortfall is
    recorded as ``write_off`` in ``meta_json``.
    """
    amount_micro = validate_amount_micro(amount_micro)
    factory = session_factory()
    owned = transaction is None
    with (factory() if owned else nullcontext(transaction)) as session:
        try:
            if owned:
                serialize_writes(session, key=account_id)
            prior = find_by_idempotency_key(idempotency_key, session=session)
            if prior is not None:
                return prior
            account = session.get(CreditAccount, account_id)
            if account is None:
                raise CreditsUnavailable(f"unknown credit account {account_id}")
            available = (
                account.granted_micro - account.spent_micro - account.reserved_micro
            )
            applied = min(amount_micro, available)
            write_off = amount_micro - applied
            now = naive_utc()
            if applied > 0:
                updated = session.execute(
                    _REVOKE_SQL,
                    {"amount": applied, "now": now, "account_id": account_id},
                )
                if rowcount(updated) != 1:
                    applied = 0
                    write_off = amount_micro
                session.refresh(account)
            payload = dict(meta or {})
            payload["write_off"] = write_off
            payload["requested_micro"] = amount_micro
            entry = append(
                session,
                account=account,
                kind="revoke",
                amount_micro=-applied,
                reason=reason,
                actor_kind=actor_kind,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                purpose="grant",
                meta_json=json.dumps(payload, sort_keys=True),
            )
            if owned:
                session.commit()
                session.refresh(entry)
                session.expunge(entry)
            return entry
        except DuplicateLedgerEntry:
            if not owned:
                raise
            session.rollback()
            return _original_or_raise(idempotency_key)
        except Exception:
            if not owned:
                raise
            session.rollback()
            raise
