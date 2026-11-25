"""FastAPI dependencies for admin routes. Later agents plug into these."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.admin.audit import record as audit_record
from app.admin.authn import CallerContext, verify_caller
from app.admin.authz import (
    CAPABILITIES,
    AdminForbidden,
    capabilities_for,
    require_capability,
)
from app.admin.identity import (
    AdminIdentity,
    AdminInactive,
    admin_for_email,
    assert_active,
)
from app.core.local_mode import is_local


@dataclass(frozen=True)
class AdminContext:
    admin: AdminIdentity
    caller: CallerContext

    @property
    def capabilities(self) -> frozenset[str]:
        return capabilities_for(self.admin.role)


def _client_ip(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


def _audit_deny(
    request: Request,
    *,
    action: str,
    reason: str,
    actor_email: str | None = None,
    actor_admin_id: str | None = None,
) -> None:
    try:
        audit_record(
            actor_admin_id=actor_admin_id,
            actor_email=actor_email,
            action=action,
            outcome="deny",
            reason=reason,
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
        )
    except Exception:
        # Auditing must not become a way to skip the denial.
        pass


def require_hosted_admin() -> None:
    if is_local():
        raise HTTPException(status_code=404, detail={"code": "not_found",
            "message": "You can check your setup at /local/health."})


def require_admin(capability: str):
    """``Depends(require_admin("credits.grant"))``.

    Verifies the HMAC assertion, re-loads the admin row from the DB (never
    trusts the role claim in the assertion), checks the capability, and writes
    exactly one audit row.
    """
    if capability not in CAPABILITIES:
        raise RuntimeError(f"unknown admin capability {capability!r}")

    def dependency(request: Request) -> AdminContext:
        require_hosted_admin()
        action = capability
        try:
            caller = verify_caller(request, required=True)
        except HTTPException as exc:
            _audit_deny(request, action=action, reason="invalid_assertion")
            raise exc

        if caller.role == "system":
            _audit_deny(
                request,
                action=action,
                reason="system_role_not_admin",
                actor_email=caller.email,
            )
            raise HTTPException(status_code=403, detail={"code": "forbidden"})

        email = (caller.email or "").strip().lower()
        admin = admin_for_email(email) if email else None
        if admin is None:
            _audit_deny(
                request,
                action=action,
                reason="not_admin",
                actor_email=email or None,
            )
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        try:
            assert_active(admin)
        except AdminInactive:
            _audit_deny(
                request,
                action=action,
                reason="inactive",
                actor_email=admin.email,
                actor_admin_id=admin.id,
            )
            raise HTTPException(status_code=403, detail={"code": "forbidden"}) from None
        try:
            require_capability(admin, capability)
        except AdminForbidden:
            _audit_deny(
                request,
                action=action,
                reason="capability",
                actor_email=admin.email,
                actor_admin_id=admin.id,
            )
            raise HTTPException(status_code=403, detail={"code": "forbidden"}) from None

        audit_record(
            actor_admin_id=admin.id,
            actor_email=admin.email,
            action=action,
            outcome="allow",
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
        )
        return AdminContext(admin=admin, caller=caller)

    return dependency
