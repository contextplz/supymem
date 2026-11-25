"""Admin identity: bootstrap from env, DB rows, lockout, encrypted TOTP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.secrets import decrypt_at_rest, encrypt_at_rest
from app.db.models import AdminUser
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

LOCK_AFTER_FAILURES = 5
LOCK_MINUTES = 15
VALID_ROLES = frozenset({"super_admin", "finance", "support", "read_only"})
VALID_STATUSES = frozenset({"active", "disabled", "revoked"})


@dataclass(frozen=True)
class AdminIdentity:
    id: str | None
    email: str
    role: str
    status: str
    source: str  # "env" | "db"
    totp_enrolled: bool
    failed_attempts: int = 0
    locked_until: datetime | None = None
    created_by: str | None = None


class AdminInactive(Exception):
    """status != active or currently locked."""


def _session_factory() -> sessionmaker[Session]:
    return db()  # type: ignore[no-untyped-call,no-any-return]


def _norm_email(email: str) -> str:
    return email.strip().lower()


def bootstrap_emails() -> frozenset[str]:
    raw = os.environ.get("ADMIN_BOOTSTRAP_EMAILS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _row_to_identity(row: AdminUser, *, source: str | None = None) -> AdminIdentity:
    email = _norm_email(row.email)
    src = source or ("env" if email in bootstrap_emails() else "db")
    role = "super_admin" if src == "env" else row.role
    return AdminIdentity(
        id=row.id,
        email=email,
        role=role,
        status=row.status,
        source=src,
        totp_enrolled=bool(row.totp_secret_enc and row.totp_enrolled_at),
        failed_attempts=int(row.failed_attempts or 0),
        locked_until=as_utc(row.locked_until),
        created_by=row.created_by,
    )


def _env_identity(email: str) -> AdminIdentity:
    return AdminIdentity(
        id=None,
        email=email,
        role="super_admin",
        status="active",
        source="env",
        totp_enrolled=False,
    )


def admin_for_email(email: str) -> AdminIdentity | None:
    email_n = _norm_email(email)
    if not email_n:
        return None
    factory = _session_factory()
    with factory() as session:
        row = session.scalar(select(AdminUser).where(AdminUser.email == email_n))
        if row is not None:
            ident = _row_to_identity(row)
            session.expunge(row)
            return ident
    if email_n in bootstrap_emails():
        return _env_identity(email_n)
    return None


def is_locked(admin: AdminIdentity, *, now: datetime | None = None) -> bool:
    until = as_utc(admin.locked_until)
    if until is None:
        return False
    return until > (now or utcnow())


def assert_active(admin: AdminIdentity, *, now: datetime | None = None) -> None:
    if admin.status != "active":
        raise AdminInactive("admin is not active")
    if is_locked(admin, now=now):
        raise AdminInactive("admin is locked")


def create_admin(
    email: str,
    role: str,
    *,
    created_by: str,
    totp_secret: str | None = None,
    status: str = "active",
) -> AdminIdentity:
    email_n = _norm_email(email)
    if not email_n or "@" not in email_n:
        raise ValueError("invalid_email")
    if role not in VALID_ROLES:
        raise ValueError("invalid_role")
    if status not in VALID_STATUSES:
        raise ValueError("invalid_status")
    if email_n in bootstrap_emails():
        role = "super_admin"
    now = utcnow()
    row = AdminUser(
        id=new_id("adm"),
        email=email_n,
        role=role,
        status=status,
        totp_secret_enc=encrypt_at_rest(totp_secret) if totp_secret else None,
        totp_enrolled_at=now if totp_secret else None,
        created_at=now,
        created_by=created_by,
        failed_attempts=0,
        locked_until=None,
    )
    factory = _session_factory()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        ident = _row_to_identity(row)
        session.expunge(row)
        return ident


def set_role(admin_id: str, role: str) -> AdminIdentity:
    if role not in VALID_ROLES:
        raise ValueError("invalid_role")
    factory = _session_factory()
    with factory() as session:
        row = session.get(AdminUser, admin_id)
        if row is None:
            raise KeyError(admin_id)
        if _norm_email(row.email) in bootstrap_emails():
            role = "super_admin"
        row.role = role
        session.commit()
        session.refresh(row)
        ident = _row_to_identity(row)
        session.expunge(row)
        return ident


def set_status(admin_id: str, status: str) -> AdminIdentity:
    if status not in VALID_STATUSES:
        raise ValueError("invalid_status")
    factory = _session_factory()
    with factory() as session:
        row = session.get(AdminUser, admin_id)
        if row is None:
            raise KeyError(admin_id)
        row.status = status
        session.commit()
        session.refresh(row)
        ident = _row_to_identity(row)
        session.expunge(row)
        return ident


def list_admins() -> list[AdminIdentity]:
    factory = _session_factory()
    seen: set[str] = set()
    out: list[AdminIdentity] = []
    with factory() as session:
        rows = list(session.scalars(select(AdminUser).order_by(AdminUser.created_at)).all())
        for row in rows:
            ident = _row_to_identity(row)
            session.expunge(row)
            seen.add(ident.email)
            out.append(ident)
    for email in sorted(bootstrap_emails() - seen):
        out.insert(0, _env_identity(email))
    return out


def record_login(email: str) -> None:
    email_n = _norm_email(email)
    factory = _session_factory()
    with factory() as session:
        row = _get_or_create_row(session, email_n)
        row.failed_attempts = 0
        row.locked_until = None
        row.last_login_at = utcnow()
        session.commit()


def record_failure(email: str, *, now: datetime | None = None) -> AdminIdentity | None:
    """Persist lockout only for known admins (incl. bootstrap). Unknown emails
    are rate-limited at the gateway so we do not mint admin_users rows for them.
    """
    email_n = _norm_email(email)
    existing = admin_for_email(email_n)
    if existing is None:
        return None
    ts = now or utcnow()
    factory = _session_factory()
    with factory() as session:
        row = _get_or_create_row(session, email_n)
        until = as_utc(row.locked_until)
        if until is not None and until <= ts:
            row.failed_attempts = 0
            row.locked_until = None
        row.failed_attempts = int(row.failed_attempts or 0) + 1
        if row.failed_attempts >= LOCK_AFTER_FAILURES:
            row.locked_until = ts + timedelta(minutes=LOCK_MINUTES)
        session.commit()
        session.refresh(row)
        ident = _row_to_identity(row)
        session.expunge(row)
        return ident


def _get_or_create_row(session: Session, email_n: str) -> AdminUser:
    row = session.scalar(select(AdminUser).where(AdminUser.email == email_n))
    if row is not None:
        return row
    # Only bootstrap emails are created implicitly (lockout / first TOTP enrol).
    if email_n not in bootstrap_emails():
        raise KeyError(email_n)
    row = AdminUser(
        id=new_id("adm"),
        email=email_n,
        role="super_admin",
        status="active",
        created_at=utcnow(),
        created_by="bootstrap",
        failed_attempts=0,
    )
    session.add(row)
    session.flush()
    return row


def load_totp_secret(email: str) -> str | None:
    email_n = _norm_email(email)
    factory = _session_factory()
    with factory() as session:
        row = session.scalar(select(AdminUser).where(AdminUser.email == email_n))
        if row is None or not row.totp_secret_enc:
            return None
        return decrypt_at_rest(row.totp_secret_enc)


def enroll_totp(email: str, secret: str) -> AdminIdentity:
    email_n = _norm_email(email)
    factory = _session_factory()
    with factory() as session:
        row = _get_or_create_row(session, email_n)
        if email_n in bootstrap_emails():
            row.role = "super_admin"
            row.status = "active"
        row.totp_secret_enc = encrypt_at_rest(secret)
        row.totp_enrolled_at = utcnow()
        session.commit()
        session.refresh(row)
        ident = _row_to_identity(row)
        session.expunge(row)
        return ident


def ensure_db_row(admin: AdminIdentity) -> AdminIdentity:
    """Guarantee an ``admin_users`` row so sessions can reference ``admin_user_id``."""
    if admin.id:
        return admin
    existing = admin_for_email(admin.email)
    if existing and existing.id:
        return existing
    return create_admin(
        admin.email,
        admin.role,
        created_by="bootstrap",
        status="active",
    )
