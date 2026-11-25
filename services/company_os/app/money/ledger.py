"""Durable double-entry postings for venture money."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.db.models import MoneyLedgerEntry
from app.state import db
from core.clock import utcnow
from core.ids import new_id


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: str
    venture_id: str
    debit_account: str
    credit_account: str
    amount_minor: int
    currency: str
    ref: str | None
    meta: dict[str, Any]
    source: str


def _as_entry(row: MoneyLedgerEntry) -> LedgerEntry:
    return LedgerEntry(
        id=row.id,
        venture_id=row.venture_id,
        debit_account=row.debit_account,
        credit_account=row.credit_account,
        amount_minor=row.amount_minor,
        currency=row.currency,
        ref=row.ref,
        meta=json.loads(row.meta_json),
        source=row.source,
    )


def record_entry(
    venture_id: str,
    debit_account: str,
    credit_account: str,
    amount_minor: int,
    currency: str,
    ref: str | None = None,
    meta: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Post one balanced entry, idempotently when a provider reference is supplied."""
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    if not debit_account or not credit_account or debit_account == credit_account:
        raise ValueError("debit and credit accounts must be distinct and non-empty")
    if not currency or len(currency) != 3:
        raise ValueError("currency must be a three-letter ISO code")

    metadata = dict(meta or {})
    source = str(metadata.get("source", "internal"))
    factory = db()
    with factory() as session:
        if ref:
            existing = session.scalars(
                select(MoneyLedgerEntry).where(
                    MoneyLedgerEntry.venture_id == venture_id,
                    MoneyLedgerEntry.ref == ref,
                )
            ).first()
            if existing:
                return _as_entry(existing)
        row = MoneyLedgerEntry(
            id=new_id("led"),
            venture_id=venture_id,
            debit_account=debit_account,
            credit_account=credit_account,
            amount_minor=amount_minor,
            currency=currency.upper(),
            ref=ref,
            meta_json=json.dumps(metadata, sort_keys=True, default=str),
            source=source,
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _as_entry(row)
