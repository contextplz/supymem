"""Admin capability matrix (doc 99 §6.4). Deny by default."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.admin.identity import AdminIdentity

# Every capability an endpoint may declare. A typo fails tests rather than
# silently granting: ROLE_CAPABILITIES values must be a subset of this set.
CAPABILITIES: frozenset[str] = frozenset(
    {
        "session.read",
        "users.view",
        "ventures.view",
        "ledger.view",
        "audit.view",
        "credits.grant",
        "credits.revoke",
        "policies.write",
        "accounts.suspend",
        "payments.act",
        "users.force_logout",
        "users.reset_password",
        "users.impersonate",
        "users.delete",
        "users.export",
        "team.manage",
        "ops.kill_switch",
        "ops.flags",
    }
)

_VIEW: frozenset[str] = frozenset(
    {
        "session.read",
        "users.view",
        "ventures.view",
        "ledger.view",
        "audit.view",
    }
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "super_admin": CAPABILITIES,
    "finance": _VIEW
    | frozenset(
        {
            "credits.grant",
            "credits.revoke",
            "policies.write",
            "accounts.suspend",
            "payments.act",
        }
    ),
    "support": _VIEW
    | frozenset(
        {
            "credits.grant",
            "accounts.suspend",
            "users.force_logout",
            "users.reset_password",
            "users.impersonate",
        }
    ),
    "read_only": _VIEW,
}

ROLES: frozenset[str] = frozenset(ROLE_CAPABILITIES)

# Policy defaults (credits) used until app.credits.policy exists.
_DEFAULT_DUAL_CONTROL = 50_000


class AdminForbidden(Exception):
    """Caller lacks the declared capability. Always audited by the caller."""

    def __init__(self, capability: str, role: str | None = None) -> None:
        super().__init__(f"role {role!r} lacks {capability!r}")
        self.capability = capability
        self.role = role


def has_capability(role: str | None, capability: str) -> bool:
    if not role or capability not in CAPABILITIES:
        return False
    return capability in ROLE_CAPABILITIES.get(role, frozenset())


def require_capability(admin: AdminIdentity, capability: str) -> None:
    role = getattr(admin, "role", None)
    if not has_capability(role, capability):
        raise AdminForbidden(capability, role)


def capabilities_for(role: str | None) -> frozenset[str]:
    if not role:
        return frozenset()
    return ROLE_CAPABILITIES.get(role, frozenset())


def _policy_int(key: str, default: int) -> int:
    # TODO: switch to app.credits.policy once that module exists.
    try:
        from app.credits import policy as policy_mod  # type: ignore[attr-defined]

        getter = getattr(policy_mod, "get_int", None) or getattr(
            policy_mod, "int_value", None
        )
        if callable(getter):
            return int(getter(key, default=default))
    except Exception:
        pass
    try:
        import json

        from sqlalchemy import select

        from app.db.models import CreditPolicy
        from app.state import db

        factory = db()
        with factory() as session:
            row = session.scalar(select(CreditPolicy).where(CreditPolicy.key == key))
            if row is None:
                return default
            value = json.loads(row.value_json)
            return int(value)
    except Exception:
        return default


def grant_cap_credits(role: str | None) -> int | None:
    """Max credits this role may grant in one request. None = uncapped."""
    if role == "super_admin":
        return None
    if role in {"support", "finance"}:
        return _policy_int(f"max_grant_credits_{role}", 2000 if role == "support" else 5000)
    return 0


def dual_control_threshold_credits() -> int:
    return _policy_int("dual_control_threshold", _DEFAULT_DUAL_CONTROL)
