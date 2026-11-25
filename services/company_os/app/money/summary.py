"""Ledger-derived money summaries with explicit provenance."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db.models import MoneyLedgerEntry, OpsStripeEvent, Venture
from app.money.receipts import charge_minor_unit_exponent, live_customer_payment
from app.state import db


def verified_customer_payment(session, row: MoneyLedgerEntry, workspace_id: str, venture_id: str) -> bool:
    """Verify the ledger amount against its separately persisted signed provider receipt."""
    try:
        meta = json.loads(row.meta_json)
        receipt = session.get(OpsStripeEvent, meta.get("receipt_id")) if meta.get("receipt_id") else None
        body = json.loads(receipt.raw_payload_json or "{}") if receipt else {}
        payment, _ = live_customer_payment(body, workspace_id, venture_id)
    except (ValueError, TypeError, AttributeError):
        return False
    return bool(row.source == "stripe" and receipt and receipt.status == "revenue_recorded"
                and receipt.workspace_id == workspace_id and receipt.stripe_event_id == body.get("id")
                and payment and row.ref == f"stripe:payment_intent:{payment['payment_intent_id']}"
                and row.amount_minor == payment["amount_minor"] and row.currency == payment["currency"]
                and row.debit_account == "cash:stripe" and row.credit_account == "revenue")


def money_summary(venture_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(MoneyLedgerEntry)
            .where(MoneyLedgerEntry.venture_id == venture_id)
            .order_by(MoneyLedgerEntry.created_at.asc())
        ).all()
        venture = session.get(Venture, venture_id)
        workspace_id = venture.workspace_id if venture else None
        verified = [row for row in rows if verified_customer_payment(session, row, workspace_id, venture_id)]
    if not verified:
        return {
            "earned_minor": None,
            "gross_received_minor": None,
            "currency": None,
            "mode": "no_data",
            "charges": [],
            "provenance": {"ledger_entries": len(rows), "excluded_entries": len(rows),
                           "reason": "no_verified_live_customer_payment_receipts"},
        }

    currencies = {row.currency for row in verified}
    if len(currencies) != 1:
        return {"earned_minor": None, "gross_received_minor": None, "currency": None, "mode": "no_data", "charges": [],
                "provenance": {"ledger_entries": len(rows), "confirmed_entries": len(verified),
                    "reason": "confirmed_payments_have_multiple_currencies"}}
    earned_minor = sum(
        row.amount_minor
        if row.credit_account == "revenue"
        else -row.amount_minor
        if row.debit_account == "revenue"
        else 0
        for row in verified
    )
    sources = Counter(row.source for row in verified)
    stripe_rows = verified
    mode = "live"
    charges = [
        {
            "ref": row.ref,
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "source": row.source,
            "meta": json.loads(row.meta_json),
        }
        for row in verified
        if row.credit_account == "revenue" or row.debit_account == "revenue"
    ]
    return {
        "earned_minor": None,
        "gross_received_minor": earned_minor,
        "basis": "gross_verified_live_payments_refunds_and_fees_not_reconciled",
        "currency": next(iter(currencies)),
        "minor_unit_exponent": charge_minor_unit_exponent(next(iter(currencies))),
        "mode": mode,
        "charges": charges,
        "provenance": {
            "ledger_entries": len(rows),
            "confirmed_entries": len(verified),
            "excluded_entries": len(rows) - len(verified),
            "sources": dict(sources),
            "stripe_refs": [row.ref for row in stripe_rows],
        },
    }
