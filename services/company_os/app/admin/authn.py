"""Verify the gateway HMAC assertion (doc 99 §6.2, threat #5)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from core.assertion import (
    MIN_SECRET_BYTES,
    AssertionFailure,
    AssertionPayload,
    WeakSecret,
    reset_nonce_store,
    verify_assertion,
)
from fastapi import HTTPException, Request

from app.core.local_mode import is_local

log = logging.getLogger(__name__)

ASSERTION_HEADER = "x-cos-assertion"
SUBJECT_HEADER = "x-subject"

_UNENFORCED_LOGGED = False


class SecurityConfigError(RuntimeError):
    """Misconfigured security control — refuse to boot."""


@dataclass(frozen=True)
class CallerContext:
    sub: str
    email: str
    role: str
    path: str
    method: str
    exp: int
    nonce: str
    legacy: bool = False
    device_id: str | None = None


def assertion_secret() -> str:
    return (os.environ.get("COS_INTERNAL_ASSERTION_SECRET") or "").strip()


def require_signed_caller() -> bool:
    return os.environ.get("COMPANY_OS_REQUIRE_SIGNED_CALLER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def admin_require_totp() -> bool:
    """Whether a second factor is mandatory for admin login.

    Unset means "follow the deployment posture": a signed-caller deployment is
    hosted, and hosted admin without MFA is a password away from total control.
    Defaulting this to off would make the most privileged surface fail *open* on
    the single most likely operator mistake — forgetting one env var.
    """
    raw = os.environ.get("ADMIN_REQUIRE_TOTP", "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return require_signed_caller()


def validate_security_settings() -> None:
    """Fail fast (doc 99 §9). A silent no-op control is worse than one that is off."""
    if is_local():
        return
    global _UNENFORCED_LOGGED
    secret = assertion_secret()
    if require_signed_caller():
        if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise SecurityConfigError(
                "COMPANY_OS_REQUIRE_SIGNED_CALLER=1 requires "
                "COS_INTERNAL_ASSERTION_SECRET of at least 32 bytes"
            )
        if not admin_require_totp():
            raise SecurityConfigError(
                "ADMIN_REQUIRE_TOTP=0 is refused on a signed-caller (hosted) "
                "deployment; admin login would be password-only"
            )
        # TOTP secrets are encrypted with the vault key. If that key is the
        # built-in local default, anyone who can read the database can mint
        # valid second factors, so MFA would only look enforced.
        from app.connectors.secrets import LOCAL_VAULT_SECRET, vault_secret

        if vault_secret() == LOCAL_VAULT_SECRET:
            raise SecurityConfigError(
                "COMPANY_OS_REQUIRE_SIGNED_CALLER=1 requires COMPANY_OS_VAULT_SECRET "
                "(or SESSION_SECRET); the default local vault key cannot protect "
                "TOTP secrets at rest"
            )
    else:
        if not _UNENFORCED_LOGGED:
            log.warning(
                "COMPANY_OS_REQUIRE_SIGNED_CALLER is off; "
                "company_os will trust the legacy X-Subject header"
            )
            _UNENFORCED_LOGGED = True
    if admin_require_totp():
        from app.admin.identity import bootstrap_emails

        if not bootstrap_emails():
            raise SecurityConfigError(
                "ADMIN_REQUIRE_TOTP=1 requires ADMIN_BOOTSTRAP_EMAILS to be set"
            )


def _payload_to_caller(payload: AssertionPayload) -> CallerContext:
    return CallerContext(
        sub=payload.sub,
        email=payload.email,
        role=payload.role,
        path=payload.path,
        method=payload.method,
        exp=payload.exp,
        nonce=payload.nonce,
        legacy=False,
        device_id=payload.device_id,
    )


def verify_caller(
    request: Request,
    *,
    required: bool | None = None,
) -> CallerContext:
    """Verify ``X-COS-Assertion``, bound to this request's path and method.

    When ``required`` is None, the env flag decides. Admin routes pass
    ``required=True`` so the flag cannot turn the second gate off.
    """
    if is_local():
        from app.core.local_owner import request_owner

        owner = request_owner(request)
        return CallerContext(sub=owner.id, email=owner.email or "", role="user",
                             path=request.url.path, method=request.method, exp=0, nonce="")
    must = require_signed_caller() if required is None else required
    token = request.headers.get(ASSERTION_HEADER) or request.headers.get(
        "X-COS-Assertion"
    )
    secret = assertion_secret()
    path = request.url.path
    method = request.method
    if token:
        # Middleware and endpoint dependencies share ASGI request state. A nonce
        # is consumed once per HTTP request, never once per dependency. Bind the
        # cache to the assertion and request target so it cannot authorize a
        # different request or a mutated token.
        cached = getattr(request.state, "cos_verified_caller", None)
        if cached and cached[:3] == (token, path, method):
            caller = cached[3]
        else:
            caller = _verify_signed(token, secret=secret, path=path, method=method)
        subject = request.headers.get(SUBJECT_HEADER)
        if subject is not None and subject.strip() != caller.sub:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_assertion", "reason": "subject_mismatch"},
            )
        if caller.sub.strip() == "system":
            raise HTTPException(status_code=401, detail={"code": "unauthenticated"})
        request.state.cos_verified_caller = (token, path, method, caller)
        return caller

    if must:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_assertion", "reason": "missing"},
        )

    subject = (request.headers.get(SUBJECT_HEADER) or "").strip()
    if not subject or subject == "system":
        raise HTTPException(status_code=401, detail={"code": "unauthenticated"})
    return CallerContext(
        sub=subject, email="", role="user", path=path, method=method,
        exp=0, nonce="", legacy=True,
    )


def _verify_signed(token: str, *, secret: str, path: str, method: str) -> CallerContext:
    """Consume an assertion nonce after cryptographic/target validation."""
    try:
        payload = verify_assertion(secret, token, path=path, method=method)
    except WeakSecret as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_assertion", "reason": "weak_secret"},
        ) from exc
    except AssertionFailure as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_assertion", "reason": exc.reason},
        ) from exc
    return _payload_to_caller(payload)


def reset_authn_state() -> None:
    global _UNENFORCED_LOGGED
    _UNENFORCED_LOGGED = False
    reset_nonce_store()
