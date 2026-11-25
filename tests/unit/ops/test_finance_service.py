"""Finance observations, recorded spend, read-only reporting and webhook auth."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

requires_stripe = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[3] / "services/company_os/app/payments/stripe.py").is_file(),
    reason="Hosted Stripe callbacks are not in the public local release",
)

@pytest.fixture(autouse=True)
def current_finance_service(ops_env):
    global finance_svc
    from app.ops.services import finance as finance_svc


@requires_stripe
def test_webhook_bad_signature_rejected(ops_env, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    ws = ops_env["workspace_id"]
    with pytest.raises(finance_svc.WebhookError) as exc:
        finance_svc.process_stripe_webhook(
            b'{"id":"evt_test","type":"payment_intent.succeeded","data":{"object":{"metadata":{"workspace_id":"%s"}}}}'
            % ws.encode(),
            "t=1,v1=badsig",
        )
    assert exc.value.status == 400


@requires_stripe
def test_webhook_valid_signature_accepted(ops_env, monkeypatch):
    secret = "whsec_test"
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", secret)
    ws = ops_env["workspace_id"]
    payload = (
        b'{"id":"evt_ok","type":"payment_intent.succeeded","data":{"object":{"metadata":{"workspace_id":"'
        + ws.encode()
        + b'"}}}}'
    )
    ts = str(int(time.time()))
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    result = finance_svc.process_stripe_webhook(payload, f"t={ts},v1={sig}")
    assert result["received"] is True


@requires_stripe
def test_webhook_no_secret_is_503(ops_env, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    with pytest.raises(finance_svc.WebhookError) as exc:
        finance_svc.process_stripe_webhook(
            json.dumps({"id": "evt_nosec", "type": "invoice.paid"}).encode(),
            None,
        )
    assert exc.value.status == 503


def test_summary_empty(ops_env):
    summary = finance_svc.get_summary(ops_env["workspace_id"])
    for key in ("mrr_cents", "arr_cents", "active_subscribers", "stripe_balance_cents"):
        assert summary[key] is None
    assert summary["revenue_basis"] == "unavailable"
    assert summary["spend_basis"] == "recorded_entries_not_provider_reconciled"
    assert summary["recorded_expense_count"] == summary["recorded_ad_campaign_count"] == 0
    assert summary["last_snapshot_date"] is None


@pytest.mark.parametrize("mrr,subscribers,balance", [(0, 0, 0), (12500, 3, 7700)])
def test_snapshot_values_and_date_survive_service_and_api(ops_env, mrr, subscribers, balance):
    from datetime import date
    from tests.company_os_harness import company_os_test_client as TestClient

    wid = ops_env["workspace_id"]
    recorded = date(2025, 2, 3)
    finance_svc.save_revenue_snapshot(wid, snapshot_date=recorded, mrr_cents=mrr,
        arr_cents=mrr * 12, active_subscribers=subscribers, stripe_balance_cents=balance)
    with TestClient(ops_env["main"].create_app()) as client:
        response = client.get("/api/v1/finance/summary", headers={"X-Workspace-Id": wid})
    assert response.status_code == 200
    summary = response.json()
    assert summary["mrr_cents"] == mrr
    assert summary["arr_cents"] == mrr * 12
    assert summary["active_subscribers"] == subscribers
    assert summary["stripe_balance_cents"] == balance
    assert summary["last_snapshot_date"] == recorded.isoformat()
    assert summary["revenue_basis"] == "recorded_snapshot"


def test_api_missing_snapshot_serializes_null_not_zero(ops_env):
    from tests.company_os_harness import company_os_test_client as TestClient

    with TestClient(ops_env["main"].create_app()) as client:
        response = client.get("/api/v1/finance/summary", headers={"X-Workspace-Id": ops_env["workspace_id"]})
    assert response.status_code == 200
    assert response.json()["mrr_cents"] is None
    assert response.json()["stripe_balance_cents"] is None
    assert response.json()["last_snapshot_date"] is None


def test_recorded_spend_is_tenant_scoped_usd_and_not_reconciled(ops_env):
    from datetime import date, timedelta
    from app.db.models import OpsAdCampaign
    from app.state import db
    from core.clock import utcnow

    wid = ops_env["workspace_id"]
    finance_svc.add_expense(wid, category="tools", vendor="Recorded", amount_cents=1700)
    finance_svc.add_expense(wid, category="tools", vendor="Zero", amount_cents=0)
    finance_svc.add_expense(wid, category="tools", vendor="Other currency", amount_cents=9999, currency="eur")
    finance_svc.add_expense(wid, category="tools", vendor="Previous month", amount_cents=9999,
        expense_date=date.today().replace(day=1) - timedelta(days=1))
    finance_svc.add_expense("wsp_other", category="tools", vendor="Other tenant", amount_cents=9999)
    with db()() as session:
        for cid, workspace, amount in (("ad_own", wid, 12.5), ("ad_zero", wid, 0), ("ad_other", "wsp_other", 999)):
            session.add(OpsAdCampaign(id=cid, workspace_id=workspace, platform="meta", name=cid,
                total_spent_usd=amount, created_at=utcnow()))
        session.commit()
    result = finance_svc.get_summary(wid)
    assert result["total_expenses_month_cents"] == 1700
    assert result["recorded_expense_count"] == 2
    assert result["expense_currency"] == "usd"
    assert result["total_ad_spend_usd"] == 12.5
    assert result["recorded_ad_campaign_count"] == 2
    assert result["spend_basis"] == "recorded_entries_not_provider_reconciled"
    assert result["mrr_cents"] is None


@pytest.mark.parametrize("with_snapshot", [False, True])
def test_finance_agent_never_creates_or_redates_observations(ops_env, monkeypatch, with_snapshot):
    from datetime import date
    from app.ops.agents.finance import FinanceAgent

    wid = ops_env["workspace_id"]
    if with_snapshot:
        finance_svc.save_revenue_snapshot(wid, snapshot_date=date(2025, 2, 3), mrr_cents=0,
            arr_cents=0, active_subscribers=0, stripe_balance_cents=0)
    before = finance_svc.list_revenue(wid)
    agent = FinanceAgent()
    def forbidden(*args, **kwargs):
        raise AssertionError("A finance summary must not write observations or call a provider")
    monkeypatch.setattr(finance_svc, "save_revenue_snapshot", forbidden)
    monkeypatch.setattr(agent, "call_claude_json", forbidden)
    result = agent.run({}, {"workspace_id": wid})
    assert finance_svc.list_revenue(wid) == before
    assert result["mrr_cents"] == (0 if with_snapshot else None)
    assert result["last_snapshot_date"] == ("2025-02-03" if with_snapshot else None)
    if not with_snapshot:
        assert "unavailable" in result["summary"]
        assert "$0.00" not in result["summary"]


def test_snapshot_writer_requires_explicit_observations(ops_env):
    from datetime import date

    with pytest.raises(TypeError):
        finance_svc.save_revenue_snapshot(ops_env["workspace_id"], snapshot_date=date.today())
    assert finance_svc.list_revenue(ops_env["workspace_id"]) == []
