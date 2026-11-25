"""Reserve / settle / release, including the concurrent TOCTOU defence."""

from __future__ import annotations
from tests.credit_fixtures import ensure_test_wallet

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from core.clock import set_clock, utcnow


def test_reserve_settle_happy_path(account) -> None:
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import reserve, settle

    hold_amount = 10_000
    actual = 4_000
    hold = reserve(account_id=account.id, amount_micro=hold_amount, purpose="llm")
    mid = get_account_by_id(account.id)
    assert mid is not None
    assert mid.reserved_micro == hold_amount

    result = settle(hold, actual_micro=actual)
    assert result.actual_micro == actual
    assert result.remainder_micro == hold_amount - actual
    assert result.clamped is False

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.spent_micro == account.spent_micro + actual
    assert after.reserved_micro == 0


def test_reserve_insufficient_leaves_reserved_unchanged(account) -> None:
    from app.credits.accounts import balance, get_account_by_id
    from app.credits.errors import InsufficientCredits
    from app.credits.reserve import reserve

    snap = balance(account)
    too_much = snap["available_micro"] + 1  # type: ignore[operator]
    with pytest.raises(InsufficientCredits) as exc:
        reserve(account_id=account.id, amount_micro=int(too_much), purpose="llm")
    assert exc.value.code == "insufficient_credits"
    assert exc.value.available_micro == snap["available_micro"]
    assert exc.value.required_micro == too_much

    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == account.reserved_micro


def test_release_and_double_release_are_noop(account) -> None:
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import release, reserve

    hold = reserve(account_id=account.id, amount_micro=1_000, purpose="llm")
    release(hold)
    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    release(hold)
    again = get_account_by_id(account.id)
    assert again is not None
    assert again.reserved_micro == 0


def test_settle_is_idempotent(account) -> None:
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import reserve, settle

    hold = reserve(account_id=account.id, amount_micro=5_000, purpose="llm")
    first = settle(hold, actual_micro=2_000)
    second = settle(hold, actual_micro=2_000)
    assert first.ledger_id == second.ledger_id
    assert first.actual_micro == second.actual_micro == 2_000
    after = get_account_by_id(account.id)
    assert after is not None
    assert after.spent_micro == account.spent_micro + 2_000
    assert after.reserved_micro == 0


def test_settle_clamps_when_actual_exceeds_cover(credits_env) -> None:
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.grants import grant
    from app.credits.policy import set_policy
    from app.credits.reserve import reserve, settle
    from app.credits.units import credits_to_micro

    set_policy("default_venture_credits", 0, updated_by="test")
    acc = ensure_test_wallet("ven_clamp")
    grant(
        account_id=acc.id,
        amount_micro=credits_to_micro(1),
        reason="tiny",
        actor_kind="system",
        actor_id="test",
        idempotency_key="grant:clamp",
    )
    hold = reserve(account_id=acc.id, amount_micro=credits_to_micro(1), purpose="llm")
    result = settle(hold, actual_micro=credits_to_micro(5))
    assert result.clamped is True
    assert result.actual_micro == hold.amount_micro
    after = get_account_by_id(acc.id)
    assert after is not None
    assert after.spent_micro + after.reserved_micro <= after.granted_micro
    assert after.reserved_micro == 0
    assert after.spent_micro == credits_to_micro(1)


def test_sweep_expired_releases_stale_hold(account) -> None:
    from app.credits.accounts import get_account_by_id
    from app.credits.reserve import reserve, sweep_expired

    hold = reserve(account_id=account.id, amount_micro=1_000, purpose="llm")
    start = utcnow()
    set_clock(lambda: start + timedelta(seconds=10_000))
    try:
        n = sweep_expired()
    finally:
        set_clock(None)
    assert n == 1
    after = get_account_by_id(account.id)
    assert after is not None
    assert after.reserved_micro == 0
    assert hold.reservation_id


def test_concurrent_reserve_never_exceeds_granted(credits_env) -> None:
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.errors import InsufficientCredits
    from app.credits.grants import grant
    from app.credits.policy import set_policy
    from app.credits.reserve import reserve
    from app.credits.units import credits_to_micro

    set_policy("default_venture_credits", 0, updated_by="test")
    acc = ensure_test_wallet("ven_race")
    amount = credits_to_micro(1)
    fits = 10
    workers = 50
    grant(
        account_id=acc.id,
        amount_micro=fits * amount,
        reason="race budget",
        actor_kind="system",
        actor_id="test",
        idempotency_key="grant:race",
    )
    account_id = acc.id
    check_errors: list[str] = []

    def attempt() -> str:
        try:
            reserve(account_id=account_id, amount_micro=amount, purpose="llm")
            return "ok"
        except InsufficientCredits:
            return "short"
        except IntegrityError as exc:
            check_errors.append(repr(exc))
            return "check"
        except OperationalError as exc:
            check_errors.append(repr(exc))
            return "locked"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: attempt(), range(workers)))

    assert not check_errors, f"contention/CHECK errors: {check_errors}"
    successes = results.count("ok")
    assert successes == fits
    assert successes * amount <= fits * amount
    after = get_account_by_id(account_id)
    assert after is not None
    assert after.reserved_micro == successes * amount
    assert after.reserved_micro <= after.granted_micro
    assert after.spent_micro + after.reserved_micro <= after.granted_micro
    assert results.count("short") == workers - fits
