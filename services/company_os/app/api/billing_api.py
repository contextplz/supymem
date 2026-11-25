"""Entitlement lease, plan checkout, and Stripe Connect onboarding."""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.authz.workspace import assert_venture_access, assert_workspace_access, subject_person_id
from app.authz.roles import role_allows
from app.billing.entitlements import activate_paid, current_plan
from app.billing.lease import is_ephemeral, public_key_b64, sign_lease
from app.billing.plans import TIERS
from app.core.local_mode import is_local
from app.payments.errors import ProviderUnavailable
from app.payments.flags import payments_accepting, payments_closed_detail
from app.workspaces.service import role_of

router = APIRouter(tags=["billing"])
local_router = APIRouter(tags=["billing"])


def local_payment_guidance():
    raise HTTPException(404, "To manage your spending, open Settings > Keys and connections > Your monthly spend cap.")


for path, methods in (
    ("/billing/checkout", ["POST"]),
    ("/billing/callback", ["GET"]),
    ("/ventures/{venture_id}/billing/stripe/connect", ["GET", "POST"]),
    ("/workspaces/{workspace_id}/billing/stripe/connect", ["GET", "POST"]),
    ("/ventures/{venture_id}/credits/packs", ["GET"]),
    ("/ventures/{venture_id}/credits/orders", ["GET"]),
    ("/ventures/{venture_id}/credits/request", ["POST"]),
    ("/ventures/{venture_id}/credits/topup", ["POST"]),
    ("/payments/stub/checkout/{provider_order_id}", ["GET"]),
):
    local_router.add_api_route(path, local_payment_guidance, methods=methods, include_in_schema=False)


class CheckoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    tier: str = Field(pattern="^(founder|pro|team)$")


class ConnectStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(default="US", pattern="^[A-Z]{2}$")
    return_path: str | None = None


def _hosted_origin() -> str:
    return (
        os.environ.get("COMPANY_OS_PUBLIC_URL")
        or os.environ.get("CONSOLE_ORIGIN")
        or "http://127.0.0.1:3100"
    ).rstrip("/")


@router.get("/entitlement")
@local_router.get("/entitlement")
def get_entitlement(
    workspace_id: str | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    person_id = subject_person_id(x_subject)
    if workspace_id:
        assert_workspace_access(workspace_id, x_subject)
    plan = current_plan(workspace_id=workspace_id, person_id=person_id)
    if is_local():
        return {"mode": "local", "status": "active", "features": list(plan.features), "payments_accepting": False}
    lease = sign_lease(plan, subject=x_subject or person_id or "unknown")
    return {
        "plan": plan.tier,
        "status": plan.status,
        "features": list(plan.features),
        "seats_used": plan.seats_used,
        "seats_max": plan.seats,
        "trial_ends_at": plan.trial_ends_at,
        "current_period_end": plan.current_period_end,
        "lease": lease,
        "public_key": public_key_b64(),
        "ephemeral": is_ephemeral(),
        "payments_accepting": payments_accepting(),
    }


@router.get("/billing/plans")
@local_router.get("/billing/plans", include_in_schema=False)
def list_plans():
    if is_local():
        return {"mode": "local", "accepting": False, "items": []}
    from app.payments.registry import house_provider_name

    return {
        "house_provider": house_provider_name(),
        "accepting": payments_accepting(),
        "items": [
            {
                "tier": name,
                "label": spec["label"],
                "seats": spec["seats"],
                "features": list(spec["features"]),
                "inr_paise": spec["inr_paise"],
                "usd_cents": spec["usd_cents"],
            }
            for name, spec in TIERS.items()
            if name != "pro"
        ],
    }


@router.post("/billing/checkout")
def start_checkout(
    body: CheckoutBody,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(body.workspace_id, x_subject)
    if not payments_accepting():
        raise HTTPException(status_code=403, detail=payments_closed_detail())
    person_id = subject_person_id(x_subject)
    spec = TIERS[body.tier]
    if spec["usd_cents"] <= 0 and spec["inr_paise"] <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "free_tier", "message": "Nothing to check out."}},
        )
    callback = f"{_hosted_origin()}/billing/callback"
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    razorpay_key = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    if stripe_key:
        from app.payments.stripe import StripeProvider

        handle = StripeProvider().create_subscription_checkout(
            workspace_id=body.workspace_id,
            tier=body.tier,
            amount_minor=spec["usd_cents"],
            currency="USD",
            callback_url=callback,
            customer_email=None,
        )
        return {
            "provider": "stripe",
            "checkout_url": handle.checkout_url,
            "provider_ref": handle.provider_order_id,
            "tier": body.tier,
        }
    if razorpay_key:
        from app.payments.razorpay import RazorpayProvider

        handle = RazorpayProvider().create_subscription_checkout(
            workspace_id=body.workspace_id,
            tier=body.tier,
            amount_paise=spec["inr_paise"],
            callback_url=callback,
            customer_email=None,
        )
        return {
            "provider": "razorpay",
            "checkout_url": handle.checkout_url,
            "provider_ref": handle.provider_order_id,
            "tier": body.tier,
        }

    try:
        activate_paid(
            workspace_id=body.workspace_id,
            person_id=person_id,
            tier=body.tier,
            provider="stub",
            provider_subscription_id=f"stub_{body.workspace_id}_{body.tier}",
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": exc.code, "message": str(exc)}},
        ) from exc
    from app.analytics.events import emit

    emit(
        "billing.started",
        person_id=person_id,
        workspace_id=body.workspace_id,
        props={"tier": body.tier, "provider": "stub"},
    )
    return {
        "provider": "stub",
        "checkout_url": callback + "?status=ok",
        "tier": body.tier,
    }


@router.get("/billing/callback")
def billing_callback(
    status: str | None = None,
    workspace_id: str | None = None,
    tier: str | None = None,
    session_id: str | None = None,
    razorpay_payment_id: str | None = None,
    razorpay_subscription_id: str | None = None,
):
    """Browser lander after Stripe or Razorpay. Desktop polls /entitlement after this."""
    _ = (
        session_id,
        razorpay_payment_id,
        razorpay_subscription_id,
        workspace_id,
        tier,
    )
    ok = status in {None, "ok", "success"}
    return {
        "ok": ok,
        "deep_link": "companyos://billing-callback?status=" + ("ok" if ok else "failed"),
        "message": "You can return to the Company OS app. Your plan updates in a few seconds.",
    }


@router.get("/ventures/{venture_id}/billing/stripe/connect")
def get_stripe_connect(
    venture_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _vid, workspace_id = assert_venture_access(venture_id, x_subject)
    from app.payments.connect import refresh_account, status_for_workspace

    try:
        return refresh_account(workspace_id)
    except ProviderUnavailable:
        return status_for_workspace(workspace_id)


@router.post("/ventures/{venture_id}/billing/stripe/connect")
def start_stripe_connect(
    venture_id: str,
    body: ConnectStartBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    _vid, workspace_id = assert_venture_access(venture_id, x_subject)
    if not role_allows(role_of(workspace_id, subject_person_id(x_subject)), "manage_billing"):
        raise HTTPException(status_code=403, detail={"code": "workspace_admin_required"})
    if not payments_accepting():
        raise HTTPException(status_code=403, detail=payments_closed_detail())
    payload = body or ConnectStartBody()
    path = payload.return_path or f"/v/{venture_id}/connect/stripe"
    from app.payments.connect import start_onboarding

    try:
        return start_onboarding(
            workspace_id=workspace_id,
            return_path=path,
            country=payload.country,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": exc.code, "message": "Stripe Connect is not configured."}},
        ) from exc


@router.get("/workspaces/{workspace_id}/billing/stripe/connect")
def get_workspace_stripe_connect(
    workspace_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    from app.payments.connect import refresh_account, status_for_workspace

    try:
        return refresh_account(workspace_id)
    except ProviderUnavailable:
        return status_for_workspace(workspace_id)


@router.post("/workspaces/{workspace_id}/billing/stripe/connect")
def start_workspace_stripe_connect(
    workspace_id: str,
    body: ConnectStartBody | None = None,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    assert_workspace_access(workspace_id, x_subject)
    if not role_allows(role_of(workspace_id, subject_person_id(x_subject)), "manage_billing"):
        raise HTTPException(status_code=403, detail={"code": "workspace_admin_required"})
    if not payments_accepting():
        raise HTTPException(status_code=403, detail=payments_closed_detail())
    payload = body or ConnectStartBody()
    path = payload.return_path or "/account"
    from app.payments.connect import start_onboarding

    try:
        return start_onboarding(
            workspace_id=workspace_id,
            return_path=path,
            country=payload.country,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": exc.code, "message": "Stripe Connect is not configured."}},
        ) from exc
