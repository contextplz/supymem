"""Founder credits summary and usage (docs/supymem/25 §9)."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import select

from app.authz.workspace import ensure_subject_person, assert_workspace_access
from app.core.local_mode import is_local
from app.credits.local_meter import snapshot
from app.credits.policy import person_wallet_enabled
from app.credits.accounts import account_for_workspace, balance, founder_snapshot_for_workspace, wallet_for
from app.credits.allowance import buckets
from app.credits.units import micro_to_credits
from app.db.models import CreditLedgerEntry
from app.state import db

router = APIRouter(tags=["credits"])


@router.get("/local/spend")
def local_spend(request: Request):
    if not is_local():
        raise HTTPException(404)
    from app.core.local_owner import request_owner
    request_owner(request)
    return snapshot()


@router.get("/workspaces/{workspace_id}/credits/summary")
def credits_summary(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    if is_local():
        return {"status": "available", **snapshot(workspace_id=workspace_id)}
    account = wallet_for(ensure_subject_person(x_subject)) if person_wallet_enabled() else account_for_workspace(workspace_id)
    if account is None:
        # Old ventures may predate account provisioning. Resolve the workspace
        # to its venture and create the standard founder allowance once.
        founder_snapshot_for_workspace(workspace_id)
        account = wallet_for(ensure_subject_person(x_subject)) if person_wallet_enabled() else account_for_workspace(workspace_id)
    if account is None:
        return {
            "status": "unavailable",
            "balance": None,
            "available": None,
            "reserved": None,
            "spent": None,
            "account_status": None,
            "allowance": None,
            "purchased": None,
            "grants": None,
            "burn_7d": {},
            "projected_days": None,
        }
    bal = balance(account)
    parts = buckets(account.id)
    burn = _burn_7d(account.id)
    daily = sum(burn.values()) / 7 if burn else 0
    available = int(bal["available_micro"])
    projected = int(available / daily) if daily > 0 else None
    return {
        "status": "available",
        "balance": micro_to_credits(available),
        "available": micro_to_credits(available),
        "reserved": micro_to_credits(int(bal["reserved_micro"])),
        "spent": micro_to_credits(int(bal["spent_micro"])),
        "account_status": account.status,
        "allowance": micro_to_credits(parts["allowance"]),
        "purchased": micro_to_credits(parts["purchased"]),
        "grants": micro_to_credits(parts["grants"]),
        "burn_7d": {k: micro_to_credits(v) for k, v in burn.items()},
        "projected_days": projected,
    }


@router.get("/workspaces/{workspace_id}/credits/usage")
def credits_usage(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
    group_by: str = Query(default="day", pattern="^(meter|app|day)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
):
    assert_workspace_access(workspace_id, x_subject)
    if is_local():
        report = snapshot(workspace_id=workspace_id)
        return {"status": "available", "group_by": "feature", "month": report["month"],
            "items": [{"key": feature, "cost_usd_micro": cost}
                for feature, cost in sorted(report["by_feature_usd_micro"].items())]}
    if group_by == "app":
        raise HTTPException(422, detail={"error": {"code": "unsupported_grouping", "message": "App attribution is not yet available for this ledger; use meter or day."}})
    account = wallet_for(ensure_subject_person(x_subject)) if person_wallet_enabled() else account_for_workspace(workspace_id)
    if account is None:
        return {"status": "unavailable", "items": [], "group_by": group_by}
    factory = db()
    start = date_from or (datetime.utcnow() - timedelta(days=30))
    end = date_to or datetime.utcnow()
    with factory() as session:
        rows = list(
            session.scalars(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.account_id == account.id,
                    CreditLedgerEntry.kind == "spend",
                    CreditLedgerEntry.created_at >= start,
                    CreditLedgerEntry.created_at <= end,
                )
            )
        )
    grouped: dict[str, int] = {}
    for row in rows:
        if group_by == "meter":
            key = row.purpose or "unknown"
        else:
            key = naive_day(row.created_at)
        grouped[key] = grouped.get(key, 0) + abs(row.amount_micro)
    return {
        "status": "available",
        "group_by": group_by,
        "items": [
            {"key": k, "credits": micro_to_credits(v)} for k, v in sorted(grouped.items())
        ],
    }


def naive_day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _burn_7d(account_id: str) -> dict[str, int]:
    factory = db()
    start = datetime.utcnow() - timedelta(days=7)
    with factory() as session:
        rows = list(
            session.scalars(
                select(CreditLedgerEntry).where(
                    CreditLedgerEntry.account_id == account_id,
                    CreditLedgerEntry.kind == "spend",
                    CreditLedgerEntry.created_at >= start,
                )
            )
        )
    out: dict[str, int] = {}
    for row in rows:
        key = row.purpose or "other"
        out[key] = out.get(key, 0) + abs(row.amount_micro)
    return out
