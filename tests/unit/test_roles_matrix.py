"""Role × capability matrix, plus the four-way minimum (42 §2, 46 T10.4, doc 06 §2)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.authz.effective import effective
from app.authz.roles import CAPABILITIES, ROLES, role_allows, roles_with

# The matrix, written out independently of the implementation so a typo in
# roles.py cannot silently agree with itself.
EXPECTED: dict[str, set[str]] = {
    "owner": {
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
    },
    "admin": {
        "read",
        "request_work",
        "approve",
        "connect_systems",
        "change_autonomy",
        "invite",
        "manage_billing",
        "manage_apps",
        "approve_production",
        "run_terminal",
    },
    "maintainer": {"read", "request_work", "approve", "manage_apps", "run_terminal"},
    "contributor": {"read", "request_work"},
    "viewer": {"read"},
}


def test_every_cell_matches() -> None:
    assert set(ROLES) == set(EXPECTED)
    for role, expected in EXPECTED.items():
        for capability in CAPABILITIES:
            assert role_allows(role, capability) is (capability in expected), (
                f"{role} x {capability}"
            )


def test_unknown_role_holds_nothing() -> None:
    for capability in CAPABILITIES:
        assert role_allows("intern", capability) is False
        assert role_allows(None, capability) is False


def test_only_owner_can_offboard() -> None:
    assert roles_with("offboard") == ["owner"]


def test_viewer_cannot_request_work() -> None:
    perm = effective("request_work", role="viewer", autonomy_level="L4")
    assert perm.allowed is False
    assert perm.limited_by == "role"


@pytest.mark.parametrize(
    ("capability", "scope"),
    [("read", "read"), ("request_work", "work:create"), ("approve", "work:approve")],
)
def test_token_must_carry_the_matching_scope(capability: str, scope: str) -> None:
    denied = effective(
        capability, role="owner", autonomy_level="L4", token_scopes=frozenset()
    )
    assert denied.allowed is False
    assert denied.limited_by == "token"

    allowed = effective(
        capability, role="owner", autonomy_level="L4", token_scopes={scope}
    )
    assert allowed.allowed is True


def test_token_can_never_change_autonomy() -> None:
    """No scope exists for it: widening permissions needs a signed-in human."""
    perm = effective(
        "change_autonomy",
        role="owner",
        autonomy_level="L4",
        token_scopes={"read", "work:create", "work:approve"},
    )
    assert perm.allowed is False
    assert perm.limited_by == "token"


def test_observe_level_gates_writes_to_their_systems() -> None:
    denied = effective(
        "request_work",
        role="owner",
        autonomy_level="L4",
        observe_level="O2",
        target_class="connection:write",
    )
    assert denied.allowed is False
    assert denied.limited_by == "observe"
    assert denied.detail["needed"] == "O4"

    allowed = effective(
        "request_work",
        role="owner",
        autonomy_level="L4",
        observe_level="O4",
        target_class="connection:write",
    )
    assert allowed.allowed is True


def test_reading_their_systems_needs_o2() -> None:
    assert (
        effective(
            "read",
            role="viewer",
            autonomy_level="L1",
            observe_level="O1",
            target_class="connection:read",
        ).allowed
        is False
    )
    assert (
        effective(
            "read",
            role="viewer",
            autonomy_level="L1",
            observe_level="O2",
            target_class="connection:read",
        ).allowed
        is True
    )


def test_denial_reason_names_the_binding_constraint() -> None:
    perm = effective(
        "request_work",
        role="owner",
        autonomy_level="L4",
        observe_level="O0",
        target_class="connection:write",
    )
    reason = perm.audit_reason()
    assert "observe" in reason
    assert "O0" in reason and "O4" in reason
