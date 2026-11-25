"""Core 5xx receipts are private, bounded, and contain no request content."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.assertion import MIN_SECRET_BYTES, sign_assertion
from tests.company_os_harness import load_company_os_app


def test_core_failures_are_saved_and_private_without_leaking_request_content(tmp_path, monkeypatch):
    secret = "s" * MIN_SECRET_BYTES
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()

    from app.admin.identity import create_admin
    from app.db.models import AnalyticsEvent
    from app.state import db
    from core.clock import utcnow

    create_admin("ops@example.com", "super_admin", created_by="test")
    app = main.create_app()

    @app.get("/probe/{private_value}")
    def handled_failure(private_value: str):
        raise HTTPException(503, detail={"token": private_value})

    @app.post("/probe/{private_value}/unexpected")
    def unhandled_failure(private_value: str):
        raise RuntimeError(f"secret={private_value}")

    @app.get("/probe-client-error")
    def client_error():
        raise HTTPException(422, detail={"code": "invalid_input"})

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/probe-client-error").status_code == 422
        assert client.get("/probe/secret-123?prompt=do-not-save").status_code == 503
        signed_path = "/probe/secret-456/unexpected"
        founder_assertion = sign_assertion(secret, sub="usr_founder", email="founder@example.com",
                                          role="user", path=signed_path, method="POST")
        assert client.post(signed_path, json={"password": "do-not-save"},
                           headers={"X-COS-Assertion": founder_assertion,
                                    "X-Request-ID": "secretToken12345"}).status_code == 500

        # A normal founder request cannot read operator diagnostics.
        path = "/admin/diagnostics/server-errors"
        assert client.get(path).status_code == 401
        founder_read = sign_assertion(secret, sub="usr_founder", email="founder@example.com",
                                      role="user", path=path, method="GET")
        assert client.get(path, headers={"X-COS-Assertion": founder_read}).status_code == 403
        assertion = sign_assertion(secret, sub="usr_ops", email="ops@example.com",
                                   role="super_admin", path=path, method="GET")
        response = client.get(path, headers={"X-COS-Assertion": assertion})
        assert response.status_code == 200, response.text
        oversized = client.get(path + "?limit=101", headers={"X-COS-Assertion": sign_assertion(
            secret, sub="usr_ops", email="ops@example.com", role="super_admin", path=path, method="GET")})
        assert oversized.status_code == 422
        items = response.json()["items"]
        assert len(items) == 2
        assert {item["status"] for item in items} == {500, 503}
        assert {item["failure_kind"] for item in items} == {"http_5xx", "unhandled_exception"}
        assert next(item for item in items if item["status"] == 503)["error_class"] == "none"
        assert next(item for item in items if item["status"] == 500)["error_class"] == "RuntimeError"
        assert {item["route"] for item in items} == {"/probe/{private_value}", "/probe/{private_value}/unexpected"}
        assert {item["method"] for item in items} == {"GET", "POST"}
        assert {item["person_id"] for item in items} == {None, "usr_founder"}
        assert next(item for item in items if item["status"] == 503)["person_id"] is None
        assert next(item for item in items if item["status"] == 500)["person_id"] == "usr_founder"
        assert next(item for item in items if item["status"] == 503)["request_id"].startswith("req_")
        assert "request_id" not in next(item for item in items if item["status"] == 500)
        body = json.dumps(response.json())
        assert "secret-123" not in body and "secret-456" not in body
        assert "do-not-save" not in body
        assert "secretToken12345" not in body

        # Founder telemetry cannot forge a server receipt into this view.
        with db()() as session:
            session.add(AnalyticsEvent(id="ane_forged", person_id=None, workspace_id=None,
                venture_id=None, event_name="server.error", props_json='{"route":"/forged"}', occurred_at=utcnow()))
            session.add(AnalyticsEvent(id="srv_extra", person_id=None, workspace_id=None,
                venture_id=None, event_name="server.error",
                props_json='{"service":"core","route":"/forged","secret":"do-not-save","error_class":"do-not-save"}', occurred_at=utcnow()))
            session.commit()
        again = client.get(path, headers={"X-COS-Assertion": sign_assertion(
            secret, sub="usr_ops", email="ops@example.com", role="super_admin", path=path, method="GET")})
        assert again.status_code == 200
        assert again.json()["sample_size"] == 3
        assert "do-not-save" not in again.text


def test_server_error_receipt_failure_does_not_replace_original_response(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    main = load_company_os_app()
    app = main.create_app()

    @app.get("/probe-503")
    def failure():
        raise HTTPException(503, detail={"code": "downstream_unavailable"})

    import app.diagnostics.server_errors as capture

    monkeypatch.setattr(capture, "db", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    with TestClient(app) as client:
        response = client.get("/probe-503")
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "downstream_unavailable"}


def test_stream_failure_after_http_200_has_separate_receipt_without_content(monkeypatch):
    load_company_os_app()
    import app.diagnostics.server_errors as capture

    recorded = []
    monkeypatch.setattr(capture, "record_http_failure", lambda *args, **kwargs: recorded.append((args, kwargs)))

    async def stream_then_fail(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream interrupted: prompt=private-value")

    async def send(message):
        pass

    with pytest.raises(RuntimeError, match="stream interrupted"):
        asyncio.run(capture.ServerErrorCaptureMiddleware(stream_then_fail)(
            {"type": "http", "method": "GET", "path": "/stream"}, None, send))
    assert len(recorded) == 1
    kwargs = dict(recorded[0][1])
    assert isinstance(kwargs.pop("frame"), str)  # code location only (file:function), never content
    assert kwargs == {"status": 200, "failure_kind": "stream_exception", "error_class": "RuntimeError"}
    assert "private-value" not in repr(recorded)


def test_successful_stream_has_no_server_error_receipt(monkeypatch):
    load_company_os_app()
    import app.diagnostics.server_errors as capture

    recorded = []
    monkeypatch.setattr(capture, "record_http_failure", lambda *args, **kwargs: recorded.append((args, kwargs)))

    async def completed_stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    async def send(message):
        pass

    asyncio.run(capture.ServerErrorCaptureMiddleware(completed_stream)(
        {"type": "http", "method": "GET", "path": "/stream"}, None, send))
    assert recorded == []


def test_stream_failure_receipt_is_closed_and_durable(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()

    from app.db.models import AnalyticsEvent
    from app.diagnostics.server_errors import record_http_failure
    from app.state import db

    record_http_failure({"method": "GET", "path": "/api/private-stream?prompt=private-value"},
                        status=200, failure_kind="stream_exception")
    with db()() as session:
        rows = session.query(AnalyticsEvent).filter(AnalyticsEvent.event_name == "server.error").all()
    assert len(rows) == 1
    assert "private-value" not in rows[0].props_json
    assert json.loads(rows[0].props_json) == {
        "error_class": "none", "failure_kind": "stream_exception", "method": "GET", "route": "/api",
        "service": "core", "status": 200, "feature": "shell",
    }


def test_unknown_exception_class_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()

    from app.db.models import AnalyticsEvent
    from app.diagnostics.server_errors import record_http_failure
    from app.state import db

    record_http_failure({"method": "GET", "path": "/api/private-value"}, status=500,
                        failure_kind="unhandled_exception", error_class="SecretToken123")
    with db()() as session:
        rows = session.query(AnalyticsEvent).filter(AnalyticsEvent.event_name == "server.error").all()
    assert len(rows) == 1
    assert json.loads(rows[0].props_json)["error_class"] == "other"
    assert "SecretToken123" not in rows[0].props_json


def test_absent_onboarding_404_is_not_a_request_error(monkeypatch):
    load_company_os_app()
    import app.diagnostics.server_errors as capture

    class Route:
        path = "/ventures/{venture_id}/onboarding"

    def fail(*a, **k):
        raise AssertionError("receipt written")

    monkeypatch.setattr(capture, "db", fail)
    scope = {"method": "GET", "route": Route(), "path": "/ventures/v1/onboarding"}
    capture.record_http_failure(scope, status=404, failure_kind="http_4xx", error_code="no_onboarding")


@pytest.mark.parametrize("route,code", [
    ("/ventures/{venture_id}/charter", "no_charter"),
    ("/workspaces/{workspace_id}/site", "no_site"),
])
def test_absent_optional_resource_404_is_not_a_request_error(monkeypatch, route, code):
    load_company_os_app()
    import app.diagnostics.server_errors as capture
    from types import SimpleNamespace

    written = []
    monkeypatch.setattr(capture, "db", lambda: written.append(True))
    scope = {"method": "GET", "route": SimpleNamespace(path=route), "path": route}
    capture.record_http_failure(scope, status=404, failure_kind="http_4xx", error_code=code)
    assert written == []
    capture.record_http_failure(scope, status=404, failure_kind="http_4xx", error_code="not_found")
    assert written == [True]


@pytest.mark.parametrize("check", [test_successful_stream_has_no_server_error_receipt,
    test_stream_failure_after_http_200_has_separate_receipt_without_content,
    test_absent_onboarding_404_is_not_a_request_error])
def test_diagnostics_checks_resolve_core_after_gateway_module_load(monkeypatch, check):
    from tests.integration.test_postgres_auth_security import service_modules
    with service_modules("gateway"):
        import app.config as previous
        assert "/gateway/" in previous.__file__
        check(monkeypatch)
