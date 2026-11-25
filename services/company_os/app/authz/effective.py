"""Effective permission = min(token scopes, role, autonomy, observe level).

One implementation (41 §8, 42 §2). Scattering this logic into feature code is
how two dials end up checked inconsistently (B40).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.authz.roles import role_allows

AUTONOMY_LEVELS = ("L0", "L1", "L2", "L3", "L4")
OBSERVE_LEVELS = ("O0", "O1", "O2", "O3", "O4")

# Scopes a token may hold (41 §8). Deliberately absent: anything that would let
# a token widen the system's own permissions.
TOKEN_SCOPES = frozenset(
    {
        "read",
        "work:create",
        "work:approve",
        "workflow:run",
        "connections:read",
        "governance:read",
    }
)

# Capability -> the token scope that grants it. Capabilities with no entry can
# never be exercised by a token, only by a browser session with a real human.
CAPABILITY_TO_SCOPE: dict[str, str] = {
    "read": "read",
    "request_work": "work:create",
    "approve": "work:approve",
    "run_workflow": "workflow:run",
}

# Capability -> minimum autonomy level. Below this we may prepare but not act.
CAPABILITY_MIN_AUTONOMY: dict[str, str] = {
    "read": "L0",
    "request_work": "L0",
    "approve": "L0",
    "connect_systems": "L0",
    "change_autonomy": "L0",
    "invite": "L0",
    "offboard": "L0",
    "run_workflow": "L0",
}

# Actions against a connected system -> minimum observe level (38 §5).
TARGET_MIN_OBSERVE: dict[str, str] = {
    "connection:metadata": "O1",
    "connection:read": "O2",
    "connection:sandbox_write": "O3",
    "connection:write": "O4",
}


def autonomy_rank(level: str | None) -> int:
    try:
        return AUTONOMY_LEVELS.index(level or "L1")
    except ValueError:
        return 1


def observe_rank(level: str | None) -> int:
    try:
        return OBSERVE_LEVELS.index(level or "O0")
    except ValueError:
        return 0


@dataclass(frozen=True)
class EffectivePermission:
    allowed: bool
    reason: str = ""
    # Which of the four constraints was the binding one, for the audit line.
    limited_by: str | None = None
    detail: dict[str, str] = field(default_factory=dict)

    def audit_reason(self) -> str:
        if self.allowed:
            return "allowed"
        parts = [self.reason]
        if self.limited_by:
            parts.append(f"limited_by={self.limited_by}")
        for key, value in sorted(self.detail.items()):
            parts.append(f"{key}={value}")
        return " ".join(p for p in parts if p)


def effective(
    capability: str,
    *,
    role: str | None,
    autonomy_level: str | None,
    observe_level: str | None = "O0",
    token_scopes: frozenset[str] | set[str] | None = None,
    target_class: str | None = None,
) -> EffectivePermission:
    """Return the four-way minimum for one capability.

    ``token_scopes`` of ``None`` means "not a token call" (a browser session
    with a real human), which is the only way to exercise capabilities that
    have no scope mapping.
    """
    # 1. Role.
    if not role_allows(role, capability) and capability not in CAPABILITY_TO_SCOPE:
        return EffectivePermission(
            False,
            f"role '{role or 'none'}' cannot {capability}",
            limited_by="role",
            detail={"role": role or "none"},
        )
    if not role_allows(role, capability) and capability in CAPABILITY_TO_SCOPE:
        # run_workflow is not in the role matrix; treat it as request_work.
        proxy = "request_work" if capability == "run_workflow" else capability
        if not role_allows(role, proxy):
            return EffectivePermission(
                False,
                f"role '{role or 'none'}' cannot {capability}",
                limited_by="role",
                detail={"role": role or "none"},
            )

    # 2. Token scopes.
    if token_scopes is not None:
        scope = CAPABILITY_TO_SCOPE.get(capability)
        if scope is None:
            return EffectivePermission(
                False,
                f"{capability} needs a signed-in human, not an API token",
                limited_by="token",
                detail={"capability": capability},
            )
        if scope not in token_scopes:
            return EffectivePermission(
                False,
                f"token is missing scope '{scope}'",
                limited_by="token",
                detail={"scope": scope},
            )

    # 3. Autonomy.
    needed_autonomy = CAPABILITY_MIN_AUTONOMY.get(capability, "L0")
    if autonomy_rank(autonomy_level) < autonomy_rank(needed_autonomy):
        return EffectivePermission(
            False,
            f"autonomy {autonomy_level} is below {needed_autonomy}",
            limited_by="autonomy",
            detail={"autonomy": autonomy_level or "L1", "needed": needed_autonomy},
        )

    # 4. Observe level, for anything touching a connected system.
    if target_class:
        needed_observe = TARGET_MIN_OBSERVE.get(target_class)
        if needed_observe and observe_rank(observe_level) < observe_rank(needed_observe):
            return EffectivePermission(
                False,
                f"observe level {observe_level} is below {needed_observe}",
                limited_by="observe",
                detail={
                    "observe": observe_level or "O0",
                    "needed": needed_observe,
                    "target_class": target_class,
                },
            )

    return EffectivePermission(True)
