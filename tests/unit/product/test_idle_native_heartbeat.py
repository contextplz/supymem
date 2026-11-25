"""A renewed native heartbeat delivers diagnostics without restoring retired order custody."""
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from desktop_runtime.bridge import create_bridge_app
from desktop_runtime import crash_report


@pytest.mark.parametrize("transition", ["expired", "disconnected", "cleared"])
def test_idle_native_handoff_retries_owned_summary_but_retires_prior_order(tmp_path, monkeypatch, transition):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    for key, value in {"COMPANY_OS_DATA_DIR": str(tmp_path), "BRIDGE_TOKEN": "synthetic-local-secret",
                       "BRIDGE_AUTH_OWNER": "native", "BRIDGE_NATIVE_TOKEN": "synthetic-native-owner-secret-more-than-32-bytes",
                       "GATEWAY_URL": "https://example.invalid", "COMPANY_OS_APP_VERSION": "0.1.84"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("COMPANY_OS_PUBLIC_URL", raising=False)
    monkeypatch.setattr(crash_report, "_ensure_error_log", lambda: None)
    deliveries = []
    def handler(request):
        if request.url.path == "/auth/session":
            return httpx.Response(200, json={"authenticated": True, "subject": "usr_one"})
        if request.url.path == "/api/telemetry":
            import json
            payload = json.loads(request.content)
            if transition == "disconnected" and request.headers["Authorization"] == "Bearer original":
                return httpx.Response(401)
            deliveries.append(payload)
            return httpx.Response(200, json={"id": "usage_fixture"})
        return httpx.Response(200, json={"id": "order", "venture_id": "ven_one", "device_id": "device",
            "review_digest": "a" * 64, "state": "approved"})
    app = create_bridge_app(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        headers = {"x-native-token": "synthetic-native-owner-secret-more-than-32-bytes"}
        body = {"account_id": "usr_one", "device_id": "device", "access_token": "original", "expires_at": time.time() + 900}
        assert client.post("/native/session", headers=headers, json=body).status_code == 200
        session = app.state.bridge_session
        old_gateway = app.state.native_order_gateway("ven_one", "order")
        prior_epoch = session.auth_epoch
        retired = threading.Event()
        session.activity_retired = retired.set
        diagnostic = {"phase": "deciding", "reason": "cu_browser_decision_unavailable", "phase_ms": 20, "elapsed_ms": 100}
        crash_report.report_error("device_worker", "run_mac_task", root=tmp_path, runtime_diagnostic=diagnostic)
        if transition == "expired":
            session.expires_at = time.time() - 1
        elif transition == "cleared":
            assert client.post("/native/clear", headers=headers).status_code == 200
        tick = {"event_id": str(uuid4()), "session_id": str(uuid4()), "app_version": "0.1.84"}
        assert client.post("/native/telemetry/runtime", headers=headers, json=tick).status_code == 401
        assert len(crash_report.pending_crash_summaries("usr_one", "device", tmp_path)) == 1
        assert client.post("/native/session", headers=headers, json={**body,
            "access_token": "renewed", "expires_at": time.time() + 900}).status_code == 200
        assert retired.is_set() and session.auth_epoch > prior_epoch
        with pytest.raises(PermissionError, match="account_changed"):
            old_gateway.read()
        assert client.post("/native/telemetry/runtime", headers=headers, json={**tick, "event_id": str(uuid4())}).status_code == 200
        assert crash_report.pending_crash_summaries("usr_one", "device", tmp_path) == []
        errors = [payload for payload in deliveries if payload["event"] == "ui.error"]
        assert len(errors) == 1 and errors[0]["context_subject"] == "usr_one"
        assert errors[0]["props"]["runtime_diagnostic"] == diagnostic
