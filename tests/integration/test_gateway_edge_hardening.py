"""Gateway edge limits and telemetry, exercised through the actual ASGI app."""

import asyncio
import importlib
import json

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from tests.integration.test_gateway_native_auth import gateway as gateway


@pytest.mark.parametrize("env", ["local", "test", "staging", "production"])
def test_docs_are_only_available_locally(gateway, monkeypatch, env):
    main, _ = gateway
    monkeypatch.setattr(main.settings, "app_env", env)
    with TestClient(main.create_app()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == (200 if env in {"local", "test"} else 404)
        assert client.get("/health").status_code == 200


def test_body_limit_rejects_auth_and_proxy_bodies_before_upstream(gateway, monkeypatch):
    main, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    monkeypatch.setattr(main.settings, "gateway_max_request_body_bytes", 128)
    proxy = importlib.import_module("app.proxy")
    original, received = httpx.AsyncClient, []

    async def upstream(request):
        received.append(request.content)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(upstream)))
    headers = {"X-CSRF-Token": login.json()["csrf"]}
    assert client.post("/api/edge-check", content=b"x" * 128, headers=headers).status_code == 200
    assert client.post("/api/edge-check", content=b"x" * 129, headers=headers).status_code == 413
    assert len(received) == 1
    for path in ("/auth/login", "/auth/signup", "/openai/v1/chat/completions"):
        assert client.post(path, content=b"x" * 129).status_code == 413
    for length in ("-1", "not-a-number", "9" * 100):
        assert client.post("/auth/login", content=b"{}", headers={"Content-Length": length}).status_code == 413


def test_chunked_and_understated_lengths_are_counted(gateway, monkeypatch):
    main, _ = gateway
    monkeypatch.setattr(main.settings, "gateway_max_request_body_bytes", 64)
    consumed = []

    @main.app.post("/edge-body-check")
    async def body_check(request: Request):
        consumed.append(await request.body())
        return JSONResponse({"ok": True})

    async def exercise():
        async def chunks():
            yield b"x" * 32
            yield b"x" * 33

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            for path in ("/edge-body-check", "/auth/login"):
                for headers in ({}, {"Content-Length": "1"}):
                    response = await client.post(path, content=chunks(), headers=headers)
                    assert response.status_code == 413, response.text
    asyncio.run(exercise())
    assert not consumed


def test_import_limit_exception_is_exact_and_preserves_32_mib(gateway, monkeypatch):
    main, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    monkeypatch.setattr(main.settings, "gateway_max_request_body_bytes", 128)
    proxy = importlib.import_module("app.proxy")
    original, received = httpx.AsyncClient, []

    async def upstream(request):
        received.append(len(request.content))
        return httpx.Response(202, json={"ok": True})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(upstream)))
    headers = {"X-CSRF-Token": login.json()["csrf"], "Content-Type": "application/zip"}
    path = "/api/ventures/ven_test/apps/app_test/imports/imp_test/archive"
    assert client.put(path, content=b"z" * (32 * 1024 * 1024), headers=headers).status_code == 202
    assert received == [32 * 1024 * 1024]
    assert client.put(path, content=b"z", headers={**headers, "Content-Length": "33554433"}).status_code == 413
    for candidate, method in ((path + "/extra", client.put), (path, client.post)):
        assert method(candidate, content=b"z" * 129, headers=headers).status_code == 413
    assert len(received) == 1


def test_csp_report_logs_only_bounded_closed_vocabulary(gateway, caplog):
    _, client = gateway
    private = "https://user:password@example.test/private?email=private@example.test#session-token"
    body = {"document-uri": private, "blocked-uri": private, "source-file": private,
            "script-sample": "secret body and bearer token", "original-policy": private,
            "effective-directive": "script-src-elem", "status-code": 200, "disposition": "report"}
    with caplog.at_level("INFO", logger="app.csp_reports"):
        assert client.post("/csp-report", json={"csp-report": body}, headers={
            "Content-Type": "application/csp-report"}).status_code == 204
        assert client.post("/csp-report", json=[{"type": "csp-violation", "body": {
            "effectiveDirective": "connect-src", "blockedURL": "data:secret", "statusCode": 200,
            "disposition": "report"}}] * 20).status_code == 204
        assert client.post("/csp-report", json={"csp-report": {
            "effective-directive": [private], "blocked-uri": "inline\nprivate@example.test",
            "status-code": True, "disposition": private}}).status_code == 204
    messages = [record.getMessage() for record in caplog.records if record.name == "app.csp_reports"]
    assert len(messages) == 12
    assert "directive=script-src-elem blocked=https status=200 disposition=report" in messages[0]
    assert "directive=other blocked=other status=0 disposition=other" in messages[-1]
    assert all(len(message) < 150 for message in messages)
    assert all(secret not in " ".join(messages) for secret in
               ("private@example.test", "password", "session-token", "secret body", "data:secret"))


def test_csp_report_rejects_oversize_encoding_malformed_and_rate_limits(gateway):
    _, client = gateway
    assert client.post("/csp-report", content=b"x" * (16 * 1024 + 1)).status_code == 413
    assert client.post("/csp-report", content=b"{}", headers={"Content-Encoding": "gzip"}).status_code == 415
    for body in (b"not-json", b"\xff", b"[" * 1100 + b"]" * 1100):
        assert client.post("/csp-report", content=body).status_code == 400
    statuses = [client.post("/csp-report", content=json.dumps({})).status_code for _ in range(40)]
    assert 204 in statuses and 429 in statuses


def test_audit_tap_is_a_ring_and_finalizes_evicted_inflight_entries(gateway):
    from app.audit_tap import AuditTap

    tap = AuditTap()
    entries = [tap.record(subject="usr_test", method="POST", path="/api/check", body=b"secret",
                          request_id=f"req_{index}") for index in range(10_005)]
    assert len(tap.entries) == 10_000
    assert tap.entries[0] is entries[5] and tap.entries[-1] is entries[-1]
    tap.finalize(entries[0], status=201, duration_ms=12)
    assert entries[0].status == 201 and entries[0].duration_ms == 12
    assert not hasattr(entries[0], "body")


def test_request_body_limit_settings_are_positive_and_env_configurable(gateway, monkeypatch):
    from app.config import Settings
    from pydantic import ValidationError

    monkeypatch.setenv("GATEWAY_MAX_REQUEST_BODY_BYTES", "42")
    monkeypatch.setenv("SESSION_SECRET_PREVIOUS", "previous-secret")
    assert Settings().gateway_max_request_body_bytes == 42
    assert Settings().session_secret_previous == "previous-secret"
    with pytest.raises(ValidationError):
        Settings(gateway_max_request_body_bytes=0)
