"""Persisted, single-use connector authorization capabilities.

Only a signed workspace administrator can mint an attempt. Callbacks authenticate
with its 256-bit nonce, and re-check that administrator before an atomic consume.
The database holds the nonce digest and an encrypted PKCE verifier, never either
bearer secret in plaintext. Composio retains provider tokens in its broker.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Any

from core.clock import utcnow
from fastapi import HTTPException
from sqlalchemy import select, update

from app.admin.authn import require_signed_caller
from app.authz.workspace import assert_workspace_access, subject_person_id
from app.connectors.secrets import decrypt_at_rest, encrypt_at_rest
from app.db.models import ContentOAuthState
from app.state import db
from app.tenancy import current_or_none
from app.workspaces.service import role_of


class OAuthError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def authorize_actor(workspace_id: str, actor: str) -> None:
    if not actor or actor in {"anonymous", "system"}:
        raise OAuthError("actor_required", "Sign in before connecting an account.")
    try:
        assert_workspace_access(workspace_id, actor)
        if role_of(workspace_id, subject_person_id(actor)) not in {"owner", "admin"}:
            raise OAuthError("workspace_admin_required", "A workspace administrator must connect accounts.")
    except HTTPException as exc:
        raise OAuthError("authorization_revoked", "Workspace access is no longer available.") from exc


def initiating_actor(workspace_id: str) -> str:
    scope = current_or_none()
    if not require_signed_caller():
        from app.workspaces.resolve import LOCAL_PERSON_ID
        actor = scope.actor if scope and scope.workspace_id == workspace_id and scope.actor != "anonymous" else LOCAL_PERSON_ID
        authorize_actor(workspace_id, actor)
        return actor
    if not scope or scope.workspace_id != workspace_id:
        raise OAuthError("actor_required", "A scoped signed request is required.")
    actor = scope.actor
    if require_signed_caller() and actor in {"founder", "local", "local-person"}:
        raise OAuthError("actor_required", "A signed founder identity is required.")
    authorize_actor(workspace_id, actor)
    return actor


def digest(state: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state):
        raise OAuthError("invalid_state", "Authorization attempt is invalid or expired.")
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def create(*, workspace_id: str, platform: str, redirect_uri: str, return_to: str,
           auth_method: str, verifier: str | None = None, gmail_intent: str | None = None,
           gmail_mailbox_id: str | None = None) -> str:
    actor = initiating_actor(workspace_id)
    state = secrets.token_urlsafe(32)
    now = utcnow()
    with db()() as session:
        session.add(ContentOAuthState(
            id=digest(state), workspace_id=workspace_id, platform=platform,
            actor_subject=actor, auth_method=auth_method,
            gmail_intent=gmail_intent, gmail_mailbox_id=gmail_mailbox_id,
            code_verifier="enc:v1:" + encrypt_at_rest(verifier) if verifier else None,
            redirect_uri=redirect_uri, return_to=return_to,
            expires_at=now + timedelta(minutes=15), created_at=now,
        ))
        session.commit()
    return state


def bind_connection(state: str, *, user_id: str, auth_config_id: str, connection_id: str) -> None:
    if not all(isinstance(v, str) and v for v in (user_id, auth_config_id, connection_id)):
        raise OAuthError("unbound_connection", "Provider did not return an account binding.")
    with db()() as session:
        changed = session.execute(update(ContentOAuthState).where(
            ContentOAuthState.id == digest(state), ContentOAuthState.consumed_at.is_(None),
            ContentOAuthState.connected_account_id.is_(None),
            ContentOAuthState.expires_at > utcnow(),
        ).values(provider_user_id=user_id, auth_config_id=auth_config_id,
                 connected_account_id=connection_id))
        if changed.rowcount != 1:
            raise OAuthError("invalid_state", "Authorization attempt is invalid or expired.")
        session.commit()


def consume(state: str, platform: str) -> dict[str, Any]:
    key, now = digest(state), utcnow()
    with db()() as session:
        row = session.get(ContentOAuthState, key)
        if row is None or row.platform != platform or not row.actor_subject:
            raise OAuthError("invalid_state", "Authorization attempt is invalid or expired.")
        authorize_actor(row.workspace_id, row.actor_subject)
        # Requiring a receipt prevents callbacks racing the Connect Link response.
        if row.auth_method == "composio" and not all((row.connected_account_id, row.auth_config_id, row.provider_user_id)):
            raise OAuthError("unbound_connection", "Authorization is not ready. Restart connection.")
        data = {column.name: getattr(row, column.name) for column in ContentOAuthState.__table__.columns}
        changed = session.execute(update(ContentOAuthState).where(
            ContentOAuthState.id == key, ContentOAuthState.platform == platform,
            ContentOAuthState.consumed_at.is_(None), ContentOAuthState.expires_at > now,
        ).values(consumed_at=now, code_verifier=None).execution_options(synchronize_session=False).returning(ContentOAuthState.id)).scalar_one_or_none()
        if changed is None:
            raise OAuthError("invalid_state", "Authorization attempt is invalid, expired, or already used.")
        session.commit()
    return data


def verifier(attempt: dict[str, Any]) -> str | None:
    value = attempt.get("code_verifier")
    if value is None:
        return None
    if not value.startswith("enc:v1:"):
        raise OAuthError("invalid_verifier", "Restart authorization to obtain a secure link.")
    return decrypt_at_rest(value[7:])


def record_outcome(state_digest: str, outcome: str) -> None:
    if outcome not in {"connected", "failed"}:
        raise ValueError("Invalid authorization outcome")
    with db()() as session:
        session.execute(update(ContentOAuthState).where(
            ContentOAuthState.id == state_digest, ContentOAuthState.consumed_at.is_not(None),
            ContentOAuthState.outcome.is_(None),
        ).values(outcome=outcome))
        session.commit()


def successful_receipts(workspace_id: str, *, platform: str | None = None) -> list[dict[str, Any]]:
    with db()() as session:
        query = select(ContentOAuthState).where(ContentOAuthState.workspace_id == workspace_id,
            ContentOAuthState.auth_method == "composio",
            ContentOAuthState.actor_subject.is_not(None), ContentOAuthState.connected_account_id.is_not(None))
        if platform:
            query = query.where(ContentOAuthState.platform == platform)
        else:
            query = query.where(ContentOAuthState.platform != "gmail")
        if platform == "gmail":
            # Google consent can finish even when our callback never arrives or mailbox sync fails.
            query = query.where(ContentOAuthState.auth_config_id.is_not(None), ContentOAuthState.provider_user_id.is_not(None))
        else:
            query = query.where(ContentOAuthState.outcome == "connected")
        rows = session.scalars(query.order_by(ContentOAuthState.created_at.desc())).all()
        receipts = {}
        for row in rows:
            receipts.setdefault(row.connected_account_id,
                {column.name: getattr(row, column.name) for column in ContentOAuthState.__table__.columns})
        return list(receipts.values())
