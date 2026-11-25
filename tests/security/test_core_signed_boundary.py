"""Actual ASGI boundary: signed identity, replay and foreign resource isolation."""

from __future__ import annotations

from datetime import timedelta
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from core.assertion import sign_assertion
from tests.company_os_harness import load_company_os_app

SECRET = "test-core-boundary-secret-at-least-32-bytes"


def headers(path: str, method: str = "GET", subject: str = "usr_alice", **claims) -> dict[str, str]:
    return {"X-COS-Assertion": sign_assertion(SECRET, sub=subject, email="", role="user", path=path, method=method, **claims)}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/core.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "test-vault-secret-with-more-than-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.test")
    main = load_company_os_app()
    app = main.create_app()

    @app.get("/_test_verified_dependency")
    def probe(request: Request):
        from app.admin.authn import verify_caller

        first = verify_caller(request, required=True)
        second = verify_caller(request, required=True)
        return {"subject": first.sub, "same": first is second, "header": request.headers.get("x-subject"), "device_id": first.device_id}

    with TestClient(app) as http:
        yield http


def make_workspace(client: TestClient, subject: str) -> str:
    response = client.post("/workspaces", json={"name": subject, "kind": "company"}, headers=headers("/workspaces", "POST", subject))
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_unsigned_subject_cannot_list_create_or_read_core(client):
    for method, path in (("GET", "/ventures"), ("POST", "/workspaces"), ("GET", "/me"), ("GET", "/api/v1/config")):
        response = client.request(method, path, json={} if method == "POST" else None, headers={"X-Subject": "usr_alice"})
        assert response.status_code == 401, (path, response.text)


def test_signing_flag_uses_same_case_insensitive_values_as_boot_contract(client, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "TRUE")
    assert client.get("/ventures", headers={"X-Subject": "usr_alice"}).status_code == 401


def test_verified_subject_is_bound_once_and_replay_is_rejected(client):
    path = "/_test_verified_dependency"
    signed = headers(path)
    response = client.get(path, headers=signed)
    assert response.status_code == 200, response.text
    assert response.json() == {"subject": "usr_alice", "same": True, "header": "usr_alice", "device_id": None}
    replay = client.get(path, headers=signed)
    assert replay.status_code == 401
    assert replay.json()["detail"]["reason"] == "replayed"


def test_signed_subject_cannot_be_replaced_by_header(client):
    response = client.get("/me", headers={**headers("/me"), "X-Subject": "usr_bob"})
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "subject_mismatch"


@pytest.mark.parametrize("signed", [True, False])
def test_http_cannot_bind_reserved_system_actor(client, monkeypatch, signed):
    from app.tenancy import current

    reached = []

    @client.app.get("/_test_scope_actor")
    def probe():
        reached.append(current().actor)
        return {"actor": current().actor}

    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1" if signed else "0")
    path = "/_test_scope_actor"
    auth = headers(path, subject="system") if signed else {"X-Subject": "system"}
    assert client.get(path, headers=auth).status_code == 401
    assert reached == []


def test_signature_target_is_bound_to_method_and_path(client):
    assert client.get("/me", headers=headers("/workspaces")).status_code == 401
    assert client.get("/me", headers=headers("/me", "POST")).status_code == 401


def test_expired_or_tampered_assertions_fail_before_reading_data(client):
    expired = client.get("/me", headers=headers("/me", ttl_seconds=-1))
    assert expired.status_code == 401
    token = headers("/me")["X-COS-Assertion"]
    left, right = token.split(".")
    changed = ("A" if right[0] != "A" else "B") + right[1:]
    assert client.get("/me", headers={"X-COS-Assertion": f"{left}.{changed}"}).status_code == 401


def test_workspace_and_portfolio_use_only_signed_identity(client):
    alice = make_workspace(client, "usr_alice")
    bob = make_workspace(client, "usr_bob")
    path = f"/workspaces/{alice}"
    assert client.get(path, headers=headers(path)).status_code == 200
    assert client.get(path, headers=headers(path, subject="usr_bob")).status_code == 404
    data = client.get("/workspaces", headers=headers("/workspaces")).json()
    assert alice in {row["id"] for row in data["items"]}
    assert bob not in {row["id"] for row in data["items"]}


def test_response_redaction_scope_comes_from_authorized_core_resources(client):
    workspace = make_workspace(client, "usr_alice")
    path = f"/workspaces/{workspace}"
    response = client.get(path, headers={**headers(path), "X-COS-Workspace-ID": "foreign"})
    assert response.headers["X-COS-Workspace-ID"] == workspace
    assert "X-COS-Workspace-ID" not in client.get(path, headers=headers(path, subject="usr_bob")).headers
    assert "X-COS-Workspace-ID" not in client.get("/me", headers={**headers("/me"), "X-COS-Workspace-ID": workspace}).headers


def test_blocked_workspace_remains_blocked_after_identity_verification(client):
    wid = make_workspace(client, "usr_alice")
    from app.db.models import Workspace
    from app.state import db

    with db()() as session:
        session.get(Workspace, wid).settings_json = '{"blocked": true}'
        session.commit()
    path = f"/workspaces/{wid}"
    assert client.get(path, headers=headers(path)).status_code == 423


def test_viewer_can_read_but_cannot_mutate_workspace(client):
    wid = make_workspace(client, "usr_alice")
    make_workspace(client, "usr_bob")  # creates Bob's account representation
    from app.db.models import WorkspaceMember
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        session.add(WorkspaceMember(id="wsm_viewer", workspace_id=wid, person_id="usr_bob", role="viewer", created_at=utcnow()))
        session.commit()
    path = f"/workspaces/{wid}"
    assert client.get(path, headers=headers(path, subject="usr_bob")).status_code == 200
    response = client.patch(path, json={"name": "Taken over"}, headers=headers(path, "PATCH", "usr_bob"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "read_only_workspace"


@pytest.mark.parametrize("caller_scope,expected", [("owner", 200), ("foreign", 404), ("viewer", 403),
                                                   ("archived", 404), ("purged", 404), ("missing", 404)])
def test_suggestion_acceptance_binds_its_saved_resource_before_guarded_execution(client, monkeypatch, caller_scope, expected):
    from app.autopilot import actions, receipts
    from app.chat import execute
    from app.db.models import Venture, VentureDocument, WorkspaceMember
    from app.db.supymem_models import AutopilotEvent, Suggestion
    from app.state import db
    from app.suggestions import service as suggestions
    from core.clock import utcnow

    wid = make_workspace(client, "usr_alice")
    make_workspace(client, "usr_bob")
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="ven_suggestion_owned", workspace_id=wid, name="Owned", raw_idea="Owned test",
                            stage="purged" if caller_scope == "purged" else "idea", created_at=now, updated_at=now,
                            archived_at=now if caller_scope == "archived" else None))
        session.add(Suggestion(id="sug_owned", venture_id="ven_suggestion_owned", person_id="usr_alice",
                               title="Draft 3 outreach emails for review", action_kind="outreach.draft", est_credits=0,
                               dedupe_key="owned", created_at=now))
        if caller_scope == "viewer":
            session.add(WorkspaceMember(id="wsm_suggestion_viewer", workspace_id=wid, person_id="usr_bob",
                                        role="viewer", created_at=now))
        session.commit()

    monkeypatch.setattr("app.ventures.next_step.next_step", lambda _: {"key": "gtm.outreach"})
    monkeypatch.setattr("app.suggestions.proposals.propose_next_actions", lambda *_: [{
        "action_kind": "outreach.draft", "title": "Draft 3 outreach emails for review", "payload": {"run": True, "review_only": True}}])
    binding = suggestions._state_binding("ven_suggestion_owned")
    with db()() as session:
        session.get(Suggestion, "sug_owned").payload_json = json.dumps({
            "run": True, "review_only": True, "_suggestion_state": binding})
        session.commit()

    calls = []

    async def produce(venture_id, name, **kwargs):
        from app.tenancy import current

        scope = current()
        assert (scope.workspace_id, scope.venture_id, scope.actor) == (wid, "ven_suggestion_owned", "usr_alice")
        calls.append((venture_id, name))
        with db()() as session:
            session.add(VentureDocument(id="suggestion_boundary_result", venture_id=venture_id, kind="draft",
                                       title="Owned result", body_md="Owned saved content", created_at=now, updated_at=now))
            session.commit()
        receipts.saved_document("suggestion_boundary_result", "draft", "Owned saved content")
        return {"kind": "text", "body_md": "Saved", "actions": []}

    monkeypatch.setattr(execute, "execute_action", produce)
    monkeypatch.setattr("app.suggestions.proposals.god_actions", lambda: actions)
    sid = "sug_missing" if caller_scope == "missing" else "sug_owned"
    subject = "usr_bob" if caller_scope in {"foreign", "viewer"} else "usr_alice"
    path = f"/suggestions/{sid}/decision"
    response = client.post(path, json={"decision": "accept"}, headers=headers(path, "POST", subject))
    assert response.status_code == expected, response.text
    with db()() as session:
        saved = session.get(Suggestion, "sug_owned")
        if expected == 200:
            assert saved.status == "done" and response.json()["result_state"] == "done"
            result = json.loads(saved.result_ref)
            assert result["outcome"] == "done" and result["documents"] == ["suggestion_boundary_result"]
            assert calls == [("ven_suggestion_owned", "outreach.draft")]
        else:
            assert saved.status == "new" and calls == []
            assert session.query(AutopilotEvent).filter_by(venture_id="ven_suggestion_owned", outcome="attempt").count() == 0


def test_foreign_approval_read_and_batch_are_denied_before_mutation(client):
    wid = make_workspace(client, "usr_bob")
    from app.db.models import Approval
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(Approval(id="apr_foreign", workspace_id=wid, kind="credit_reserve_large", subject_ref="target", summary="Private decision", requested_by="system", expires_at=now + timedelta(days=1), created_at=now))
        session.commit()
    path = "/approvals/apr_foreign"
    assert client.get(path, headers=headers(path)).status_code == 404
    assert client.get(path, headers=headers(path, subject="usr_bob")).status_code == 200
    path = "/approvals/batch-decide"
    response = client.post(path, json={"ids": ["apr_foreign"], "decision": "approve", "decided_by": "usr_alice"}, headers=headers(path, "POST"))
    assert response.status_code == 404
    with db()() as session:
        assert session.get(Approval, "apr_foreign").status == "pending"


def test_foreign_entry_workspace_rejected_before_execution(client):
    wid = make_workspace(client, "usr_bob")
    path = "/entry"
    response = client.post(path, json={"text": "Update this company", "active_workspace_id": wid}, headers=headers(path, "POST"))
    assert response.status_code == 404


def test_approval_decision_actor_cannot_be_spoofed(client):
    wid = make_workspace(client, "usr_alice")
    path = f"/workspaces/{wid}/approvals"
    response = client.post(path, json={"kind": "credit_reserve_large", "subject_ref": "item", "summary": "Approve", "requested_by": "usr_bob"}, headers=headers(path, "POST"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "actor_mismatch"


def test_legacy_ops_requires_owned_explicit_workspace(client):
    alice = make_workspace(client, "usr_alice")
    bob = make_workspace(client, "usr_bob")
    path = "/api/v1/config"
    assert client.get(path, headers=headers(path)).status_code == 400
    assert client.get(path, params={"workspace_id": bob}, headers=headers(path)).status_code == 404
    response = client.get(path, params={"workspace_id": alice}, headers=headers(path))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "No company config yet"


@pytest.mark.parametrize("path", ["/events/recent", "/trace", "/llm/usage", "/mcp/resources"])
def test_global_helpers_do_not_disclose_tenant_data(client, path):
    assert client.get(path, headers=headers(path)).status_code == 403


def test_public_and_independently_authenticated_routes_keep_their_contracts(client):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # Distinguish independent API-key validation from missing gateway assertion.
    for path in ("/api/v1/workspaces", "/openai/v1/models"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json().get("detail", {}).get("code") != "invalid_assertion"
    response = client.post("/internal/ops/run-cycle", json={"cycle": "morning"})
    assert response.status_code in {401, 503}
    assert response.json().get("detail") != {"code": "invalid_assertion", "reason": "missing"}
    # Anonymous product beacon is exempted; unknown keys still return 204.
    events = client.post(
        "/e",
        content='{"key":"pk_app_missing","event":"landing_view"}',
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert events.status_code == 204


def test_old_unauthenticated_websocket_is_not_mounted_hosted(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/activity?workspace_id=wsp_other"):
            pass


def test_desktop_notices_and_ack_are_founder_scoped(client):
    venture_ids = {}
    for user in ("usr_alice", "usr_bob"):
        response = client.post("/ventures", json={"raw_idea": "A private planning tool for independent architects", "start": False}, headers=headers("/ventures", "POST", user))
        assert response.status_code == 201, response.text
        venture_ids[user] = response.json()["id"]
    from app.desktop_notices import enqueue, pending

    alice = enqueue(title="Alice notice", body="Alice data", venture_id=venture_ids["usr_alice"])
    bob = enqueue(title="Bob notice", body="Bob data", venture_id=venture_ids["usr_bob"])
    enqueue(title="Machine notice", body="No founder attribution")
    path = "/desktop/notices"
    response = client.get(path, headers=headers(path))
    assert [n["id"] for n in response.json()["notices"]] == [alice]
    path = "/desktop/notices/ack"
    response = client.post(path, json={"ids": [alice, bob]}, headers=headers(path, "POST"))
    assert response.json() == {"acked": 1}
    assert bob in {n["id"] for n in pending()}


def test_core_caller_retains_only_signed_device_identity(client):
    path = "/_test_verified_dependency"
    forged = {**headers(path), "X-Device-Id": "dev_forged"}
    assert client.get(path, headers=forged).json()["device_id"] is None
    signed = {**headers(path, device_id="dev_verified_mac"), "X-Device-Id": "dev_forged"}
    result = client.get(path, headers=signed)
    assert result.status_code == 200
    assert result.json()["device_id"] == "dev_verified_mac"
