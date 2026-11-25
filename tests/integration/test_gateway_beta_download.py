"""Public beta endpoints: hashed client id only, redirect preserved, verify CORS for the marketing site."""
import importlib

import httpx
from tests.integration.test_gateway_native_auth import gateway as gateway  # noqa: F401


def test_beta_forwards_hash_not_ip_and_preserves_redirect(gateway, monkeypatch):
    _, client = gateway
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    seen = []

    async def upstream(request):
        seen.append(request)
        assert request.headers["x-subject"] == ""
        if request.url.path == "/public/beta/verify":
            return httpx.Response(200, json={"ok": True, "download_url": "https://x/public/beta/download?code=ABCD2345"})
        return httpx.Response(302, headers={"Location": "https://supymem.com/downloads/Supymem.dmg"})

    monkeypatch.setattr(proxy.httpx, "AsyncClient",
                        lambda **kw: original(**kw, transport=httpx.MockTransport(upstream)))
    ok = client.post("/public/beta/verify", json={"code": "abcd2345"},
                     headers={"Origin": "https://supymem.com", "X-Client-Hash": "forged"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://supymem.com"
    dl = client.get("/public/beta/download?code=ABCD2345", headers={"User-Agent": "UA/1"}, follow_redirects=False)
    assert dl.status_code == 302 and dl.headers["location"].endswith("/downloads/Supymem.dmg")
    for req in seen:
        digest = req.headers["x-client-hash"]
        assert len(digest) == 64 and digest != "forged" and "testclient" not in digest
    assert seen[1].headers["x-client-ua"] == "UA/1"
    assert client.get("/public/beta/verify").status_code == 404
    assert client.post("/public/beta/other").status_code == 404
