"""Credit accounts: get-or-create, balances, status, workspace lookup."""

from __future__ import annotations

import logging

from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from app.core.local_mode import is_local
from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.credits.errors import CreditsUnavailable
from app.credits.policy import WALLET_PHASE_KEY, default_venture_credits, person_wallet_enabled, wallet_phase
from app.credits.units import credits_to_micro, format_credits, format_usd, micro_to_credits
from app.db.models import CreditAccount
from core.ids import new_id  # type: ignore[import-not-found]

log = logging.getLogger("app.credits.accounts")

_ALLOWED_STATUS = frozenset({"active", "suspended", "frozen"})


def _expunge_account(session: Session, account: CreditAccount) -> CreditAccount:
    session.expunge(account)
    return account


def _acting_person(session: Session, actor: str | None) -> str | None:
    from app.db.models import Person, PersonIdentity
    if not actor or actor in {"system", "local", "none"}:
        return None
    if session.get(Person, actor):
        return actor
    return session.scalar(select(PersonIdentity.person_id).where(PersonIdentity.subject == actor))


def _owner(session: Session, venture_id: str, workspace_id: str | None = None, *,
           legacy_fallback: bool = True) -> tuple[str | None, str | None]:
    from app.db.models import ConsumerUser, Venture, Workspace
    if venture_id.startswith("consumer:"):
        person_id = session.scalar(select(ConsumerUser.person_id).where(
            ConsumerUser.cognito_sub == venture_id.removeprefix("consumer:")))
        if person_id:
            return person_id, None
    venture = session.get(Venture, venture_id)
    wid = venture.workspace_id if venture else workspace_id
    workspace = session.get(Workspace, wid) if wid else None
    if workspace:
        return workspace.owner_id, workspace.id
    legacy = session.scalar(select(CreditAccount).where(CreditAccount.venture_id == venture_id)) if legacy_fallback else None
    return (legacy.owner_person_id, legacy.workspace_id) if legacy else (None, wid)


def _payer(session: Session, owner_id: str | None, workspace_id: str | None,
           acting_person_id: str | None = None) -> str | None:
    from app.db.models import WorkspaceMember
    from app.tenancy import current_or_none
    scope = current_or_none()
    actor = _acting_person(session, acting_person_id or (scope.actor if scope else None))
    if actor and workspace_id:
        role = session.scalar(select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.person_id == actor))
        if actor == owner_id or role in {"owner", "admin", "maintainer", "contributor"}:
            return actor
    return owner_id


def account_for_venture(venture_id: str, *, acting_person_id: str | None = None,
                        session: Session | None = None) -> CreditAccount | None:
    """Charge a spend-capable acting member, otherwise the venture owner.

    Read-only lookup: signup/ensure_account creates wallets. Historical account
    ids remain resolvable for settlement, but new spend never selects them.
    """
    if is_local():
        return None
    def resolve(s: Session) -> CreditAccount | None:
        if not person_wallet_enabled(s):
            return s.scalar(select(CreditAccount).where(
                CreditAccount.scope == "venture", CreditAccount.venture_id == venture_id))
        owner_id, wid = _owner(s, venture_id)
        person_id = _payer(s, owner_id, wid, acting_person_id)
        if not person_id:
            return None
        return s.scalar(select(CreditAccount).where(
            CreditAccount.scope == "person", CreditAccount.owner_person_id == person_id))
    if session is not None:
        return resolve(session)
    with session_factory()() as owned:
        row = resolve(owned)
        return _expunge_account(owned, row) if row else None


def get_account(venture_id: str) -> CreditAccount | None:
    """Compatibility entry point; all existing venture callers resolve wallets."""
    return account_for_venture(venture_id)


def account_can_spend(session: Session, account: CreditAccount, workspace_id: str,
                      venture_id: str, *, receipt_only: bool = False) -> bool:
    """Validate new spend; old funded custody is an explicit receipt-only path."""
    from app.db.models import WorkspaceMember
    owner_id, wid = _owner(session, venture_id)
    if wid != workspace_id:
        return False
    if account.scope == "venture":
        return (account.workspace_id == wid and account.venture_id == venture_id
                and (receipt_only or not person_wallet_enabled(session)))
    if account.scope != "person" or not account.owner_person_id:
        return False
    if not receipt_only and not person_wallet_enabled(session):
        return False
    if account.owner_person_id == owner_id:
        return True
    return session.scalar(select(WorkspaceMember.id).where(
        WorkspaceMember.workspace_id == wid, WorkspaceMember.person_id == account.owner_person_id,
        WorkspaceMember.role.in_(("owner", "admin", "maintainer", "contributor")))) is not None



def get_account_by_id(account_id: str) -> CreditAccount | None:
    factory = session_factory()
    with factory() as session:
        row = session.get(CreditAccount, account_id)
        if row is None:
            return None
        return _expunge_account(session, row)


def balance(account: CreditAccount) -> dict[str, object]:
    granted = account.granted_micro
    spent = account.spent_micro
    reserved = account.reserved_micro
    available = granted - spent - reserved
    return {
        "granted_micro": granted,
        "spent_micro": spent,
        "reserved_micro": reserved,
        "available_micro": available,
        "granted_credits": format_credits(granted),
        "spent_credits": format_credits(spent),
        "reserved_credits": format_credits(reserved),
        "available_credits": format_credits(available),
        "granted_usd": format_usd(granted),
        "spent_usd": format_usd(spent),
        "reserved_usd": format_usd(reserved),
        "available_usd": format_usd(available),
    }


def _credits_number(micro: int) -> float:
    return float(micro_to_credits(int(micro)))


def founder_snapshot(
    venture_id: str,
    *,
    workspace_id: str | None = None,
    owner_person_id: str | None = None,
) -> dict[str, object] | None:
    """Founder-facing balance. Micro-credits stay server-side (spec §7.4).

    Ensures the founder wallet when identity is known. Missing identity returns
    None so the dashboard can offer a recoverable credits request.
    """
    if is_local():
        return None
    try:
        account = get_account(venture_id)
        if account is None:
            account = ensure_account(
                venture_id,
                workspace_id=workspace_id,
                owner_person_id=owner_person_id,
            )
    except Exception:
        log.exception("founder credit snapshot failed for %s", venture_id)
        return None
    return {
        "granted": _credits_number(account.granted_micro),
        "available": _credits_number(
            account.granted_micro - account.spent_micro - account.reserved_micro
        ),
        "spent": _credits_number(account.spent_micro),
        "reserved": _credits_number(account.reserved_micro),
        "status": account.status,
    }


def founder_snapshot_for_workspace(workspace_id: str) -> dict[str, object] | None:
    if is_local():
        return None
    account = account_for_workspace(workspace_id)
    if not person_wallet_enabled():
        if account is not None and account.venture_id:
            return founder_snapshot(account.venture_id, workspace_id=workspace_id)
        from app.workspaces.resolve import venture_for_workspace
        venture = venture_for_workspace(workspace_id)
        return founder_snapshot(venture.id, workspace_id=workspace_id) if venture else None
    if not account:
        return None
    return {"granted": _credits_number(account.granted_micro),
            "available": _credits_number(account.granted_micro - account.spent_micro - account.reserved_micro),
            "spent": _credits_number(account.spent_micro), "reserved": _credits_number(account.reserved_micro),
            "status": account.status}


def exempt_person_ids() -> set[str]:
    import os
    return {p.strip() for p in os.environ.get("SUPYMEM_VENTURE_LIMIT_EXEMPT_PERSON_IDS", "").split(",") if p.strip()}


def _wallet(session: Session, person_id: str, *, grant_initial: bool = True) -> CreditAccount:
    from app.db.models import Person
    from app.credits.ledger import append, find_by_idempotency_key
    if not person_id or session.scalar(select(Person.id).where(Person.id == person_id)) is None:
        raise CreditsUnavailable("founder_identity_required")
    serialize_writes(session, key="wallet:" + person_id)
    account_id = session.scalar(select(CreditAccount.id).where(
        CreditAccount.scope == "person", CreditAccount.owner_person_id == person_id))
    if account_id:
        # All money paths take the account advisory lock before its row lock.
        serialize_writes(session, key=account_id)
        account = session.scalar(select(CreditAccount).where(CreditAccount.id == account_id)
            .with_for_update().execution_options(populate_existing=True))
    else:
        account = None
    if account is None:
        now = naive_utc()
        account = CreditAccount(id=new_id("cra"), scope="person", owner_person_id=person_id,
            granted_micro=0, spent_micro=0, reserved_micro=0, status="active", version=0,
            created_at=now, updated_at=now)
        serialize_writes(session, key=account.id)
        session.add(account)
        session.flush()
        if grant_initial:
            amount = credits_to_micro(default_venture_credits())
            account.granted_micro += amount
            account.version += 1
            append(session, account=account, kind="grant", amount_micro=amount,
                reason="initial founder allowance", actor_kind="system", actor_id="system",
                idempotency_key=f"initial:person:{person_id}", purpose="grant")
    from app.credits.reset_balances import RESET_PREFIX
    grant_initial = grant_initial and find_by_idempotency_key(RESET_PREFIX + account.id, session) is None
    if grant_initial:
        initial = find_by_idempotency_key(f"initial:person:{person_id}", session)
        allowance = credits_to_micro(default_venture_credits())
        key = f"launch-allowance:person:{person_id}:{allowance}"
        if initial is not None and initial.amount_micro < allowance and not find_by_idempotency_key(key, session):
            amount = allowance - initial.amount_micro
            account.granted_micro += amount
            account.version += 1
            append(session, account=account, kind="grant", amount_micro=amount,
                reason="launch starter-credit increase", actor_kind="system", actor_id="system",
                idempotency_key=key, purpose="grant")
    if grant_initial and person_id in exempt_person_ids():
        key = f"grant:exempt-50k:{person_id}"
        if find_by_idempotency_key(key, session) is None:
            amount = max(0, credits_to_micro(50000) - account.granted_micro)
            account.granted_micro += amount
            account.version += 1
            append(session, account=account, kind="grant", amount_micro=amount,
                reason="exempt founder allowance", actor_kind="system", actor_id="system",
                idempotency_key=key, purpose="grant")
    return account


def wallet_for(person_id: str, *, transaction: Session | None = None) -> CreditAccount | None:
    """One person allowance after cutover; before it, read an owned legacy account."""
    if is_local():
        return None
    def resolve(session: Session) -> CreditAccount:
        if _phase_for_ensure(session) == "legacy":
            from app.db.models import ConsumerUser, Workspace
            row = session.scalar(select(CreditAccount).where(
                CreditAccount.scope == "venture",
                (CreditAccount.owner_person_id == person_id)
                | CreditAccount.workspace_id.in_(select(Workspace.id).where(Workspace.owner_id == person_id))
                | CreditAccount.id.in_(select(ConsumerUser.credit_account_id).where(ConsumerUser.person_id == person_id)))
                .order_by(CreditAccount.created_at, CreditAccount.id).limit(1))
            if row is None:
                raise CreditsUnavailable("founder wallet is not active yet")
            return row
        return _wallet(session, person_id)
    if transaction is not None:
        return resolve(transaction)
    with session_factory()() as session:
        account = resolve(session)
        session.commit()
        session.refresh(account)
        return _expunge_account(session, account)


def ensure_account(venture_id: str, *, workspace_id: str | None = None,
                   owner_person_id: str | None = None) -> CreditAccount | None:
    if not (venture_id or "").strip():
        raise ValueError("venture_id is required")
    if is_local():
        return None
    with session_factory()() as session:
        if _phase_for_ensure(session) == "legacy":
            account = _legacy_account(session, venture_id, workspace_id, owner_person_id)
        else:
            owner_id, wid = _owner(session, venture_id, workspace_id)
            person_id = _payer(session, owner_id or owner_person_id, wid)
            account = _wallet(session, person_id or "")
        session.commit()
        session.refresh(account)
        return _expunge_account(session, account)


def _phase_for_ensure(session: Session) -> str:
    # ponytail: ensures serialize globally; per-person locks remain the money
    # locks. Remove this bootstrap lock only when every database has a marker.
    serialize_writes(session, key=WALLET_PHASE_KEY)
    phase = wallet_phase(session)
    from app.db.models import CreditPolicy
    if phase == "person" and session.scalar(select(CreditPolicy.key).where(CreditPolicy.key == WALLET_PHASE_KEY)) is None:
        session.add(CreditPolicy(key=WALLET_PHASE_KEY, value_json='"person"', updated_at=naive_utc(),
                                 updated_by="fresh-wallet-bootstrap"))
        session.flush()
    return phase


def _legacy_account(session: Session, venture_id: str, workspace_id: str | None,
                    owner_person_id: str | None) -> CreditAccount:
    """Keep legacy custody, with one new starter entitlement per actual founder."""
    from app.credits.grants import grant
    from app.credits.ledger import find_by_idempotency_key
    owner, actual_workspace = _owner(session, venture_id, workspace_id)
    if workspace_id and actual_workspace and workspace_id != actual_workspace:
        raise CreditsUnavailable("credit account workspace mismatch")
    if owner and owner_person_id and owner_person_id != owner and _acting_person(session, owner_person_id) != owner:
        raise CreditsUnavailable("credit account owner mismatch")
    if owner:
        owner_person_id = owner
    workspace_id = actual_workspace or workspace_id
    serialize_writes(session, key=venture_id)
    account_id = session.scalar(select(CreditAccount.id).where(
        CreditAccount.scope == "venture", CreditAccount.venture_id == venture_id))
    if account_id:
        serialize_writes(session, key=account_id)
        account = session.scalar(select(CreditAccount).where(CreditAccount.id == account_id)
            .with_for_update().execution_options(populate_existing=True))
        if workspace_id and account.workspace_id and workspace_id != account.workspace_id:
            raise CreditsUnavailable("credit account workspace mismatch")
        if owner_person_id and account.owner_person_id and owner_person_id != account.owner_person_id:
            person = _acting_person(session, owner_person_id)
            if not person or person != _acting_person(session, account.owner_person_id):
                raise CreditsUnavailable("credit account owner mismatch")
        if workspace_id and account.workspace_id is None:
            account.workspace_id = workspace_id
        if owner_person_id and account.owner_person_id is None:
            account.owner_person_id = owner_person_id
    else:
        now = naive_utc()
        account = CreditAccount(id=new_id("cra"), scope="venture", venture_id=venture_id,
            workspace_id=workspace_id, owner_person_id=owner_person_id, status="active",
            granted_micro=0, spent_micro=0, reserved_micro=0, version=0, created_at=now, updated_at=now)
        serialize_writes(session, key=account.id)
        session.add(account)
    session.flush()
    allowance = credits_to_micro(default_venture_credits())
    if allowance > 0:
        initial_key = f"initial:{venture_id}"
        initial = find_by_idempotency_key(initial_key, session)
        if (initial is not None and initial.amount_micro < allowance) or (
                initial is None and _legacy_starter_allowed(session, account)):
            grant(account_id=account.id, amount_micro=allowance if initial is None else allowance - initial.amount_micro,
                  reason="initial venture allowance" if initial is None else "launch starter-credit increase",
                  actor_kind="system", actor_id="system", transaction=session,
                  idempotency_key=initial_key if initial is None else f"launch-allowance:{venture_id}:{default_venture_credits()}")
    return account


def _legacy_starter_allowed(session: Session, account: CreditAccount) -> bool:
    """A new empty account cannot reset an existing founder's funded allowance.

    The phase lock in ensure_account serializes first-allowance claims. Separate
    legacy accounts and their access rules stay intact until the wallet bridge.
    """
    from app.db.models import ConsumerUser, Workspace
    owner, _ = _owner(session, account.venture_id, account.workspace_id, legacy_fallback=False)
    person = _acting_person(session, owner)
    if not person:
        return False
    owners = {owner, person}
    resources = (
        CreditAccount.workspace_id.in_(select(Workspace.id).where(Workspace.owner_id.in_(owners)))
        | CreditAccount.venture_id.in_(select(literal("consumer:") + ConsumerUser.cognito_sub)
            .where(ConsumerUser.person_id == person))
        | CreditAccount.id.in_(select(ConsumerUser.credit_account_id).where(ConsumerUser.person_id == person)))
    funded = resources | CreditAccount.owner_person_id.in_(owners)
    if session.scalar(select(CreditAccount.id).where(funded, CreditAccount.granted_micro > 0).limit(1)):
        return False
    first = session.scalar(select(CreditAccount.id).where(CreditAccount.scope == "venture", resources)
        .order_by(CreditAccount.created_at, CreditAccount.id).limit(1))
    return first == account.id



def set_status(
    account_id: str, status: str, *, actor_id: str, reason: str
) -> CreditAccount:
    """Suspend / resume / freeze. Not a value movement — no ledger row."""
    if status not in _ALLOWED_STATUS:
        raise ValueError(f"status must be one of {sorted(_ALLOWED_STATUS)}")
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=account_id)
        row = session.get(CreditAccount, account_id)
        if row is None:
            raise CreditsUnavailable(f"unknown credit account {account_id}")
        row.status = status
        row.updated_at = naive_utc()
        row.version = row.version + 1
        session.commit()
        session.refresh(row)
        log.info(
            "credit account %s status -> %s by %s (%s)",
            account_id,
            status,
            actor_id,
            reason,
        )
        return _expunge_account(session, row)


def account_for_workspace(workspace_id: str) -> CreditAccount | None:
    if is_local():
        return None
    """Preserve legacy workspace lookup until the database activates wallets."""
    from app.db.models import Venture, Workspace
    wid = (workspace_id or "").strip()
    if not wid:
        return None
    with session_factory()() as session:
        from app.tenancy import current_or_none
        scope = current_or_none()
        if scope and scope.workspace_id == wid and scope.venture_id:
            if _owner(session, scope.venture_id)[1] != wid:
                return None
            row = account_for_venture(scope.venture_id, session=session)
            return _expunge_account(session, row) if row else None
        if not person_wallet_enabled(session):
            vid = session.scalar(select(Venture.id).where(Venture.workspace_id == wid).limit(1))
            row = session.scalar(select(CreditAccount).where(
                CreditAccount.scope == "venture", CreditAccount.venture_id == vid)) if vid else None
            if row is None:
                row = session.scalar(select(CreditAccount).where(
                    CreditAccount.scope == "venture", CreditAccount.workspace_id == wid).limit(1))
            return _expunge_account(session, row) if row else None
        workspace = session.get(Workspace, wid)
        if workspace is None:
            return None
        person_id = _payer(session, workspace.owner_id, wid)
        row = session.scalar(select(CreditAccount).where(
            CreditAccount.scope == "person", CreditAccount.owner_person_id == person_id))
        return _expunge_account(session, row) if row else None
