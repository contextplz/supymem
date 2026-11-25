"""Admin authentication, authorisation, and audit (doc 99 §6)."""

from __future__ import annotations

from app.admin.authz import (
    CAPABILITIES,
    ROLE_CAPABILITIES,
    AdminForbidden,
    has_capability,
    require_capability,
)
from app.admin.deps import AdminContext, require_admin
from app.admin.identity import AdminIdentity, admin_for_email

__all__ = [
    "AdminContext",
    "AdminForbidden",
    "AdminIdentity",
    "CAPABILITIES",
    "ROLE_CAPABILITIES",
    "admin_for_email",
    "has_capability",
    "require_admin",
    "require_capability",
]
