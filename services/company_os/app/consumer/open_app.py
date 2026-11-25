"""One-time desktop open tokens. The raw token is never stored."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select

from app.credits._txn import naive_utc, serialize_writes, session_factory
from app.db.models import ConsumerOpenToken
from core.ids import new_id  # type: ignore[import-not-found]

OPEN_TOKEN_TTL = timedelta(minutes=10)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_open_token(user_id: str) -> dict[str, object]:
    raw = secrets.token_urlsafe(32)
    now = naive_utc()
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=f"open:{user_id}")
        session.add(
            ConsumerOpenToken(
                id=new_id("cot"),
                user_id=user_id,
                token_hash=_hash(raw),
                expires_at=now + OPEN_TOKEN_TTL,
                consumed_at=None,
                created_at=now,
            )
        )
        session.commit()
    return {
        "token": raw,
        "deep_link": f"companyos://auth?token={raw}",
        "expires_in": int(OPEN_TOKEN_TTL.total_seconds()),
    }


class OpenTokenError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def consume_open_token(token: str) -> str:
    """Return the user_id bound to this unused token."""
    presented = (token or "").strip()
    if not presented:
        raise OpenTokenError("invalid_token", "Open token is missing")
    digest = _hash(presented)
    factory = session_factory()
    with factory() as session:
        serialize_writes(session, key=f"openhash:{digest}")
        row = session.scalars(
            select(ConsumerOpenToken).where(ConsumerOpenToken.token_hash == digest)
        ).first()
        if row is None:
            raise OpenTokenError("invalid_token", "Open token was not accepted")
        now = naive_utc()
        if row.consumed_at is not None:
            raise OpenTokenError("used_token", "Open token was already used")
        if row.expires_at <= now:
            raise OpenTokenError("expired_token", "Open token expired. Open the app from the website again.")
        row.consumed_at = now
        session.commit()
        return row.user_id
