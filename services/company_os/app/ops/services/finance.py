"""Finance summaries, expenses, Stripe webhook ingest."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.db.models import (
    OpsAdCampaign,
    OpsExpenseRecord,
    OpsRevenueSnapshot,
    OpsStripeEvent,
    MoneyLedgerEntry,
    Venture,
)
from app.ops import config as ops_config
from app.state import db
from app.db.session import serialize_writes
from core.clock import utcnow
from core.ids import new_id


class WebhookError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def get_summary(workspace_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        snap = session.scalars(
            select(OpsRevenueSnapshot)
            .where(OpsRevenueSnapshot.workspace_id == workspace_id)
            .order_by(OpsRevenueSnapshot.snapshot_date.desc())
        ).first()
        ad_spend, ad_count = session.execute(
            select(func.coalesce(func.sum(OpsAdCampaign.total_spent_usd), 0.0), func.count(OpsAdCampaign.id)).where(
                OpsAdCampaign.workspace_id == workspace_id
            )
        ).one()
        month_start = date.today().replace(day=1)
        expenses, expense_count = session.execute(
            select(func.coalesce(func.sum(OpsExpenseRecord.amount_cents), 0), func.count(OpsExpenseRecord.id)).where(
                OpsExpenseRecord.workspace_id == workspace_id,
                OpsExpenseRecord.expense_date >= month_start,
                func.lower(OpsExpenseRecord.currency) == "usd",
            )
        ).one()
        # Missing observations are distinct from a snapshot that explicitly records zero.
        return {
            "mrr_cents": snap.mrr_cents if snap is not None else None,
            "arr_cents": snap.arr_cents if snap is not None else None,
            "active_subscribers": snap.active_subscribers if snap is not None else None,
            "stripe_balance_cents": snap.stripe_balance_cents if snap is not None else None,
            "last_snapshot_date": snap.snapshot_date.isoformat() if snap is not None else None,
            "revenue_basis": "recorded_snapshot" if snap is not None else "unavailable",
            "total_ad_spend_usd": float(ad_spend),
            "total_expenses_month_cents": int(expenses),
            "spend_basis": "recorded_entries_not_provider_reconciled",
            "recorded_ad_campaign_count": ad_count,
            "recorded_expense_count": expense_count,
            "expense_currency": "usd",
            "expense_period_start": month_start.isoformat(),
        }


def list_revenue(workspace_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(OpsRevenueSnapshot)
            .where(OpsRevenueSnapshot.workspace_id == workspace_id)
            .order_by(OpsRevenueSnapshot.snapshot_date.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "snapshot_date": r.snapshot_date.isoformat(),
                "mrr_cents": r.mrr_cents,
                "arr_cents": r.arr_cents,
                "active_subscribers": r.active_subscribers,
                "stripe_balance_cents": r.stripe_balance_cents,
            }
            for r in rows
        ]


def list_expenses(
    workspace_id: str,
    *,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        q = select(OpsExpenseRecord).where(OpsExpenseRecord.workspace_id == workspace_id)
        if category:
            q = q.where(OpsExpenseRecord.category == category)
        rows = session.scalars(q.order_by(OpsExpenseRecord.expense_date.desc()).limit(limit)).all()
        return [
            {
                "id": r.id,
                "category": r.category,
                "vendor": r.vendor,
                "amount_cents": r.amount_cents,
                "currency": r.currency,
                "description": r.description,
                "date": r.expense_date.isoformat(),
            }
            for r in rows
        ]


def save_revenue_snapshot(
    workspace_id: str,
    *,
    snapshot_date: date,
    mrr_cents: int,
    arr_cents: int,
    active_subscribers: int,
    stripe_balance_cents: int,
    churned_today: int = 0,
    new_today: int = 0,
    total_revenue_month_cents: int = 0,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        existing = session.scalars(
            select(OpsRevenueSnapshot).where(
                OpsRevenueSnapshot.workspace_id == workspace_id,
                OpsRevenueSnapshot.snapshot_date == snapshot_date,
            )
        ).first()
        if existing:
            existing.mrr_cents = mrr_cents
            existing.arr_cents = arr_cents
            existing.active_subscribers = active_subscribers
            existing.stripe_balance_cents = stripe_balance_cents
            existing.churned_today = churned_today
            existing.new_today = new_today
            existing.total_revenue_month_cents = total_revenue_month_cents
            row = existing
        else:
            row = OpsRevenueSnapshot(
                id=new_id("ors"),
                workspace_id=workspace_id,
                snapshot_date=snapshot_date,
                mrr_cents=mrr_cents,
                arr_cents=arr_cents,
                active_subscribers=active_subscribers,
                churned_today=churned_today,
                new_today=new_today,
                total_revenue_month_cents=total_revenue_month_cents,
                stripe_balance_cents=stripe_balance_cents,
                created_at=utcnow(),
            )
            session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "snapshot_date": row.snapshot_date.isoformat(),
            "mrr_cents": row.mrr_cents,
            "arr_cents": row.arr_cents,
        }


def add_expense(
    workspace_id: str,
    *,
    category: str,
    vendor: str,
    amount_cents: int,
    expense_date: date | None = None,
    currency: str = "usd",
    description: str | None = None,
) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = OpsExpenseRecord(
            id=new_id("oex"),
            workspace_id=workspace_id,
            category=category,
            vendor=vendor,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            expense_date=expense_date or date.today(),
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "category": row.category,
            "vendor": row.vendor,
            "amount_cents": row.amount_cents,
            "date": row.expense_date.isoformat(),
        }


def process_stripe_webhook(
    payload: bytes,
    sig_header: str | None,
) -> dict[str, Any]:
    from app.payments.stripe import verify_stripe_signature

    secret = ops_config.stripe_webhook_secret()
    if not secret:
        raise WebhookError("Stripe webhook secret is not configured", status=503)
    if not sig_header or not verify_stripe_signature(payload, sig_header, secret):
        raise WebhookError("Invalid Stripe signature", status=400)
    try:
        body = json.loads(payload.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise WebhookError("Invalid JSON payload", status=400) from exc
    if not isinstance(body, dict):
        raise WebhookError("Invalid Stripe event", status=400)
    data = body.get("data")
    data_obj = data.get("object") if isinstance(data, dict) else {}
    if not isinstance(data_obj, dict):
        raise WebhookError("Invalid Stripe event object", status=400)
    meta = data_obj.get("metadata") if isinstance(data_obj, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    workspace_id = str(
        meta.get("workspace_id")
        or body.get("workspace_id")
        or ""
    ).strip()
    if not workspace_id:
        raise WebhookError("workspace_id missing from Stripe metadata", status=400)

    event_id = str(body.get("id") or "")
    if not event_id:
        raise WebhookError("Stripe event id missing", status=400)
    event_type = str(body.get("type") or "unknown")
    factory = db()
    with factory() as session:
        # Different event IDs can describe the same payment. Serialize by the
        # provider object across workspaces, before reading either receipt.
        serialize_writes(session, key=f"money:stripe:{data_obj.get('id') or event_id}")
        existing = session.scalars(
            select(OpsStripeEvent).where(
                OpsStripeEvent.workspace_id == workspace_id,
                OpsStripeEvent.stripe_event_id == event_id,
            )
        ).first()
        if existing:
            from app.money.summary import verified_customer_payment
            vid = str(meta.get("venture_id") or "")
            ref = f"stripe:payment_intent:{data_obj.get('id') or ''}"
            ledger = session.scalar(select(MoneyLedgerEntry).where(MoneyLedgerEntry.ref == ref))
            try:
                same_receipt = json.loads(existing.raw_payload_json or "{}") == body
            except (ValueError, TypeError):
                same_receipt = False
            verified = bool(existing.status == "revenue_recorded" and same_receipt and ledger
                            and ledger.venture_id == vid
                            and verified_customer_payment(session, ledger, workspace_id, vid))
            return {
                "received": True,
                "duplicate": True,
                "id": existing.id,
                "workspace_id": workspace_id,
                "revenue_recorded": verified,
                "revenue_reason": "confirmed_live_payment" if verified else "duplicate_receipt_revenue_unconfirmed",
            }
        row = OpsStripeEvent(
            id=new_id("ost"),
            workspace_id=workspace_id,
            stripe_event_id=event_id,
            event_type=event_type,
            customer_id=data_obj.get("customer"),
            amount_cents=data_obj.get("amount") or data_obj.get("amount_total"),
            currency=data_obj.get("currency"),
            status="processed",
            raw_payload_json=json.dumps(body, default=str),
            processed_at=utcnow(),
        )
        session.add(row)
        revenue_reason = "not_a_customer_payment_receipt"
        # Persist the signed provider receipt and its exact venture posting in
        # one transaction. A failed posting must remain retryable on redelivery.
        if event_type == "payment_intent.succeeded":
            from app.money.receipts import live_customer_payment

            vid = str(meta.get("venture_id") or "")
            venture = session.get(Venture, vid) if vid else None
            payment, revenue_reason = live_customer_payment(body, workspace_id, vid)
            if payment and (venture is None or venture.workspace_id != workspace_id):
                payment, revenue_reason = None, "venture_attribution_mismatch"
            if payment:
                ref = f"stripe:payment_intent:{payment['payment_intent_id']}"
                existing_payment = session.scalar(select(MoneyLedgerEntry).where(MoneyLedgerEntry.ref == ref))
                if existing_payment is not None:
                    from app.money.summary import verified_customer_payment
                    if (existing_payment.venture_id != vid or not verified_customer_payment(session, existing_payment, workspace_id, vid)
                            or existing_payment.amount_minor != payment["amount_minor"]
                            or existing_payment.currency != payment["currency"]):
                        payment, revenue_reason = None, "payment_receipt_already_attributed_or_mismatched"
                if existing_payment is None:
                    session.add(MoneyLedgerEntry(id=new_id("led"), venture_id=vid,
                        debit_account="cash:stripe", credit_account="revenue", amount_minor=payment["amount_minor"],
                        currency=payment["currency"], ref=ref, source="stripe", created_at=utcnow(),
                        meta_json=json.dumps({"source": "stripe", "receipt_id": row.id,
                            "stripe_event_id": event_id, "payment_intent_id": payment["payment_intent_id"]}, sort_keys=True)))
                row.status = "revenue_recorded" if payment else "revenue_unconfirmed"
            else:
                row.status = "test_mode" if revenue_reason == "test_mode" else "revenue_unconfirmed"
        session.commit()
        return {"received": True, "id": row.id, "workspace_id": workspace_id,
                "revenue_recorded": row.status == "revenue_recorded", "revenue_reason": revenue_reason}
