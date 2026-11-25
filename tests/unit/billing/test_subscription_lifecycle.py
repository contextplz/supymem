"""Paid plans lose access when their prepaid period or subscription ends."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def billing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/billing.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    load_company_os_app()
    from app import state

    state.reset_db_factory()
    now = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.billing.entitlements.utcnow", lambda: now)
    return now


@pytest.mark.parametrize("workspace", ["warehouse", "family_walks"])
def test_local_access_does_not_create_or_require_a_hosted_subscription(tmp_path, monkeypatch, workspace):
    from sqlalchemy import select

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/billing.db")
    load_company_os_app()
    from app.billing.entitlements import current_plan
    from app.db.models import PlanSubscription
    from app.state import db

    plan = current_plan(workspace_id=workspace, person_id=None)
    assert (plan.tier, plan.status, plan.seats) == ("local", "active", None)
    assert plan.allows("harness_research")
    with db()() as session:
        assert session.scalar(select(PlanSubscription.id)) is None


@pytest.mark.parametrize("tier", ["pro", "team"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_paid_period_expiry(billing_env, tier, offset):
    from app.billing.entitlements import _RENEWAL_GRACE, activate_paid, current_plan
    from app.billing.plans import TIERS

    activate_paid(workspace_id="wsp_paid", person_id=None, tier=tier,
        provider="stripe", provider_subscription_id="sub_1",
        period_end=billing_env - _RENEWAL_GRACE + timedelta(seconds=offset))
    for _ in range(2):
        plan = current_plan(workspace_id="wsp_paid", person_id=None)
        assert plan.status == ("expired" if offset <= 0 else "active")
        assert plan.tier == ("free" if offset <= 0 else tier)
        assert plan.seats == TIERS[plan.tier]["seats"]
        assert plan.allows("cloud_agents") is (offset > 0)


@pytest.mark.parametrize("status", ["past_due", "canceled", "expired"])
def test_status_writer_revokes_access_and_allows_renewal(billing_env, status):
    from app.billing import entitlements

    original = entitlements.activate_paid(workspace_id="wsp_paid", person_id=None, tier="team",
        provider="stripe", provider_subscription_id="sub_1", period_end=billing_env + timedelta(days=1))
    assert entitlements.update_subscription_status(provider="stripe", provider_subscription_id="sub_1",
        workspace_id=None, status=status)
    plan = entitlements.current_plan(workspace_id="wsp_paid", person_id=None)
    assert plan.status == status
    assert not plan.allows("team_seats")
    assert plan.features == ("local_app", "credits_meter")
    assert plan.seats == 1
    assert plan.current_period_end == original.current_period_end
    stale = entitlements.activate_paid(workspace_id="wsp_paid", person_id=None, tier="team",
        provider="stripe", provider_subscription_id="sub_1", period_end=billing_env + timedelta(days=31))
    assert stale.allows("team_seats") is (status != "canceled")  # canceled ids are terminal
    renewed = entitlements.activate_paid(workspace_id="wsp_paid", person_id=None, tier="team",
        provider="stripe", provider_subscription_id="sub_2", period_end=billing_env + timedelta(days=31))
    assert renewed.allows("team_seats")
    assert renewed.current_period_end == "2026-03-03T12:00:00Z"


@pytest.mark.parametrize("provider,subscription_id,workspace_id", [
    ("razorpay", "sub_1", "wsp_paid"), ("stripe", "sub_other", "wsp_paid"),
    ("stripe", "sub_1", "wsp_other"),
])
def test_status_writer_does_not_revoke_another_subscription(billing_env, provider, subscription_id, workspace_id):
    from app.billing import entitlements

    entitlements.activate_paid(workspace_id="wsp_paid", person_id=None, tier="team",
        provider="stripe", provider_subscription_id="sub_1", period_end=billing_env + timedelta(days=1))
    assert not entitlements.update_subscription_status(provider=provider, provider_subscription_id=subscription_id,
        workspace_id=workspace_id, status="canceled")
    assert entitlements.current_plan(workspace_id="wsp_paid", person_id=None).status == "active"


def test_trial_expiry_still_downgrades(billing_env, monkeypatch):
    from app.billing.entitlements import current_plan

    trial = current_plan(workspace_id="wsp_trial", person_id=None)
    assert trial.status == "trialing"
    monkeypatch.setattr("app.billing.entitlements.utcnow", lambda: billing_env + timedelta(days=14))
    expired = current_plan(workspace_id="wsp_trial", person_id=None)
    assert (expired.status, expired.tier, expired.seats) == ("expired", "free", 1)


def test_status_writer_rejects_invalid_status(billing_env):
    from app.billing.entitlements import update_subscription_status

    with pytest.raises(ValueError):
        update_subscription_status(provider="stripe", provider_subscription_id="sub_1",
            workspace_id="wsp_paid", status="active")


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 12, 31, 12, tzinfo=timezone.utc), "2027-01-31T12:00:00Z"),
    (datetime(2028, 1, 31, 12, tzinfo=timezone.utc), "2028-02-29T12:00:00Z"),
])
def test_razorpay_calendar_month(billing_env, monkeypatch, now, expected):
    from app.billing.entitlements import activate_paid

    monkeypatch.setattr("app.billing.entitlements.utcnow", lambda: now)
    plan = activate_paid(workspace_id="wsp_paid", person_id=None, tier="team",
        provider="razorpay", provider_subscription_id="pay_1")
    assert plan.current_period_end == expected


def test_founder_tier_activates_within_schema(billing_env):
    from app.billing.entitlements import activate_paid, current_plan

    activate_paid(workspace_id="wsp_f", person_id=None, tier="founder",
        provider="stripe", provider_subscription_id="sub_f", period_end=billing_env + timedelta(days=30))
    plan = current_plan(workspace_id="wsp_f", person_id=None)
    assert (plan.tier, plan.status) == ("pro", "active")
    assert plan.allows("cloud_agents")
