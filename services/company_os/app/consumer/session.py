"""Legacy local-only HS256 session issued after a Cognito identity is accepted.

The token is the website Authorization bearer and the desktop
``HOSTED_ACCOUNT_TOKEN``. It carries Cognito ``sub`` + email + our user id.
Hosted issuance and parsing are disabled; gateway sessions and PKCE are canonical.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from app.consumer.cognito import CognitoIdentity
from app.core.local_mode import is_local


class SessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AccountPrincipal:
    user_id: str
    email: str
    cognito_sub: str


def session_secret() -> str:
    if os.environ.get("APP_ENV", "local").strip().lower() not in {"local", "test", ""}:
        raise SessionError(
            "legacy_account_auth_disabled",
            "Hosted founder access uses the gateway session and desktop PKCE flow.",
        )
    raw = (
        os.environ.get("ACCOUNT_SESSION_SECRET")
        or os.environ.get("SESSION_SECRET")
        or ""
    ).strip()
    if len(raw) < 32:
        # Local/tests only. Production go-live sets ACCOUNT_SESSION_SECRET.
        raw = (raw + "local-account-session-secret-32b!!")[:48]
    return raw


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def issue_session(
    *,
    user_id: str,
    identity: CognitoIdentity,
    ttl_s: int = 12 * 3600,
    now: int | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {
                "iss": "company-os",
                "uid": user_id,
                "sub": identity.sub,
                "email": identity.email,
                "iat": issued,
                "exp": issued + int(ttl_s),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    signing = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(session_secret().encode("utf-8"), signing, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def parse_session(token: str | None) -> AccountPrincipal:
    if is_local():
        from app.core.local_owner import current_owner

        owner = current_owner()
        return AccountPrincipal(user_id=owner.id, email=owner.email or "", cognito_sub=owner.id)
    presented = (token or "").strip()
    if presented.lower().startswith("bearer "):
        presented = presented[7:].strip()
    parts = presented.split(".")
    if len(parts) != 3:
        raise SessionError("invalid_session", "Sign in to continue")
    signing = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected = hmac.new(session_secret().encode("utf-8"), signing, hashlib.sha256).digest()
    try:
        presented_sig = _b64url_decode(parts[2])
    except Exception as exc:
        raise SessionError("invalid_session", "Sign in to continue") from exc
    if not hmac.compare_digest(expected, presented_sig):
        raise SessionError("invalid_session", "Sign in to continue")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:
        raise SessionError("invalid_session", "Sign in to continue") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise SessionError("expired_session", "Session expired. Sign in again.")
    user_id = str(payload.get("uid") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    sub = str(payload.get("sub") or "").strip()
    if not user_id or not email or not sub:
        raise SessionError("invalid_session", "Sign in to continue")
    return AccountPrincipal(user_id=user_id, email=email, cognito_sub=sub)
