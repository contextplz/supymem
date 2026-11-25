"""Consumer users: Cognito identity, 14-day plan, ledger-backed credits."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.consumer.cognito import CognitoIdentity
from app.consumer.founder import is_founder_email
from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.credits.accounts import balance, ensure_account, get_account_by_id
from app.credits.errors import InsufficientCredits
from app.credits.grants import grant
from app.credits.units import credits_to_micro
from app.db.models import ConsumerUsageEvent, ConsumerUser
from app.identity.resolve import person_for_subject
from core.ids import new_id  # type: ignore[import-not-found]
from llm_gateway.credit_lease import TRIAL_DAYS, issue_lease
from llm_gateway.hosted_server import HostedDenied

log = logging.getLogger("app.consumer.users")

CONSUMER_SCOPE_PREFIX = "consumer:"
PLAN_EXTEND_DAYS = TRIAL_DAYS


def consumer_venture_id(cognito_sub: str) -> str:
    return f"{CONSUMER_SCOPE_PREFIX}{cognito_sub}"


def _aware(dt: datetime) -> datetime:
    from datetime import timezone

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def trial_days() -> int:
    from app.credits.policy import get_int_policy

    return get_int_policy("trial_days", TRIAL_DAYS)


def _expunge(session: Any, row: Any) -> Any:
    session.expunge(row)
    return row


def get_user(user_id: str) -> ConsumerUser | None:
    factory = session_factory()
    with factory() as session:
        row = session.get(ConsumerUser, user_id)
        if row is None:
            return None
        return _expunge(session, row)


def get_user_by_sub(cognito_sub: str) -> ConsumerUser | None:
    factory = session_factory()
    with factory() as session:
        row = session.scalars(
            select(ConsumerUser).where(ConsumerUser.cognito_sub == cognito_sub)
        ).first()
        if row is None:
            return None
        return _expunge(session, row)


def get_user_by_email(email: str) -> ConsumerUser | None:
    addr = (email or "").strip().lower()
    if not addr:
        return None
    factory = session_factory()
    with factory() as session:
        row = session.scalars(
            select(ConsumerUser).where(ConsumerUser.email == addr)
        ).first()
        if row is None:
            return None
        return _expunge(session, row)


def plan_active(user: ConsumerUser, *, now: datetime | None = None) -> bool:
    moment = naive_utc(now)
    return user.status == "active" and user.plan_expires_at > moment


def upsert_from_cognito(identity: CognitoIdentity) -> ConsumerUser:
    """Identify by Cognito sub + email. First visit starts the 14-day trial."""
    sub = (identity.sub or "").strip()
    email = (identity.email or "").strip().lower()
    if not sub or not email:
        raise ValueError("Cognito identity requires sub and email")
    person = person_for_subject(f"cognito:{sub}", email=email)
    factory = session_factory()
    user_id: str | None = None
    created = False
    for _ in range(4):
        with factory() as session:
            serialize_writes(session, key=f"consumer:{sub}")
            existing = session.scalars(
                select(ConsumerUser).where(ConsumerUser.cognito_sub == sub)
            ).first()
            now = naive_utc()
            if existing is not None:
                if existing.email != email:
                    existing.email = email
                    existing.updated_at = now
                if existing.person_id is None:
                    existing.person_id = person.id
                    existing.updated_at = now
                session.commit()
                user_id = existing.id
                break
            days = trial_days()
            row = ConsumerUser(
                id=new_id("csu"),
                cognito_sub=sub,
                email=email,
                person_id=person.id,
                credit_account_id=None,
                plan="trial",
                plan_started_at=now,
                plan_expires_at=now + timedelta(days=days),
                status="active",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            user_id = row.id
            created = True
            break
    if user_id is None:
        raise RuntimeError("could not upsert consumer user")

    account = ensure_account(
        consumer_venture_id(sub),
        owner_person_id=person.id,
    )
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=user_id)
        row = session.get(ConsumerUser, user_id)
        if row is None:
            raise RuntimeError("consumer user vanished")
        if account is not None and row.credit_account_id != account.id:
            row.credit_account_id = account.id
            row.updated_at = naive_utc()
            session.commit()
        session.refresh(row)
        user = _expunge(session, row)
    if created:
        log.info("consumer signup sub=%s trial_days=%s", sub, trial_days())
    return user


def extend_plan(user_id: str, *, days: int | None = None, plan: str = "paid") -> ConsumerUser:
    extra = int(days if days is not None else PLAN_EXTEND_DAYS)
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=user_id)
        row = session.get(ConsumerUser, user_id)
        if row is None:
            raise RuntimeError(f"unknown consumer user {user_id}")
        now = naive_utc()
        base = row.plan_expires_at if row.plan_expires_at > now else now
        row.plan_expires_at = base + timedelta(days=extra)
        row.plan = plan
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return _expunge(session, row)


def extend_plan_for_account(account_id: str) -> None:
    factory = session_factory()
    with factory() as session:
        row = session.scalars(
            select(ConsumerUser).where(ConsumerUser.credit_account_id == account_id)
        ).first()
        user_id = row.id if row is not None else None
    if user_id:
        extend_plan(user_id)


def credit_snapshot(user: ConsumerUser) -> dict[str, Any]:
    account = None
    if user.credit_account_id:
        account = get_account_by_id(user.credit_account_id)
    if account is None:
        return {
            "available_credits": "0.00",
            "available_micro": 0,
            "granted_credits": "0.00",
            "spent_credits": "0.00",
            "reserved_credits": "0.00",
            "status": "active",
        }
    snap = balance(account)
    return {
        "available_credits": snap["available_credits"],
        "available_micro": snap["available_micro"],
        "granted_credits": snap["granted_credits"],
        "spent_credits": snap["spent_credits"],
        "reserved_credits": snap["reserved_credits"],
        "status": account.status,
    }


def account_payload(user: ConsumerUser) -> dict[str, Any]:
    credits = credit_snapshot(user)
    return {
        "user_id": user.id,
        "cognito_sub": user.cognito_sub,
        "email": user.email,
        "plan": user.plan,
        "plan_started_at": naive_utc(user.plan_started_at).isoformat() + "Z",
        "plan_expires_at": naive_utc(user.plan_expires_at).isoformat() + "Z",
        "trial_days": trial_days(),
        "plan_active": plan_active(user),
        "credits": credits,
        "is_founder": is_founder_email(user.email),
    }


def issue_user_lease(user: ConsumerUser):
    credits = credit_snapshot(user)
    return issue_lease(
        user_id=user.id,
        email=user.email,
        cognito_sub=user.cognito_sub,
        remaining_micro=int(credits["available_micro"]),
        remaining_credits=str(credits["available_credits"]),
        plan=user.plan,
        plan_expires_at=_aware(user.plan_expires_at),
    )


def require_hosted_entitlement(user_id: str) -> ConsumerUser:
    user = get_user(user_id)
    if user is None:
        raise HostedDenied(401, "authentication required")
    if not plan_active(user):
        raise HostedDenied(
            402,
            "plan_expired",
        )
    credits = credit_snapshot(user)
    if int(credits["available_micro"]) <= 0:
        raise HostedDenied(402, "credit_required")
    return user


def charge_hosted_default(user_id: str, *, route: str = "spend_default_llm") -> None:
    from app.credits.gate import charge_sync
    from app.credits.units import MIN_RESERVE_MICRO

    user = require_hosted_entitlement(user_id)
    account_id = user.credit_account_id
    if not account_id:
        raise HostedDenied(402, "credit_required")
    try:
        with charge_sync(
            account_id=account_id,
            purpose=route,
            estimate_micro=MIN_RESERVE_MICRO,
        ) as meter:
            meter.record_micro(MIN_RESERVE_MICRO)
    except InsufficientCredits as exc:
        raise HostedDenied(402, "credit_required") from exc
    record_usage(
        user_id=user.id,
        route=route,
        tokens_in=0,
        tokens_out=0,
        credits_micro=MIN_RESERVE_MICRO,
    )


def debit_byok_platform_fee(user_id: str) -> None:
    from llm_gateway.hosted import BYOK_PLATFORM_FEE_MICRO
    from app.credits.gate import charge_sync

    user = get_user(user_id)
    if user is None:
        raise HostedDenied(401, "authentication required")
    if not user.credit_account_id:
        return
    try:
        with charge_sync(
            account_id=user.credit_account_id,
            purpose="spend_byok_platform_fee",
            estimate_micro=BYOK_PLATFORM_FEE_MICRO,
        ) as meter:
            meter.record_micro(BYOK_PLATFORM_FEE_MICRO)
    except InsufficientCredits:
        # BYOK still works; the platform fee is best-effort and never Azure.
        log.info("byok platform fee skipped; insufficient credits user=%s", user_id)
        record_usage(
            user_id=user.id,
            route="spend_byok_platform_fee",
            tokens_in=0,
            tokens_out=0,
            credits_micro=0,
        )
        return
    record_usage(
        user_id=user.id,
        route="spend_byok_platform_fee",
        tokens_in=0,
        tokens_out=0,
        credits_micro=BYOK_PLATFORM_FEE_MICRO,
    )


def record_usage(
    *,
    user_id: str,
    route: str,
    tokens_in: int,
    tokens_out: int,
    credits_micro: int,
) -> None:
    factory = session_factory()
    with factory() as session:
        session.add(
            ConsumerUsageEvent(
                id=new_id("cue"),
                user_id=user_id,
                route=route,
                tokens_in=int(tokens_in),
                tokens_out=int(tokens_out),
                credits_micro=int(credits_micro),
                created_at=naive_utc(),
            )
        )
        session.commit()


def founder_grant(*, email: str, credits: int, reason: str, actor_email: str) -> dict[str, Any]:
    addr = (email or "").strip().lower()
    user = get_user_by_email(addr)
    if user is None:
        raise LookupError("user not found")
    if not user.credit_account_id:
        raise LookupError("user has no credit account")
    amount = credits_to_micro(int(credits))
    entry = grant(
        account_id=user.credit_account_id,
        amount_micro=amount,
        reason=reason or "founder grant",
        actor_kind="admin",
        actor_id=actor_email,
        idempotency_key=f"founder-grant:{user.id}:{new_id('idk')}",
    )
    extend_plan(user.id)
    refreshed = get_user(user.id)
    assert refreshed is not None
    return {
        "user_id": refreshed.id,
        "email": refreshed.email,
        "credits_granted": credits,
        "ledger_id": entry.id,
        "account": account_payload(refreshed),
    }


def list_users(*, limit: int = 100) -> list[dict[str, Any]]:
    cap = min(max(int(limit), 1), 500)
    factory = session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(ConsumerUser).order_by(ConsumerUser.created_at.desc()).limit(cap)
            ).all()
        )
        for row in rows:
            session.expunge(row)
    return [account_payload(row) for row in rows]
