"""Tokenized public unsubscribe that stays inside one workspace (doc 130 OT-35)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any
from urllib.parse import quote

from core.clock import utcnow
from core.env_contract import parse_app_env
from core.ids import new_id
from sqlalchemy import select

from app.core.local_mode import is_local
from app.db.models import GtmContact, GtmSuppression
from app.gtm.relationships.scope import gtm_get
from app.gtm.relationships.service import OutreachError
from app.state import db

_LOCAL_SECRET = "outreach-unsub-local-secret-32bytes!!"
_PUBLIC_DEFAULT_SECRETS = frozenset({
    _LOCAL_SECRET,
    "local-account-session-secret-32b!!",
    "dev-session-secret-change-me-32b!!",
})
REPLY_OPT_OUT = "Reply STOP to stop these emails."


def _secret() -> bytes:
    if is_local():
        from app.core.local_owner import _ensure_proxy_secret
        from core.local_security import _proxy_secret
        _ensure_proxy_secret()
        return hmac.new(_proxy_secret(), b"outreach-unsubscribe:v1", hashlib.sha256).digest()
    local = parse_app_env() == "local"
    for source in ("OUTREACH_UNSUB_SECRET", "ACCOUNT_SESSION_SECRET", "SESSION_SECRET"):
        raw = (os.environ.get(source) or "").strip()
        if raw:
            break
    key = raw.encode("utf-8")
    valid = len(key) >= 32 and raw not in _PUBLIC_DEFAULT_SECRETS
    if not local and not valid:
        raise OutreachError("unsubscribe_unavailable", 503)
    if source == "SESSION_SECRET":
        if valid:
            # Domain separation prevents an unsubscribe MAC from being a valid
            # session MAC even when both are backed by the configured session key.
            return hmac.new(key, b"supymem:outreach-unsubscribe:v1", hashlib.sha256).digest()
        return _LOCAL_SECRET.encode("utf-8")
    return key or _LOCAL_SECRET.encode("utf-8")


def issue_unsubscribe_token(
    workspace_id: str, contact_id: str, email: str | None = None
) -> str:
    email_n = (email or "").strip().lower()
    payload = f"{workspace_id}|{contact_id}|{email_n}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(token: str) -> tuple[str, str, str] | None:
    presented = (token or "").strip()
    if not presented:
        return None
    pad = "=" * ((4 - len(presented) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(presented + pad).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    parts = raw.split("|")
    if len(parts) != 4:
        return None
    workspace_id, contact_id, email_n, sig = parts
    payload = f"{workspace_id}|{contact_id}|{email_n}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return workspace_id, contact_id, email_n


def unsubscribe_url(workspace_id: str, contact_id: str, email: str | None = None) -> str:
    if is_local():
        from app.gtm.local_mail import public_url
        base = public_url()
        if not base:
            return ""
        return f"{base}/public/unsubscribe?t={quote(issue_unsubscribe_token(workspace_id, contact_id, email))}"
    base = (os.environ.get("EMAIL_PUBLIC_URL") or os.environ.get("PUBLIC_GATEWAY_URL") or "https://gateway.supymem.com").rstrip("/")
    return f"{base}/public/unsubscribe?t={quote(issue_unsubscribe_token(workspace_id, contact_id, email))}"


def _contact(session, token: str) -> tuple[str, GtmContact, str]:
    parsed = _decode(token)
    if parsed is None:
        raise OutreachError("not_found", 404)
    workspace_id, contact_id, email_n = parsed
    contact = gtm_get(session, GtmContact, contact_id, workspace_id)
    if contact is None:
        raise OutreachError("not_found", 404)
    return workspace_id, contact, email_n


def preview_unsubscribe(token: str) -> None:
    with db()() as session:
        _contact(session, token)


def honor_unsubscribe_token(token: str) -> dict[str, Any]:
    now = utcnow()
    with db()() as session:
        workspace_id, contact, email_n = _contact(session, token)
        contact.unsubscribed_at = now
        contact.do_not_contact = 1
        email = email_n or (contact.email or "").strip().lower()
        if email:
            existing = session.scalar(
                select(GtmSuppression).where(
                    GtmSuppression.workspace_id == workspace_id,
                    GtmSuppression.email == email,
                )
            )
            if existing is None:
                session.add(
                    GtmSuppression(
                        id=new_id("gtmsup"),
                        workspace_id=workspace_id,
                        email=email,
                        reason="public_unsubscribe",
                        source="opt_out",
                        created_at=now,
                    )
                )
        from app.gtm.email_learning import suppress
        suppress(session, email)
        session.commit()
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "contact_id": contact.id,
        }
