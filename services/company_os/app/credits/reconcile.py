"""Nightly / on-demand integrity check for a credit account (spec §4.5)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.credits._txn import session_factory
from app.credits.errors import CreditsUnavailable
from app.credits.ledger import verify_chain
from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation


@dataclass(frozen=True)
class InvariantCheck:
    name: str
    passed: bool
    expected: int | str | None = None
    actual: int | str | None = None
    drift: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    account_id: str
    ok: bool
    ledger_sum: InvariantCheck
    chain: InvariantCheck
    reservations: InvariantCheck


def reconcile(account_id: str, *, transaction: Session | None = None) -> ReconcileReport:
    if transaction is None:
        with session_factory()() as session:
            return reconcile(account_id, transaction=session)
    account = transaction.get(CreditAccount, account_id)
    if account is None:
        raise CreditsUnavailable(f"unknown credit account {account_id}")
    granted = account.granted_micro
    spent = account.spent_micro
    reserved = account.reserved_micro
    ledger_total = transaction.scalar(
        select(func.coalesce(func.sum(CreditLedgerEntry.amount_micro), 0)).where(
            CreditLedgerEntry.account_id == account_id
        )
    )
    open_holds = transaction.scalar(
        select(func.coalesce(func.sum(case(
            (CreditReservation.is_pool.is_(True), CreditReservation.remaining_micro),
            else_=CreditReservation.amount_micro,
        )), 0)).where(
            CreditReservation.account_id == account_id,
            CreditReservation.status.in_(("open", "active", "reconciling")),
        )
    )
    ledger_total_i = int(ledger_total or 0)
    open_holds_i = int(open_holds or 0)

    expected_net = granted - spent
    ledger_drift = ledger_total_i - expected_net
    ledger_check = InvariantCheck(
        name="ledger_sum",
        passed=ledger_total_i == expected_net,
        expected=expected_net,
        actual=ledger_total_i,
        drift=ledger_drift,
        detail=(
            None
            if ledger_total_i == expected_net
            else "sum(amount_micro) != granted_micro - spent_micro"
        ),
    )

    chain_ok, chain_detail = verify_chain(account_id, transaction=transaction)
    chain_check = InvariantCheck(
        name="chain",
        passed=chain_ok,
        expected=GENESIS if chain_ok else None,
        actual="valid" if chain_ok else "broken",
        drift=0 if chain_ok else 1,
        detail=chain_detail,
    )

    res_drift = open_holds_i - reserved
    res_check = InvariantCheck(
        name="reservations",
        passed=open_holds_i == reserved,
        expected=reserved,
        actual=open_holds_i,
        drift=res_drift,
        detail=(
            None
            if open_holds_i == reserved
            else "sum(outstanding reservations) != reserved_micro"
        ),
    )

    return ReconcileReport(
        account_id=account_id,
        ok=ledger_check.passed and chain_check.passed and res_check.passed,
        ledger_sum=ledger_check,
        chain=chain_check,
        reservations=res_check,
    )


GENESIS = "genesis"


def reconcile_all() -> list[ReconcileReport]:
    factory = session_factory()
    with factory() as session:
        ids = list(session.scalars(select(CreditAccount.id)).all())
    return [reconcile(account_id) for account_id in ids]
