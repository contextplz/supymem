"""App API keys (sk_app_ / pk_app_). Secret is shown once; only sha256 is stored."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import select

from app.db.supymem_models import AppApiKey, FounderApp
from app.state import db
from app.db.models import Venture
from app.tenancy import bind, current_or_none
from core.clock import utcnow
from core.ids import new_id

_SECRET_BYTES = 24


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_key(
    app_id: str,
    *,
    scopes: list[str] | None = None,
    monthly_cap_credits: int | None = None,
    kind: str = "secret",
) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    prefix_body = new_id("key")[4:16]
    if kind == "ingest":
        token = f"pk_app_{prefix_body}"
        prefix = token
        secret = token
        display_secret = token
    else:
        secret = secrets.token_urlsafe(_SECRET_BYTES)
        prefix = f"sk_app_{prefix_body}"
        token = f"{prefix}.{secret}"
        display_secret = token
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise KeyError("app")
        row = AppApiKey(
            id=new_id("apk"),
            app_id=app_id,
            workspace_id=app.workspace_id,
            prefix=prefix,
            key_hash=_hash_secret(secret if kind != "ingest" else token),
            scopes=json.dumps(scopes or []),
            monthly_cap_credits=monthly_cap_credits,
            state="active",
            created_at=now,
        )
        session.add(row)
        session.commit()
        return {
            "id": row.id,
            "prefix": prefix,
            "secret": display_secret,
            "scopes": scopes or [],
            "monthly_cap_credits": monthly_cap_credits,
            "kind": kind,
        }


def list_keys(app_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(AppApiKey).where(AppApiKey.app_id == app_id)))
        return [
            {
                "id": r.id,
                "prefix": r.prefix,
                "scopes": json.loads(r.scopes or "[]"),
                "monthly_cap_credits": r.monthly_cap_credits,
                "state": r.state,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            }
            for r in rows
        ]


def revoke_key(key_id: str) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(AppApiKey, key_id)
        if row is None:
            raise KeyError("key")
        row.state = "revoked"
        row.revoked_at = now
        session.commit()
        return {"id": row.id, "state": row.state}


def lookup_bearer(token: str) -> dict[str, Any] | None:
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return None
    if raw.startswith("pk_app_"):
        prefix = raw
        secret = raw
    elif "." in raw and raw.startswith("sk_app_"):
        prefix, _, secret = raw.partition(".")
    else:
        return None
    digest = _hash_secret(secret)
    factory = db()
    incoming = current_or_none()
    with factory() as session:
        row = session.scalar(
            select(AppApiKey).where(
                AppApiKey.prefix == prefix,
                AppApiKey.key_hash == digest,
                AppApiKey.state == "active",
                AppApiKey.revoked_at.is_(None),
            )
        )
        if row is None:
            return None
        key_id, app_id, workspace_id = row.id, row.app_id, row.workspace_id
    if not workspace_id or (incoming and incoming.workspace_id not in {"", "none", workspace_id}):
        return None
    # This authority comes only from the verified durable key. A fresh session
    # starts after binding, so tenant SQL and ORM identity caches cannot inherit
    # an earlier anonymous transaction or another caller's scope.
    with bind(workspace_id=workspace_id, venture_id="", actor=key_id):
        with factory() as session:
            row = session.scalar(select(AppApiKey).where(
                AppApiKey.id == key_id, AppApiKey.app_id == app_id,
                AppApiKey.workspace_id == workspace_id, AppApiKey.prefix == prefix,
                AppApiKey.key_hash == digest, AppApiKey.state == "active",
                AppApiKey.revoked_at.is_(None),
            ))
            if row is None:
                return None
            app = session.scalar(select(FounderApp).where(
                FounderApp.id == app_id, FounderApp.workspace_id == workspace_id,
                FounderApp.deleted_at.is_(None),
            ))
            if app is None or not app.venture_id or (incoming and incoming.venture_id
                    and incoming.venture_id != app.venture_id):
                return None
            venture = session.scalar(select(Venture).where(
                Venture.id == app.venture_id, Venture.workspace_id == workspace_id,
            ))
            if venture is None:
                return None
            result = {
                "id": row.id, "app_id": row.app_id, "workspace_id": row.workspace_id,
                "venture_id": app.venture_id, "app_state": app.state,
                "scopes": json.loads(row.scopes or "[]"),
                "monthly_cap_credits": row.monthly_cap_credits, "prefix": row.prefix,
            }
            row.last_used_at = utcnow()
            session.commit()
            return result


def ensure_ingest_key(app_id: str) -> dict[str, Any]:
    """Return the single active public ingest key for an app.

    Concurrent callers are serialized with a transaction advisory lock so two
    active ``pk_app_`` keys cannot be created for the same app.
    """
    from app.db.session import serialize_writes

    factory = db()
    with factory() as session:
        serialize_writes(session, key=f"app_ingest_key:{app_id}")
        existing = session.scalar(
            select(AppApiKey).where(
                AppApiKey.app_id == app_id,
                AppApiKey.prefix.startswith("pk_app_"),
                AppApiKey.state == "active",
            )
        )
        if existing is not None:
            return {
                "id": existing.id,
                "prefix": existing.prefix,
                "secret": existing.prefix,
                "kind": "ingest",
            }
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise KeyError("app")
        now = utcnow()
        prefix_body = new_id("key")[4:16]
        token = f"pk_app_{prefix_body}"
        row = AppApiKey(
            id=new_id("apk"),
            app_id=app_id,
            workspace_id=app.workspace_id,
            prefix=token,
            key_hash=_hash_secret(token),
            scopes=json.dumps(["ingest"]),
            monthly_cap_credits=None,
            state="active",
            created_at=now,
        )
        session.add(row)
        session.commit()
        return {"id": row.id, "prefix": token, "secret": token, "kind": "ingest"}
