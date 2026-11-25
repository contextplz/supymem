"""Reserve a reviewed device ceiling atomically with its approval.

Paid native calls reconcile against their reviewed hold. Hosted subscription
coding keeps a zero-amount hold; local custody uses the order and stop receipts.
"""
from datetime import datetime

from sqlalchemy import select

from app.core.local_mode import is_local
from app.credits._txn import naive_utc, serialize_writes
from app.credits.errors import AccountSuspended, CreditsUnavailable, InsufficientCredits
from app.db.models import CreditAccount, CreditReservation
from core.ids import new_id


def lock_account(session, wid, vid, *, reservation_id=None):
    if is_local() and not reservation_id:
        return None
    from app.credits.accounts import account_for_venture
    if reservation_id:
        # Receipt-only accounting uses immutable funded custody, not today's payer.
        account_id = session.scalar(select(CreditReservation.account_id).where(
            CreditReservation.id == reservation_id, CreditReservation.workspace_id == wid,
            CreditReservation.purpose == "device.work_order"))
    else:
        resolved = account_for_venture(vid, session=session)
        account_id = resolved.id if resolved else None
    if account_id is None:
        return None
    serialize_writes(session, key=account_id)
    return session.scalar(select(CreditAccount).where(CreditAccount.id == account_id)
        .with_for_update().execution_options(populate_existing=True))


def _subscription_coding(manifest):
    """Only reviewed, confined native provider coding may reserve zero credits."""
    loop = manifest.get("coding_loop_verification")
    deploy = manifest.get("deploy_verification")
    if loop or deploy:
        # The typed manifest owns these shapes; retain zero-credit settlement
        # custody for plan, implementation and every separate deployment.
        return (manifest.get("credit_ceiling_micro") == 0
            and manifest.get("applications") == ["com.companyos.app"] and manifest.get("origins") == []
            and (bool(loop) and loop.get("operation") == "workspace_loop_v1"
                and loop.get("provider") in {"claude", "codex"} and loop.get("network") == "none"
                and loop.get("profile") == "web_app_vercel_v1"
                and loop.get("read_scope") == loop.get("write_scope") == "venture_workspace"
                and manifest.get("actions") == ["editor.edit", "terminal.execute"]
                or bool(deploy) and deploy.get("operation") == "workspace_deploy_v1"
                and deploy.get("provider") == "vercel" and manifest.get("actions") == ["terminal.execute"]))
    verification = manifest.get("coding_verification") or {}
    return (isinstance(verification, dict)
        and verification.get("operation") == "workspace_edit_v1"
        and verification.get("provider") in ("codex", "claude")
        and verification.get("write_scope") == "venture_workspace"
        and verification.get("command_network") == "disabled"
        and manifest.get("applications") == ["com.companyos.app"]
        and manifest.get("origins") == []
        and manifest.get("actions") == ["editor.edit", "terminal.execute"])


def apply(session, account, body, action, now):
    """Caller holds account, venture, then order locks; caller commits."""
    if is_local() and not body.get("reservation_id"):
        if action == "approve":
            body["budget_source"] = "local"
        elif action == "claim" and body.get("budget_source") != "local":
            raise ValueError("device_budget_not_reserved")
        return
    ceiling = body["manifest"]["credit_ceiling_micro"]
    requires_hold = bool(ceiling) or _subscription_coding(body["manifest"])
    # A zero-amount hold preserves execution/settlement binding without charging
    # for model usage supplied by the founder's own provider account.
    if action == "approve" and requires_hold:
        if account is None:
            raise CreditsUnavailable("device_credit_account_missing")
        if account.status != "active":
            raise AccountSuspended(account.id, account.status)
        available = account.granted_micro - account.spent_micro - account.reserved_micro
        if available < ceiling:
            raise InsufficientCredits(account.id, available, ceiling)
        if body.get("reservation_id"):
            raise ValueError("device_budget_already_reserved")
        rid = new_id("crv")
        session.add(CreditReservation(id=rid, account_id=account.id, amount_micro=ceiling,
            status="active", purpose="device.work_order", workspace_id=body["workspace_id"], venture_id=body.get("venture_id"),
            request_id=body["id"], expires_at=naive_utc(datetime.fromisoformat(body["expires_at"])),
            created_at=naive_utc(now), clamped=0))
        account.reserved_micro += ceiling
        account.version += 1
        account.updated_at = naive_utc(now)
        body["reservation_id"] = rid
        return
    rid = body.get("reservation_id")
    if not rid:
        if action == "claim" and requires_hold:
            raise ValueError("device_budget_not_reserved")
        return
    reservation = session.get(CreditReservation, rid, with_for_update=True)
    if (account is None or reservation is None or reservation.account_id != account.id
            or reservation.request_id != body["id"] or reservation.amount_micro != ceiling
            or reservation.workspace_id != body["workspace_id"] or reservation.is_pool
            or reservation.purpose != "device.work_order"):
        raise ValueError("device_budget_binding_invalid")
    if action == "claim":
        if account.status != "active":
            raise AccountSuspended(account.id, account.status)
        if reservation.status != "active" or naive_utc(reservation.expires_at) <= naive_utc(now):
            raise ValueError("device_budget_unavailable")
        reservation.status = "reconciling"
    elif action == "cancel" and not body.get("claimed_at"):
        if reservation.status in {"released", "expired"}:
            return
        if reservation.status != "active":
            raise ValueError("device_budget_requires_reconciliation")
        account.reserved_micro -= reservation.amount_micro
        account.version += 1
        account.updated_at = naive_utc(now)
        reservation.status, reservation.settled_at = "released", naive_utc(now)
