"""R15 — nothing may quietly widen its own permissions (42, 47 R15)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/r15.db")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def workspace(client: TestClient) -> str:
    return client.post("/workspaces", json={"kind": "venture", "name": "Target"}).json()[
        "id"
    ]


def _join(client: TestClient, workspace: str, role: str, email: str) -> str:
    invite = client.post(
        f"/workspaces/{workspace}/invites", json={"email": email, "role": role}
    ).json()
    token = invite["link"].rsplit("/", 1)[-1]
    return client.post(f"/invites/{token}/accept", json={}).json()["person_id"]


def test_the_last_owner_cannot_be_removed(client, workspace) -> None:
    members = client.get(f"/workspaces/{workspace}/members").json()["items"]
    owner = next(m for m in members if m["role"] == "owner")
    r = client.delete(f"/members/{owner['id']}")
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "last_owner"


def test_the_last_owner_cannot_be_demoted(client, workspace) -> None:
    members = client.get(f"/workspaces/{workspace}/members").json()["items"]
    owner = next(m for m in members if m["role"] == "owner")
    r = client.patch(f"/members/{owner['id']}", json={"role": "viewer"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "last_owner"


def test_an_invite_cannot_mint_a_second_owner(client, workspace) -> None:
    r = client.post(
        f"/workspaces/{workspace}/invites",
        json={"email": "usurper@example.com", "role": "owner"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "cannot_invite_owner"


def test_an_invite_token_is_single_use(client, workspace) -> None:
    invite = client.post(
        f"/workspaces/{workspace}/invites",
        json={"email": "once@example.com", "role": "viewer"},
    ).json()
    token = invite["link"].rsplit("/", 1)[-1]
    assert client.post(f"/invites/{token}/accept", json={}).status_code == 200
    replay = client.post(f"/invites/{token}/accept", json={})
    assert replay.status_code == 409
    assert replay.json()["detail"]["error"]["code"] == "invite_used"


def test_a_raw_invite_token_is_never_stored(client, workspace) -> None:
    from app.db.models import Invite
    from app.state import db

    invite = client.post(
        f"/workspaces/{workspace}/invites",
        json={"email": "secret@example.com", "role": "viewer"},
    ).json()
    token = invite["link"].rsplit("/", 1)[-1]

    factory = db()
    with factory() as session:
        rows = session.scalars(select(Invite)).all()
        for row in rows:
            assert token not in (row.token_hash or "")
            assert row.token_hash != token


def test_a_token_cannot_raise_autonomy() -> None:
    """No scope maps to change_autonomy, so no API token can ever hold it."""
    from app.authz.effective import CAPABILITY_TO_SCOPE, TOKEN_SCOPES, effective

    assert "change_autonomy" not in CAPABILITY_TO_SCOPE
    perm = effective(
        "change_autonomy",
        role="owner",
        autonomy_level="L4",
        token_scopes=TOKEN_SCOPES,
    )
    assert perm.allowed is False
    assert perm.limited_by == "token"


def test_a_token_cannot_grant_a_connection_scope() -> None:
    from app.authz.effective import TOKEN_SCOPES, effective

    perm = effective(
        "connect_systems", role="owner", autonomy_level="L4", token_scopes=TOKEN_SCOPES
    )
    assert perm.allowed is False
    assert perm.limited_by == "token"


def test_maintainer_can_approve_but_cannot_widen_anything(client, workspace) -> None:
    from app.authz.effective import effective

    assert effective("approve", role="maintainer", autonomy_level="L2").allowed is True
    for capability in ("connect_systems", "change_autonomy", "invite", "offboard"):
        perm = effective(capability, role="maintainer", autonomy_level="L4")
        assert perm.allowed is False, capability
        assert perm.limited_by == "role"


def test_max_autonomy_does_not_bypass_the_observe_ladder() -> None:
    """L4 plus O1 must still refuse a write, naming both dials."""
    from app.authz.effective import effective

    perm = effective(
        "request_work",
        role="owner",
        autonomy_level="L4",
        observe_level="O1",
        target_class="connection:write",
    )
    assert perm.allowed is False
    assert perm.limited_by == "observe"
    assert "O1" in perm.audit_reason()


def test_every_denial_is_audited_with_an_actor(client, workspace) -> None:
    from app.db.models import EventLog
    from app.state import db

    client.patch(
        f"/members/{client.get(f'/workspaces/{workspace}/members').json()['items'][0]['id']}",
        json={"role": "viewer"},
    )
    factory = db()
    with factory() as session:
        rows = session.scalars(select(EventLog)).all()
        assert rows
        for row in rows:
            assert row.actor_id, f"{row.topic} has no actor"
            assert row.actor_kind in {"person", "agent", "token", "system"}


def test_no_system_actor_writes_to_a_connected_system(client, workspace) -> None:
    """`system` is only for scheduler ticks and health checks (42 §5)."""
    from app.db.models import EventLog
    from app.state import db

    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(EventLog).where(EventLog.actor_kind == "system")
        ).all()
        for row in rows:
            assert not (row.target_ref or "").startswith("con_"), (
                f"system actor touched a connection in {row.topic}"
            )


def _member_id(client: TestClient, workspace: str, person_id: str) -> str:
    items = client.get(f"/workspaces/{workspace}/members").json()["items"]
    return next(m["id"] for m in items if m["person_id"] == person_id)


@pytest.mark.parametrize("role", ["contributor", "maintainer", "viewer"])
def test_a_non_admin_member_cannot_promote_themselves_or_remove_the_owner(client, workspace, role) -> None:
    person = _join(client, workspace, role, f"{role}@example.com")
    me = _member_id(client, workspace, person)
    owner = next(m for m in client.get(f"/workspaces/{workspace}/members").json()["items"] if m["role"] == "owner")
    as_member = {"X-Subject": person}
    assert client.patch(f"/members/{me}", json={"role": "owner"}, headers=as_member).status_code in (403, 404)
    assert client.delete(f"/members/{owner['id']}", headers=as_member).status_code in (403, 404)
    roles = {m["person_id"]: m["role"] for m in client.get(f"/workspaces/{workspace}/members").json()["items"]}
    assert roles[person] == role and roles[owner["person_id"]] == "owner"


def test_an_admin_manages_members_but_cannot_mint_or_remove_an_owner(client, workspace) -> None:
    admin = _join(client, workspace, "admin", "admin@example.com")
    helper = _join(client, workspace, "contributor", "helper@example.com")
    owner = next(m for m in client.get(f"/workspaces/{workspace}/members").json()["items"] if m["role"] == "owner")
    as_admin = {"X-Subject": admin}
    assert client.patch(f"/members/{_member_id(client, workspace, helper)}", json={"role": "maintainer"}, headers=as_admin).status_code == 200
    assert client.patch(f"/members/{_member_id(client, workspace, admin)}", json={"role": "owner"}, headers=as_admin).status_code == 403
    assert client.delete(f"/members/{owner['id']}", headers=as_admin).status_code == 403


def test_a_member_can_leave_the_workspace(client, workspace) -> None:
    person = _join(client, workspace, "contributor", "leaver@example.com")
    assert client.delete(f"/members/{_member_id(client, workspace, person)}", headers={"X-Subject": person}).status_code == 200
