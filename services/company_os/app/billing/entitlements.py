"""Chokepoint for paid features. Never trust a client-supplied plan flag."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import func, select

from app.admin.authn import require_signed_caller
from app.billing.plans import FEATURES, TIERS, TRIAL_DAYS
from app.config import is_local_env
from app.core.local_mode import is_local
from app.db.models import PlanSubscription, WorkspaceMember
from app.payments.errors import ProviderUnavailable
from app.state import db
from core.clock import utcnow
from core.ids import new_id

__all__ = [
    "FEATURES",
    "TIERS",
    "EntitlementDenied",
    "PlanView",
    "current_plan",
    "require_entitlement",
    "seats_used",
]


# A paid period stays usable briefly past its end so a renewal webhook that lands
# late does not flap the workspace to free; failed payments revoke via past_due.
_RENEWAL_GRACE = timedelta(days=1)


class EntitlementDenied(Exception):
    def __init__(self, feature: str, tier: str) -> None:
        super().__init__(f"feature {feature!r} requires a paid plan (current={tier})")
        self.feature = feature
        self.tier = tier


@dataclass(frozen=True)
class PlanView:
    id: str
    workspace_id: str | None
    person_id: str | None
    tier: str
    status: str
    seats: int | None
    seats_used: int
    features: tuple[str, ...]
    trial_ends_at: str | None
    current_period_end: str | None

    def allows(self, feature: str) -> bool:
        if is_local():
            return feature in FEATURES
        from app.payments.flags import payments_accepting
        if not payments_accepting():
            return feature in FEATURES  # free rollout has no subscription gate; credit budgets still apply
        if self.status in {"canceled", "expired", "past_due"} and self.tier != "free":
            return feature in TIERS["free"]["features"]
        return feature in self.features


def seats_used(workspace_id: str | None) -> int:
    if not workspace_id:
        return 0
    factory = db()
    with factory() as session:
        n = session.scalar(
            select(func.count()).where(WorkspaceMember.workspace_id == workspace_id)
        )
    return int(n or 0)


def _aware(dt):
    """Postgres returns naive datetimes for these columns; treat them as UTC."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return _aware(dt).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _view(row: PlanSubscription) -> PlanView:
    spec = TIERS.get(row.tier) or TIERS["free"]
    seats = int(row.seats or spec["seats"])
    if row.status in {"past_due", "canceled", "expired"}:
        spec = TIERS["free"]
        seats = spec["seats"]
    from app.payments.flags import payments_accepting
    effective_features = spec["features"] if payments_accepting() else tuple(sorted(FEATURES))
    return PlanView(
        id=row.id,
        workspace_id=row.workspace_id,
        person_id=row.person_id,
        tier=row.tier,
        status=row.status,
        seats=seats,
        seats_used=seats_used(row.workspace_id),
        features=effective_features,
        trial_ends_at=_iso(row.trial_ends_at),
        current_period_end=_iso(row.current_period_end),
    )


def current_plan(
    *,
    workspace_id: str | None,
    person_id: str | None,
    start_trial: bool = True,
) -> PlanView:
    """Return the workspace plan, creating a 14-day Pro trial on first look."""
    if is_local():
        return PlanView(id="local", workspace_id=workspace_id, person_id=person_id, tier="local", status="active",
            seats=None, seats_used=seats_used(workspace_id), features=tuple(sorted(FEATURES)),
            trial_ends_at=None, current_period_end=None)
    factory = db()
    now = utcnow()
    with factory() as session:
        row = None
        if workspace_id:
            row = session.scalars(
                select(PlanSubscription).where(
                    PlanSubscription.workspace_id == workspace_id
                )
            ).first()
        if row is None and person_id and not workspace_id:
            row = session.scalars(
                select(PlanSubscription)
                .where(PlanSubscription.person_id == person_id)
                .order_by(PlanSubscription.created_at.desc())
            ).first()
        if row is None:
            trial_end = now + timedelta(days=TRIAL_DAYS) if start_trial else None
            row = PlanSubscription(
                id=new_id("pln"),
                person_id=person_id,
                workspace_id=workspace_id,
                tier="pro" if start_trial else "free",
                status="trialing" if start_trial else "active",
                seats=TIERS["pro"]["seats"] if start_trial else TIERS["free"]["seats"],
                trial_ends_at=trial_end,
                current_period_end=trial_end,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        elif (
            (
                row.status == "trialing"
                and row.trial_ends_at is not None
                and _aware(row.trial_ends_at) <= _aware(now)
            )
            or (
                row.tier != "free"
                and row.current_period_end is not None
                and _aware(row.current_period_end) + _RENEWAL_GRACE <= _aware(now)
            )
        ):
            row.tier = "free"
            row.status = "expired"
            row.seats = TIERS["free"]["seats"]
            row.updated_at = now
            session.commit()
            session.refresh(row)
        session.expunge(row)
        return _view(row)


def require_entitlement(
    feature: str,
    *,
    workspace_id: str | None,
    person_id: str | None,
) -> PlanView:
    if feature not in FEATURES:
        raise EntitlementDenied(feature, "unknown")
    plan = current_plan(workspace_id=workspace_id, person_id=person_id)
    if not plan.allows(feature):
        raise EntitlementDenied(feature, plan.tier)
    return plan


def activate_paid(
    *,
    workspace_id: str | None,
    person_id: str | None,
    tier: str,
    provider: str,
    provider_subscription_id: str | None,
    provider_customer_id: str | None = None,
    period_end=None,
) -> PlanView:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier}")
    if provider == "stub" and (require_signed_caller() or not is_local_env()):
        raise ProviderUnavailable("Payment provider is not configured.")
    if tier == "founder":
        tier = "pro"  # ck_plan_subscriptions_tier predates the rename; TIERS["pro"] is TIERS["founder"]
    spec = TIERS[tier]
    now = utcnow()
    if provider == "razorpay" and period_end is None:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        period_end = now.replace(
            year=year, month=month, day=min(now.day, monthrange(year, month)[1])
        )
    factory = db()
    with factory() as session:
        row = None
        if workspace_id:
            row = session.scalars(
                select(PlanSubscription).where(
                    PlanSubscription.workspace_id == workspace_id
                )
            ).first()
        if row is None:
            row = PlanSubscription(
                id=new_id("pln"),
                person_id=person_id,
                workspace_id=workspace_id,
                created_at=now,
                updated_at=now,
                tier=tier,
                status="active",
                seats=spec["seats"],
            )
            session.add(row)
        elif (
            row.status == "canceled"
            and provider_subscription_id
            and row.provider == provider
            and row.provider_subscription_id == provider_subscription_id
        ):
            # Canceled provider subscriptions are terminal; a late or replayed
            # "active" event for the same id must not re-upgrade the workspace.
            session.expunge(row)
            return _view(row)
        row.tier = tier
        row.status = "active"
        row.seats = spec["seats"]
        row.person_id = person_id or row.person_id
        row.provider = provider
        row.provider_subscription_id = provider_subscription_id
        row.provider_customer_id = provider_customer_id
        row.current_period_end = period_end
        row.updated_at = now
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return _view(row)


def update_subscription_status(
    *,
    workspace_id: str | None,
    provider: str,
    provider_subscription_id: str | None,
    status: str,
) -> bool:
    if status not in {"past_due", "canceled", "expired"}:
        raise ValueError(f"unsupported subscription status {status}")
    if not provider_subscription_id:
        return False
    with db()() as session:
        stmt = select(PlanSubscription).where(
            PlanSubscription.provider == provider,
            PlanSubscription.provider_subscription_id == provider_subscription_id,
        )
        if workspace_id:
            stmt = stmt.where(PlanSubscription.workspace_id == workspace_id)
        row = session.scalars(stmt).first()
        if row is None:
            return False
        row.status = status
        row.updated_at = utcnow()
        session.commit()
        return True
