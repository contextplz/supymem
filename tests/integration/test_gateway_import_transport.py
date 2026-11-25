"""Real gateway forwarding boundary; fixture upstream never reaches a provider."""
import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest

from tests.integration.test_gateway_native_auth import gateway as gateway


PATH = "/api/ventures/ven_test/apps/app_test/imports/imp_test/archive"


@pytest.mark.parametrize("mime,body", [("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
                                    ("application/pdf", b"%PDF-1.7\nfixture")])
def test_asset_download_safety_headers_survive_gateway(gateway, monkeypatch, mime, body):
    _, client = gateway
    client.post("/auth/login", json={"passphrase": "changeme"})
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    headers = {"content-type": mime, "content-disposition": 'attachment; filename="asset"',
               "content-security-policy": "sandbox; default-src 'none'", "x-content-type-options": "nosniff"}

    async def upstream(request):
        return httpx.Response(200, content=body, headers=headers)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs:
        original(**kwargs, transport=httpx.MockTransport(upstream)))
    response = client.get("/api/v1/marketing/assets/mas_test/file", headers={"X-Workspace-ID": "wsp_test"})
    assert response.status_code == 200
    assert response.content == body
    for name, value in headers.items():
        assert response.headers[name] == value


def test_authenticated_zip_exact_bytes_and_rejections_before_upstream(gateway, monkeypatch):
    _, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    proxy = importlib.import_module("app.proxy")
    original, received = httpx.AsyncClient, []
    async def upstream(request):
        received.append(request.content)
        assert request.headers["content-type"] == "application/zip"
        assert request.headers["x-workspace-id"] == "wsp_test"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(202, json={"state": "queued"})
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)))
    headers = {"X-CSRF-Token": login.json()["csrf"], "Content-Type": "application/zip", "X-Workspace-ID": "wsp_test"}
    body = b"controlled-exact-zip-transport\x00\xff"
    response = client.put(PATH, content=body, headers=headers)
    assert response.status_code == 202 and received == [body]
    for change, status in [({"Content-Encoding": "gzip"}, 415), ({"Content-Type": "application/json"}, 415), ({"Content-Length": "33554433"}, 413)]:
        assert client.put(PATH, content=body, headers={**headers, **change}).status_code == status
    assert len(received) == 1


def test_import_capacity_covers_only_exact_put_route(gateway, monkeypatch):
    proxy = importlib.import_module("app.proxy")
    slots = proxy._IMPORT_UPLOAD_SLOTS
    assert slots.acquire(False) and slots.acquire(False)
    visited = []
    async def unchanged(path, request, session, **kwargs):
        visited.append((path, request.method))
        return SimpleNamespace(status_code=204)
    monkeypatch.setattr(proxy, "_forward", unchanged)
    path = PATH.removeprefix("/api")
    try:
        assert asyncio.run(proxy.forward(path, SimpleNamespace(method="PUT"))).status_code == 429
        assert not visited
        for candidate, method in [(path + "/unmatched", "PUT"), (path, "POST"), ("/uploads/anything", "PUT")]:
            assert asyncio.run(proxy.forward(candidate, SimpleNamespace(method=method))).status_code == 204
        assert len(visited) == 3
    finally:
        slots.release()
        slots.release()
