"""Plan checkout prefers house Stripe over Razorpay."""

from __future__ import annotations

from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


class _Handle:
    provider_order_id = "cs_plan"
    checkout_url = "https://checkout.stripe.com/c/pay/cs_plan"


def test_checkout_uses_stripe_when_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/bill_stripe.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unit")
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_should_not_win")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    monkeypatch.setattr(
        "app.payments.stripe.StripeProvider.create_subscription_checkout",
        lambda self, **_kw: _Handle(),
    )
    with TestClient(main.create_app()) as client:
        plans = client.get("/billing/plans")
        assert plans.status_code == 200
        body = plans.json()
        assert body["house_provider"] == "stripe"
        founder = next(item for item in body["items"] if item["tier"] == "founder")
        assert founder["usd_cents"] == 2900
        ws = client.post("/workspaces", json={"kind": "venture", "name": "Bill"}).json()
        r = client.post(
            "/billing/checkout",
            json={"workspace_id": ws["id"], "tier": "founder"},
        )
        assert r.status_code == 200, r.text
        checkout = r.json()
        assert checkout["provider"] == "stripe"
        assert checkout["checkout_url"] == _Handle.checkout_url


def test_checkout_closed_until_payments_accepting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/bill_closed.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_unit")
    monkeypatch.delenv("PAYMENTS_ACCEPTING", raising=False)
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()) as client:
        plans = client.get("/billing/plans")
        assert plans.status_code == 200
        assert plans.json()["accepting"] is False
        ws = client.post("/workspaces", json={"kind": "venture", "name": "Closed"}).json()
        r = client.post(
            "/billing/checkout",
            json={"workspace_id": ws["id"], "tier": "founder"},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["error"]["code"] == "payments_closed"


def test_plans_include_usd_cents() -> None:
    from app.billing.plans import TIERS

    assert TIERS["founder"]["usd_cents"] == 2900
    assert TIERS["team"]["usd_cents"] == 9500
    assert TIERS["free"]["usd_cents"] == 0
