"""Build budgets reserved once, then transferred to metered invocation holds.

All mutations are session-aware and use account → run → pool → child/call locks.
The caller commits the run/receipt/job transition in the same transaction. Pool
expiry never proves that a provider did not spend or that compute has stopped.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.credits.errors import AccountSuspended, CreditError, CreditsUnavailable, InsufficientCredits
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.accounts import account_can_spend
from app.db.models import CreditAccount, CreditReservation
from app.db.supymem_models import BuildRun

OPEN_CHILD = {"active", "open"}


@dataclass(frozen=True)
class LockedPool:
    account: CreditAccount
    run: BuildRun
    pool: CreditReservation


def lock_account(session: Session, account_id: str) -> CreditAccount:
    serialize_writes(session, key=account_id)
    account = session.scalar(select(CreditAccount).where(CreditAccount.id == account_id).with_for_update().execution_options(populate_existing=True))
    if account is None:
        raise CreditsUnavailable("build_credit_account_missing")
    return account


def lock_pool(session: Session, pool_id: str) -> LockedPool:
    # The first read discovers the immutable account key, without taking a row
    # lock. Every mutator must take the account lock before run/pool/call locks.
    account_id = session.scalar(select(CreditReservation.account_id).where(CreditReservation.id == pool_id))
    if account_id is None:
        raise CreditError("build_pool_missing")
    account = lock_account(session, account_id)
    run = session.scalar(select(BuildRun).where(BuildRun.reservation_id == pool_id).with_for_update().execution_options(populate_existing=True))
    pool = session.scalar(select(CreditReservation).where(CreditReservation.id == pool_id).with_for_update().execution_options(populate_existing=True))
    if run is None or pool is None or not pool.is_pool or run.runtime_version != 1:
        raise CreditError("build_pool_binding_invalid")
    if pool.workspace_id != run.workspace_id:
        raise CreditError("build_pool_scope_invalid")
    # Custody of an already funded pool survives membership revocation.
    if account.scope == "person":
        if pool.venture_id != run.venture_id:
            raise CreditError("build_pool_scope_invalid")
    elif account.scope != "venture" or account.venture_id != run.venture_id or account.workspace_id != run.workspace_id:
        raise CreditError("build_pool_scope_invalid")
    return LockedPool(account, run, pool)


def create_pool(session: Session, *, account: CreditAccount, run: BuildRun,
                reservation_id: str, amount_micro: int, expires_at: datetime) -> CreditReservation:
    """Caller holds account then run (or owns its uncommitted insertion)."""
    if type(amount_micro) is not int or amount_micro < 1:
        raise ValueError("invalid_build_budget")
    now = naive_utc()
    if naive_utc(expires_at) <= now:
        raise CreditError("build_budget_expired")
    if run.runtime_version != 1 or run.reservation_id or run.closed_at:
        raise CreditError("build_pool_binding_invalid")
    if not account_can_spend(session, account, run.workspace_id, run.venture_id):
        raise CreditError("build_pool_scope_invalid")
    if account.status != "active":
        raise AccountSuspended(account.id, account.status)
    available = account.granted_micro - account.spent_micro - account.reserved_micro
    if available < amount_micro:
        raise InsufficientCredits(account.id, available, amount_micro)
    pool = CreditReservation(id=reservation_id, account_id=account.id, is_pool=True,
        remaining_micro=amount_micro, amount_micro=amount_micro, status="active", purpose="build.run",
        workspace_id=run.workspace_id, venture_id=run.venture_id, request_id=run.id, clamped=0,
        expires_at=naive_utc(expires_at), created_at=now)
    account.reserved_micro += amount_micro
    account.version += 1
    account.updated_at = now
    run.reservation_id = reservation_id
    session.add(pool)
    session.flush()
    return pool


def allocate(session: Session, ctx: LockedPool, *, reservation_id: str, amount_micro: int,
             purpose: str, model: str | None = None, provider: str | None = None):
    from app.credits.reserve import Hold
    if type(amount_micro) is not int or amount_micro < 1:
        raise ValueError("invalid_build_invocation_budget")
    pool, account, run = ctx.pool, ctx.account, ctx.run
    if not account_can_spend(session, account, run.workspace_id, run.venture_id):
        raise CreditError("build_pool_scope_invalid")
    if account.status != "active":
        raise AccountSuspended(account.id, account.status)
    if pool.status != "active" or naive_utc(pool.expires_at) <= naive_utc() or run.closed_at or run.state in {"cancelling", "cancelled", "failed", "succeeded"}:
        raise CreditError("build_pool_not_accepting_calls")
    prior = session.get(CreditReservation, reservation_id)
    if prior is not None:
        if (prior.parent_id, prior.amount_micro, prior.purpose, prior.model, prior.provider) != (pool.id, amount_micro, purpose, model, provider):
            raise CreditError("build_invocation_budget_conflict")
        return Hold(prior.id, account.id, prior.amount_micro)
    if pool.remaining_micro < amount_micro:
        raise InsufficientCredits(account.id, pool.remaining_micro, amount_micro)
    pool.remaining_micro -= amount_micro
    session.add(CreditReservation(id=reservation_id, account_id=account.id, parent_id=pool.id,
        is_pool=False, remaining_micro=None, amount_micro=amount_micro, status="active", purpose=purpose,
        model=model, provider=provider, workspace_id=run.workspace_id, venture_id=run.venture_id, request_id=run.id,
        clamped=0, expires_at=pool.expires_at, created_at=naive_utc()))
    session.flush()
    return Hold(reservation_id, account.id, amount_micro)


def _child(session: Session, ctx: LockedPool, reservation_id: str) -> CreditReservation:
    child = session.scalar(select(CreditReservation).where(CreditReservation.id == reservation_id).with_for_update().execution_options(populate_existing=True))
    if child is None or child.is_pool or child.parent_id != ctx.pool.id or child.account_id != ctx.account.id:
        raise CreditError("build_child_binding_invalid")
    return child


def settle_child(session: Session, ctx: LockedPool, *, reservation_id: str, actual_micro: int,
                 prompt_tokens: int | None = None, completion_tokens: int | None = None,
                 metadata: dict | None = None):
    from app.credits.reserve import _result_from_settled
    if type(actual_micro) is not int or actual_micro < 0:
        raise ValueError("invalid_build_actual_usage")
    child = _child(session, ctx, reservation_id)
    key = "settle:" + child.id
    if child.status == "settled":
        return _result_from_settled(child, find_by_idempotency_key(key, session))
    if child.status not in OPEN_CHILD:
        raise CreditError("build_child_already_closed")
    # Never charge beyond the explicitly approved slice, even if a provider
    # violates its requested token cap. Preserve raw actual usage for review.
    charged = min(actual_micro, child.amount_micro)
    account, pool, run = ctx.account, ctx.pool, ctx.run
    account.reserved_micro -= charged
    account.spent_micro += charged
    account.version += 1
    account.updated_at = naive_utc()
    pool.remaining_micro += child.amount_micro - charged
    child.status, child.settled_micro, child.settled_at = "settled", charged, naive_utc()
    child.clamped = int(charged != actual_micro)
    run.credits_used += charged
    entry = append(session, account=account, kind="spend", amount_micro=-charged,
        reason="settled build invocation", actor_kind="system", actor_id="build",
        idempotency_key=key, purpose=child.purpose, model=child.model, provider=child.provider,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        reservation_id=child.id, request_id=run.id,
        meta={**(metadata or {}), "venture_id": run.venture_id, "workspace_id": run.workspace_id,
              "build_run_id": run.id, "pool_id": pool.id,
              "actual_micro": actual_micro, "hold_micro": child.amount_micro, "clamped": bool(child.clamped)})
    if child.clamped:
        pool.status = "reconciling"
        run.error_code = "build_provider_overage"
        mark_reconciliation(session, ctx, "build_provider_overage")
    session.flush()
    return _result_from_settled(child, entry)


def release_child(session: Session, ctx: LockedPool, *, reservation_id: str, no_external_call: bool):
    """A timed-out request is not proof of no spend. Only a known pre-call exit."""
    if not no_external_call:
        raise CreditError("build_usage_requires_reconciliation")
    child = _child(session, ctx, reservation_id)
    if child.status not in OPEN_CHILD:
        return
    child.status, child.settled_at = "released", naive_utc()
    ctx.pool.remaining_micro += child.amount_micro
    session.flush()


def absorb_platform_child(session: Session, ctx: LockedPool, *, reservation_id: str):
    """Release a founder hold while Supymem accepts an uncertain provider cost.

    This is deliberately separate from ``release_child``: an external request
    may have happened, so the release is a platform-funded customer credit and
    never evidence that provider usage was zero.  Only the platform-owned
    Foundry deployment is eligible; BYOK and arbitrary providers stay held for
    explicit reconciliation.
    """
    child = _child(session, ctx, reservation_id)
    if (child.provider, child.model) != ("azure_foundry", "DeepSeek-V4-Pro"):
        raise CreditError("build_platform_absorption_scope_invalid")
    if child.status not in OPEN_CHILD:
        return
    child.status, child.settled_at = "released", naive_utc()
    ctx.pool.remaining_micro += child.amount_micro
    session.flush()


def mark_reconciliation(session: Session, ctx: LockedPool, reason: str, *, actor="system") -> None:
    from app.analytics.error_events import closed_code
    logging.getLogger(__name__).warning("build_reconciliation_required", extra={
        "build_run_id": ctx.run.id, "error_code": closed_code(reason)})
    ctx.pool.status = "reconciling"
    spec = json.loads(ctx.run.spec or "{}")
    spec.update({"credentials_revoked": True, "reconciliation_required": True,
                 "reconciliation_reason": reason, "cleanup_state": spec.get("cleanup_state", "pending")})
    ctx.run.spec = json.dumps(spec, sort_keys=True)
    if ctx.run.closed_at is None and ctx.run.state not in {"failed", "cancelled", "succeeded", "rejected", "cancelling"}:
        from app.core.fsm import advance
        from app.db.supymem_models import BuildEvent
        advance(session, ctx.run, "cancelling", actor=actor, reason=reason)
        ctx.run.event_seq += 1
        session.add(BuildEvent(build_run_id=ctx.run.id, seq=ctx.run.event_seq, ts=naive_utc(), kind="state",
            payload=json.dumps({"message": "Build paused for cleanup and usage reconciliation.", "reason": reason, "actor": actor})))
    if reason != "founder_cancelled":
        ctx.run.error_code = reason


def close_pool(session: Session, ctx: LockedPool) -> bool:
    if ctx.pool.status in {"closed", "released", "expired"}:
        return False
    spec = json.loads(ctx.run.spec or "{}")
    from app.builder.lifecycle import managed, cleanup_complete
    if spec.get("cleanup_state") != "complete" or managed(spec) and not cleanup_complete(spec):
        raise CreditError("build_cleanup_unresolved")
    if session.scalar(select(CreditReservation.id).where(CreditReservation.parent_id == ctx.pool.id,
                                                       CreditReservation.status.in_(OPEN_CHILD)).limit(1)):
        raise CreditError("build_usage_requires_reconciliation")
    ctx.account.reserved_micro -= ctx.pool.remaining_micro
    ctx.account.version += 1
    ctx.account.updated_at = naive_utc()
    ctx.pool.remaining_micro = 0
    ctx.pool.status, ctx.pool.settled_at = "closed", naive_utc()
    ctx.run.closed_at = naive_utc()
    spec["reconciliation_required"] = False
    ctx.run.spec = json.dumps(spec, sort_keys=True)
    session.flush()
    return True


def pool_for_reservation(reservation_id: str) -> str | None:
    with session_factory()() as session:
        row = session.get(CreditReservation, reservation_id)
        if row is None:
            return None
        if row.is_pool:
            return row.id
        parent = session.get(CreditReservation, row.parent_id) if row.parent_id else None
        return parent.id if parent is not None and parent.is_pool else None


def settle_legacy_entry(hold, *, actual_micro: int, prompt_tokens=None, completion_tokens=None):
    """Generic callers may settle a child, but cannot spend a parent wholesale."""
    pool_id = pool_for_reservation(hold.reservation_id)
    with session_factory()() as session:
        ctx = lock_pool(session, pool_id)
        if ctx.account.id != hold.account_id or hold.reservation_id == pool_id:
            raise CreditError("build_hold_requires_invocation")
        result = settle_child(session, ctx, reservation_id=hold.reservation_id, actual_micro=actual_micro,
                              prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        session.commit()
        return result


def expire_pools(cutoff: datetime) -> int:
    with session_factory()() as session:
        ids = list(session.scalars(select(CreditReservation.id).where(CreditReservation.is_pool.is_(True),
            CreditReservation.status == "active", CreditReservation.expires_at <= cutoff).limit(200)))
    count = 0
    for pool_id in ids:
        with session_factory()() as session:
            ctx = lock_pool(session, pool_id)
            if ctx.pool.status == "active" and naive_utc(ctx.pool.expires_at) <= cutoff:
                mark_reconciliation(session, ctx, "build_budget_expired")
                count += 1
            session.commit()
    return count
