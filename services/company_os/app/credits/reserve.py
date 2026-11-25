"""Atomic reserve → settle → release. The WHERE clause is the race defence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, rowcount, serialize_writes, session_factory
from app.credits.errors import (
    AccountSuspended,
    CreditError,
    CreditsUnavailable,
    DuplicateLedgerEntry,
    InsufficientCredits,
)
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.policy import reservation_ttl_seconds
from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation
from core.ids import new_id  # type: ignore[import-not-found]

log = logging.getLogger("app.credits.reserve")

# Spec §4.2 — predicate lives in the WHERE clause. Never read-then-write.
_RESERVE_SQL = text(
    """
    UPDATE credit_accounts
       SET reserved_micro = reserved_micro + :amount,
           version        = version + 1,
           updated_at     = :now
     WHERE id     = :account_id
       AND status = 'active'
       AND granted_micro - spent_micro - reserved_micro >= :amount
    """
)

# Spec §4.3 — convert the hold into spend and release the remainder.
_SETTLE_SQL = text(
    """
    UPDATE credit_accounts
       SET reserved_micro = reserved_micro - :hold,
           spent_micro    = spent_micro + :actual,
           version        = version + 1,
           updated_at     = :now
     WHERE id = :account_id
       AND reserved_micro >= :hold
       AND spent_micro + :actual + (reserved_micro - :hold) <= granted_micro
    """
)

_RELEASE_SQL = text(
    """
    UPDATE credit_accounts
       SET reserved_micro = reserved_micro - :hold,
           version        = version + 1,
           updated_at     = :now
     WHERE id = :account_id
       AND reserved_micro >= :hold
    """
)

_CLOSED = frozenset({"settled", "released", "expired"})
_HOLD = frozenset({"open", "active"})


def _is_hold(status: str) -> bool:
    return status in _HOLD


def protect_outcome(hold: Hold) -> None:
    """Keep an ordinary hold until a provider outcome is explicitly reconciled.

    Call only after persisting the operation receipt and before external I/O.
    Account serialization makes this transition exclusive with expiry/settlement.
    Build pools use their separate invocation receipt protocol.
    """
    from app.credits.pools import pool_for_reservation
    if pool_for_reservation(hold.reservation_id):
        raise CreditError("build_usage_requires_reconciliation")
    with session_factory()() as session:
        serialize_writes(session, key=hold.account_id)
        row = session.get(CreditReservation, hold.reservation_id)
        if row is None or row.account_id != hold.account_id or row.amount_micro != hold.amount_micro:
            raise CreditError("reservation_scope_mismatch")
        if row.status == "reconciling":
            return
        if not _is_hold(row.status) or naive_utc(row.expires_at) <= naive_utc():
            raise CreditError("reservation_no_longer_available")
        row.status = "reconciling"
        session.commit()


@dataclass(frozen=True)
class Hold:
    reservation_id: str
    account_id: str
    amount_micro: int


@dataclass(frozen=True)
class SettleResult:
    reservation_id: str
    account_id: str
    hold_micro: int
    actual_micro: int
    remainder_micro: int
    clamped: bool
    ledger_id: str


def _available(account: CreditAccount) -> int:
    return account.granted_micro - account.spent_micro - account.reserved_micro


def _raise_reserve_failure(
    session: Session, account_id: str, amount_micro: int
) -> NoReturn:
    account = session.get(CreditAccount, account_id)
    if account is None:
        raise CreditsUnavailable(f"unknown credit account {account_id}")
    if account.status != "active":
        raise AccountSuspended(account_id, account.status)
    raise InsufficientCredits(account_id, _available(account), amount_micro)


def reserve(
    *,
    account_id: str,
    amount_micro: int,
    purpose: str,
    model: str | None = None,
    provider: str | None = None,
    workspace_id: str | None = None,
    request_id: str | None = None,
    parent_id: str | None = None,
    venture_id: str | None = None,
    task_invocation: str | None = None,
) -> Hold:
    if type(amount_micro) is not int or amount_micro < 1:
        raise ValueError("amount_micro must be a positive int")
    if parent_id:
        from app.credits.pools import pool_for_reservation
        if pool_for_reservation(parent_id):
            raise CreditError("build_pool_requires_invocation_allocation")
    factory = session_factory()
    with factory() as session:
        try:
            serialize_writes(session, key=account_id)
            now = naive_utc()
            updated = session.execute(
                _RESERVE_SQL,
                {"amount": amount_micro, "now": now, "account_id": account_id},
            )
            if rowcount(updated) != 1:
                _raise_reserve_failure(session, account_id, amount_micro)
            from app.tenancy import current_or_none
            scope = current_or_none()
            attribution_venture = venture_id or (scope.venture_id if scope and scope.workspace_id == workspace_id else None)
            ttl = reservation_ttl_seconds()
            reservation_id = new_id("crv")
            session.add(
                CreditReservation(
                    id=reservation_id,
                    account_id=account_id,
                    amount_micro=amount_micro,
                    settled_micro=None,
                    status="active",
                    parent_id=parent_id,
                    purpose=purpose,
                    model=model,
                    provider=provider,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    venture_id=attribution_venture,
                    task_invocation=task_invocation,
                    clamped=0,
                    expires_at=now + timedelta(seconds=ttl),
                    created_at=now,
                    settled_at=None,
                )
            )
            session.commit()
            return Hold(
                reservation_id=reservation_id,
                account_id=account_id,
                amount_micro=amount_micro,
            )
        except (InsufficientCredits, AccountSuspended, CreditsUnavailable):
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise


def _result_from_settled(
    reservation: CreditReservation, ledger: CreditLedgerEntry | None
) -> SettleResult:
    actual = int(reservation.settled_micro or 0)
    return SettleResult(
        reservation_id=reservation.id,
        account_id=reservation.account_id,
        hold_micro=reservation.amount_micro,
        actual_micro=actual,
        remainder_micro=max(0, reservation.amount_micro - actual),
        clamped=bool(reservation.clamped),
        ledger_id=ledger.id if ledger is not None else "",
    )


def settle(
    hold: Hold,
    *,
    actual_micro: int,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cached_tokens: int = 0,
    provider_actual_micro: int | None = None,
) -> SettleResult:
    if type(actual_micro) is not int or actual_micro < 0:
        raise ValueError("actual_micro must be a non-negative int")
    raw_actual = actual_micro if provider_actual_micro is None else provider_actual_micro
    if type(raw_actual) is not int or raw_actual < actual_micro:
        raise ValueError("provider_actual_micro must be an int at least actual_micro")
    from app.credits.pools import pool_for_reservation, settle_legacy_entry
    if pool_for_reservation(hold.reservation_id):
        return settle_legacy_entry(hold, actual_micro=actual_micro, prompt_tokens=prompt_tokens,
                                   completion_tokens=completion_tokens)
    key = f"settle:{hold.reservation_id}"
    factory = session_factory()
    with factory() as session:
        try:
            serialize_writes(session, key=hold.account_id)
            reservation = session.get(CreditReservation, hold.reservation_id)
            if reservation is None:
                raise CreditError(f"unknown reservation {hold.reservation_id}")
            if reservation.account_id != hold.account_id or reservation.amount_micro != hold.amount_micro:
                raise CreditError("reservation_scope_mismatch")
            if reservation.status == "settled":
                ledger = find_by_idempotency_key(key, session)
                result = _result_from_settled(reservation, ledger)
                session.expunge_all()
                return result
            if reservation.status in _CLOSED:
                raise CreditError(
                    f"reservation {reservation.id} is {reservation.status}"
                )

            hold_micro = reservation.amount_micro
            account = session.get(CreditAccount, reservation.account_id)
            if account is None:
                raise CreditsUnavailable("credit_account_unavailable")
            # Charge confirmed overage, with a 5% tolerance capped at one credit.
            # Other concurrent holds remain protected by the atomic UPDATE.
            tolerance = min(10000, max(1, hold_micro // 20))
            available_after_release = account.granted_micro - account.spent_micro - account.reserved_micro + hold_micro
            charged = min(actual_micro, hold_micro + tolerance, available_after_release)
            clamped = charged != actual_micro
            now = naive_utc()
            updated = session.execute(
                _SETTLE_SQL,
                {
                    "hold": hold_micro,
                    "actual": charged,
                    "now": now,
                    "account_id": reservation.account_id,
                },
            )
            if rowcount(updated) != 1:
                charged = hold_micro
                clamped = True
                retry = session.execute(
                    _SETTLE_SQL,
                    {
                        "hold": hold_micro,
                        "actual": charged,
                        "now": now,
                        "account_id": reservation.account_id,
                    },
                )
                if rowcount(retry) != 1:
                    raise CreditError(
                        f"settle failed for reservation {reservation.id}"
                    )
                log.warning(
                    "credits.overage_clamped account=%s reservation=%s "
                    "requested=%s clamped_to=%s",
                    reservation.account_id,
                    reservation.id,
                    actual_micro,
                    charged,
                )

            account = session.get(CreditAccount, reservation.account_id)
            if account is None:
                raise CreditsUnavailable(
                    f"unknown credit account {reservation.account_id}"
                )
            session.refresh(account)
            entry = append(
                session,
                account=account,
                kind="spend",
                amount_micro=-charged,
                reason="settled reservation",
                actor_kind="system",
                actor_id="system",
                idempotency_key=key,
                purpose=reservation.purpose,
                model=reservation.model,
                provider=reservation.provider,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reservation_id=reservation.id,
                request_id=reservation.request_id,
                meta={"clamped": clamped, "hold_micro": hold_micro, "provider_actual_micro": actual_micro,
                      "raw_provider_actual_micro": raw_actual,
                      "venture_id": reservation.venture_id, "workspace_id": reservation.workspace_id,
                      "task_invocation": reservation.task_invocation, "cached_tokens": cached_tokens},
            )
            reservation.status = "settled"
            reservation.settled_micro = charged
            reservation.clamped = 1 if clamped else 0
            reservation.settled_at = now
            session.commit()
            try:
                from app.email.triggers import on_credit_balance
                on_credit_balance(hold.account_id)
            except Exception:  # email failure must never change a settled credit receipt
                pass
            return SettleResult(
                reservation_id=reservation.id,
                account_id=reservation.account_id,
                hold_micro=hold_micro,
                actual_micro=charged,
                remainder_micro=max(0, hold_micro - charged),
                clamped=clamped,
                ledger_id=entry.id,
            )
        except DuplicateLedgerEntry:
            session.rollback()
            prior = find_by_idempotency_key(key)
            factory2 = session_factory()
            with factory2() as retry_session:
                reservation = retry_session.get(CreditReservation, hold.reservation_id)
                if reservation is None:
                    raise
                result = _result_from_settled(reservation, prior)
                retry_session.expunge_all()
                return result
        except Exception:
            session.rollback()
            raise


def _close_open_hold(session: Session, reservation: CreditReservation, new_status: str,
                     *, definitive: bool = False) -> bool:
    if not _is_hold(reservation.status) and not (definitive and reservation.status == "reconciling"):
        return False
    now = naive_utc()
    updated = session.execute(
        _RELEASE_SQL,
        {
            "hold": reservation.amount_micro,
            "now": now,
            "account_id": reservation.account_id,
        },
    )
    if rowcount(updated) != 1:
        log.warning(
            "release rowcount=0 for reservation %s status=%s reserved may already be clear",
            reservation.id,
            reservation.status,
        )
    reservation.status = new_status
    reservation.settled_at = now
    return True


def release(hold: Hold, *, definitive: bool = False) -> None:
    """Idempotent on ordinary status; pooled usage needs an explicit receipt."""
    from app.credits.pools import pool_for_reservation
    if pool_for_reservation(hold.reservation_id):
        raise CreditError("build_usage_requires_reconciliation")
    factory = session_factory()
    with factory() as session:
        try:
            serialize_writes(session, key=hold.account_id)
            reservation = session.get(CreditReservation, hold.reservation_id)
            if reservation is not None and (reservation.account_id != hold.account_id or reservation.amount_micro != hold.amount_micro):
                raise CreditError("reservation_scope_mismatch")
            if reservation is not None and reservation.status == "reconciling" and not definitive:
                raise CreditError("provider_outcome_requires_reconciliation")
            if reservation is None or (not _is_hold(reservation.status) and reservation.status != "reconciling"):
                session.commit()
                return
            _close_open_hold(session, reservation, "released", definitive=definitive)
            session.commit()
        except Exception:
            session.rollback()
            raise


def sweep_expired(now: datetime | None = None) -> int:
    """Release every active hold past ``expires_at``. Also expires allowances."""
    cutoff = naive_utc(now) if now is not None else naive_utc()
    from app.credits.pools import expire_pools
    expire_pools(cutoff)
    from app.builder.reconcile import reconcile_held_builds
    reconcile_held_builds()
    factory = session_factory()
    with factory() as session:
        pools = select(CreditReservation.id).where(CreditReservation.is_pool.is_(True))
        ids = list(
            session.scalars(
                select(CreditReservation.id).where(
                    CreditReservation.status.in_(tuple(_HOLD)),
                    CreditReservation.is_pool.is_(False),
                    (CreditReservation.parent_id.is_(None) | CreditReservation.parent_id.not_in(pools)),
                    CreditReservation.expires_at <= cutoff,
                )
            ).all()
        )
    count = 0
    for reservation_id in ids:
        with factory() as peek:
            reservation = peek.get(CreditReservation, reservation_id)
            if reservation is None or not _is_hold(reservation.status):
                continue
            if naive_utc(reservation.expires_at) > cutoff:
                continue
            account_id = reservation.account_id
        with factory() as session:
            try:
                serialize_writes(session, key=account_id)
                reservation = session.get(CreditReservation, reservation_id)
                if reservation is None or not _is_hold(reservation.status):
                    session.commit()
                    continue
                if naive_utc(reservation.expires_at) > cutoff:
                    session.commit()
                    continue
                if _close_open_hold(session, reservation, "expired"):
                    count += 1
                session.commit()
            except Exception:
                session.rollback()
                log.exception("sweep failed for reservation %s", reservation_id)
    try:
        from app.credits.allowance import expire_allowances

        expire_allowances(now=cutoff)
    except Exception:
        log.exception("allowance expiry failed")
    return count


def reserve_child(
    *,
    parent_id: str,
    amount_micro: int,
    purpose: str,
    model: str | None = None,
    provider: str | None = None,
) -> Hold:
    """Hold a slice of a parent reservation (plan → build run)."""
    if type(amount_micro) is not int or amount_micro < 1:
        raise ValueError("amount_micro must be a positive int")
    factory = session_factory()
    with factory() as session:
        parent = session.get(CreditReservation, parent_id)
        if parent is not None and parent.is_pool:
            raise CreditError("build_pool_requires_invocation_allocation")
        if parent is None or not _is_hold(parent.status):
            raise CreditsUnavailable(f"parent reservation {parent_id} is not active")
        children = list(
            session.scalars(
                select(CreditReservation).where(CreditReservation.parent_id == parent_id)
            )
        )
        used = sum(c.amount_micro for c in children if _is_hold(c.status) or c.status == "settled")
        if used + amount_micro > parent.amount_micro:
            raise InsufficientCredits(parent.account_id, parent.amount_micro - used, amount_micro)
    return reserve(
        account_id=parent.account_id,
        amount_micro=amount_micro,
        purpose=purpose,
        model=model,
        provider=provider,
        workspace_id=parent.workspace_id,
        parent_id=parent_id,
    )
