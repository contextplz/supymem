"""Fresh start changes available credits, not historical rows or hold custody."""
import json

import pytest
from sqlalchemy import select

from tests.unit.credits.test_founder_wallet import founder, legacy
from tests.unit.credits.test_wallet_bridge import phase_marker


@pytest.fixture
def wallets(credits_env, monkeypatch):
    from app.credits._txn import session_factory
    from app.credits.accounts import wallet_for, set_status
    from app.credits.grants import grant
    from app.credits.reserve import protect_outcome, reserve, settle
    from app.credits.units import credits_to_micro

    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "per_exempt,per_exempt2")
    rows = {}
    for name in ("low", "high", "exempt", "exempt2", "frozen"):
        founder("per_" + name, "ven_" + name, "ws_" + name)
        rows[name] = wallet_for("per_" + name)
    spent = reserve(account_id=rows["low"].id, amount_micro=credits_to_micro(200), purpose="test")
    settle(spent, actual_micro=credits_to_micro(200))
    grant(account_id=rows["high"].id, amount_micro=credits_to_micro(2000), reason="test grant",
          actor_kind="system", actor_id="system", idempotency_key="test:high")
    open_hold = reserve(account_id=rows["high"].id, amount_micro=credits_to_micro(100), purpose="test")
    protected = reserve(account_id=rows["high"].id, amount_micro=credits_to_micro(300), purpose="test")
    protect_outcome(protected)
    set_status(rows["frozen"].id, "frozen", actor_id="system", reason="test")
    with session_factory()() as session:
        from app.db.models import CreditLedgerEntry, CreditReservation
        before = list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)
                                      .order_by(CreditLedgerEntry.seq)))
        holds = list(session.execute(select(CreditReservation.id, CreditReservation.status,
            CreditReservation.amount_micro, CreditReservation.expires_at).where(
                CreditReservation.id.in_((open_hold.reservation_id, protected.reservation_id)))))
    return rows, before, holds


def test_dry_run_rollback_apply_deltas_holds_exempt_frozen_and_idempotency(wallets, capsys):
    from app.credits._txn import session_factory
    from app.credits.accounts import get_account_by_id, wallet_for
    from app.credits.reconcile import reconcile
    from app.credits.reset_balances import main, RESET_PREFIX
    from app.credits.units import credits_to_micro
    from app.db.models import CreditLedgerEntry, CreditReservation

    rows, before, holds = wallets
    assert main(["--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] and not preview["committed"]
    deltas = {row["account_id"]: row for row in preview["accounts"]}
    assert deltas[rows["low"].id]["delta"] == 200
    assert deltas[rows["high"].id]["before_available"] == 6600
    assert deltas[rows["high"].id]["delta"] == -1600
    assert deltas[rows["exempt"].id]["target"] == deltas[rows["exempt2"].id]["target"] == 50000
    assert deltas[rows["frozen"].id]["skipped"] == "frozen"
    with session_factory()() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)
                                    .order_by(CreditLedgerEntry.seq))) == before
    low = get_account_by_id(rows["low"].id)
    assert low.granted_micro - low.spent_micro - low.reserved_micro == credits_to_micro(4800)
    assert main(["--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["committed"] and applied["totals"]["touched"] == 4
    for name, row in rows.items():
        account = get_account_by_id(row.id)
        target = 50000 if name.startswith("exempt") else 5000
        assert account.granted_micro - account.spent_micro - account.reserved_micro == credits_to_micro(target)
        assert reconcile(row.id).ok
        assert wallet_for("per_" + name).granted_micro == account.granted_micro
    assert get_account_by_id(rows["high"].id).reserved_micro == credits_to_micro(400)
    assert main(["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["touched"] == 0
    with session_factory()() as session:
        assert list(session.execute(select(CreditReservation.id, CreditReservation.status,
            CreditReservation.amount_micro, CreditReservation.expires_at).where(
                CreditReservation.id.in_([hold.id for hold in holds])))) == holds
        entries = list(session.scalars(select(CreditLedgerEntry).order_by(CreditLedgerEntry.seq)))
        assert [(entry.id, entry.entry_hash) for entry in entries[:len(before)]] == before
        resets = [entry for entry in entries if entry.idempotency_key.startswith(RESET_PREFIX)]
        assert len(resets) == 4
        assert all(entry.kind == "adjust" and entry.reason == "fresh start reset"
                   and entry.purpose == "admin.reset" and entry.actor_kind == "system" for entry in resets)


@pytest.mark.parametrize("fault", ["chain", "ledger", "holds"])
def test_reset_refuses_corruption_and_rolls_back_every_wallet(wallets, capsys, monkeypatch, fault):
    from app.credits._txn import session_factory
    from app.credits.reset_balances import main, RESET_PREFIX
    from app.db.models import CreditAccount, CreditLedgerEntry

    rows, _, _ = wallets
    if fault == "chain":
        monkeypatch.setattr("app.credits.reconcile.verify_chain", lambda *args, **kwargs: (False, "corrupt"))
    else:
        with session_factory()() as session:
            account = session.get(CreditAccount, rows["exempt"].id)
            if fault == "ledger":
                account.granted_micro += 1
            else:
                account.reserved_micro = 1
            session.commit()
    assert main(["--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and not report["committed"]
    with session_factory()() as session:
        assert session.scalar(select(CreditLedgerEntry.id).where(
            CreditLedgerEntry.idempotency_key.startswith(RESET_PREFIX))) is None


@pytest.mark.parametrize("exempt", [False, True])
def test_deferred_cutover_rerun_reapplies_reset_target(credits_env, monkeypatch, capsys, exempt):
    from app.credits._txn import session_factory
    from app.credits.accounts import wallet_for
    from app.credits.cutover import main as cutover
    from app.credits.reconcile import reconcile
    from app.credits.reserve import protect_outcome, reserve, settle
    from app.credits.reset_balances import main
    from app.credits.units import credits_to_micro
    from app.db.models import CreditLedgerEntry

    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "per_late" if exempt else "")
    founder("per_late", "ven_late", "ws_late")
    phase_marker("legacy")
    with session_factory()() as session:
        legacy(session, "per_late", "ven_late", "ws_late", "cra_late", grant=credits_to_micro(9000))
        session.commit()
    hold = reserve(account_id="cra_late", amount_micro=credits_to_micro(100), purpose="test")
    protect_outcome(hold)
    assert cutover(["--apply"]) == 0
    capsys.readouterr()
    assert main(["--apply"]) == 0
    capsys.readouterr()
    settle(hold, actual_micro=credits_to_micro(40))
    assert cutover(["--apply"]) == 0
    capsys.readouterr()
    wallet = wallet_for("per_late")
    target = credits_to_micro(50000 if exempt else 5000)
    assert wallet.granted_micro - wallet.spent_micro - wallet.reserved_micro == target
    with session_factory()() as session:
        keys = list(session.scalars(select(CreditLedgerEntry.idempotency_key)))
        assert keys.count("migration:cra_late:in") == 1
        assert len([key for key in keys if key.startswith("reset:")]) == 2
    assert cutover(["--apply"]) == 0
    capsys.readouterr()
    assert wallet_for("per_late").granted_micro == wallet.granted_micro
    assert reconcile(wallet.id).ok and reconcile("cra_late").ok


def test_reset_rerun_does_not_remove_later_admin_grant(wallets, capsys):
    from app.credits.accounts import get_account_by_id
    from app.credits.grants import grant
    from app.credits.reset_balances import main
    from app.credits.units import credits_to_micro

    account = wallets[0]["low"]
    assert main(["--apply"]) == 0
    capsys.readouterr()
    grant(account_id=account.id, amount_micro=credits_to_micro(600), reason="later grant",
          actor_kind="admin", actor_id="admin", idempotency_key="later:grant")
    assert main(["--apply"]) == 0
    capsys.readouterr()
    current = get_account_by_id(account.id)
    assert current.granted_micro - current.spent_micro - current.reserved_micro == credits_to_micro(5600)


def test_reset_cli_bootstraps_and_dry_run_does_not_commit(wallets):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from app.credits._txn import session_factory
    from app.db.models import CreditLedgerEntry

    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "-m", "app.credits.reset_balances", "--dry-run"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(str(root / path) for path in
            ("services/company_os", "packages"))}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and not report["committed"] and report["totals"]["touched"] == 4
    with session_factory()() as session:
        assert list(session.execute(select(CreditLedgerEntry.id, CreditLedgerEntry.entry_hash)
                                    .order_by(CreditLedgerEntry.seq))) == wallets[1]


def test_reset_refuses_legacy_phase(wallets, capsys):
    from app.credits.reset_balances import main

    phase_marker("legacy")
    assert main(["--apply"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["committed"] and report["error"] == "wallet_reset_requires_person_phase"


def test_wallet_lookup_does_not_restore_pre_reset_starter_or_exempt_allowance(credits_env, monkeypatch, capsys):
    from app.credits._txn import session_factory
    from app.credits.accounts import wallet_for
    from app.credits.policy import invalidate
    from app.credits.reserve import reserve, settle
    from app.credits.reset_balances import main
    from app.credits.units import credits_to_micro
    from app.db.models import CreditPolicy

    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "")
    founder("per_old_starter", "ven_old_starter", "ws_old_starter")
    with session_factory()() as session:
        session.get(CreditPolicy, "default_venture_credits").value_json = "500"
        session.commit()
    invalidate()
    wallet_for("per_old_starter")
    monkeypatch.setenv("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "per_old_starter")
    with session_factory()() as session:
        session.get(CreditPolicy, "default_venture_credits").value_json = "5000"
        session.commit()
    invalidate()
    assert main(["--apply"]) == 0
    capsys.readouterr()
    wallet = wallet_for("per_old_starter")
    assert wallet.granted_micro == credits_to_micro(50000)
    hold = reserve(account_id=wallet.id, amount_micro=credits_to_micro(2000), purpose="test")
    settle(hold, actual_micro=credits_to_micro(2000))
    assert wallet_for("per_old_starter").granted_micro == wallet.granted_micro
