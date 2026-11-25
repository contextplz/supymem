"""Allowance consume order, child reservations, feasibility, no $ in founder APIs."""

from __future__ import annotations
from tests.credit_fixtures import ensure_test_wallet

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from tests.company_os_harness import load_company_os_app


pytestmark = pytest.mark.hosted_only


@pytest.mark.parametrize("idea", ["Supplier quality tracking for factories", "Family cycling classes"])
def test_expiry_revokes_only_each_unused_allowance_once(credits_env, monkeypatch, idea):
    from app.credits import allowance
    from app.credits.accounts import get_account_by_id
    from app.credits.grants import grant
    from app.credits.reserve import release, reserve, settle
    from app.credits.units import credits_to_micro
    from app.credits._txn import naive_utc

    now = naive_utc()
    account = ensure_test_wallet("ven_expiry_" + idea.split()[0].lower())
    initial = account.granted_micro
    grant(account_id=account.id, amount_micro=credits_to_micro(30), reason="pack", actor_kind="system",
          actor_id="test", idempotency_key=idea + ":purchase", meta={"bucket": "purchased"})
    allowance.grant_allowance(account_id=account.id, credits=100,
        period_end=now + timedelta(days=1), idempotency_key=idea + ":old")
    hold = reserve(account_id=account.id, amount_micro=credits_to_micro(40), purpose=idea)
    settle(hold, actual_micro=credits_to_micro(40))
    allowance.grant_allowance(account_id=account.id, credits=25,
        period_end=now + timedelta(days=2), idempotency_key=idea + ":next")
    allowance.grant_allowance(account_id=account.id, credits=70,
        period_end=now + timedelta(days=4), idempotency_key=idea + ":active")
    held = reserve(account_id=account.id, amount_micro=credits_to_micro(20), purpose=idea)
    cutoff = now + timedelta(days=3)
    assert allowance.expire_allowances(now=cutoff) == 2
    assert allowance.expire_allowances(now=cutoff) == 0
    after = get_account_by_id(account.id)
    assert after.granted_micro == initial + credits_to_micro(30 + 40 + 70)
    assert after.spent_micro == credits_to_micro(40)
    assert after.reserved_micro == credits_to_micro(20)
    assert allowance.buckets(account.id, now=cutoff) == {
        "allowance": credits_to_micro(70), "purchased": credits_to_micro(30), "grants": initial}
    release(held, definitive=True)
    assert allowance.expire_allowances(now=cutoff) == 0
    assert get_account_by_id(account.id).granted_micro == after.granted_micro
    held_account = ensure_test_wallet("ven_held_" + idea.split()[0].lower())
    original_grant = held_account.granted_micro
    allowance.grant_allowance(account_id=held_account.id, credits=10,
        period_end=now - timedelta(days=1), idempotency_key=idea + ":held")
    blocked = reserve(account_id=held_account.id, amount_micro=original_grant + credits_to_micro(10), purpose=idea)
    assert allowance.expire_allowances(now=now) == 0
    assert get_account_by_id(held_account.id).reserved_micro == original_grant + credits_to_micro(10)
    release(blocked, definitive=True)
    assert allowance.expire_allowances(now=now) == 1
    assert allowance.expire_allowances(now=now) == 0
    assert get_account_by_id(held_account.id).granted_micro == original_grant
    allowance.grant_allowance(account_id=held_account.id, credits=10,
        period_end=now - timedelta(days=1), idempotency_key=idea + ":partial")
    partial = reserve(account_id=held_account.id, amount_micro=original_grant + credits_to_micro(5), purpose=idea)
    assert allowance.expire_allowances(now=now) == 1
    assert allowance.expire_allowances(now=now) == 0
    covered = get_account_by_id(held_account.id)
    assert covered.granted_micro == covered.reserved_micro == original_grant + credits_to_micro(5)
    release(partial, definitive=True)
    assert allowance.expire_allowances(now=now) == 1
    assert allowance.expire_allowances(now=now) == 0
    assert get_account_by_id(held_account.id).granted_micro == original_grant


def test_allowance_then_purchased_then_grants(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/allow.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.credits.accounts import ensure_account
    from app.credits.allowance import buckets, grant_allowance
    from app.credits.grants import grant
    from app.credits.reserve import reserve, settle
    from app.credits.units import credits_to_micro
    from app.db.models import Base
    from app.db.session import make_engine

    state.reset_db_factory()
    engine = make_engine(f"sqlite:///{tmp_path}/allow.db")
    Base.metadata.create_all(engine)
    acc = ensure_test_wallet("ven_allow")
    grant_allowance(
        account_id=acc.id,
        credits=10,
        period_end=datetime.utcnow() + timedelta(days=20),
        idempotency_key="allow:1",
    )
    grant(
        account_id=acc.id,
        amount_micro=credits_to_micro(5),
        reason="pack",
        actor_kind="system",
        actor_id="test",
        idempotency_key="buy:1",
        meta={"bucket": "purchased"},
    )
    parts = buckets(acc.id)
    assert parts["allowance"] >= credits_to_micro(10)
    assert parts["purchased"] >= credits_to_micro(5)
    initial_grants = parts["grants"]

    hold = reserve(account_id=acc.id, amount_micro=credits_to_micro(12), purpose="test")
    settle(hold, actual_micro=credits_to_micro(12))
    parts = buckets(acc.id)
    assert parts == {"allowance": 0, "purchased": credits_to_micro(3), "grants": initial_grants}

    hold = reserve(account_id=acc.id, amount_micro=credits_to_micro(4), purpose="test")
    settle(hold, actual_micro=credits_to_micro(4))
    assert buckets(acc.id) == {"allowance": 0, "purchased": 0,
                               "grants": initial_grants - credits_to_micro(1)}


def test_feasibility_options_and_no_dollars(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/est.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app.estimate.feasibility import feasibility
    from app.estimate.meters import compose_estimate

    est = compose_estimate(plan={"tasks": [{"kind": "scaffold", "complexity": "hard"}]}, existing_assets=[{"kind": "cursor"}])
    assert est.gross_upfront_micro >= est.total_upfront_micro
    assert any(o.get("asset") == "cursor" for o in est.offsets)
    result = feasibility("wsp_missing", est)
    assert result.ok is False
    kinds = {o["kind"] for o in result.options}
    assert kinds == {"TopUp", "ReduceScope", "Phase", "UseOwnResources"}
    assert "$" not in result.explanation and "USD" not in result.explanation


def test_child_reservation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/child.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.credits.accounts import ensure_account
    from app.credits.reserve import reserve, reserve_child
    from app.credits.units import credits_to_micro
    from app.db.models import Base, CreditReservation
    from app.db.session import make_engine
    from app.state import db

    state.reset_db_factory()
    engine = make_engine(f"sqlite:///{tmp_path}/child.db")
    Base.metadata.create_all(engine)
    acc = ensure_test_wallet("ven_child")
    parent = reserve(account_id=acc.id, amount_micro=credits_to_micro(20), purpose="plan")
    child = reserve_child(parent_id=parent.reservation_id, amount_micro=credits_to_micro(5), purpose="build")
    factory = db()
    with factory() as session:
        row = session.get(CreditReservation, child.reservation_id)
        assert row is not None
        assert row.parent_id == parent.reservation_id
        assert row.status in {"active", "open"}


def test_credits_summary_has_no_dollar(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/sum.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    with TestClient(main.create_app()) as client:
        ws = client.post("/workspaces", json={"kind": "venture", "name": "Cash"}).json()
        r = client.get(f"/workspaces/{ws['id']}/credits/summary")
        assert r.status_code == 200, r.text
        text = r.text
        assert "$" not in text and "USD" not in text
        assert r.json()["status"] == "available"
        assert r.json()["balance"] == 5000


def test_plans_are_free_founder_team() -> None:
    from app.billing.plans import TIERS

    assert TIERS["founder"]["inr_paise"] == 249900
    assert TIERS["team"]["inr_paise"] == 799900
    assert TIERS["free"]["inr_paise"] == 0
    assert TIERS["founder"]["usd_cents"] == 2900
    assert TIERS["team"]["usd_cents"] == 9500


def test_existing_founder_account_receives_launch_allowance_increase(tmp_path, monkeypatch) -> None:
    """Raising the starter allowance keeps the ledger append-only and idempotent."""
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/starter-upgrade.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    from app import state
    from app.credits.accounts import balance, ensure_account
    from app.credits.policy import set_policy
    from app.db.models import Base
    from app.db.session import make_engine

    state.reset_db_factory()
    engine = make_engine(f"sqlite:///{tmp_path}/starter-upgrade.db")
    Base.metadata.create_all(engine)
    set_policy("default_venture_credits", 500, updated_by="test")
    account = ensure_test_wallet("ven_existing")
    assert balance(account)["available_micro"] == 5_000_000

    set_policy("default_venture_credits", 5000, updated_by="test")
    upgraded = ensure_test_wallet("ven_existing")
    assert balance(upgraded)["available_micro"] == 50_000_000
    # Repeat reads cannot duplicate the launch upgrade.
    assert balance(ensure_test_wallet("ven_existing"))["available_micro"] == 50_000_000
