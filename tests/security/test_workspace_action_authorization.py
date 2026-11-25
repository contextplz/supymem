"""Persisted workspace authority applies to chat dispatch and REST mutations."""

import asyncio
from types import SimpleNamespace

import pytest

from tests.security.test_audit_authorization import _member, _venture
from tests.security.test_core_signed_boundary import client as client, headers


def test_every_chat_action_declares_a_workspace_permission(client):
    from app.authz.roles import CAPABILITIES
    from app.chat.actions import all_actions

    for action in all_actions():
        assert action.required_permission in CAPABILITIES
    permissions = {action.name: action.required_permission for action in all_actions()}
    assert permissions["approve"] == "approve"
    assert permissions["site.unpublish"] == "manage_apps"
    reducing = {"pause", "autopilot.god_off", "autopilot.send_off"}
    assert all(permission == "change_autonomy" for name, permission in permissions.items()
               if name.startswith("autopilot.") and name not in reducing)
    assert all(permissions[name] == "request_work" for name in reducing)


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer", "unknown", None])
def test_autopilot_settings_chat_and_rest_require_change_autonomy(client, monkeypatch, role):
    from app.autopilot import service

    vid, wid = _venture(client)
    if role:
        _member(wid, role)
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")
    before = service.public_settings(vid)
    permitted = role in {"owner", "admin"}
    expected = 200 if permitted else 403 if role else 404
    for action, payload in (
        ("autopilot.allow_send", {}),
        ("autopilot.send_cap", {"send_max": 37}),
        ("autopilot.set_cap", {"cap": 100}),
        ("autopilot.god_on", {}),
    ):
        path = f"/ventures/{vid}/chat/confirm"
        response = client.post(path, json={"action": action, "payload": payload},
                               headers=headers(path, "POST", "usr_bob"))
        assert response.status_code == expected, (action, response.text)
        if not permitted:
            assert service.public_settings(vid) == before
    path = f"/ventures/{vid}/autopilot"
    response = client.put(path, json={"allow_send": True, "send_max_per_day": 37},
                          headers=headers(path, "PUT", "usr_bob"))
    assert response.status_code == expected, response.text
    saved = service.public_settings(vid)
    if permitted:
        assert saved["allow_send"] and saved["send_max_per_day"] == 37
    else:
        assert saved == before
        if role in {"contributor", "maintainer"}:
            from app.chat.execute import execute_action

            for action in ("autopilot.send_cap", "autopilot.god_on"):
                with pytest.raises(PermissionError, match="change_autonomy_required"):
                    asyncio.run(execute_action(vid, action, payload={"send_max": 37}, actor_subject="usr_bob"))
            assert service.public_settings(vid) == before


@pytest.mark.parametrize("role,can_approve", [("owner", True), ("admin", True), ("maintainer", True),
                                            ("maintainer", False), ("contributor", True)])
@pytest.mark.parametrize("entrypoint", ["chat", "direct", "batch", "service"])
def test_rejection_requires_an_authorized_approver(client, role, can_approve, entrypoint):
    from app.approvals import service
    from app.db.models import Approval, WorkspaceMember
    from app.state import db

    vid, wid = _venture(client)
    _member(wid, role)
    with db()() as session:
        session.get(WorkspaceMember, "wsm_bob").can_approve = int(can_approve)
        session.commit()
    pending = service.request_approval(workspace_id=wid, kind="engagement_brief", subject_ref=f"venture:{vid}",
                                       summary="Review the neighborhood calendar", requested_by="usr_alice")
    permitted = role in {"owner", "admin", "maintainer"} and can_approve
    if entrypoint == "service":
        if permitted:
            service.decide(pending.id, decision="rejected", decided_by="usr_bob")
        else:
            with pytest.raises(service.ApprovalError, match="can't approve"):
                service.decide(pending.id, decision="rejected", decided_by="usr_bob")
    else:
        if entrypoint == "chat":
            path = f"/ventures/{vid}/chat/confirm"
            body = {"action": "approve", "payload": {"query": "reject this approval", "approval_id": pending.id}}
        elif entrypoint == "batch":
            path = "/approvals/batch-decide"
            body = {"ids": [pending.id], "decision": "rejected", "decided_by": "usr_bob"}
        else:
            path = f"/approvals/{pending.id}/decide"
            body = {"decision": "rejected", "decided_by": "usr_bob"}
        response = client.post(path, json=body, headers=headers(path, "POST", "usr_bob"))
        expected = 200 if permitted or (entrypoint == "chat" and role == "maintainer") else 403
        assert response.status_code == expected, response.text
    with db()() as session:
        row = session.get(Approval, pending.id)
        assert row.status == ("rejected" if permitted else "pending")
        assert row.decided_by == ("usr_bob" if permitted else None)


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer", None])
def test_venture_offboarding_requires_owner(client, role):
    from app.db.models import Venture, Workspace
    from app.state import db

    vid, wid = _venture(client)
    if role:
        _member(wid, role)
    path = f"/ventures/{vid}/delete"
    response = client.post(path, json={"confirmation": "DELETE"}, headers=headers(path, "POST", "usr_bob"))
    assert response.status_code == (200 if role == "owner" else 403 if role else 404), response.text
    with db()() as session:
        assert bool(session.get(Venture, vid).archived_at) == (role == "owner")
        assert bool(session.get(Workspace, wid).archived_at) == (role == "owner")


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer", None])
@pytest.mark.parametrize("level", ["O4", "O0", "invalid"])
def test_discovery_governance_bump_is_authorized_before_discovery(client, monkeypatch, role, level):
    from app.discovery import map as map_mod
    from app.workspaces import service

    _, wid = _venture(client)
    if role:
        _member(wid, role)
    service.set_observe_level(wid, "O2")
    calls = []

    async def discover(workspace_id, **kwargs):
        calls.append((workspace_id, kwargs["actor_id"]))
        return SimpleNamespace(to_dict=lambda: {"workspace_id": workspace_id})

    monkeypatch.setattr(map_mod, "discover_and_map", discover)
    permitted = role in {"owner", "admin"}
    for endpoint in ("discover", "observe-level"):
        path = f"/workspaces/{wid}/{endpoint}"
        body = {"bump_observe_to": level} if endpoint == "discover" else {"level": level}
        response = client.post(path, json=body, headers=headers(path, "POST", "usr_bob"))
        expected = (422 if level == "invalid" else 200) if permitted else 403 if role else 404
        assert response.status_code == expected, response.text
        assert service.get_workspace(wid).observe_level == (level if permitted and level != "invalid" else "O2")
    assert calls == ([(wid, "usr_bob")] if permitted else [])
    if role in {"maintainer", "contributor"}:
        for body in ({}, {"bump_observe_to": ""}):
            path = f"/workspaces/{wid}/discover"
            response = client.post(path, json=body, headers=headers(path, "POST", "usr_bob"))
            assert response.status_code == 200, response.text
        assert calls == [(wid, "usr_bob"), (wid, "usr_bob")]
        assert service.get_workspace(wid).observe_level == "O2"


def test_only_requester_or_approver_can_withdraw(client):
    from app.approvals import service

    vid, wid = _venture(client)
    _member(wid, "contributor")
    other = service.request_approval(workspace_id=wid, kind="engagement_brief", subject_ref=f"venture:{vid}",
                                     summary="Review the neighborhood calendar", requested_by="usr_alice")
    with pytest.raises(service.ApprovalError, match="can't approve"):
        service.decide(other.id, decision="withdrawn", decided_by="usr_bob")
    own = service.request_approval(workspace_id=wid, kind="engagement_brief", subject_ref=f"venture:{vid}",
                                   summary="Review the fictional flyer", requested_by="usr_bob")
    assert service.decide(own.id, decision="withdrawn", decided_by="usr_bob").status == "withdrawn"


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "contributor", "viewer"])
def test_any_contributor_can_reduce_autonomy_but_viewers_cannot(client, monkeypatch, role):
    # Pausing or switching autonomy off only makes things safer; contributors may do it.
    vid, wid = _venture(client)
    _member(wid, role)
    monkeypatch.setenv("SUPYMEM_BETA_SCOPE", "0")
    for action in ("autopilot.god_off", "autopilot.send_off", "pause"):
        path = f"/ventures/{vid}/chat/confirm"
        response = client.post(path, json={"action": action, "payload": {}},
                               headers=headers(path, "POST", "usr_bob"))
        assert response.status_code == (403 if role == "viewer" else 200), (action, response.text)
