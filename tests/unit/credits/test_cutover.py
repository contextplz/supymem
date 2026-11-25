"""Cutover uses the real ledger and one transaction, never synthetic receipts."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.unit.credits.test_founder_wallet import founder, legacy
from tests.unit.credits.test_wallet_bridge import phase_marker


@pytest.fixture
def sources(credits_env):
    from app.credits._txn import session_factory
    founder("per_deferred", "ven_deferred", "ws_deferred")
    founder("per_ready", "ven_ready", "ws_ready")
    founder("per_new", "ven_new", "ws_new")
    phase_marker("legacy")
    with session_factory()() as session:
        legacy(session, "per_deferred", "ven_deferred", "ws_deferred", "cra_deferred", grant=4000)
        legacy(session, "per_ready", "ven_ready", "ws_ready", "cra_ready", grant=6000)
        session.commit()


@pytest.mark.parametrize("close", ["settle", "release"])
def test_deferred_hold_keeps_custody_then_transfers_once(sources, close):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import ensure_account, get_account_by_id, wallet_for
    from app.credits.errors import CreditError
    from app.credits.ledger import verify_chain
    from app.credits.migration import migrate_wallets
    from app.credits.policy import wallet_phase
    from app.credits.reconcile import reconcile
    from app.credits.reserve import protect_outcome, release, reserve, settle
    from app.credits.units import credits_to_micro
    from app.db.models import CreditLedgerEntry, CreditReservation
    hold = reserve(account_id="cra_deferred", amount_micro=100, purpose="test")
    protect_outcome(hold)
    with session_factory()() as session:
        session.get(CreditReservation, hold.reservation_id).expires_at = naive_utc() - timedelta(days=1)
        session.commit()
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq)))
    with session_factory()() as session:
        report = migrate_wallets(session)
        session.commit()
    assert report["transferred_micro"] == 6000 and report["expired_holds"] == 0
    assert report["deferred_accounts"] == [{"account_id": "cra_deferred", "holds": [
        {"id": hold.reservation_id, "status": "reconciling", "amount_micro": 100}]}]
    assert wallet_phase() == "person"
    deferred = wallet_for("per_deferred")
    assert deferred.granted_micro == ensure_account("ven_deferred").granted_micro == 0
    assert wallet_for("per_new").granted_micro == credits_to_micro(5000)
    assert get_account_by_id("cra_deferred").reserved_micro == 100
    with pytest.raises(CreditError, match="provider_outcome_requires_reconciliation"):
        release(hold)
    if close == "settle":
        assert settle(hold, actual_micro=40).account_id == "cra_deferred"
        settle(hold, actual_micro=40)
    else:
        release(hold, definitive=True)
        release(hold, definitive=True)
    with session_factory()() as session:
        assert migrate_wallets(session)["transferred_micro"] == (3960 if close == "settle" else 4000)
        session.commit()
        assert migrate_wallets(session)["transferred_micro"] == 0
        session.commit()
        after = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq)))
        assert after[:len(before)] == before
        keys = list(session.scalars(select(CreditLedgerEntry.idempotency_key)))
        assert keys.count("migration:cra_deferred:out") == keys.count("migration:cra_deferred:in") == 1
        assert "initial:person:per_deferred" not in keys
        for account_id in ("cra_deferred", "cra_ready", deferred.id):
            assert verify_chain(account_id) == (True, None)
            assert reconcile(account_id).ok
    assert wallet_for("per_deferred").granted_micro == (3960 if close == "settle" else 4000)


@pytest.mark.parametrize("apply", [False, True])
def test_cutover_reports_uncommitted_checks_and_dry_run_rolls_back(sources, capsys, apply):
    from app.credits._txn import session_factory
    from app.credits.accounts import get_account_by_id
    from app.credits.cutover import main
    from app.credits.policy import wallet_phase
    from app.db.models import CreditAccount, CreditLedgerEntry
    with session_factory()() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq)))
    assert main(["--apply" if apply else "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["committed"] == apply
    assert report["wallets"] == 3 and report["transferred_micro"] == 10000
    assert len(report["accounts"]) == 5
    assert all(check["verify_chain"] and check["reconcile"] for check in report["accounts"])
    assert wallet_phase() == ("person" if apply else "legacy")
    assert get_account_by_id("cra_ready").status == ("frozen" if apply else "active")
    if not apply:
        with session_factory()() as session:
            assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "person")) is None
            assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq))) == before


@pytest.mark.parametrize("fault", ["chain", "ledger", "reserved"])
def test_cutover_refuses_integrity_failure_and_rolls_back(sources, capsys, monkeypatch, fault):
    from app.credits._txn import session_factory
    from app.credits.cutover import main
    from app.credits.policy import wallet_phase
    from app.db.models import CreditAccount
    if fault == "chain":
        monkeypatch.setattr("app.credits.ledger.compute_entry_hash", lambda *args: "broken")
    else:
        with session_factory()() as session:
            account = session.get(CreditAccount, "cra_ready")
            if fault == "ledger":
                account.granted_micro += 1
            else:
                account.reserved_micro = 1
            session.commit()
    assert main(["--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and not report["committed"]
    assert wallet_phase() == "legacy"
    with session_factory()() as session:
        assert session.get(CreditAccount, "cra_ready").status == "active"
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "person")) is None


@pytest.mark.parametrize("kind", ["live", "pool_child"])
def test_live_and_allocated_pool_holds_are_deferred_without_releases(sources, kind):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.migration import migrate_wallets
    from app.credits.reconcile import reconcile
    from app.db.models import CreditAccount, CreditReservation
    now = naive_utc()
    with session_factory()() as session:
        account = session.get(CreditAccount, "cra_deferred")
        account.reserved_micro = 100
        session.add(CreditReservation(id="crv_pool", account_id=account.id, amount_micro=100,
            remaining_micro=60 if kind == "pool_child" else None, is_pool=kind == "pool_child",
            status="active", purpose="test", clamped=0, created_at=now,
            expires_at=now + timedelta(days=1) if kind == "live" else now - timedelta(days=1)))
        if kind == "pool_child":
            session.add(CreditReservation(id="crv_child", parent_id="crv_pool", account_id=account.id,
                amount_micro=40, status="active", purpose="test", clamped=0,
                created_at=now, expires_at=now - timedelta(days=1)))
        session.commit()
        report = migrate_wallets(session)
        assert report["transferred_micro"] == 6000 and report["expired_holds"] == 0
        assert len(report["deferred_accounts"][0]["holds"]) == (2 if kind == "pool_child" else 1)
        assert account.reserved_micro == 100 and account.granted_micro == 4000
        assert reconcile(account.id, transaction=session).ok
        session.commit()


def test_deferred_exempt_grant_counts_pending_balance_once(sources, monkeypatch):
    from app.credits._txn import session_factory
    from app.credits.accounts import wallet_for
    from app.credits.migration import migrate_wallets
    from app.credits.reserve import protect_outcome, release, reserve
    from app.credits.units import credits_to_micro
    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "per_deferred")
    hold = reserve(account_id="cra_deferred", amount_micro=100, purpose="test")
    protect_outcome(hold)
    with session_factory()() as session:
        migrate_wallets(session)
        session.commit()
    assert wallet_for("per_deferred").granted_micro == credits_to_micro(50000) - 4000
    release(hold, definitive=True)
    with session_factory()() as session:
        migrate_wallets(session)
        session.commit()
    assert wallet_for("per_deferred").granted_micro == credits_to_micro(50000)


def test_deferred_consumer_reference_routes_new_spend_without_losing_legacy_owner(sources):
    from app.credits._txn import naive_utc, session_factory
    from app.credits.accounts import wallet_for
    from app.credits.migration import migrate_wallets
    from app.credits.reserve import protect_outcome, release, reserve
    from app.db.models import ConsumerUser, CreditAccount, CreditReservation
    now = naive_utc()
    with session_factory()() as session:
        account = session.get(CreditAccount, "cra_deferred")
        account.owner_person_id = account.workspace_id = None
        session.add(ConsumerUser(id="cusr_deferred", cognito_sub="synthetic-deferred", email="deferred@example.test",
            person_id="per_deferred", credit_account_id=account.id, plan="trial", plan_started_at=now,
            plan_expires_at=now + timedelta(days=14), status="active", created_at=now, updated_at=now))
        session.commit()
    hold = reserve(account_id="cra_deferred", amount_micro=100, purpose="test")
    protect_outcome(hold)
    with session_factory()() as session:
        migrate_wallets(session)
        session.commit()
    wallet = wallet_for("per_deferred")
    with session_factory()() as session:
        assert session.get(ConsumerUser, "cusr_deferred").credit_account_id == wallet.id
        assert session.get(CreditReservation, hold.reservation_id).account_id == "cra_deferred"
        assert session.get(CreditAccount, "cra_deferred").owner_person_id == "per_deferred"
    release(hold, definitive=True)
    with session_factory()() as session:
        assert migrate_wallets(session)["transferred_micro"] == 4000
        session.commit()
    assert wallet_for("per_deferred").granted_micro == 4000


def test_reserved_mismatch_is_hard_error_even_without_an_owner(sources, capsys):
    from app.credits._txn import session_factory
    from app.credits.cutover import main
    from app.credits.policy import wallet_phase
    from app.db.models import CreditAccount
    with session_factory()() as session:
        account = session.get(CreditAccount, "cra_deferred")
        account.owner_person_id = account.workspace_id = None
        account.reserved_micro = 1
        session.commit()
    assert main(["--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "wallet_migration_reserved_balance_mismatch"
    assert not report["committed"] and wallet_phase() == "legacy"


def test_cutover_redacts_unexpected_errors(sources, capsys, monkeypatch):
    from app.credits.cutover import main
    from app.credits.policy import wallet_phase

    def fail(session):
        raise RuntimeError("postgresql://secret:password@host founder@example.test")

    monkeypatch.setattr("app.credits.cutover.migrate_wallets", fail)
    assert main(["--apply"]) == 1
    output = capsys.readouterr().out
    assert "password" not in output and "@" not in output
    assert json.loads(output)["error"] == "wallet_cutover_failed"
    assert wallet_phase() == "legacy"


def test_dry_run_module_bootstraps_without_committing(sources):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from app.credits._txn import session_factory
    from app.credits.policy import wallet_phase
    from app.db.models import CreditAccount, CreditLedgerEntry
    with session_factory()() as session:
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq)))
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "-m", "app.credits.cutover", "--dry-run"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(str(root / path) for path in
            ("services/company_os", "packages"))}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and not report["committed"]
    assert report["wallets"] == 3 and report["transferred_micro"] == 10000
    assert wallet_phase() == "legacy"
    with session_factory()() as session:
        assert session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "person")) is None
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash).order_by(CreditLedgerEntry.seq))) == before
