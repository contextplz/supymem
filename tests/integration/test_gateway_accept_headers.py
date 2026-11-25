"""Gateway must forward Accept and a narrow response-header allowlist.

PR18 already shipped this transport on the freeze lineage. Core now negotiates
HTML receipts when Accept prefers text/html. These tests still mock upstream
so they stay a Gateway contract, not a Core persistence proof.
"""
import importlib

import httpx

from tests.integration.test_gateway_native_auth import gateway


def test_gateway_forwards_accept_and_preserves_rate_limit_headers(gateway, monkeypatch):
    _, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    assert login.status_code == 200
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(
            201,
            content=b'{"id":"interest_fixture","received":true}',
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store, max-age=0",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Vary": "Accept, Origin",
                "X-RateLimit-Limit": "1",
                "X-RateLimit-Remaining": "0",
                "Retry-After": "12",
                "Set-Cookie": "upstream_secret=must-not-forward",
                "X-Internal-Secret": "must-not-forward",
            },
        )

    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)),
    )
    response = client.post(
        "/api/w/wsp_receipt/interest",
        content="email=synthetic%40example.invalid",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml",
            "X-CSRF-Token": login.json()["csrf"],
        },
    )
    assert response.status_code == 201, response.text
    assert seen[0].headers.get("accept") == "text/html,application/xhtml+xml"
    assert "cookie" not in seen[0].headers
    assert "authorization" not in seen[0].headers
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert {part.strip().lower() for part in response.headers["vary"].split(",")} >= {"accept", "origin"}
    assert response.headers["x-ratelimit-limit"] == "1"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["retry-after"] == "12"
    assert "set-cookie" not in response.headers
    assert "x-internal-secret" not in response.headers


def test_gateway_preserves_html_body_when_upstream_negotiates_html(gateway, monkeypatch):
    _, client = gateway
    login = client.post("/auth/login", json={"passphrase": "changeme"})
    proxy = importlib.import_module("app.proxy")
    original = httpx.AsyncClient

    async def upstream(request):
        assert request.headers.get("accept") == "text/html"
        return httpx.Response(
            201,
            content=b"<html><body>Thank you</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8", "Vary": "Accept"},
        )

    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(upstream)),
    )
    response = client.post(
        "/api/w/wsp_receipt/interest",
        content="email=synthetic%40example.invalid",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
            "X-CSRF-Token": login.json()["csrf"],
        },
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("text/html")
    assert "Thank you" in response.text
    vary_tokens = {part.strip() for part in response.headers["vary"].lower().split(",")}
    assert "accept" in vary_tokens
