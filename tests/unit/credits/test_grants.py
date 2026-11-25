"""Grant / revoke movements and amount validation."""

from __future__ import annotations
from tests.credit_fixtures import ensure_test_wallet

import json

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.credits.units import MAX_GRANT_MICRO, credits_to_micro, validate_amount_micro


def test_grant_increases_available(account) -> None:
    from app.credits.accounts import balance, get_account_by_id
    from app.credits.grants import grant

    before = balance(account)
    extra = credits_to_micro(10)
    grant(
        account_id=account.id,
        amount_micro=extra,
        reason="top up",
        actor_kind="admin",
        actor_id="adm_test",
        idempotency_key="grant:once",
    )
    after = get_account_by_id(account.id)
    assert after is not None
    snap = balance(after)
    assert snap["available_micro"] == before["available_micro"] + extra
    assert snap["granted_micro"] == before["granted_micro"] + extra


def test_replayed_idempotency_key_grants_once(account) -> None:
    from app.credits.accounts import get_account_by_id
    from app.credits.grants import grant

    kwargs = {
        "account_id": account.id,
        "amount_micro": credits_to_micro(7),
        "reason": "once",
        "actor_kind": "admin",
        "actor_id": "adm_test",
        "idempotency_key": "grant:replay",
    }
    first = grant(**kwargs)  # type: ignore[arg-type]
    second = grant(**kwargs)  # type: ignore[arg-type]
    assert first.id == second.id
    after = get_account_by_id(account.id)
    assert after is not None
    assert after.granted_micro == account.granted_micro + credits_to_micro(7)


def test_revoke_floors_at_available_and_records_write_off(credits_env) -> None:
    from app.credits.accounts import balance, ensure_account, get_account_by_id
    from app.credits.grants import grant, revoke
    from app.credits.policy import set_policy

    set_policy("default_venture_credits", 0, updated_by="test")
    acc = ensure_test_wallet("ven_revoke")
    granted = credits_to_micro(5)
    grant(
        account_id=acc.id,
        amount_micro=granted,
        reason="seed",
        actor_kind="system",
        actor_id="test",
        idempotency_key="grant:revoke-seed",
    )
    requested = credits_to_micro(12)
    entry = revoke(
        account_id=acc.id,
        amount_micro=requested,
        reason="clawback",
        actor_id="adm_test",
        idempotency_key="revoke:floor",
    )
    meta = json.loads(entry.meta_json)
    assert meta["write_off"] == requested - granted
    assert entry.amount_micro == -granted
    after = get_account_by_id(acc.id)
    assert after is not None
    snap = balance(after)
    assert snap["available_micro"] == 0
    assert snap["spent_micro"] == granted
    assert snap["granted_micro"] == granted


@pytest.mark.parametrize(
    "value",
    [0, -1, -500, 2**63, MAX_GRANT_MICRO + 1, 1.5, "1", True, None],
)
def test_validate_amount_micro_rejects_negative_zero_oversized(value: object) -> None:
    with pytest.raises(ValueError):
        validate_amount_micro(value)
