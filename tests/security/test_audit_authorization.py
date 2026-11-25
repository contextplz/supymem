"""Audited outbound, billing and connection decisions use the verified person."""

import asyncio
import json

import pytest

from tests.security.test_core_signed_boundary import client, headers  # noqa: F401


def _venture(client):
    response = client.post("/ventures", json={"raw_idea": "a booking tool for independent studios", "start": False},
                           headers=headers("/ventures", "POST"))
    assert response.status_code == 201, response.text
    from app.db.models import Venture
    from app.state import db

    vid = response.json()["id"]
    with db()() as session:
        return vid, session.get(Venture, vid).workspace_id


def _member(wid, role):
    from app.db.models import Person, WorkspaceMember
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add(Person(id="usr_bob", display_name="Bob", created_at=utcnow()))
        session.flush()
        session.add(WorkspaceMember(id="wsm_bob", workspace_id=wid, person_id="usr_bob", role=role,
                                    can_approve=int(role in {"owner", "admin", "maintainer"}), created_at=utcnow()))
        session.commit()


@pytest.mark.parametrize("role", ["contributor", "maintainer", "admin", "owner"])
def test_chat_outreach_requires_verified_approver(client, monkeypatch, role):
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.state import db
    from core.clock import utcnow

    vid, wid = _venture(client)
    _member(wid, role)
    with db()() as session:
        session.add(GtmContact(id="gtmc_audit", workspace_id=wid, email="hello@studio.example",
                              lawful_basis="legitimate_interest", source=f"venture:{vid}:research",
                              recipient_geo="US", enrichment_json=json.dumps({"found_at": "https://studio.example/contact"}),
                              stage_changed_at=utcnow(), created_at=utcnow()))
        from app.gtm.relationships.service import _settings
        settings = _settings(session, wid)
        settings.sender_postal_address, settings.send_enabled = "1 Founder Street, New York, NY 10001", True
        session.commit()
    outreach._save_draft(vid, {"contact_id": "gtmc_audit", "to": "hello@studio.example", "channel": "email",
                              "subject": "Hello", "body": "A booking tool for studios.", "state": "draft",
                              "batch_id": "audit", "requires_batch_approval": True})
    monkeypatch.setenv("SUPYMEM_OUTBOUND_EXECUTION_ENABLED", "1")
    monkeypatch.setattr(outreach, "_mailbox", lambda _wid: {"user": "gmail:test", "account": "ca_test"})
    sent = []

    def send(*args):
        sent.append(args)
        return {"provider_message_id": "msg_audit", "thread_id": "thread_audit"}

    monkeypatch.setattr(outreach, "_send_one", send)
    path = f"/ventures/{vid}/chat/confirm"
    response = client.post(path, json={"action": "outreach.send", "payload": {
        "approved_by_founder": True, "batch_id": "audit", "actor_subject": "usr_alice"}},
        headers=headers(path, "POST", "usr_bob"))
    if role == "contributor":
        assert response.status_code == 403, response.text
        assert not sent
        assert outreach.drafts(vid)[0]["state"] == "draft"
    else:
        assert response.status_code == 200, response.text
        assert len(sent) == 1


@pytest.mark.parametrize("role", ["contributor", "maintainer", "admin", "owner"])
@pytest.mark.parametrize("scope", ["workspaces", "ventures"])
def test_stripe_connect_requires_manage_billing(client, monkeypatch, role, scope):
    from app.payments import connect

    vid, wid = _venture(client)
    _member(wid, role)
    monkeypatch.setenv("PAYMENTS_ACCEPTING", "1")
    calls = []

    def start(**kwargs):
        calls.append(kwargs)
        return {"onboarding_url": "https://stripe.example/test"}

    monkeypatch.setattr(connect, "start_onboarding", start)
    path = f"/{scope}/{wid if scope == 'workspaces' else vid}/billing/stripe/connect"
    response = client.post(path, json={}, headers=headers(path, "POST", "usr_bob"))
    assert response.status_code == (200 if role in {"admin", "owner"} else 403), response.text
    assert len(calls) == int(role in {"admin", "owner"})


@pytest.mark.parametrize("operation", ["create", "revoke"])
@pytest.mark.parametrize("role", ["contributor", "maintainer", "admin", "owner"])
def test_connections_use_caller_capability_and_audit(client, monkeypatch, operation, role):
    from app.audit.query import list_audit
    from app.connectors import service
    from app.connectors.fakes import reset_fakes
    from app.connectors.registry import reset_registry
    from app.connectors.secrets import reset_store
    from app.db.models import Connection
    from app.state import db

    monkeypatch.setenv("CONNECTORS_MODE", "local")
    reset_registry()
    reset_fakes()
    reset_store()
    vid, wid = _venture(client)
    _member(wid, role)
    path = f"/workspaces/{wid}/connections"
    body = {"provider": "github"}
    if operation == "revoke":
        created = asyncio.run(service.create_connection(workspace_id=wid, provider="github", actor_id="usr_alice"))
        cid = created.id
        with db()() as session:
            secret_ref = session.get(Connection, cid).secret_ref
        path, body = f"/connections/{cid}/revoke", {}
    response = client.post(path, json=body, headers=headers(path, "POST", "usr_bob"))
    allowed = role in {"admin", "owner"}
    assert response.status_code == ((201 if operation == "create" else 200) if allowed else 403), response.text
    if allowed:
        topic = "connection.created" if operation == "create" else "connection.revoked"
        assert list_audit(wid, topic=topic)["items"][0]["actor_id"] == "usr_bob"
    elif operation == "revoke":
        assert service.get_connection(cid).status == "active"
        with db()() as session:
            assert session.get(Connection, cid).secret_ref == secret_ref


def test_revoke_service_checks_actor_before_provider_or_secret(client, monkeypatch):
    from app.authz.gate import Denied
    from app.connectors import service
    from app.connectors.registry import reset_registry
    from app.connectors.secrets import reset_store

    monkeypatch.setenv("CONNECTORS_MODE", "local")
    reset_registry()
    reset_store()
    vid, wid = _venture(client)
    _member(wid, "contributor")
    connection = asyncio.run(service.create_connection(workspace_id=wid, provider="github", actor_id="usr_alice"))
    monkeypatch.setattr(service, "get_connector", lambda *args, **kwargs: pytest.fail("provider revoke reached"))
    monkeypatch.setattr(service.secret_store(), "forget", lambda *args: pytest.fail("secret deletion reached"))
    with pytest.raises(Denied):
        asyncio.run(service.revoke_connection(connection.id, actor_id="usr_bob"))


@pytest.mark.parametrize("operation", ["load", "status", "queue"])
def test_opengtm_api_rejects_state_paths(client, monkeypatch, operation):
    from subprocess import CompletedProcess

    from app.gtm import opengtm_bridge

    monkeypatch.setattr(opengtm_bridge, "_require_bin", lambda: None)
    monkeypatch.setattr(opengtm_bridge, "_run", lambda *args, **kwargs: CompletedProcess([], 0, stdout="{}", stderr=""))
    wid = _venture(client)[1]
    path = f"/api/v1/ai-gtm/opengtm/outreach/{operation}"
    for state_file in ("/tmp/audit-missing.json", "../outside.json", "nested/file.json"):
        method = "POST" if operation == "load" else "GET"
        kwargs = {"json": {"leads": [], "state_file": state_file}} if method == "POST" else {"params": {"state_file": state_file}}
        response = client.request(method, path, headers={**headers(path, method), "X-Workspace-ID": wid}, **kwargs)
        assert response.status_code == 400, response.text
