"""The packaged helper trusts only native handoff plus real gateway identity."""
from __future__ import annotations

import importlib
import json
import time
from uuid import uuid4

import httpx
import pytest
from desktop_runtime.bridge import create_bridge_app
from fastapi.testclient import TestClient

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway

NATIVE_SECRET = "native-only-fixture-credential-with-entropy"
JS_SECRET = "fixture-webview-scoped-token"
HEADERS = {"x-native-token": NATIVE_SECRET}
JS_HEADERS = {"x-bridge-token": JS_SECRET}


@pytest.fixture
def native_bridge(tmp_path, monkeypatch):
    _main, gateway, saved = _gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "bridge"))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.0")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", NATIVE_SECRET)
    monkeypatch.setenv("BRIDGE_TOKEN", JS_SECRET)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    seen = []
    crash_payloads = []
    fail_crash = []

    def upstream(request):
        seen.append(request.url.path)
        if request.url.path == "/api/telemetry":
            assert request.headers.get("Authorization", "").startswith("Bearer ")
            payload = json.loads(request.content)
            if payload["event"] == "ui.error":
                crash_payloads.append(payload)
                if fail_crash:
                    raise httpx.ConnectError("offline", request=request)
                return httpx.Response(200, json={"id": "usage_crash_fixture"})
            assert payload["event"] == "desktop.process_heartbeat"
            assert payload["context_subject"] == "usr_native_a"
            assert payload["workspace_id"] is None and payload["venture_id"] is None
            return httpx.Response(200, json={"id": "usage_runtime_fixture"})
        response = gateway.request(request.method, request.url.path,
                                   headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, content=response.content)

    app = create_bridge_app(transport=httpx.MockTransport(upstream))
    app.state.test_crash_payloads = crash_payloads
    app.state.test_fail_crash = fail_crash
    try:
        yield gateway, TestClient(app), app, seen, tmp_path
    finally:
        _close_gateway(saved)


def device(gateway, subject="usr_native_a"):
    devices = importlib.import_module("app.device_auth")
    # Real gateway storage, PKCE verification, ES256 signing, and device lookup.
    device_id = devices.create_device(person_id=subject, name="Native fixture")
    verifier = "v" * 43
    code = devices.store_auth_code(person_id=subject, device_id=device_id,
                                  code_challenge=devices._s256(verifier))
    response = gateway.post("/auth/device/token", json={"code": code, "code_verifier": verifier})
    assert response.status_code == 200
    tokens = response.json()
    return tokens, {"account_id": subject, "device_id": device_id,
                    "access_token": tokens["access_token"], "expires_at": time.time() + 890}


def test_real_device_handoff_does_not_expose_or_persist_tokens(native_bridge):
    gateway, bridge, app, seen, directory = native_bridge
    tokens, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    assert seen == ["/auth/session"]
    assert app.state.bridge_session.account_id == "usr_native_a"
    assert app.state.bridge_session.refresh_token is None
    public = bridge.get("/config").text + bridge.get("/status", headers=JS_HEADERS).text
    for secret in (NATIVE_SECRET, JS_SECRET, tokens["access_token"], tokens["refresh_token"]):
        assert secret not in public
        assert secret.encode() not in (directory / "bridge" / "bridge_outbox.sqlite").read_bytes()
    assert bridge.post("/native/clear", headers=HEADERS).status_code == 200
    assert bridge.get("/outbox", headers=JS_HEADERS).status_code == 401


def test_desktop_inventory_requires_native_token_and_current_device(native_bridge, monkeypatch):
    from desktop_runtime.peekaboo_driver import AppChoice, InstalledApp, PeekabooDriver
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    app = AppChoice("Google Chrome", "com.google.Chrome", 123, "987654321")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "installed_apps", classmethod(
        lambda cls: [InstalledApp(app.name, app.bundle_id, "/Applications/Google Chrome.app")]))
    monkeypatch.setattr(PeekabooDriver, "default_browser_bundle", classmethod(
        lambda cls, installed: app.bundle_id))
    monkeypatch.setattr(PeekabooDriver, "running_windows",
                        classmethod(lambda cls, selected: [{"window_id": 44, "title": "Search",
                            "on_screen": True, "width": 1000, "height": 700}]))
    path = "/native/desktop/apps?bundle_id=com.google.Chrome"
    assert bridge.get(path).status_code == 401
    assert bridge.get(path, headers=JS_HEADERS).status_code == 401
    assert bridge.get(path, headers=HEADERS).status_code == 401
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    result = bridge.get(path, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["items"][0]["process_start_identity"] == "987654321"
    assert result.json()["windows"][0]["window_id"] == 44
    launch_path = "/native/desktop/apps/launch"
    for headers in ({}, JS_HEADERS, {**HEADERS, "Origin": "https://app.supymem.com"}):
        assert bridge.post(launch_path, headers=headers, json={"bundle_id": app.bundle_id}).status_code == 401
    requested = []
    monkeypatch.setattr(PeekabooDriver, "launch_installed_app", classmethod(
        lambda cls, bundle, *, ensure_window=False: requested.append((bundle, ensure_window)) or app))
    launched = bridge.post(launch_path, headers=HEADERS, json={"bundle_id": app.bundle_id})
    assert launched.status_code == 409
    assert launched.json()["detail"]["error"]["code"] == "desktop_app_launch_unavailable"
    assert bridge.post(launch_path, headers=HEADERS,
                       json={"bundle_id": app.bundle_id, "ensure_window": True}).status_code == 409
    assert requested == []
    assert bridge.post(launch_path, headers=HEADERS,
                       json={"bundle_id": app.bundle_id, "ensure_window": "true"}).status_code == 400


def test_permission_setup_works_before_sign_in_but_only_from_native_shell(native_bridge, monkeypatch):
    from desktop_runtime.peekaboo_driver import PeekabooDriver
    _, bridge, _, _, _ = native_bridge
    current = {"screen_recording": False, "accessibility": False, "event_synthesizing": False}
    monkeypatch.setattr(PeekabooDriver, "permissions_status", classmethod(lambda cls: current))
    monkeypatch.setattr(PeekabooDriver, "request_permission", classmethod(
        lambda cls, kind: {**current, "requested": kind}))
    path = "/native/desktop/permissions"
    for headers in ({}, JS_HEADERS, {**HEADERS, "Origin": "https://app.supymem.com"}):
        assert bridge.get(path, headers=headers).status_code == 401
    assert bridge.get(path, headers=HEADERS).json()["permissions"] == current
    request_path = path + "/request"
    assert bridge.post(request_path, headers=JS_HEADERS, json={"kind": "accessibility"}).status_code == 401
    assert bridge.post(request_path, headers=HEADERS, json={"kind": "unknown"}).status_code == 400
    assert bridge.post(request_path, headers=HEADERS, json={"kind": "accessibility"}).json()[
        "permissions"]["requested"] == "accessibility"


def test_webview_secret_cannot_replace_or_clear_native_identity(native_bridge):
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    for headers in ({}, JS_HEADERS, {**HEADERS, "Origin": "https://app.supymem.com"}):
        assert bridge.post("/native/session", headers=headers, json=body).status_code == 401
        assert bridge.post("/native/clear", headers=headers).status_code == 401
    for route in ("/auth/pkce/start", "/auth/pkce/complete", "/disconnect"):
        assert bridge.post(route, headers=JS_HEADERS, json={"code": "no"}).status_code == 409


def test_foreign_claim_expired_and_revoked_device_are_rejected(native_bridge):
    gateway, bridge, app, _, _ = native_bridge
    tokens, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json={**body, "account_id": "usr_native_b"}).status_code == 401
    assert bridge.post("/native/session", headers=HEADERS, json={**body, "expires_at": 1}).status_code == 400
    assert bridge.post("/native/session", headers=HEADERS, json={**body, "refresh_token": tokens["refresh_token"]}).status_code == 400
    revoked = gateway.post("/auth/device/revoke", json={"device_id": tokens["device_id"], "refresh_token": tokens["refresh_token"]})
    assert revoked.status_code == 200
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 401
    assert not app.state.bridge_session.connected


def test_expired_native_session_never_refreshes_in_helper(native_bridge):
    gateway, bridge, app, seen, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    app.state.bridge_session.expires_at = 1
    response = bridge.post("/connect", headers=JS_HEADERS)
    assert response.status_code == 401
    assert "/auth/device/refresh" not in seen


def test_native_account_switch_keeps_outbox_partitioned(native_bridge):
    from desktop_runtime import offline
    gateway, bridge, _, _, _ = native_bridge
    _, first = device(gateway)
    _, second = device(gateway, "usr_native_b")
    for body in (first, second):
        offline.enqueue("POST", "/api/tasks", {"owner": body["account_id"]}, idempotency_key="same-operation",
                        gateway="https://gateway.supymem.com", account_id=body["account_id"])
        assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
        items = bridge.get("/outbox", headers=JS_HEADERS).json()["items"]
        assert len(items) == 1 and items[0]["body"]["owner"] == body["account_id"]


def test_rotated_device_recovers_only_old_cancelled_preinference_journal(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    old_device = "dev_old"
    scope = ("https://gateway.supymem.com", body["account_id"], old_device)
    device_journal.create(scope, "ven_one", "dwo_one")
    device_journal.save(scope, "ven_one", "dwo_one", {
        "order_id": "dwo_one", "state": "reconciliation_required", "tool": "coding",
        "failure_code": "native_worker_upstream_rejected", "task_verified": False,
        "credits_reconciled": False, "evidence_digest": "b" * 64,
        "evidence": {"outcome": "execution_requires_reconciliation",
                     "processes_stopped": False, "execution_id": "dex_one",
                     "inventory": {"inventory_complete": True, "processes": []}},
    })
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: {
        "id": "dwo_one", "state": "cancelled", "device_id": old_device,
        "execution_id": "dex_one"})
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "reconcile_startup_failure",
        lambda self, execution_id, digest: calls.append((execution_id, digest)) or {"state": "stopped"})
    status = bridge.get("/native/device-work-orders/ven_one/dwo_one/status", headers=HEADERS)
    assert status.status_code == 200 and status.json()["state"] == "reconciliation_required"
    recovered = bridge.post("/native/device-work-orders/ven_one/dwo_one/reconcile", headers=HEADERS)
    assert recovered.status_code == 200 and recovered.json()["credits_reconciled"] is True
    assert calls == [("dex_one", "b" * 64)]
    assert device_journal.read(scope, "ven_one", "dwo_one")["state"] == "stopped"
    assert device_journal.read((scope[0], scope[1], body["device_id"]), "ven_one", "dwo_one") is None


@pytest.mark.parametrize("driver_code,failure_code,effect_state", [
    ("desktop_fresh_observation_required", "computer_use_effect_outcome_uncertain", "uncertain"),
    ("desktop_pre_dispatch_unknown", "native_worker_upstream_rejected", "admitted"),
])
def test_cancelled_desktop_pre_dispatch_reconciliation_uses_exact_saved_receipt(
        native_bridge, monkeypatch, driver_code, failure_code, effect_state):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    saved = {"order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": failure_code,
        "driver_failure_code": driver_code,
        "invocation_id": "din_one", "evidence_digest": "d" * 64,
        "evidence": {"outcome": "execution_requires_reconciliation", "execution_id": "dex_one"}}
    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": {
            "execution_id": "dex_one", "evidence_digest": "d" * 64, "processes_stopped": True},
        "invocations": {"din_one": {"effect": {"state": effect_state, "digest": "c" * 64}}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "effect_receipt",
        lambda self, *args: calls.append(("effect", *args)))
    monkeypatch.setattr(WorkOrderGateway, "reconcile_desktop_pre_dispatch",
        lambda self, *args: calls.append(("stop", *args)) or {"state": "stopped"})
    path = "/native/device-work-orders/ven_one/dwo_one/reconcile"
    assert bridge.post(path, headers=HEADERS).status_code == 200
    assert calls == [("effect", "din_one", "c" * 64, "not_dispatched", driver_code),
                     ("stop", "dex_one", "d" * 64)]
    assert device_journal.read(scope, "ven_one", "dwo_one")["credits_reconciled"] is True

    device_journal.save(scope, "ven_one", "dwo_one", {**saved,
        "driver_failure_code": "desktop_driver_transport_unavailable"})
    assert bridge.post(path, headers=HEADERS).status_code == 409
    assert len(calls) == 2


def test_cancelled_desktop_text_effect_closes_only_as_unverified(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    saved = {"order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": "computer_use_effect_outcome_uncertain",
        "driver_failure_code": "desktop_effect_outcome_uncertain",
        "evidence_digest": "d" * 64,
        "evidence": {"outcome": "execution_requires_reconciliation", "execution_id": "dex_one"}}
    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": {
            "execution_id": "dex_one", "evidence_digest": "d" * 64, "processes_stopped": True},
        "invocations": {"din_one": {"proposal": {"operation": "type"},
            "effect": {"state": "uncertain", "digest": "c" * 64}}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "reconcile_stopped_desktop_text",
        lambda self, *args: calls.append(args) or {"state": "stopped"})
    path = "/native/device-work-orders/ven_one/dwo_one/reconcile"
    result = bridge.post(path, headers=HEADERS)
    assert result.status_code == 200 and result.json()["credits_reconciled"] is True
    assert result.json()["task_verified"] is False
    assert calls == [("dex_one", "d" * 64)]
    assert device_journal.read(scope, "ven_one", "dwo_one")["outcome"] == \
        "reviewed_text_effect_outcome_unverified_reconciled"

    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order["invocations"]["din_one"]["proposal"]["operation"] = "click"
    assert bridge.post(path, headers=HEADERS).status_code == 409
    assert len(calls) == 1


def test_cancelled_desktop_retries_only_final_known_usage_stop(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    saved = {"order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": "computer_use_scope_denied", "evidence_digest": "d" * 64,
        "evidence": {"outcome": "cancelled", "execution_id": "dex_one"}}
    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": {"execution_id": "dex_one",
            "evidence_digest": "d" * 64, "processes_stopped": True},
        "invocations": {"din_one": {"state": "completed", "effect": {"state": "returned"}},
                        "din_two": {"state": "completed"}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "retry_stopped_known_usage",
        lambda self, *args: calls.append(args) or {"state": "stopped"})
    path = "/native/device-work-orders/ven_one/dwo_one/reconcile"
    result = bridge.post(path, headers=HEADERS)
    assert result.status_code == 200 and result.json()["credits_reconciled"] is True
    assert calls == [("dex_one", "d" * 64)]
    assert device_journal.read(scope, "ven_one", "dwo_one")["task_verified"] is False

    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order["invocations"]["din_two"]["state"] = "uncertain"
    assert bridge.post(path, headers=HEADERS).status_code == 409
    assert len(calls) == 1


def test_cancelled_desktop_unknown_usage_reconciles_only_exact_stopped_receipt(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    saved = {"order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": "computer_use_scope_denied", "evidence_digest": "d" * 64,
        "evidence": {"outcome": "cancelled", "execution_id": "dex_one"}}
    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": {"execution_id": "dex_one",
            "evidence_digest": "d" * 64, "processes_stopped": True},
        "invocations": {"din_one": {"state": "completed", "effect": {"state": "returned"}},
                        "din_two": {"state": "uncertain"}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "settle_stopped_unknown_usage",
        lambda self, *args: calls.append(args) or {
            "state": "stopped", "reconciliation": "admitted_quote_upper_bound"})
    path = "/native/device-work-orders/ven_one/dwo_one/reconcile"
    result = bridge.post(path, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["credits_reconciled"] is True
    assert result.json()["actual_usage_unconfirmed"] is True
    assert result.json()["task_verified"] is False
    assert calls == [("dex_one", "d" * 64)]

    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order["native_stop"]["evidence_digest"] = "e" * 64
    assert bridge.post(path, headers=HEADERS).status_code == 409
    assert len(calls) == 1


def test_cancelled_desktop_retries_missing_server_stop_then_unknown_usage(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    device_journal.save(scope, "ven_one", "dwo_one", {
        "order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": "computer_use_scope_denied", "evidence_digest": "d" * 64,
        "evidence": {"outcome": "cancelled", "execution_id": "dex_one"}})
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": None,
        "invocations": {"din_one": {"state": "uncertain"}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    stops = []
    def stop(self, execution_id, evidence_digest):
        stops.append((execution_id, evidence_digest))
        order["native_stop"] = {"execution_id": execution_id,
            "evidence_digest": evidence_digest, "processes_stopped": True}
        return {"state": "cancelled"}
    monkeypatch.setattr(WorkOrderGateway, "stop_receipt", stop)
    charged = []
    monkeypatch.setattr(WorkOrderGateway, "settle_stopped_unknown_usage",
        lambda self, *args: charged.append(args) or {
            "state": "stopped", "reconciliation": "admitted_quote_upper_bound"})
    result = bridge.post("/native/device-work-orders/ven_one/dwo_one/reconcile", headers=HEADERS)
    assert result.status_code == 200 and result.json()["credits_reconciled"] is True
    assert stops == [("dex_one", "d" * 64)] and charged == stops


def test_founder_acknowledged_click_reconciles_only_matching_stopped_effect(native_bridge, monkeypatch):
    from desktop_runtime import device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway

    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    device_journal.create(scope, "ven_one", "dwo_one")
    saved = {"order_id": "dwo_one", "state": "stopped", "tool": "desktop",
        "task_verified": False, "credits_reconciled": False,
        "failure_code": "computer_use_effect_outcome_uncertain",
        "driver_failure_code": "desktop_driver_UNKNOWN_ERROR", "operation": "click",
        "evidence_digest": "d" * 64,
        "evidence": {"outcome": "execution_requires_reconciliation", "execution_id": "dex_one"}}
    device_journal.save(scope, "ven_one", "dwo_one", saved)
    order = {"state": "cancelled", "device_id": body["device_id"],
        "execution_id": "dex_one", "native_stop": {
            "execution_id": "dex_one", "evidence_digest": "d" * 64, "processes_stopped": True},
        "invocations": {"din_one": {"proposal": {"operation": "click"},
            "effect": {"state": "uncertain", "digest": "c" * 64}}}}
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: order)
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "reconcile_acknowledged_desktop_click",
        lambda self, *args: calls.append(args) or {"state": "stopped"})
    path = "/native/device-work-orders/ven_one/dwo_one/reconcile"
    assert bridge.post(path, headers=HEADERS).status_code == 409
    assert calls == []
    order["uncertain_desktop_click_ack"] = {"execution_id": "dex_one",
        "evidence_digest": "d" * 64, "effect_digest": "c" * 64}
    result = bridge.post(path, headers=HEADERS)
    assert result.status_code == 200 and result.json()["credits_reconciled"] is True
    assert result.json()["task_verified"] is False
    assert calls == [("dex_one", "d" * 64)]
    assert device_journal.read(scope, "ven_one", "dwo_one")["outcome"] == \
        "founder_reviewed_click_outcome_unverified_reconciled"


def test_saved_source_upload_requires_native_token_and_exact_stopped_device(native_bridge, monkeypatch):
    from desktop_runtime import device_journal, source_receipt
    from desktop_runtime.device_gateway import WorkOrderGateway
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    scope = ("https://gateway.supymem.com", body["account_id"], body["device_id"])
    frozen = {"order_id": "dwo_one", "venture_id": "ven_one", "device_id": body["device_id"],
              "sha256": "a" * 64, "qa_verified": False}
    device_journal.create(scope, "ven_one", "dwo_one")
    device_journal.save(scope, "ven_one", "dwo_one", {
        "order_id": "dwo_one", "state": "stopped", "tool": "coding",
        "task_verified": False, "source_receipt": frozen})
    monkeypatch.setattr(WorkOrderGateway, "read", lambda self: {"state": "stopped", "device_id": body["device_id"]})
    monkeypatch.setattr(source_receipt, "load_archive", lambda receipt: b"exact saved archive")
    calls = []
    monkeypatch.setattr(WorkOrderGateway, "store_coding_source",
        lambda self, receipt, blob: calls.append((receipt, blob)) or {"state": "stored", "sha256": "a" * 64})
    path = "/native/device-work-orders/ven_one/dwo_one/source-upload"
    assert bridge.post(path, headers=JS_HEADERS).status_code == 401
    response = bridge.post(path, headers=HEADERS)
    assert response.status_code == 200 and response.json()["source_storage_state"] == "stored"
    assert calls == [(frozen, b"exact saved archive")]
    assert device_journal.read(scope, "ven_one", "dwo_one")["source_storage_state"] == "stored"


def test_native_notice_channel_proxies_with_session_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "bridge"))
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", NATIVE_SECRET)
    monkeypatch.setenv("BRIDGE_TOKEN", JS_SECRET)
    seen = []

    def upstream(request):
        seen.append((request.method, request.url.path, request.headers.get("Authorization"), request.content))
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": True, "subject": "usr_native_a"})
        if request.url.path == "/api/desktop/notices":
            return httpx.Response(200, json={"notices": [{"id": "not_1", "title": "Needs you", "body": "Approve the draft"}]})
        if request.url.path == "/api/desktop/notices/ack":
            return httpx.Response(200, json={"acked": 1})
        return httpx.Response(404)

    bridge = TestClient(create_bridge_app(transport=httpx.MockTransport(upstream)))
    body = {"account_id": "usr_native_a", "device_id": "dev_a",
            "access_token": "signed-access-token", "expires_at": time.time() + 890}
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    got = bridge.get("/native/notices", headers=HEADERS)
    assert got.status_code == 200 and any(
        notice["id"] == "not_1" for notice in got.json()["notices"])
    assert got.json()["recipient_subject"] == "usr_native_a"
    unbound = bridge.post("/native/notices/ack", headers=HEADERS, json={"ids": ["not_1"]})
    assert unbound.status_code == 409
    acked = bridge.post("/native/notices/ack", headers=HEADERS,
                        json={"ids": ["not_1"], "recipient_subject": got.json()["recipient_subject"]})
    assert acked.status_code == 200 and acked.json() == {"acked": 1}
    notice_calls = [entry for entry in seen if entry[1].startswith("/api/desktop/notices")]
    assert [entry[:2] for entry in notice_calls] == [
        ("GET", "/api/desktop/notices"), ("POST", "/api/desktop/notices/ack")]
    assert all(entry[2] == "Bearer signed-access-token" for entry in notice_calls)
    assert b'"not_1"' in notice_calls[1][3]


def test_native_notice_channel_rejects_webview_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "bridge"))
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", NATIVE_SECRET)
    monkeypatch.setenv("BRIDGE_TOKEN", JS_SECRET)
    bridge = TestClient(create_bridge_app(transport=httpx.MockTransport(lambda _: httpx.Response(500))))
    assert bridge.get("/native/notices", headers=JS_HEADERS).status_code == 401
    assert bridge.post("/native/notices/ack", headers=JS_HEADERS, json={"ids": ["not_1"]}).status_code == 401


def test_native_runtime_heartbeat_is_identity_bound_and_webview_cannot_send(native_bridge):
    gateway, bridge, _, seen, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    heartbeat = {"event_id": str(uuid4()), "session_id": str(uuid4()), "app_version": "0.1.48"}
    assert bridge.post("/native/telemetry/runtime", headers=JS_HEADERS, json=heartbeat).status_code == 401
    sent = bridge.post("/native/telemetry/runtime", headers=HEADERS, json=heartbeat)
    assert sent.status_code == 200, sent.text
    assert seen[-1] == "/api/telemetry"


def test_native_crash_summary_retries_offline_without_leaking_stack_or_switching_owner(native_bridge):
    from desktop_runtime.crash_report import enqueue_crash, pending_crash_summaries, queue_dir

    gateway, bridge, app, _, directory = native_bridge
    root = directory / "bridge"
    # Legacy/unowned crashes are not attached to whoever signs in next.
    legacy = enqueue_crash(stack="legacy secret", root=root)
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    current = enqueue_crash(stack="sensitive prompt and secret", root=root)
    rust = queue_dir(root) / f"crash-{uuid4()}.json"
    rust.write_text(json.dumps({"kind": "crash", "crash_process": "rust",
        "crash_owner": {"account_id": body["account_id"], "device_id": body["device_id"]},
        "app_version": "0.1.52", "stack": "native private path and token"}), encoding="utf-8")
    assert pending_crash_summaries("usr_different_account", body["device_id"], root) == []
    assert pending_crash_summaries(body["account_id"], "dev_different_device", root) == []
    app.state.test_fail_crash.append(True)
    heartbeat = {"event_id": str(uuid4()), "session_id": str(uuid4()), "app_version": "0.1.48"}
    assert bridge.post("/native/telemetry/runtime", headers=HEADERS, json=heartbeat).status_code == 200
    assert current.exists() and rust.exists() and legacy.exists()
    first = app.state.test_crash_payloads[-1]
    expected_text = {"0.1.0": "Mac uncaught_exception", "0.1.52": "Mac panic_observed"}
    assert first["props"]["app_version"] in expected_text
    assert {k: v for k, v in first["props"].items() if k not in {"error_source", "error_intent"}} == {"feature": "settings", "platform": "macos",
        "app_version": first["props"]["app_version"],
        "session_id": first["props"]["session_id"], "error_code": "native_error",
        "error_context": "native", "error_text": expected_text[first["props"]["app_version"]]}
    assert "sensitive" not in json.dumps(first) and "secret" not in json.dumps(first)
    app.state.test_fail_crash.clear()
    assert bridge.post("/native/telemetry/runtime", headers=HEADERS, json=heartbeat).status_code == 200
    assert not current.exists() and not rust.exists() and legacy.exists()
    assert first in app.state.test_crash_payloads  # Stable idempotency key and body.
    assert {payload["props"]["error_text"] for payload in app.state.test_crash_payloads} == {
        "Mac uncaught_exception", "Mac panic_observed"}
    assert all("stack" not in json.dumps(payload) and "token" not in json.dumps(payload)
               for payload in app.state.test_crash_payloads)
    assert bridge.post("/native/clear", headers=HEADERS).status_code == 200
    assert len(list(queue_dir(root).glob("crash-*.json"))) == 1


@pytest.mark.parametrize("protocol,socket", [("all_windows", True), ("exact_window_observation_v1", False)])
def test_cua_unknown_or_orphan_protocol_refuses_before_journal(native_bridge, monkeypatch, protocol, socket):
    from desktop_runtime import device_journal
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    monkeypatch.setenv("SUPYMEM_NATIVE_BROWSER_ENABLED", "1")
    monkeypatch.setattr(device_journal, "create", lambda *_: pytest.fail("protocol must refuse before journal"))
    headers = dict(HEADERS, **{"x-supymem-cua-protocol": protocol})
    if socket:
        headers.update({"x-supymem-cua-socket": "/private/tmp/supymem-cua-test/driver.sock",
                        "x-supymem-cua-host-pid": "1234"})
    response = bridge.post("/native/device-work-orders/ven_one/dwo_one/start", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "desktop_cua_protocol_unsupported"


def test_cua_protocol_reaches_worker_after_native_handoff(native_bridge, monkeypatch):
    from desktop_runtime import device_worker, device_journal
    from desktop_runtime.device_gateway import WorkOrderGateway
    gateway, bridge, _, _, _ = native_bridge
    _, body = device(gateway)
    assert bridge.post("/native/session", headers=HEADERS, json=body).status_code == 200
    monkeypatch.setenv("SUPYMEM_NATIVE_BROWSER_ENABLED", "1")
    monkeypatch.setattr(WorkOrderGateway, "read", lambda _: {"id": "dwo_one"})
    monkeypatch.setattr(device_journal, "create", lambda *_: None)
    received = []

    class Worker:
        closed = True
        def __init__(self, gateway, save, **kwargs): received.append(kwargs)
        def run(self): pass
        def cancel(self): self.closed = True

    monkeypatch.setattr(device_worker, "DeviceWorker", Worker)
    response = bridge.post("/native/device-work-orders/ven_one/dwo_one/start", headers={**HEADERS,
        "x-supymem-cua-protocol": "exact_window_observation_v1",
        "x-supymem-cua-socket": "/private/tmp/supymem-cua-test/driver.sock",
        "x-supymem-cua-host-pid": "1234"})
    assert response.status_code == 200 and response.json()["task_verified"] is False
    assert len(received) == 1
    activity_current = received[0]["activity_current"]
    assert received == [{"cua_socket": "/private/tmp/supymem-cua-test/driver.sock",
                         "cua_host_pid": 1234, "cua_protocol": "exact_window_observation_v1",
                         "activity_current": activity_current}]
    assert callable(activity_current) and activity_current() is True
    assert bridge.post("/native/clear", headers=HEADERS).status_code == 200
    assert activity_current() is False
