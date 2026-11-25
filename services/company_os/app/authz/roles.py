"""Five roles, seven capabilities (42 §2).

The matrix is data, not branching logic, so the test can assert all 35 cells.
"""

from __future__ import annotations

from typing import Literal

Capability = Literal[
    "read",
    "request_work",
    "approve",
    "connect_systems",
    "change_autonomy",
    "invite",
    "offboard",
    "manage_billing",
    "manage_apps",
    "approve_production",
    "run_terminal",
    "manage_external_aws",
]

CAPABILITIES: tuple[Capability, ...] = (
    "read",
    "request_work",
    "approve",
    "connect_systems",
    "change_autonomy",
    "invite",
    "offboard",
    "manage_billing",
    "manage_apps",
    "approve_production",
    "run_terminal",
    "manage_external_aws",
)

Role = Literal["owner", "admin", "maintainer", "contributor", "viewer"]

ROLES: dict[str, frozenset[str]] = {
    "owner": frozenset(CAPABILITIES),
    # An admin runs the workspace but cannot remove it from under the owner.
    "admin": frozenset(CAPABILITIES) - {"offboard", "manage_external_aws"},
    # Can approve work, but cannot widen permissions: different risk classes.
    "maintainer": frozenset({"read", "request_work", "approve", "manage_apps", "run_terminal"}),
    "contributor": frozenset({"read", "request_work"}),
    "viewer": frozenset({"read"}),
}

ROLE_RANK: dict[str, int] = {
    "viewer": 0,
    "contributor": 1,
    "maintainer": 2,
    "admin": 3,
    "owner": 4,
}


def role_allows(role: str | None, capability: str) -> bool:
    if not role:
        return False
    return capability in ROLES.get(role, frozenset())


def roles_with(capability: str) -> list[str]:
    """Roles that hold a capability, weakest first — used for escalation (42 §3)."""
    return sorted(
        (r for r, caps in ROLES.items() if capability in caps),
        key=lambda r: ROLE_RANK[r],
    )
