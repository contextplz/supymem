"""User-scoped usage and founder platform analytics."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from app.credits._txn import naive_utc, session_factory
from app.credits.units import format_credits
from app.db.models import ConsumerUsageEvent, ConsumerUser, CreditAccount


def user_usage(user_id: str, *, limit: int = 50) -> dict[str, Any]:
    cap = min(max(int(limit), 1), 200)
    factory = session_factory()
    with factory() as session:
        rows = list(
            session.scalars(
                select(ConsumerUsageEvent)
                .where(ConsumerUsageEvent.user_id == user_id)
                .order_by(ConsumerUsageEvent.created_at.desc())
                .limit(cap)
            ).all()
        )
        items = [
            {
                "id": row.id,
                "route": row.route,
                "tokens_in": row.tokens_in,
                "tokens_out": row.tokens_out,
                "credits": format_credits(int(row.credits_micro)),
                "credits_micro": int(row.credits_micro),
                "at": naive_utc(row.created_at).isoformat() + "Z",
            }
            for row in rows
        ]
    return {"user_id": user_id, "items": items}


def platform_analytics() -> dict[str, Any]:
    factory = session_factory()
    now = naive_utc()
    week_ago = now - timedelta(days=7)
    with factory() as session:
        signups = session.scalar(select(func.count()).select_from(ConsumerUser)) or 0
        signups_7d = (
            session.scalar(
                select(func.count())
                .select_from(ConsumerUser)
                .where(ConsumerUser.created_at >= week_ago)
            )
            or 0
        )
        granted = (
            session.scalar(
                select(func.coalesce(func.sum(CreditAccount.granted_micro), 0)).where(
                    CreditAccount.scope == "person",
                    CreditAccount.id.in_(select(ConsumerUser.credit_account_id).distinct())
                )
            )
            or 0
        )
        spent = (
            session.scalar(
                select(func.coalesce(func.sum(CreditAccount.spent_micro), 0)).where(
                    CreditAccount.scope == "person",
                    CreditAccount.id.in_(select(ConsumerUser.credit_account_id).distinct())
                )
            )
            or 0
        )
        hosted_calls = (
            session.scalar(
                select(func.count())
                .select_from(ConsumerUsageEvent)
                .where(ConsumerUsageEvent.route == "spend_default_llm")
            )
            or 0
        )
        byok_fees = (
            session.scalar(
                select(func.count())
                .select_from(ConsumerUsageEvent)
                .where(ConsumerUsageEvent.route == "spend_byok_platform_fee")
            )
            or 0
        )
        agents = 0
        try:
            from app.db.models import AgentInstance

            agents = session.scalar(select(func.count()).select_from(AgentInstance)) or 0
        except Exception:
            agents = 0
    return {
        "signups": int(signups),
        "signups_7d": int(signups_7d),
        "credits_granted": format_credits(int(granted)),
        "credits_spent": format_credits(int(spent)),
        "hosted_default_calls": int(hosted_calls),
        "byok_platform_fee_calls": int(byok_fees),
        "agents": int(agents),
    }
