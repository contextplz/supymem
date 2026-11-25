"""Conserve legacy balances with paired append-only transfers during maintenance.

Stop API/worker spend before running. Accounts with protected or live holds are
deferred; only expired, ordinary unprotected holds are released automatically.
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.credits._txn import naive_utc, serialize_writes
from app.credits.accounts import _wallet, exempt_person_ids
from app.credits.errors import CreditError
from app.credits.ledger import append, find_by_idempotency_key
from app.credits.reset_balances import RESET_PREFIX, reset_account
from app.credits.units import credits_to_micro
from app.db.models import CreditAccount, CreditReservation, ConsumerUser, CreditOrder, CreditGrantApproval, CreditPolicy, Person, Workspace


def migrate_wallets(session: Session) -> dict[str, object]:
    """Caller owns the single transaction. Reruns never transfer twice."""
    serialize_writes(session, key="founder-wallet-migration")
    from app.credits.policy import WALLET_PHASE_KEY
    serialize_writes(session, key=WALLET_PHASE_KEY)
    account_ids = list(session.scalars(select(CreditAccount.id).where(CreditAccount.scope == "venture")
                                      .order_by(CreditAccount.id)))
    accounts = []
    for account_id in account_ids:
        serialize_writes(session, key=account_id)
        accounts.append(session.scalar(select(CreditAccount).where(CreditAccount.id == account_id)
            .with_for_update().execution_options(populate_existing=True)))
    now = naive_utc()
    owners = {}
    deferred = {}
    # Preflight all source accounts before modifying balances or holds.
    for account in accounts:
        holds = list(session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == account.id,
            CreditReservation.status.in_(("open", "active", "reconciling")))))
        if sum(hold.remaining_micro if hold.is_pool else hold.amount_micro for hold in holds) != account.reserved_micro:
            raise CreditError("wallet_migration_reserved_balance_mismatch")
        owner = account.owner_person_id
        if not owner and account.workspace_id:
            owner = session.scalar(select(Workspace.owner_id).where(Workspace.id == account.workspace_id))
        if not owner:
            owner = session.scalar(select(ConsumerUser.person_id).where(ConsumerUser.credit_account_id == account.id))
        if not owner or session.scalar(select(Person.id).where(Person.id == owner)) is None:
            if account.granted_micro or account.reserved_micro:
                raise CreditError("wallet_migration_owner_required")
            continue
        owners[account.id] = owner
        if any(hold.status == "reconciling" or hold.is_pool or hold.parent_id or naive_utc(hold.expires_at) > now for hold in holds):
            deferred[account.id] = {"account_id": account.id, "holds": [
                {"id": hold.id, "status": hold.status, "amount_micro": hold.amount_micro}
                for hold in sorted(holds, key=lambda hold: hold.id)]}
    transferred = 0
    released = 0
    wallets = {}
    for account in accounts:
        if account.id not in owners:
            continue
        owner = owners[account.id]
        wallet = wallets.get(owner)
        if wallet is None:
            wallet = _wallet(session, owner, grant_initial=False)
            serialize_writes(session, key=wallet.id)
            wallets[owner] = wallet
        for user in session.scalars(select(ConsumerUser).where(ConsumerUser.credit_account_id == account.id)):
            account.owner_person_id = owner
            user.credit_account_id = wallet.id
        key = "migration:" + account.id
        if find_by_idempotency_key(key + ":out", session):
            continue
        legacy_status = account.status
        status_rank = {"active": 0, "suspended": 1, "frozen": 2}
        if status_rank.get(legacy_status, 2) > status_rank.get(wallet.status, 2):
            wallet.status = legacy_status
        if account.id in deferred:
            continue
        holds = list(session.scalars(select(CreditReservation).where(
            CreditReservation.account_id == account.id, CreditReservation.status.in_(("open", "active")))))
        for hold in holds:
            account.reserved_micro -= hold.amount_micro
            hold.status = "expired"
            hold.settled_at = now
            append(session, account=account, kind="release", amount_micro=0,
                   reason="expired unprotected hold before wallet migration", actor_kind="system", actor_id="system",
                   idempotency_key=key + ":release:" + hold.id, reservation_id=hold.id)
            released += 1
        amount = account.granted_micro - account.spent_micro
        account.spent_micro += amount
        account.status = "frozen"
        account.version += 1
        account.updated_at = now
        wallet.granted_micro += amount
        wallet.version += 1
        wallet.updated_at = now
        meta = {"legacy_account_id": account.id, "venture_id": account.venture_id,
                "workspace_id": account.workspace_id, "owner_person_id": owner,
                "legacy_status": legacy_status, "legacy_granted_micro": account.granted_micro, "legacy_spent_micro": account.spent_micro - amount}
        append(session, account=account, kind="adjust", amount_micro=-amount,
               reason="migrate_out", actor_kind="system", actor_id="system",
               idempotency_key=key + ":out", purpose="wallet.migration", meta=meta)
        append(session, account=wallet, kind="adjust", amount_micro=amount,
               reason="migrate_in", actor_kind="system", actor_id="system",
               idempotency_key=key + ":in", purpose="wallet.migration", meta=meta)
        reset = find_by_idempotency_key(RESET_PREFIX + wallet.id, session)
        if reset is not None:
            reset_account(session, wallet, key=RESET_PREFIX + wallet.id + ":" + key,
                          target_micro=json.loads(reset.meta_json)["target_micro"])
        transferred += amount
        # These mutable references route delayed checkout/refund/admin mutations.
        # Original value movements and their account ids remain append-only.
        for order in session.scalars(select(CreditOrder).where(CreditOrder.account_id == account.id)):
            order.account_id = wallet.id
        for approval in session.scalars(select(CreditGrantApproval).where(
                CreditGrantApproval.target_account_id == account.id, CreditGrantApproval.status == "pending")):
            approval.target_account_id = wallet.id
    # Existing people with no legacy credit row receive their first allowance.
    policy = session.get(CreditPolicy, "default_venture_credits")
    allowance = json.loads(policy.value_json) if policy else 5000
    if type(allowance) is not int or allowance < 0:
        raise CreditError("wallet_migration_invalid_allowance")
    for person_id in session.scalars(select(Person.id)):
        if person_id not in wallets:
            existing = session.scalar(select(CreditAccount.id).where(
                CreditAccount.scope == "person", CreditAccount.owner_person_id == person_id))
            wallet = _wallet(session, person_id, grant_initial=False)
            wallets[person_id] = wallet
            if existing is None:
                amount = credits_to_micro(allowance)
                wallet.granted_micro += amount
                append(session, account=wallet, kind="grant", amount_micro=amount,
                    reason="initial founder allowance", actor_kind="system", actor_id="system",
                    idempotency_key=f"initial:person:{person_id}", purpose="grant")
    for person_id, wallet in wallets.items():
        if person_id in exempt_person_ids():
            key = f"grant:exempt-50k:{person_id}"
            if not find_by_idempotency_key(key, session) and not find_by_idempotency_key(RESET_PREFIX + wallet.id, session):
                pending = sum(legacy.granted_micro - legacy.spent_micro for legacy in accounts
                              if legacy.id in deferred and owners[legacy.id] == person_id)
                amount = max(0, credits_to_micro(50000) - wallet.granted_micro - pending)
                wallet.granted_micro += amount
                append(session, account=wallet, kind="grant", amount_micro=amount,
                       reason="exempt founder allowance", actor_kind="system", actor_id="system",
                       idempotency_key=key, purpose="grant")
    # This authoritative marker is part of the same value-transfer transaction.
    phase = session.get(CreditPolicy, WALLET_PHASE_KEY)
    if phase is None:
        session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"person"', updated_at=now,
                                 updated_by="wallet-value-transfer"))
    else:
        phase.value_json, phase.updated_at, phase.updated_by = '"person"', now, "wallet-value-transfer"
    session.flush()
    return {"wallets": len(wallets), "transferred_micro": transferred, "expired_holds": released,
            "deferred_accounts": list(deferred.values())}
