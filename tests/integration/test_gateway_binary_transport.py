"""Authenticated gateway transport preserves file bytes and security headers."""
import importlib

import httpx
import pytest

from tests.integration.test_gateway_native_auth import gateway as gateway


@pytest.mark.parametrize("mime,body", [
    ("image/png", b"\x89PNG\r\n\x1a\n\xff\x00\x81"),
    ("video/mp4", b"\x00\x00\x00\x20ftyp\xff\xfe\x00"),
    ("application/pdf", b"%PDF-1.7\n\xff\xfe\x00"),
    ("application/octet-stream", b"{\"looks\":\"like JSON but is a binary file\"}"),
])
def test_owned_binary_response_is_byte_exact(gateway, monkeypatch, mime, body):
    _, client = gateway
    assert client.post("/auth/login", json={"passphrase": "changeme"}).status_code == 200
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    async def upstream(request):
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=body, headers={
            "Content-Type": mime, "Content-Disposition": 'attachment; filename="asset.bin"',
            "Content-Security-Policy": "sandbox; default-src 'none'", "X-Content-Type-Options": "nosniff",
            "Set-Cookie": "must-not-reach-founder=1",
        })
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    result = client.get("/api/assets/example/file")
    assert result.status_code == 200
    assert result.content == body
    assert result.headers["content-type"] == mime
    assert result.headers["content-disposition"] == 'attachment; filename="asset.bin"'
    assert result.headers["content-security-policy"].startswith("sandbox")
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in result.headers


def test_multipart_and_version_selector_cross_gateway_unchanged(gateway, monkeypatch):
    _, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    body = b"--test-boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"logo.png\"\r\n\r\n\xff\x00\r\n--test-boundary--\r\n"
    async def upstream(request):
        assert request.content == body
        assert request.headers["content-type"] == "multipart/form-data; boundary=test-boundary"
        assert request.headers["if-match"] == '"revision-1"'
        assert request.headers["x-workspace-id"] == "wsp_test"
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    response = client.post("/api/assets/upload", content=body, headers={
        "Content-Type": "multipart/form-data; boundary=test-boundary", "X-CSRF-Token": login.json()["csrf"],
        "If-Match": '"revision-1"', "X-Workspace-ID": "wsp_test",
    })
    assert response.status_code == 200


@pytest.mark.parametrize("method,status", [("DELETE", 204), ("GET", 304), ("HEAD", 200)])
def test_empty_protocol_responses_do_not_become_json_errors(gateway, monkeypatch, method, status):
    _, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient

    async def upstream(request):
        return httpx.Response(status, headers={"Content-Type": "application/json", "ETag": '"v1"'})

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    result = client.request(method, "/api/assets/example", headers={"X-CSRF-Token": login.json()["csrf"]})
    assert result.status_code == status
    assert result.content == b""
    assert result.headers["etag"] == '"v1"'
