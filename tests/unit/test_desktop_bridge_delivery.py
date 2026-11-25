"""The bridge cannot authenticate or acknowledge a write without real upstream proof."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from desktop_runtime import offline
from desktop_runtime.bridge import create_bridge_app

TOKEN = "bridge-test-token"
GATEWAY = "https://gateway.supymem.com"
HEADERS = {"x-bridge-token": TOKEN}
BRIDGE_URL = "http://127.0.0.1:7350"


@pytest.mark.parametrize("venture", ["ven_operations", "ven_hobby"])
def test_local_native_build_controls_use_owner_transport_before_hosted_auth(venture):
    root = Path(__file__).resolve().parents[2]
    native = (root / "src-tauri/src/lib.rs").read_text()
    inspection = native.split("async fn inspect_device_work_order(", 1)[1].split("#[tauri::command]", 1)[0]
    action = native.split("async fn device_work_order_action(", 1)[1].split("#[tauri::command]", 1)[0]
    legacy = native.split("fn start_cursor_ide_session(", 1)[1].split("#[tauri::command]", 1)[0]
    assert inspection.index("local_stack::is_local()") < inspection.index("state.auth.acquire()"), venture
    assert action.index("local_stack::is_local()") < action.index("await_bridge_ready("), venture
    assert "local_build_request" in inspection and "local_build_request" in action
    assert '"X-Local-Proxy-Secret"' in native and 'join("owner-session.secret")' in native
    assert "device_work_order_action" in legacy and '"start"' in legacy
    assert "opened: false" in legacy and "open_cursor_window" not in legacy
    ide = (root / "src-tauri/src/ide.rs").read_text()
    assert "cursor://" not in ide and "submit_in_front_cursor_window" not in ide
    assert "opencode web" not in (root / "src-tauri/src/stdio.rs").read_text()
    detection = native.split("async fn list_local_tools(", 1)[1].split("#[tauri::command]", 1)[0]
    assert "native_bridge_request(" in detection and '"/native/coding-agents"' in detection
    assert "nav_guard::owner_secret" in detection
    pty = (root / "src-tauri/src/pty.rs").read_text()
    assert "stop_coding_tree" in pty and "observe_process_exit" in pty
    planning = native.split("fn plan_session(", 1)[1].split("#[tauri::command]", 1)[0]
    assert planning.index("local_stack::is_local()") < planning.index("admitted_legacy_session("), venture


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    return tmp_path


def tokens(subject="usr_a", *, access="access-a", refresh="refresh-a"):
    return {"subject": subject, "device_id": "device-" + subject,
            "access_token": access, "refresh_token": refresh, "expires_in": 900}


def authenticate(client, code="code-a"):
    start = client.post("/auth/pkce/start", headers=HEADERS, json={"device_name": "Test Mac"})
    assert start.status_code == 200
    completed = client.post("/auth/pkce/complete", headers=HEADERS, json={"code": code})
    assert completed.status_code == 200, completed.text
    assert "access_token" not in completed.json()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_config_never_exposes_bridge_secret_or_account(setup, domain):
    client = TestClient(create_bridge_app(), base_url=BRIDGE_URL)
    for path in ("/health", "/ready", "/config"):
        assert client.get(path, headers={"Host": "localhost:7350"}).status_code == 200
        assert client.get(path, headers={"Host": f"{domain}:7350"}).status_code == 403
    assert TOKEN not in client.get("/config").text
    assert "bridgeToken" not in client.get("/config").json()
    assert "account_id" not in client.get("/health").json()
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/connect", headers=HEADERS, json={"account_id": "usr_a"}).status_code == 401
    assert not client.get("/health").json()["connected"]


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_bridge_rejects_hosted_origins(setup, monkeypatch, domain):
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1:7310")
    hosted = TestClient(create_bridge_app(), base_url=BRIDGE_URL)
    assert hosted.get("/health", headers={"Origin": "https://app.supymem.com"}).status_code == 200
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    local = TestClient(create_bridge_app(), base_url=BRIDGE_URL)
    for origin in ("https://app.supymem.com", "https://app.staging.supymem.com", f"https://{domain}"):
        denied = local.get("/health", headers={"Origin": origin})
        assert denied.status_code == 403, origin
        assert denied.json()["error"]["code"] == "bridge_origin_denied"
    for origin in ("http://127.0.0.1:7310", "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"):
        assert local.get("/health", headers={"Origin": origin}).status_code == 200, origin


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_native_heartbeat_never_contacts_an_upstream(setup, monkeypatch, domain):
    from core import local_security
    from desktop_runtime.crash_report import enqueue_crash

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:3300")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    native_token = "native-test-token-" * 2
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", native_token)
    owner_credential = b"0" * 64
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: owner_credential)
    queued = enqueue_crash(stack=f"private test traceback from {domain}")

    def no_requests(request):
        pytest.fail(f"Local heartbeat for {domain} accessed {request.url.host}")

    application = create_bridge_app(transport=httpx.MockTransport(no_requests))
    session = application.state.bridge_session
    session.account_id = "usr_test"
    session.device_id = domain
    session.access_token = owner_credential.decode("ascii")
    session.connected = True
    bridge = TestClient(application, base_url=BRIDGE_URL)
    reply = bridge.post("/native/telemetry/runtime", headers={"x-native-token": native_token}, json={
        "event_id": "00000000-0000-4000-8000-000000000001",
        "session_id": "00000000-0000-4000-8000-000000000002",
        "app_version": "1.0.0",
    })
    assert reply.status_code == 200
    assert reply.json() == {"ok": True}
    assert queued.is_file()


def test_pkce_uses_generated_verifier_and_does_not_store_credentials(setup):
    seen = []

    def upstream(request):
        seen.append(request)
        assert request.url.path == "/auth/device/token"
        body = json.loads(request.content)
        assert body["code"] == "code-a"
        assert body["code_verifier"] != "caller-supplied-verifier"
        assert 43 <= len(body["code_verifier"]) <= 128
        return httpx.Response(200, json=tokens())

    client = TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)))
    client.post("/auth/pkce/start", headers=HEADERS)
    result = client.post("/auth/pkce/complete", headers=HEADERS,
                         json={"code": "code-a", "code_verifier": "caller-supplied-verifier"})
    assert result.status_code == 200
    assert client.get("/status", headers=HEADERS).json()["account_id"] == "usr_a"
    assert len(seen) == 1
    raw = (setup / "bridge_outbox.sqlite").read_bytes()
    assert b"access-a" not in raw and b"refresh-a" not in raw and b"code-a" not in raw
    assert (setup / "bridge_outbox.sqlite").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_pkce_rejection_never_claims_connected(setup, status):
    client = TestClient(create_bridge_app(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": "no"}))), base_url=BRIDGE_URL)
    client.post("/auth/pkce/start", headers=HEADERS)
    assert client.post("/auth/pkce/complete", headers=HEADERS, json={"code": "bad"}).status_code >= 400
    assert not client.get("/health").json()["connected"]
    assert client.get("/outbox", headers=HEADERS).status_code == 401


def test_delivery_sends_bearer_and_idempotency_and_waits_for_ack(setup):
    writes = []

    def upstream(request):
        if request.url.path == "/auth/device/token":
            return httpx.Response(200, json=tokens())
        writes.append(request)
        assert request.headers["Authorization"] == "Bearer access-a"
        assert request.headers["Idempotency-Key"] == "op-1"
        assert json.loads(request.content) == {"text": "hello"}
        return httpx.Response(202, json={"job_id": "job-real"})

    client = TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)))
    authenticate(client)
    body = {"path": "/api/ventures/ven_a/tasks", "body": {"text": "hello"}}
    result = client.post("/outbox", headers={**HEADERS, "Idempotency-Key": "op-1"}, json=body)
    assert result.json()["sent"] is True
    assert result.json()["state"] == "delivered"
    assert len(writes) == 1
    again = client.post("/outbox", headers={**HEADERS, "Idempotency-Key": "op-1"}, json=body)
    assert again.json()["already_sent"] is True and len(writes) == 1
    changed = client.post("/outbox", headers={**HEADERS, "Idempotency-Key": "op-1"},
                          json={**body, "body": {"text": "different"}})
    assert changed.status_code == 409


@pytest.mark.parametrize("failure,state", [("network", "uncertain"), (503, "uncertain"),
                                           (401, "auth_required"), (403, "auth_required"),
                                           (400, "rejected"), (302, "rejected"), ("semantic", "rejected")])
def test_failure_retains_write_and_does_not_blindly_retry(setup, failure, state):
    count = 0

    def upstream(request):
        nonlocal count
        if request.url.path == "/auth/device/token":
            return httpx.Response(200, json=tokens())
        count += 1
        if failure == "network":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200 if failure == "semantic" else failure,
                              json={"ok": False}, headers={"Location": "https://evil.example"})

    client = TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)))
    authenticate(client)
    queued = client.post("/outbox", headers={**HEADERS, "Idempotency-Key": "uncertain-1"},
                         json={"path": "/api/ventures/ven_a/tasks", "body": {}})
    assert queued.status_code == 200
    assert queued.json()["sent"] is False
    items = client.get("/outbox", headers=HEADERS).json()["items"]
    assert len(items) == 1 and items[0]["state"] == state
    client.post("/outbox/replay", headers=HEADERS)
    assert count == 1


def test_account_and_gateway_partition_and_legacy_quarantine(setup):
    db = sqlite3.connect(setup / "bridge_outbox.sqlite")
    db.execute("CREATE TABLE outbox(idempotency_key TEXT, body TEXT)")
    db.execute("INSERT INTO outbox VALUES ('legacy','unknown owner')")
    db.execute("CREATE TABLE cache(key TEXT, value TEXT)")
    db.execute("INSERT INTO cache VALUES ('pkce_tokens','old plaintext credentials')")
    db.commit()
    db.close()
    for gateway, account in [(GATEWAY, "usr_a"), (GATEWAY, "usr_b"), ("https://gateway.staging.supymem.com", "usr_a")]:
        offline.enqueue("POST", "/api/test", {"owner": account}, idempotency_key="shared-key", gateway=gateway, account_id=account)
    assert len(offline.pending(gateway=GATEWAY, account_id="usr_a")) == 1
    assert offline.pending(gateway=GATEWAY, account_id="usr_b")[0]["body"]["owner"] == "usr_b"
    with offline.connect() as db:
        assert db.execute("SELECT count(*) FROM cache WHERE key='pkce_tokens'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert offline.claim("shared-key", gateway=GATEWAY, account_id="usr_a")
    offline.recover_interrupted()
    assert offline.pending(gateway=GATEWAY, account_id="usr_a")[0]["state"] == "uncertain"
    assert not offline.claim("shared-key", gateway=GATEWAY, account_id="usr_a")
    assert offline.pending(gateway=GATEWAY, account_id="usr_b")[0]["state"] == "queued"


def test_refresh_rotation_and_disconnect_require_verified_identity(setup):
    seen = []

    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == "/auth/device/token":
            return httpx.Response(200, json=tokens())
        if request.url.path == "/auth/device/refresh":
            assert json.loads(request.content)["refresh_token"] == "refresh-a"
            return httpx.Response(200, json=tokens(access="access-rotated", refresh="refresh-rotated"))
        assert request.headers["Authorization"] == "Bearer access-rotated"
        return httpx.Response(200, json={"authenticated": True, "subject": "usr_a"})

    app = create_bridge_app(transport=httpx.MockTransport(upstream))
    client = TestClient(app)
    authenticate(client)
    app.state.bridge_session.expires_at = 1
    assert client.post("/connect", headers=HEADERS).status_code == 200
    assert app.state.bridge_session.refresh_token == "refresh-rotated"
    assert "/auth/device/refresh" in seen
    client.post("/disconnect", headers=HEADERS)
    assert app.state.bridge_session.access_token is None
    assert client.get("/outbox", headers=HEADERS).status_code == 401


@pytest.mark.parametrize("path", ["https://evil.example/api/a", "//evil.example/api/a", "/auth/logout",
                                  "/api/../auth/logout", "/api/%2e%2e/auth/logout", "/api/a\\b"])
def test_outbox_rejects_credential_redirect_and_path_escape(setup, path):
    client = TestClient(create_bridge_app())
    result = client.post("/outbox", headers={**HEADERS, "Idempotency-Key": "op"}, json={"path": path})
    assert result.status_code == 400


def test_unsigned_remote_work_order_cannot_execute_host_code(setup):
    client = TestClient(create_bridge_app())
    result = client.post("/dispatch", headers=HEADERS,
                         json={"workdir": "/", "prompt": "execute", "allow_write": True})
    assert result.status_code == 409


def test_native_notices_bind_local_delivery_and_ack_to_verified_account(setup, monkeypatch):
    import time
    from desktop_runtime import local_notices, notice_delivery
    local_notices.reset_for_tests()
    notice_delivery.reset_for_tests()
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-fixture-token-" * 3)
    native_headers={"x-native-token": "native-fixture-token-" * 3}
    def upstream(request):
        actor=request.headers["authorization"].removeprefix("Bearer ")
        if request.url.path == "/auth/session":
            return httpx.Response(200,json={"authenticated":True,"subject":actor})
        assert request.url.path == "/api/desktop/notices"
        return httpx.Response(200,json={"notices":[],"recipient_subject":"untrusted_override"})
    client=TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)))
    def bind(actor):
        result=client.post("/native/session",headers=native_headers,json={"access_token":actor,"account_id":actor,"device_id":"test-device","expires_at":time.time()+900})
        assert result.status_code == 200
    a=local_notices.push(title="A private",body="A data",actor_id="usr_a")
    b=local_notices.push(title="B private",body="B data",actor_id="usr_b")
    local_notices.push(title="Unattributed",body="No owner")
    bind("usr_a")
    result=client.get("/native/notices",headers=native_headers)
    assert result.status_code == 200
    assert result.json()["recipient_subject"] == "usr_a"
    assert [row["id"] for row in result.json()["notices"]] == [a]
    bind("usr_b")
    assert [row["id"] for row in client.get("/native/notices",headers=native_headers).json()["notices"]] == [b]
    rejected=client.post("/native/notices/ack",headers=native_headers,json={"ids":[a],"recipient_subject":"usr_a"})
    assert rejected.status_code == 409
    safe=client.post("/native/notices/ack",headers=native_headers,json={"ids":[a,b],"recipient_subject":"usr_b"})
    assert safe.status_code == 200 and safe.json()["acked"] == 1
    assert [row["id"] for row in local_notices.pending(actor_id="usr_a")] == [a]


@pytest.mark.parametrize("review,gateway,identity,status", [
    ("enabled", "http://127.0.0.1:3200", "founder", 200),
    (None, "http://127.0.0.1:3200", "founder", 400),
    ("enabled", "https://gateway.supymem.com", "founder", 400),
    ("enabled", "http://localhost:3200", "founder", 400),
    ("enabled", "http://127.0.0.1:3201", "founder", 400),
    ("enabled", "http://127.0.0.1:3200", "usr_other", 401),
])
def test_local_review_native_founder_requires_exact_gateway_and_verified_identity(
        setup, monkeypatch, review, gateway, identity, status):
    import time
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-fixture-token-" * 3)
    monkeypatch.setenv("GATEWAY_URL", gateway)
    if review is None:
        monkeypatch.delenv("SUPYMEM_LOCAL_REVIEW", raising=False)
    else:
        monkeypatch.setenv("SUPYMEM_LOCAL_REVIEW", review)
    calls = []
    def upstream(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"authenticated": True, "subject": identity})
    client = TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)), base_url=BRIDGE_URL)
    response = client.post("/native/session", headers={"x-native-token": "native-fixture-token-" * 3},
        json={"account_id": "founder", "device_id": "qa-device", "access_token": "fixture-access",
              "expires_at": time.time() + 900})
    assert response.status_code == status
    assert client.get("/health").json()["connected"] is (status == 200)
    assert calls == (["/auth/session"] if status in {200, 401} else [])
    assert "fixture-access" not in response.text


def test_standalone_token_grant_still_refuses_founder_in_review_environment(setup, monkeypatch):
    from desktop_runtime.bridge import BridgeSession
    from fastapi import HTTPException
    monkeypatch.setenv("SUPYMEM_LOCAL_REVIEW", "enabled")
    session = BridgeSession("http://127.0.0.1:3200")
    with pytest.raises(HTTPException) as rejected:
        session.accept_tokens(tokens("founder"))
    assert rejected.value.status_code == 502
    assert not session.connected


@pytest.mark.parametrize("review_actor", [None, "per_other", "founder"])
def test_work_order_gateway_founder_is_never_a_default_identity(review_actor):
    from desktop_runtime.device_gateway import WorkOrderGateway
    with pytest.raises(ValueError, match="native_order_identity_invalid"):
        WorkOrderGateway(venture_id="ven_local", order_id="dwo_local", device_id="qa-device",
            account_id="founder", local_review_actor=review_actor, request=lambda *a: {})


@pytest.mark.parametrize("actor", ["per_local000000", "usr_other", "founder"])
def test_verified_review_bridge_maps_local_actor_through_real_gateway(setup, monkeypatch, actor):
    import time
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-fixture-token-" * 3)
    monkeypatch.setenv("SUPYMEM_LOCAL_REVIEW", "enabled")
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:3200")
    calls = []
    def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": True, "subject": "founder"})
        assert request.method == "GET" and request.url.path == "/api/ventures/ven_local/device-work-orders/dwo_local"
        return httpx.Response(200, json={"id": "dwo_local", "venture_id": "ven_local", "device_id": "qa-device",
            "actor_id": actor, "state": "approved", "review_digest": "a" * 64, "manifest": {}})
    app = create_bridge_app(transport=httpx.MockTransport(upstream))
    client = TestClient(app)
    response = client.post("/native/session", headers={"x-native-token": "native-fixture-token-" * 3},
        json={"account_id": "founder", "device_id": "qa-device", "access_token": "fixture-access",
              "expires_at": time.time() + 900})
    assert response.status_code == 200
    gateway = app.state.native_order_gateway("ven_local", "dwo_local")
    assert gateway.account_id == "per_local000000" and gateway.local_review_actor == "per_local000000"
    if actor == "per_local000000":
        assert gateway.read()["state"] == "approved"
    else:
        with pytest.raises(PermissionError, match="native_order_identity_changed"):
            gateway.read()
    assert gateway.claim_attempted is False and gateway.execution_id is None
    assert calls == ["/auth/session", "/api/ventures/ven_local/device-work-orders/dwo_local"]
