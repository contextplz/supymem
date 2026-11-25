"""API tokens (41 §8). Plaintext is shown once and never stored.

Scopes deliberately exclude anything that would widen the system's own
permissions: autonomy changes, connection creation and offboarding need a
browser session with a real human.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select

from app.audit.writer import audit
from app.authz.effective import TOKEN_SCOPES
from app.db.models import ApiToken
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

TOKEN_PREFIX = "cos_"
VALID_SURFACES = frozenset({"mcp", "api", "cli", "widget", "sdk"})

_hasher = PasswordHasher()


@dataclass(frozen=True)
class MintedToken:
    id: str
    label: str
    surface: str
    scopes: list[str]
    workspace_id: str | None
    expires_at: str | None
    # Returned once, at creation.
    token: str

    def to_dict(self, *, include_token: bool = True) -> dict[str, Any]:
        body = {
            "id": self.id,
            "label": self.label,
            "surface": self.surface,
            "scopes": self.scopes,
            "workspace_id": self.workspace_id,
            "expires_at": self.expires_at,
        }
        if include_token:
            body["token"] = self.token
        return body


class TokenError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _lookup_hash(token: str) -> str:
    """Cheap deterministic index so verification is one row, not a table scan."""
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token(
    *,
    person_id: str,
    label: str,
    scopes: list[str],
    surface: str = "api",
    workspace_id: str | None = None,
    ttl_days: int | None = None,
) -> MintedToken:
    if surface not in VALID_SURFACES:
        raise TokenError("invalid_surface", f"Unknown surface: {surface}")
    unknown = sorted(set(scopes) - TOKEN_SCOPES)
    if unknown:
        raise TokenError(
            "invalid_scope",
            f"These scopes don't exist or can't be granted to a token: {', '.join(unknown)}",
        )

    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{secret}"
    now = utcnow()
    row = ApiToken(
        id=new_id("tok"),
        workspace_id=workspace_id,
        person_id=person_id,
        label=label[:120],
        # Argon2 for the real check, SHA-256 as the lookup index.
        hash=f"{_lookup_hash(token)}:{_hasher.hash(token)}",
        scopes_json=json.dumps(sorted(set(scopes))),
        surface=surface,
        expires_at=now + timedelta(days=ttl_days) if ttl_days else None,
        created_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        minted = MintedToken(
            id=row.id,
            label=row.label,
            surface=row.surface,
            scopes=sorted(set(scopes)),
            workspace_id=row.workspace_id,
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
            token=token,
        )

    audit(
        "token.created",
        workspace_id=workspace_id,
        actor_id=person_id,
        actor_kind="person",
        surface="api",
        target_ref=minted.id,
        payload={"label": label, "scopes": minted.scopes, "surface": surface},
    )
    return minted


@dataclass(frozen=True)
class VerifiedToken:
    id: str
    person_id: str
    workspace_id: str | None
    label: str
    surface: str
    scopes: frozenset[str]


def verify_token(token: str) -> VerifiedToken:
    """Resolve a bearer token, or raise. Revoked and expired fail immediately."""
    if not token or not token.startswith(TOKEN_PREFIX):
        raise TokenError("invalid_token", "That token isn't valid.")

    index = _lookup_hash(token)
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ApiToken).where(ApiToken.hash.startswith(f"{index}:"))
        ).first()
        if row is None:
            raise TokenError("invalid_token", "That token isn't valid.")
        if row.revoked_at is not None:
            _audit_rejection(row, "revoked")
            raise TokenError("token_revoked", "That token was revoked.")
        expires = as_utc(row.expires_at)
        if expires is not None and expires <= utcnow():
            _audit_rejection(row, "expired")
            raise TokenError("token_expired", "That token has expired.")
        try:
            _hasher.verify(row.hash.split(":", 1)[1], token)
        except (VerifyMismatchError, IndexError) as exc:
            raise TokenError("invalid_token", "That token isn't valid.") from exc

        row.last_used_at = utcnow()
        session.commit()
        try:
            scopes = frozenset(json.loads(row.scopes_json or "[]"))
        except json.JSONDecodeError:
            scopes = frozenset()
        return VerifiedToken(
            id=row.id,
            person_id=row.person_id,
            workspace_id=row.workspace_id,
            label=row.label,
            surface=row.surface,
            scopes=scopes,
        )


def _audit_rejection(row: ApiToken, reason: str) -> None:
    audit(
        "token.rejected",
        workspace_id=row.workspace_id,
        actor_id=row.person_id,
        actor_kind="token",
        surface=row.surface,
        target_ref=row.id,
        outcome="denied",
        denied_reason=reason,
    )


def revoke_token(token_id: str, *, actor_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(ApiToken, token_id)
        if row is None:
            raise TokenError("not_found", "That token doesn't exist.")
        row.revoked_at = utcnow()
        session.commit()
        workspace_id = row.workspace_id

    audit(
        "token.revoked",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface="api",
        target_ref=token_id,
    )
    return {"id": token_id, "revoked": True}


def list_tokens(person_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ApiToken)
            .where(ApiToken.person_id == person_id)
            .order_by(ApiToken.created_at.desc())
        ).all()
        return [
            {
                "id": r.id,
                "label": r.label,
                "surface": r.surface,
                "workspace_id": r.workspace_id,
                "scopes": json.loads(r.scopes_json or "[]"),
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            }
            for r in rows
        ]
