"""Synthetic PostgreSQL contracts for the schema-first wallet bridge.

No provider calls or deployed images are exercised. The test-only FK transition
models a later drained cutover; it is deliberately absent from upgrade-to-head.
Passing here does not qualify a Core/worker image or task-definition rollback.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    ROOT, migration_head, pg_database as _pg_database, service_modules,
)


@pytest.fixture
def bridge_db(tmp_path_factory, tmp_path, monkeypatch):
    # Reuse the loopback-only create/migrate/drop lifecycle, but give every case
    # its own database. Phase and global transfer scans cannot leak across cases.
    with contextmanager(_pg_database.__wrapped__)(tmp_path_factory) as (url, engine):
        for key, value in {
            "APP_ENV": "local", "COMPANY_OS_DB_URL": url,
            "COMPANY_OS_DATA_DIR": str(tmp_path),
            "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
            "LLM_OFFLINE": "1", "CHANNELS_DRY_RUN": "true",
            "SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS": "",
        }.items():
            monkeypatch.setenv(key, value)
        with service_modules("company_os"):
            from app.credits.policy import invalidate
            from app.state import set_factory
            factory = sessionmaker(engine, expire_on_commit=False)
            set_factory(factory)  # Never create_all over a migration gap.
            invalidate()
            yield {"factory": factory, "engine": engine}


def _migrate(command_name, revision):
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    getattr(command, command_name)(cfg, revision)


def _seed(d, *, actor=False):
    from app.credits._txn import naive_utc
    from sqlalchemy import insert

    from app.db.models import Person, Venture, Workspace, WorkspaceMember
    now = naive_utc()
    with d["factory"]() as session:
        session.add(Person(id="per_bridge_owner", display_name="Synthetic bridge founder",
                           email="bridge-owner@example.test", created_at=now))
        if actor:
            session.add(Person(id="per_bridge_member", display_name="Synthetic bridge contributor", created_at=now))
        session.flush()
        # Core inserts name only seeded columns: this runs at s041, before later workspace/venture columns exist.
        session.execute(insert(Workspace.__table__).values(id="ws_bridge", owner_id="per_bridge_owner", kind="company",
                              name="Synthetic wallet rollout", entry_mode="idea", created_at=now, updated_at=now))
        session.flush()
        for vid in ("ven_bridge_one", "ven_bridge_two"):
            session.execute(insert(Venture.__table__).values(id=vid, workspace_id="ws_bridge", name="Synthetic venture",
                                raw_idea="Synthetic wallet contract", stage="draft", gtm_order=0,
                                daily_token_budget=10000, created_at=now, updated_at=now))
        if actor:
            session.add(WorkspaceMember(id="wsm_bridge_member", workspace_id="ws_bridge",
                                        person_id="per_bridge_member", role="contributor", created_at=now))
        session.commit()


def _legacy_accounts(*, historical=True):
    from app.credits.accounts import ensure_account
    accounts = tuple(ensure_account(vid, workspace_id="ws_bridge", owner_person_id="per_bridge_owner")
                     for vid in ("ven_bridge_one", "ven_bridge_two"))
    if historical:
        # Cutover/rollback/payment tests preserve balances granted before this policy.
        from app.credits.grants import grant
        from app.credits.units import credits_to_micro
        for account, vid in zip(accounts, ("ven_bridge_one", "ven_bridge_two")):
            if account.granted_micro == 0:
                grant(account_id=account.id, amount_micro=credits_to_micro(5000),
                      reason="Synthetic historical starter allowance", actor_kind="system", actor_id="fixture",
                      idempotency_key="initial:" + vid)
        accounts = tuple(ensure_account(vid) for vid in ("ven_bridge_one", "ven_bridge_two"))
    return accounts


def _snapshot(d):
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditPolicy, CreditReservation
    from app.credits.policy import WALLET_PHASE_KEY
    with d["factory"]() as session:
        return {
            "accounts": list(session.execute(select(CreditAccount.id, CreditAccount.scope,
                CreditAccount.granted_micro, CreditAccount.spent_micro, CreditAccount.reserved_micro,
                CreditAccount.status, CreditAccount.version).order_by(CreditAccount.id))),
            "ledger": list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.account_id,
                CreditLedgerEntry.amount_micro, CreditLedgerEntry.prev_hash, CreditLedgerEntry.entry_hash,
                CreditLedgerEntry.idempotency_key).order_by(CreditLedgerEntry.seq))),
            "holds": list(session.execute(select(CreditReservation.id, CreditReservation.account_id,
                CreditReservation.amount_micro, CreditReservation.status).order_by(CreditReservation.id))),
            "phase": session.get(CreditPolicy, WALLET_PHASE_KEY).value_json,
        }


def _publication_fk(d):
    return next(fk for fk in inspect(d["engine"]).get_foreign_keys("content_publication_runs")
                if fk["name"] == "fk_publication_credit_scope")


def _cutover(session):
    """Test-only staged FK + helper transaction, never an app rollout switch."""
    from app.credits.migration import migrate_wallets
    session.execute(text("ALTER TABLE content_publication_runs DROP CONSTRAINT fk_publication_credit_scope"))
    session.execute(text("ALTER TABLE content_publication_runs ADD CONSTRAINT fk_publication_credit_scope "
                         "FOREIGN KEY (credit_account_id) REFERENCES credit_accounts (id)"))
    return migrate_wallets(session)


def _assert_integrity(d):
    from app.credits.ledger import verify_chain
    from app.credits.reconcile import reconcile
    from app.db.models import CreditAccount
    with d["factory"]() as session:
        ids = list(session.scalars(select(CreditAccount.id)))
    for account_id in ids:
        assert verify_chain(account_id) == (True, None)
        report = reconcile(account_id)
        assert report.ok, report


def test_expansion_preserves_legacy_value_grants_quota_and_publication_scope(bridge_db):
    from app.credits._txn import naive_utc
    from app.credits.accounts import ensure_account, get_account
    from app.credits.grants import grant
    from app.credits.policy import wallet_phase
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry
    from app.ventures.quota import assert_can_create_venture
    d = bridge_db
    _migrate("downgrade", "s041refreshgrace")
    _seed(d)
    now = naive_utc()
    with d["factory"]() as session:
        session.add(CreditAccount(id="cra_legacy_before_expansion", scope="venture", venture_id="ven_bridge_one",
            workspace_id="ws_bridge", owner_person_id="per_bridge_owner", status="active", granted_micro=0,
            spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now))
        session.commit()
    grant(account_id="cra_legacy_before_expansion", amount_micro=credits_to_micro(5000),
          reason="Synthetic legacy allowance", actor_kind="system", actor_id="synthetic",
          idempotency_key="initial:ven_bridge_one")
    with d["factory"]() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)))
    _migrate("upgrade", "head")
    assert wallet_phase() == "legacy"
    with d["engine"].connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_head()
    assert _publication_fk(d)["constrained_columns"] == ["credit_account_id", "workspace_id", "venture_id"]
    assert {"venture_id", "task_invocation"} <= {c["name"] for c in inspect(d["engine"]).get_columns("credit_reservations")}
    with d["factory"]() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash))) == before
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "person")) is None
        old = session.get(CreditAccount, "cra_legacy_before_expansion")
        assert (old.granted_micro, old.spent_micro, old.reserved_micro, old.status) == (credits_to_micro(5000), 0, 0, "active")
    one, two = _legacy_accounts(historical=False)
    assert one.id != two.id and one.scope == two.scope == "venture"
    assert one.granted_micro == credits_to_micro(5000) and two.granted_micro == 0
    assert ensure_account("ven_bridge_one").id == get_account("ven_bridge_one").id == one.id
    assert_can_create_venture("per_bridge_owner")
    with d["factory"]() as session:
        assert list(session.scalars(select(CreditLedgerEntry.idempotency_key).order_by(CreditLedgerEntry.seq))) == [
            "initial:ven_bridge_one"]
    _assert_integrity(d)


@pytest.mark.parametrize("kind", ["live", "expired_pool", "unknown"])
def test_cutover_defers_protected_holds_and_transfers_other_accounts(bridge_db, kind):
    from app.credits._txn import naive_utc
    from app.credits.accounts import get_account_by_id, wallet_for
    from app.credits.policy import wallet_phase
    from app.credits.units import credits_to_micro
    from app.credits.reserve import Hold, protect_outcome, reserve
    from app.db.models import CreditReservation
    d = bridge_db
    _seed(d)
    one, two = _legacy_accounts()
    if kind == "expired_pool":
        from app.credits.pools import create_pool, lock_account
        from app.db.supymem_models import BuildRun, FounderApp
        now = naive_utc()
        with d["factory"].begin() as session:
            session.add(FounderApp(id="app_bridge_pool", workspace_id="ws_bridge", venture_id="ven_bridge_one",
                slug="synthetic-bridge-pool", name="Synthetic pool", kind="landing", created_at=now, updated_at=now))
            session.flush()
            account = lock_account(session, one.id)
            run = BuildRun(id="bld_bridge_pool", app_id="app_bridge_pool", workspace_id="ws_bridge",
                venture_id="ven_bridge_one", trigger="founder", agent="openhands", state="queued",
                runtime_version=1, created_at=now, spec="{}")
            session.add(run)
            pool = create_pool(session, account=account, run=run, reservation_id="crv_bridge_pool",
                amount_micro=1000, expires_at=now + timedelta(minutes=30))
            hold = Hold(pool.id, account.id, pool.amount_micro)
    else:
        hold = reserve(account_id=one.id, amount_micro=1000, purpose="synthetic.bridge", workspace_id="ws_bridge")
    if kind == "unknown":
        protect_outcome(hold)
    if kind != "live":
        with d["factory"]() as session:
            row = session.get(CreditReservation, hold.reservation_id)
            row.expires_at = naive_utc() - timedelta(hours=1)
            session.commit()
    with d["factory"].begin() as session:
        result = _cutover(session)
    assert result["deferred_accounts"] == [{"account_id": one.id, "holds": [
        {"id": hold.reservation_id, "status": "reconciling" if kind == "unknown" else "active", "amount_micro": 1000}]}]
    assert result["transferred_micro"] == credits_to_micro(5000)
    assert wallet_phase() == "person"
    assert wallet_for("per_bridge_owner").granted_micro == credits_to_micro(5000)
    assert get_account_by_id(one.id).reserved_micro == 1000
    assert get_account_by_id(two.id).status == "frozen"
    assert _publication_fk(d)["constrained_columns"] == ["credit_account_id"]
    _assert_integrity(d)


def test_cutover_failure_after_helper_rolls_back_transfer_and_private_phase(bridge_db):
    from app.credits.policy import wallet_phase
    d = bridge_db
    _seed(d)
    _legacy_accounts()
    before = _snapshot(d)
    with pytest.raises(RuntimeError, match="synthetic precommit crash"), d["factory"].begin() as session:
        _cutover(session)
        assert wallet_phase(session) == "person"
        raise RuntimeError("synthetic precommit crash")
    assert _snapshot(d) == before
    assert _publication_fk(d)["constrained_columns"] == ["credit_account_id", "workspace_id", "venture_id"]
    _assert_integrity(d)


def test_same_bridge_person_charge_replay_and_uncertain_custody_survive_restart(bridge_db, monkeypatch):
    from app.credits._txn import naive_utc
    from app.credits.accounts import account_for_venture, ensure_account, get_account_by_id, wallet_for
    from app.credits.errors import CreditError
    from app.credits.gate import charge_sync
    from app.credits.migration import migrate_wallets
    from app.credits.policy import WALLET_PHASE_KEY, set_policy, wallet_phase
    from app.credits.reserve import release, reserve, settle, sweep_expired
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Person
    from app.state import set_factory
    from app.tenancy import bind
    from app.ventures.quota import assert_can_create_venture
    d = bridge_db
    _seed(d, actor=True)
    one, two = _legacy_accounts()
    legacy_hold = reserve(account_id=one.id, amount_micro=1000, purpose="synthetic.legacy-receipt", workspace_id="ws_bridge")
    legacy_receipt = settle(legacy_hold, actual_micro=600)
    before = _snapshot(d)
    with d["factory"]() as session:
        people = set(session.scalars(select(Person.id)))
    # Historical migrations seed a local Founder/Desk, even in an empty DB.
    unfunded_people = people - {"per_bridge_owner"}
    with d["factory"].begin() as session:
        result = _cutover(session)
    assert result == {"wallets": len(people), "transferred_micro": credits_to_micro(10000) - 600,
                      "expired_holds": 0, "deferred_accounts": []}
    assert _publication_fk(d)["constrained_columns"] == ["credit_account_id"]
    owner, actor = wallet_for("per_bridge_owner"), wallet_for("per_bridge_member")
    assert owner.granted_micro == credits_to_micro(10000) - 600
    assert actor.granted_micro == credits_to_micro(5000)
    with d["factory"]() as session:
        net = session.scalar(select(func.sum(CreditAccount.granted_micro - CreditAccount.spent_micro)))
        assert net == sum(row.granted_micro - row.spent_micro for row in before["accounts"]) + credits_to_micro(5000 * len(unfunded_people))
        initial_keys = set(session.scalars(select(CreditLedgerEntry.idempotency_key).where(
            CreditLedgerEntry.idempotency_key.like("initial:person:%"))))
        assert initial_keys == {"initial:person:" + person_id for person_id in unfunded_people}
    assert account_for_venture("ven_bridge_one", acting_person_id="per_bridge_member").id == actor.id
    with bind(workspace_id="ws_bridge", venture_id="ven_bridge_one", actor="per_bridge_member"):
        with charge_sync(workspace_id="ws_bridge", venture_id="ven_bridge_one", purpose="synthetic.member",
                         estimate_micro=1000, request_id="synthetic-member-receipt") as metered:
            assert metered.account_id == actor.id
            metered.record_micro(700)
    assert get_account_by_id(owner.id).spent_micro == 0
    assert get_account_by_id(actor.id).spent_micro == 700
    with charge_sync(workspace_id="ws_bridge", venture_id="ven_bridge_one", purpose="synthetic.owner",
                     estimate_micro=1000, request_id="synthetic-owner-receipt") as paid:
        assert paid.account_id == owner.id
        paid.record_micro(500)
    assert paid.settle_result.actual_micro == 500
    assert settle(legacy_hold, actual_micro=999).ledger_id == legacy_receipt.ledger_id
    with charge_sync(workspace_id="ws_bridge", venture_id="ven_bridge_two", purpose="synthetic.uncertain",
                     estimate_micro=1000, request_id="synthetic-unknown-receipt") as uncertain:
        uncertain.protect_outcome()  # Synthetic receipt boundary; no external I/O.
    held_snapshot = _snapshot(d)
    # Process restart and old env configuration still read the committed DB phase.
    d["engine"].dispose()
    set_factory(sessionmaker(d["engine"], expire_on_commit=False))
    monkeypatch.setenv("SUPYMEM_FOUNDER_WALLET_PHASE", "legacy")
    monkeypatch.setenv("FOUNDER_WALLET_PHASE", "legacy")
    assert wallet_phase() == "person"
    assert ensure_account("ven_bridge_one").id == ensure_account("ven_bridge_two").id == owner.id
    assert_can_create_venture("per_bridge_owner")
    assert _snapshot(d) == held_snapshot
    with pytest.raises(ValueError, match="drained financial migration"):
        set_policy(WALLET_PHASE_KEY, "legacy", updated_by="synthetic")
    with d["factory"]() as session:
        session.get(CreditReservation, uncertain.hold.reservation_id).expires_at = naive_utc() - timedelta(hours=1)
        session.commit()
    assert sweep_expired() == 0
    with pytest.raises(CreditError, match="requires_reconciliation"):
        release(uncertain.hold)
    assert get_account_by_id(owner.id).reserved_micro == 1000
    final = settle(uncertain.hold, actual_micro=400)
    assert settle(uncertain.hold, actual_micro=999).ledger_id == final.ledger_id
    assert get_account_by_id(owner.id).reserved_micro == 0
    assert get_account_by_id(owner.id).spent_micro == 900
    with d["factory"].begin() as session:
        assert migrate_wallets(session)["transferred_micro"] == 0
    after = _snapshot(d)
    assert after["ledger"][:len(before["ledger"])] == before["ledger"]
    with d["factory"]() as session:
        transfers = list(session.scalars(select(CreditLedgerEntry.amount_micro).where(CreditLedgerEntry.purpose == "wallet.migration")))
        assert len(transfers) == 4 and sum(transfers) == 0
        assert session.scalar(select(func.count(CreditLedgerEntry.id)).where(
            CreditLedgerEntry.idempotency_key == "initial:person:per_bridge_owner")) == 0
        for old in (one, two):
            row = session.get(CreditAccount, old.id)
            assert row.status == "frozen" and row.granted_micro - row.spent_micro == row.reserved_micro == 0
    # Actual PG append-only triggers, not a mocked ledger verification verdict.
    for statement in ("UPDATE credit_ledger SET reason='tampered' WHERE id=:id", "DELETE FROM credit_ledger WHERE id=:id"):
        with pytest.raises(DBAPIError), d["engine"].begin() as conn:
            conn.execute(text(statement), {"id": legacy_receipt.ledger_id})
    _assert_integrity(d)


def _stub_event(order, event_id, kind):
    from app.payments.service import handle_webhook
    from app.payments.stub import sign_webhook
    body = json.dumps({"id": event_id, "type": kind, "data": {
        "provider_order_id": order["provider_order_id"], "amount_minor": order["amount_minor"],
        "currency": order["currency"], "provider_ref": "synthetic-stub-receipt"}}).encode()
    return handle_webhook(provider_name="stub", raw_body=body, headers=sign_webhook(body, event_id=event_id))


def test_delayed_stub_checkout_refund_and_pending_admin_grant_follow_wallet(bridge_db, monkeypatch):
    from app.admin.grants import apply_grant, decide_approval
    from app.credits._txn import naive_utc
    from app.credits.accounts import get_account_by_id, wallet_for
    from app.credits.policy import set_policy
    from app.credits.units import credits_to_micro
    from app.db.models import ConsumerUser, CreditGrantApproval, CreditOrder, PaymentEvent
    from app.payments.service import create_order
    d = bridge_db
    _seed(d)
    one, _ = _legacy_accounts()
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    set_policy("topup_provider_by_currency", {"USD": "stub"}, updated_by="synthetic")
    order = create_order(venture_id="ven_bridge_one", workspace_id="ws_bridge", person_id="per_bridge_owner",
                         sku="cr_500", currency="USD")
    pending = apply_grant(credits=50001, reason="Synthetic dual control", actor_id="synthetic-finance",
        actor_email="finance@example.test", actor_role="super_admin", idempotency_key="synthetic-admin",
        account_id=one.id)
    assert pending["pending_approval"] and not pending["applied"]
    now = naive_utc()
    with d["factory"].begin() as session:
        session.add(ConsumerUser(id="cusr_bridge", cognito_sub="synthetic-bridge-sub", email="bridge-owner@example.test",
            person_id="per_bridge_owner", credit_account_id=one.id, plan="trial", plan_started_at=now,
            plan_expires_at=now + timedelta(days=14), status="active", created_at=now, updated_at=now))
    before = _snapshot(d)
    with d["factory"].begin() as session:
        _cutover(session)
    wallet = wallet_for("per_bridge_owner")
    with d["factory"]() as session:
        assert session.get(CreditOrder, order["order_id"]).account_id == wallet.id
        assert session.get(CreditGrantApproval, pending["approval_id"]).target_account_id == wallet.id
        assert session.get(ConsumerUser, "cusr_bridge").credit_account_id == wallet.id
    assert _stub_event(order, "synthetic-paid-delayed", "payment.succeeded")["status"] == "ok"
    assert _stub_event(order, "synthetic-paid-delayed", "payment.succeeded")["status"] == "duplicate"
    assert _stub_event(order, "synthetic-paid-distinct", "payment.succeeded")["status"] == "ok"
    assert get_account_by_id(wallet.id).granted_micro == credits_to_micro(10500)
    assert _stub_event(order, "synthetic-refund-delayed", "payment.refunded")["status"] == "ok"
    assert _stub_event(order, "synthetic-refund-distinct", "payment.refunded")["status"] == "ok"
    assert get_account_by_id(wallet.id).spent_micro == credits_to_micro(500)
    approved = decide_approval(pending["approval_id"], actor_id="synthetic-second-admin", approve=True)
    assert approved["applied"] and approved["target_account_id"] == wallet.id
    assert get_account_by_id(wallet.id).granted_micro == credits_to_micro(60501)
    with d["factory"]() as session:
        assert session.get(CreditOrder, order["order_id"]).status == "refunded"
        assert all(event.account_id == wallet.id for event in session.scalars(select(PaymentEvent)))
    assert _snapshot(d)["ledger"][:len(before["ledger"])] == before["ledger"]
    assert get_account_by_id(one.id).granted_micro - get_account_by_id(one.id).spent_micro == 0
    _assert_integrity(d)
