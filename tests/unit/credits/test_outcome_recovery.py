"""Unknown provider outcomes keep real credit holds, including across expiry."""
from datetime import timedelta

import pytest


def test_protected_timeout_survives_expiry_and_settles_once(account):
    from core.clock import utcnow
    from app.credits.accounts import get_account_by_id
    from app.credits.errors import CreditError
    from app.credits.gate import charge_sync
    from app.credits.reconcile import reconcile
    from app.credits.reserve import release, settle, sweep_expired
    with pytest.raises(TimeoutError):
        with charge_sync(account_id=account.id, purpose="journaled.call", estimate_micro=5000) as meter:
            meter.protect_outcome()
            raise TimeoutError("unknown response")
    assert sweep_expired(utcnow() + timedelta(days=2)) == 0
    assert get_account_by_id(account.id).reserved_micro == 5000
    assert reconcile(account.id).ok
    with pytest.raises(CreditError, match="requires_reconciliation"):
        release(meter.hold)
    first = settle(meter.hold, actual_micro=1250)
    replay = settle(meter.hold, actual_micro=1250)
    assert first.ledger_id == replay.ledger_id
    after = get_account_by_id(account.id)
    assert after.spent_micro == account.spent_micro + 1250 and after.reserved_micro == 0
    assert reconcile(account.id).ok


def test_definitive_rejection_can_release_protected_hold(account):
    from app.credits.accounts import get_account_by_id
    from app.credits.gate import charge_sync
    with pytest.raises(ValueError):
        with charge_sync(account_id=account.id, purpose="journaled.call", estimate_micro=5000) as meter:
            meter.protect_outcome()
            meter.confirm_unspent()
            raise ValueError("definitively rejected")
    after = get_account_by_id(account.id)
    assert after.spent_micro == account.spent_micro and after.reserved_micro == 0


def test_crash_leaves_protection_and_closed_hold_cannot_be_reused(account):
    from core.clock import utcnow
    from app.credits.errors import CreditError
    from app.credits.reserve import protect_outcome, reserve, settle, sweep_expired
    hold = reserve(account_id=account.id, purpose="crash", amount_micro=5000)
    protect_outcome(hold)  # Simulate process loss: no context-manager exit.
    assert sweep_expired(utcnow() + timedelta(days=2)) == 0
    settle(hold, actual_micro=1000)
    with pytest.raises(CreditError, match="no_longer_available"):
        protect_outcome(hold)


def test_wrong_account_or_amount_cannot_mutate_hold(account):
    from app.credits.errors import CreditError
    from app.credits.reserve import Hold, protect_outcome, release, reserve, settle
    hold = reserve(account_id=account.id, purpose="scope", amount_micro=5000)
    for forged in (Hold(hold.reservation_id, "different_account", 5000),
                   Hold(hold.reservation_id, account.id, 1)):
        for action in (protect_outcome, release, lambda h: settle(h, actual_micro=1)):
            with pytest.raises(CreditError, match="scope_mismatch"):
                action(forged)
    release(hold)


def test_expiry_winner_prevents_later_provider_start(account):
    from core.clock import utcnow
    from app.credits.errors import CreditError
    from app.credits.reserve import protect_outcome, reserve, sweep_expired
    hold = reserve(account_id=account.id, purpose="expired", amount_micro=5000)
    assert sweep_expired(utcnow() + timedelta(days=2)) == 1
    with pytest.raises(CreditError, match="no_longer_available"):
        protect_outcome(hold)


def test_settlement_failure_retains_hold_for_receipt_recovery(account, monkeypatch):
    from core.clock import utcnow
    from app.credits import gate
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import settle, sweep_expired
    def unavailable(*args, **kwargs):
        raise RuntimeError("database settlement temporarily unavailable")
    from app.credits.errors import CreditsUnavailable
    monkeypatch.setattr(gate, "settle", unavailable)
    with pytest.raises(CreditsUnavailable, match="reconciliation"):
        with gate.charge_sync(account_id=account.id, purpose="receipt.saved", estimate_micro=5000) as meter:
            meter.protect_outcome()
            meter.record_micro(1250)
    assert meter.settle_result is None
    assert sweep_expired(utcnow() + timedelta(days=2)) == 0
    assert get_account_by_id(account.id).reserved_micro == 5000
    settle(meter.hold, actual_micro=1250)
    assert get_account_by_id(account.id).reserved_micro == 0
