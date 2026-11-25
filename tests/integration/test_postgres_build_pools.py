"""Real migrated PostgreSQL accounting: one pool, many concurrent invocation slices."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.integration.test_postgres_auth_security import concurrent, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.fixture()
def build_pool(journey_db):
    from app.credits import pools
    from app.credits.grants import grant
    from app.db.models import CreditAccount
    from app.db.supymem_models import BuildRun, FounderApp
    from core.clock import utcnow
    from core.ids import new_id
    d = journey_db
    aid, rid, pid, cid = new_id("app"), new_id("bld"), new_id("crv"), new_id("cra")
    with d["factory"]() as session:
        session.add(CreditAccount(id=cid, workspace_id=d["wid"], venture_id=d["vid"], scope="venture",
            owner_person_id=d["actor"], granted_micro=0, spent_micro=0, reserved_micro=0,
            status="active", version=0, created_at=utcnow(), updated_at=utcnow()))
        session.add(FounderApp(id=aid, workspace_id=d["wid"], venture_id=d["vid"], slug="pool-" + uuid.uuid4().hex,
                              name="Pool test", kind="landing", created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    grant(account_id=cid, amount_micro=5000, reason="isolated PostgreSQL fixture", actor_kind="system",
          actor_id="test", idempotency_key="fixture:" + cid)
    with d["factory"]() as session:
        account = pools.lock_account(session, cid)
        run = BuildRun(id=rid, app_id=aid, workspace_id=d["wid"], venture_id=d["vid"], trigger="founder",
                       agent="openhands", state="queued", runtime_version=1, created_at=utcnow(), spec="{}")
        session.add(run)
        pools.create_pool(session, account=account, run=run, reservation_id=pid, amount_micro=1000,
                          expires_at=utcnow() + timedelta(minutes=30))
        session.commit()
    yield {**d, "app_id": aid, "run_id": rid, "pool_id": pid, "account_id": cid}


def allocate(d, child="child", amount=300):
    from app.credits import pools
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        hold = pools.allocate(session, ctx, reservation_id=child + d["run_id"], amount_micro=amount,
                              purpose="build.model", model="DeepSeek-V4-Pro", provider="foundry")
        session.commit()
        return hold


def snapshot(d):
    from app.db.models import CreditAccount, CreditReservation
    with d["factory"]() as session:
        account = session.get(CreditAccount, d["account_id"])
        pool = session.get(CreditReservation, d["pool_id"])
        children = list(session.scalars(select(CreditReservation).where(CreditReservation.parent_id == pool.id)))
        outstanding = sum(c.amount_micro for c in children if c.status in {"active", "open"})
        assert account.reserved_micro == pool.remaining_micro + outstanding
        return account.reserved_micro, account.spent_micro, pool.remaining_micro, pool.status


def test_pool_allocation_transfers_existing_hold_without_double_reservation(build_pool):
    d = build_pool
    assert snapshot(d)[:3] == (1000, 0, 1000)
    allocate(d)
    assert snapshot(d)[:3] == (1000, 0, 700)


def test_concurrent_pool_allocations_cannot_oversubscribe(build_pool):
    from app.credits.errors import InsufficientCredits
    d = build_pool
    def attempt():
        try:
            return allocate(d, uuid.uuid4().hex).reservation_id
        except InsufficientCredits:
            return None
    result = concurrent(attempt)
    assert sum(r is not None for r in result) == 3
    assert snapshot(d)[:3] == (1000, 0, 100)


def test_concurrent_duplicate_slice_and_settlement_spend_once(build_pool):
    from app.credits import pools
    from app.db.models import CreditLedgerEntry
    d = build_pool
    holds = concurrent(lambda: allocate(d))
    assert len({h.reservation_id for h in holds}) == 1
    assert snapshot(d)[:3] == (1000, 0, 700)
    def settle():
        with d["factory"]() as session:
            ctx = pools.lock_pool(session, d["pool_id"])
            result = pools.settle_child(session, ctx, reservation_id=holds[0].reservation_id, actual_micro=100)
            session.commit()
            return result.ledger_id
    assert len(set(concurrent(settle))) == 1
    assert snapshot(d)[:3] == (900, 100, 900)
    with d["factory"]() as session:
        rows = list(session.scalars(select(CreditLedgerEntry).where(CreditLedgerEntry.reservation_id == holds[0].reservation_id)))
        assert len(rows) == 1 and rows[0].amount_micro == -100


def test_unknown_call_cannot_release_hold_and_expiry_preserves_it(build_pool):
    from app.credits import pools
    from app.credits.errors import CreditError
    from app.credits.reserve import release, sweep_expired
    from app.db.models import CreditReservation
    from app.db.supymem_models import BuildRun
    from core.clock import utcnow
    d = build_pool
    hold = allocate(d)
    with pytest.raises(CreditError, match="reconciliation"):
        release(hold)
    with d["factory"]() as session:
        session.get(CreditReservation, d["pool_id"]).expires_at = utcnow() - timedelta(seconds=1)
        session.get(CreditReservation, hold.reservation_id).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    sweep_expired()
    assert snapshot(d) == (1000, 0, 700, "reconciling")
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        assert run.state == "cancelling" and run.closed_at is None
        assert json.loads(run.spec)["credentials_revoked"] is True
        ctx = pools.lock_pool(session, d["pool_id"])
        with pytest.raises(CreditError, match="not_accepting"):
            pools.allocate(session, ctx, reservation_id="late", amount_micro=1, purpose="build.model")


def test_definite_no_call_release_then_cleanup_releases_remaining_pool(build_pool):
    from app.credits import pools
    from app.credits.errors import CreditError
    d = build_pool
    hold = allocate(d)
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        with pytest.raises(CreditError, match="reconciliation"):
            pools.release_child(session, ctx, reservation_id=hold.reservation_id, no_external_call=False)
        pools.release_child(session, ctx, reservation_id=hold.reservation_id, no_external_call=True)
        with pytest.raises(CreditError, match="cleanup_unresolved"):
            pools.close_pool(session, ctx)
        ctx.run.spec = '{"cleanup_state":"complete"}'
        assert pools.close_pool(session, ctx)
        assert not pools.close_pool(session, ctx)
        session.commit()
    assert snapshot(d) == (0, 0, 0, "closed")


def test_provider_overage_is_preserved_but_never_charged_beyond_slice(build_pool):
    from app.credits import pools
    from app.db.models import CreditLedgerEntry
    d = build_pool
    hold = allocate(d)
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        result = pools.settle_child(session, ctx, reservation_id=hold.reservation_id, actual_micro=350)
        session.commit()
        assert result.actual_micro == 300 and result.clamped
        entry = session.get(CreditLedgerEntry, result.ledger_id)
        assert json.loads(entry.meta_json)["actual_micro"] == 350
    assert snapshot(d) == (700, 300, 700, "reconciling")


def test_rollback_restores_allocation_and_pool_counters(build_pool):
    from app.credits import pools
    d = build_pool
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        pools.allocate(session, ctx, reservation_id="rolled-back", amount_micro=700, purpose="build.model")
        session.rollback()
    assert snapshot(d)[:3] == (1000, 0, 1000)


def test_cleanup_cannot_close_with_unresolved_child(build_pool):
    from app.credits import pools
    from app.credits.errors import CreditError
    d = build_pool
    allocate(d)
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        ctx.run.spec = '{"cleanup_state":"complete"}'
        with pytest.raises(CreditError, match="reconciliation"):
            pools.close_pool(session, ctx)
    assert snapshot(d)[:3] == (1000, 0, 700)


def test_generic_settle_is_pool_aware_and_cannot_spend_parent(build_pool):
    from app.credits.reserve import settle, Hold
    from app.credits.errors import CreditError
    d = build_pool
    hold = allocate(d)
    result = settle(hold, actual_micro=100)
    assert result.actual_micro == 100
    assert snapshot(d)[:3] == (900, 100, 900)
    with pytest.raises(CreditError, match="requires_invocation"):
        settle(Hold(d["pool_id"], d["account_id"], 1000), actual_micro=1000)


def test_scoped_call_fk_and_single_open_run_are_database_constraints(build_pool):
    from app.db.supymem_models import BuildModelCall, BuildRun
    from core.clock import utcnow
    d = build_pool
    with d["factory"]() as session:
        session.add(BuildModelCall(id="foreign-" + d["run_id"], build_run_id=d["run_id"], workspace_id="foreign",
            venture_id=d["vid"], invocation_key="one", request_digest="a" * 64, sequence=1,
            purpose="build.model", state="allocated", generation=1, attempt=0, provider="foundry",
            model="DeepSeek-V4-Pro", pricing_json="{}", ceiling_micro=10, created_at=utcnow(), updated_at=utcnow()))
        with pytest.raises(IntegrityError):
            session.commit()
    with d["factory"]() as session:
        session.add(BuildRun(id="duplicate-" + d["run_id"], app_id=d["app_id"], workspace_id=d["wid"],
            venture_id=d["vid"], trigger="founder", agent="openhands", runtime_version=1))
        with pytest.raises(IntegrityError):
            session.commit()


def test_pool_identity_and_original_amount_are_immutable_in_database(build_pool):
    from app.db.models import CreditReservation
    from sqlalchemy.exc import DBAPIError
    d = build_pool
    child = allocate(d)
    for rid in (d["pool_id"], child.reservation_id):
        with d["factory"]() as session:
            row = session.get(CreditReservation, rid)
            row.amount_micro += 1
            with pytest.raises(DBAPIError, match="identity_immutable"):
                session.commit()
    assert snapshot(d)[:3] == (1000, 0, 700)


def test_cleanup_reconciliation_preserves_completed_execution_result(build_pool):
    from app.builder.reconcile import reconcile_held_builds
    from app.credits import pools
    from app.db.supymem_models import BuildRun
    d = build_pool
    with d["factory"]() as session:
        ctx = pools.lock_pool(session, d["pool_id"])
        ctx.run.state = "succeeded"
        ctx.run.spec = '{"cleanup_state":"complete"}'
        pools.mark_reconciliation(session, ctx, "build_budget_expired")
        session.commit()
    assert reconcile_held_builds()["closed"] >= 1
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        assert run.state == "succeeded" and run.closed_at is not None
    assert snapshot(d) == (0, 0, 0, "closed")
