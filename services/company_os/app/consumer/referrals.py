"""Authenticated onboarding invitations. Caller owns the encompassing transaction."""
from __future__ import annotations
import os
import secrets
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.db.supymem_models import BetaCode, BetaCodeRedemption, PersonReferralCode, BetaCodeAllowanceChange, ReferralAdmission
from core.ids import new_id
from core.clock import utcnow

THRESHOLD_CREDITS = 2000
REWARD_CREDITS = 2000

class ReferralDenied(ValueError):
    pass

def normalize(raw: str | None) -> str:
    value = (raw or "").strip().upper()
    return value if len(value) == 8 and value.isascii() and value.isalnum() else ""

def beta_max_founders() -> int:
    """Closed-beta seat cap (non-legacy, non-founder admissions via invite)."""
    try:
        return int(os.environ.get("SUPYMEM_BETA_MAX_FOUNDERS", "20"))
    except ValueError:
        return 20

def seats_taken(session) -> int:
    return session.scalar(select(func.count()).select_from(BetaCodeRedemption)) or 0

def mint(session, *, owner: str, label: str, max_uses: int) -> BetaCode:
    for _ in range(10):
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        if session.get(BetaCode, code) is None:
            row = BetaCode(code=code, owner_person_id=owner, label=label, active=True, max_uses=max_uses,
                           created_at=datetime.now(timezone.utc))
            session.add(row)
            session.flush()
            return row
    raise RuntimeError("could not allocate beta code")

def used(session, code: str) -> int:
    return session.scalar(select(func.count()).select_from(BetaCodeRedemption).where(BetaCodeRedemption.code == code)) or 0

def download_code_failure(raw: str) -> str | None:
    from app.state import db
    code = normalize(raw)
    with db()() as session:
        row = session.get(BetaCode, code) if code else None
        if row is None:
            return "mistyped"
        if not row.active:
            return "disabled"
        if row.max_uses is not None and used(session, code) >= row.max_uses:
            return "exhausted"
    return None

def provision(session, person_id: str) -> BetaCode:
    if session.get_bind().dialect.name == "postgresql":
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"referral-person:{person_id}")
    mapping = session.get(PersonReferralCode, person_id)
    if mapping:
        return session.get(BetaCode, mapping.code)
    # Keep the founder's original code and any legacy owner code unchanged.
    row = session.scalar(select(BetaCode).where(BetaCode.owner_person_id == person_id).order_by(BetaCode.created_at, BetaCode.code).limit(1))
    if row is None:
        for _ in range(10):
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            try:
                with session.begin_nested():
                    row = BetaCode(code=code, owner_person_id=person_id, label="Your invitations", active=True, max_uses=2, created_at=datetime.now(timezone.utc))
                    session.add(row)
                    session.flush()
                break
            except IntegrityError:
                row = None
        if row is None:
            raise RuntimeError("could not allocate invitation code")
    session.add(PersonReferralCode(person_id=person_id, code=row.code))
    session.flush()
    return row

def redeem(session, *, code: str, person_id: str, user_id: str) -> None:
    code = normalize(code)
    row = session.scalar(select(BetaCode).where(BetaCode.code == code).with_for_update())
    prior = session.get(BetaCodeRedemption, person_id)
    if prior:
        if prior.code == code and prior.user_id == user_id:
            return
        raise ReferralDenied("already_onboarded")
    if row is None or not row.active:
        raise ReferralDenied("invalid_invite_code")
    if row.owner_person_id == person_id:
        raise ReferralDenied("self_invitation")
    if row.max_uses is not None and used(session, code) >= row.max_uses:
        raise ReferralDenied("invite_code_exhausted")
    session.add(BetaCodeRedemption(person_id=person_id, code=code, user_id=user_id, created_at=datetime.now(timezone.utc)))
    session.flush()

def payload(session, row: BetaCode) -> dict:
    count = used(session, row.code)
    remaining = None if row.max_uses is None else max(0, row.max_uses - count)
    return {"code": row.code, "label": row.label, "active": row.active, "max_uses": row.max_uses,
            "successful_invitations": count, "remaining": remaining,
            "status": "disabled" if not row.active else "exhausted" if remaining == 0 else "available",
            "invite_url": "https://app.supymem.com/?invite=" + row.code}

def change_allowance(session, *, row: BetaCode, actor: str, limit: int, active: bool, reason: str) -> None:
    session.add(BetaCodeAllowanceChange(id=new_id("bca"), code=row.code, actor_admin_id=actor, reason=reason,
        before_limit=row.max_uses, after_limit=limit, before_active=row.active, after_active=active,
        created_at=datetime.now(timezone.utc)))
    row.max_uses = limit
    row.active = active


def admit(session, *, person_id: str, user_id: str, invite_code: str | None,
          required: bool, founder: bool = False, existing: bool = False) -> None:
    """Activation receipt and redemption commit together in the caller transaction."""
    if session.get(ReferralAdmission, person_id):
        provision(session, person_id)
        return
    if not existing:
        if required and not founder:
            if session.get_bind().dialect.name == "postgresql":
                from app.db.session import serialize_writes
                serialize_writes(session, key="beta-seats")  # cap must hold under concurrent admissions
            if seats_taken(session) >= beta_max_founders():
                raise ReferralDenied("beta_full")
        if invite_code:
            redeem(session, code=invite_code, person_id=person_id, user_id=user_id)
        elif required and not founder:
            raise ReferralDenied("invite_code_required")
    session.add(ReferralAdmission(person_id=person_id, created_at=datetime.now(timezone.utc), legacy=existing))
    if not existing:  # staged in this transaction: the receipt and the event commit or roll back together
        from app.analytics.events import emit_in_session
        if invite_code:
            emit_in_session(session, "invite.redeemed", event_key=f"invite:{person_id}", person_id=person_id)
        emit_in_session(session, "signup", event_key=f"signup:{person_id}", person_id=person_id)
    provision(session, person_id)
    session.flush()


def paying_person(session, account) -> str | None:
    from app.credits.accounts import _acting_person, _owner
    from app.db.models import ConsumerUser
    owner = account.owner_person_id
    if not owner:
        owner, _ = _owner(session, account.venture_id or "", account.workspace_id, legacy_fallback=False)
    if not owner:
        owner = session.scalar(select(ConsumerUser.person_id).where(ConsumerUser.credit_account_id == account.id))
    return _acting_person(session, owner)


def paying_accounts(session, person_id: str) -> list[str]:
    from sqlalchemy import or_
    from app.db.models import CreditAccount, PersonIdentity
    candidates = select(CreditAccount).where(or_(CreditAccount.owner_person_id == person_id,
        CreditAccount.owner_person_id.is_(None), CreditAccount.owner_person_id.in_(
            select(PersonIdentity.subject).where(PersonIdentity.person_id == person_id))))
    # ponytail: ownerless legacy accounts resolve on read, backfill owner ids if summary traffic grows
    return [account.id for account in session.scalars(candidates)
            if paying_person(session, account) == person_id]


def used_micro(session, receipt) -> int:
    from app.db.models import CreditLedgerEntry
    from app.credits._txn import naive_utc
    if receipt.installed_at is None:
        return 0
    cutoff = max(naive_utc(receipt.created_at), naive_utc(receipt.installed_at))
    return int(session.scalar(select(func.coalesce(func.sum(-CreditLedgerEntry.amount_micro), 0)).where(
        CreditLedgerEntry.account_id.in_(paying_accounts(session, receipt.person_id)),
        CreditLedgerEntry.kind == "spend", CreditLedgerEntry.amount_micro < 0,
        CreditLedgerEntry.created_at >= cutoff)) or 0)


def qualify(session, receipt) -> bool:
    from app.credits.units import credits_to_micro
    if receipt.qualified_at is None and used_micro(session, receipt) >= credits_to_micro(THRESHOLD_CREDITS):
        receipt.qualified_at = utcnow()
    return receipt.qualified_at is not None


def qualify_spend(session, account) -> None:
    person_id = paying_person(session, account)
    if person_id:
        receipt = session.scalar(select(BetaCodeRedemption).where(
            BetaCodeRedemption.person_id == person_id).with_for_update().execution_options(populate_existing=True))
        if receipt is not None and receipt.reward_ledger_id is None:
            qualify(session, receipt)


def reward(session, person_id: str) -> bool:
    from app.core.local_mode import is_local
    if is_local():
        return False
    from app.credits.accounts import wallet_for
    from app.credits.grants import grant
    from app.credits.units import credits_to_micro
    from app.db.models import CreditAccount
    from app.db.session import serialize_writes
    receipt = session.scalar(select(BetaCodeRedemption).where(
        BetaCodeRedemption.person_id == person_id).with_for_update().execution_options(populate_existing=True))
    if receipt is None or receipt.reward_ledger_id is not None or not qualify(session, receipt):
        return False
    code = session.get(BetaCode, receipt.code)
    wallet = wallet_for(code.owner_person_id, transaction=session)
    serialize_writes(session, key=wallet.id)
    session.scalar(select(CreditAccount).where(CreditAccount.id == wallet.id)
        .with_for_update().execution_options(populate_existing=True))
    entry = grant(transaction=session, account_id=wallet.id, amount_micro=credits_to_micro(REWARD_CREDITS),
        reason="Referral reward", actor_kind="system", actor_id=None,
        idempotency_key=f"referral-reward:{receipt.person_id}")
    receipt.reward_ledger_id = entry.id
    session.flush()
    return True


def sweep_rewards() -> int:
    from app.state import db
    from app.db.session import serialize_writes
    from app.credits.errors import CreditsUnavailable
    with db()() as session:
        people = list(session.scalars(select(BetaCodeRedemption.person_id).where(
            BetaCodeRedemption.installed_at.is_not(None), BetaCodeRedemption.reward_ledger_id.is_(None))))
    paid = 0
    for person_id in people:
        try:
            with db()() as session:
                serialize_writes(session, key=f"referral-person:{person_id}")
                rewarded = reward(session, person_id)
                session.commit()
                paid += int(rewarded)
        except CreditsUnavailable:
            logging.getLogger(__name__).warning("referral_reward_wallet_unavailable", extra={"person_id": person_id})
    return paid


def claim(session, *, code: str, person_id: str) -> None:
    from app.db.models import Person, CreditAccount, CreditLedgerEntry
    from app.db.supymem_models import ReferralProgramState
    from app.credits._txn import naive_utc
    from app.db.session import serialize_writes
    prior = session.get(BetaCodeRedemption, person_id)
    if prior:
        redeem(session, code=code, person_id=person_id, user_id=prior.user_id)
        return
    serialize_writes(session, key="wallet:" + person_id)
    account_ids = paying_accounts(session, person_id)
    for account_id in sorted(account_ids):
        serialize_writes(session, key=account_id)
        session.scalar(select(CreditAccount).where(CreditAccount.id == account_id).with_for_update())
    person = session.get(Person, person_id)
    admission = session.get(ReferralAdmission, person_id)
    state = session.get(ReferralProgramState, 1)
    if (person is None or (admission and admission.legacy)
            or (state and naive_utc(person.created_at) < naive_utc(state.grandfather_before))
            or session.scalar(select(CreditLedgerEntry.id).where(
                CreditLedgerEntry.account_id.in_(account_ids),
                CreditLedgerEntry.kind == "spend").limit(1))):
        raise ReferralDenied("ineligible")
    serialize_writes(session, key="beta-seats")
    if seats_taken(session) >= beta_max_founders():
        raise ReferralDenied("beta_full")
    redeem(session, code=code, person_id=person_id, user_id=person_id)
    if admission is None:
        admit(session, person_id=person_id, user_id=person_id, invite_code=code, required=False)


def summary(session, person_id: str) -> dict:
    from app.db.models import CreditLedgerEntry
    from app.credits.units import micro_to_credits
    code = provision(session, person_id)
    receipts = session.scalars(select(BetaCodeRedemption).join(BetaCode).where(BetaCode.owner_person_id == person_id)
        .order_by(BetaCodeRedemption.created_at, BetaCodeRedemption.person_id))
    items = []
    for receipt in receipts:
        grant = session.get(CreditLedgerEntry, receipt.reward_ledger_id) if receipt.reward_ledger_id else None
        items.append({"referral_id": receipt.person_id,
            "status": "rewarded" if grant else "active" if receipt.installed_at else "joined",
            "used_credits": float(micro_to_credits(used_micro(session, receipt))),
            "rewarded_at": grant.created_at.replace(tzinfo=timezone.utc).isoformat() if grant else None})
    return {"code": code.code, "share_url": "https://supymem.com/?ref=" + code.code,
            "threshold_credits": THRESHOLD_CREDITS, "reward_credits": REWARD_CREDITS, "referrals": items}


def nudge(venture_id: str, person_id: str) -> bool:
    from app.chat.proactive import quiet, DAILY_NUDGES, NUDGE, _day_start
    from app.chat.milestones import post_milestone
    from app.chat.conversations import current_conversation
    from app.db.models import ChatMessage
    from app.db.session import serialize_writes
    from app.credits._txn import naive_utc
    from app.state import db
    now = utcnow()
    conversation = current_conversation()
    if (not conversation or conversation.actor_id != person_id
            or conversation.venture_id != venture_id or quiet(venture_id, now, person_id=person_id)):
        return False
    with db()() as session:
        # Ask only founders who have used Supymem: account at least a day old and a few messages in this venture.
        from app.db.models import Person
        person = session.get(Person, person_id)
        if person is None or naive_utc(now) - naive_utc(person.created_at) < timedelta(days=1):
            return False
        sent = session.scalar(select(func.count()).select_from(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.role == "user")) or 0
        if sent < 3:
            return False
        serialize_writes(session, key=f"referral-person:{person_id}")
        provision(session, person_id)
        mapping = session.get(PersonReferralCode, person_id, with_for_update=True)
        if mapping.last_nudged_at and naive_utc(now) - naive_utc(mapping.last_nudged_at) < timedelta(days=3):
            return False
        serialize_writes(session, key="chat-notices:" + venture_id)
        count = session.scalar(select(func.count()).select_from(ChatMessage).where(
            ChatMessage.venture_id == venture_id, ChatMessage.kind == NUDGE,
            ChatMessage.created_at >= _day_start(session, venture_id, now))) or 0
        if count >= DAILY_NUDGES:
            return False
        posted = post_milestone(venture_id, session=session, kind=NUDGE,
            key=f"referral:{person_id}:{now.isoformat()}",
            body_md="Invite a friend to Supymem. You earn 2,000 credits once they install and use 2,000 credits.",
            actions=[{"label": "Refer a friend", "action": "referrals.show", "style": "primary"}])
        if posted:
            mapping.last_nudged_at = now
        session.commit()
        return posted
