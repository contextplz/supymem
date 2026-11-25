"""Exact public transport exceptions, without provider/state or live acceptance.

S10 owns the Apollo attempt and unsubscribe token handlers. These tests cover
only the Core authorization exception and the assembled Gateway transport.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

from fastapi import HTTPException, Request
import httpx
import pytest

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


ROOT = Path(__file__).resolve().parents[2]
CALLBACK = "/api/v1/connections/apollo/callback"
UNSUBSCRIBE = "/public/unsubscribe"
ATTACKER_HEADERS = {
    "Cookie": "cos_session=attacker-cookie",
    "Authorization": "Bearer attacker-authorization",
    "X-COS-Assertion": "attacker-assertion",
    "X-Subject": "usr_attacker",
    "X-Workspace-ID": "wsp_attacker",
    "X-Session-Id": "attacker-session",
    "X-Request-Id": "attacker-request",
    "X-Device-Id": "attacker-device",
}


@pytest.fixture
def core_http(monkeypatch):
    # The exception table needs no DB, vault, or assembled S10 handler. Fail if
    # this unit boundary unexpectedly tries to invoke resource authorization.
    def unexpected_access(*_args, **_kwargs):
        raise AssertionError("Unexpected resource access in independent_auth")

    workspace = ModuleType("app.authz.workspace")
    workspace.assert_venture_access = unexpected_access
    workspace.assert_workspace_access = unexpected_access
    tenancy = ModuleType("app.tenancy")
    tenancy.Scope = object
    monkeypatch.setitem(sys.modules, workspace.__name__, workspace)
    monkeypatch.setitem(sys.modules, tenancy.__name__, tenancy)
    spec = importlib.util.spec_from_file_location(
        "_core_public_route_boundary", ROOT / "services/company_os/app/authz/http.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(path, method):
    return Request({
        "type": "http", "method": method, "path": path,
        "scheme": "https", "server": ("core.test", 443),
        "headers": [], "query_string": b"",
    })


@pytest.mark.parametrize("path,method,module", [
    (UNSUBSCRIBE, "GET", "app.api.ops_api"),
    (UNSUBSCRIBE, "POST", "app.api.ops_api"),
    (CALLBACK, "GET", "app.api.connections_api"),
])
def test_only_the_exact_public_handlers_are_independent(core_http, path, method, module):
    assert core_http.independent_auth(_request(path, method), module)


@pytest.mark.parametrize("path,method,module", [
    (UNSUBSCRIBE, "HEAD", "app.api.ops_api"),
    (UNSUBSCRIBE, "OPTIONS", "app.api.ops_api"),
    (UNSUBSCRIBE, "PUT", "app.api.ops_api"),
    (UNSUBSCRIBE, "DELETE", "app.api.ops_api"),
    (UNSUBSCRIBE, "GET", "app.api.connections_api"),
    (UNSUBSCRIBE, "POST", "app.api.email_api"),
    (UNSUBSCRIBE, "GET", "app.api.ops_api.attacker"),
    (UNSUBSCRIBE + "/", "GET", "app.api.ops_api"),
    (UNSUBSCRIBE + "/extra", "POST", "app.api.ops_api"),
    ("/public/other", "GET", "app.api.ops_api"),
    ("/api/v1/public/unsubscribe", "GET", "app.api.ops_api"),
    (CALLBACK, "POST", "app.api.connections_api"),
    (CALLBACK, "HEAD", "app.api.connections_api"),
    (CALLBACK, "OPTIONS", "app.api.connections_api"),
    (CALLBACK, "DELETE", "app.api.connections_api"),
    (CALLBACK, "GET", "app.api.ops_api"),
    (CALLBACK, "GET", "app.api.connections_api.attacker"),
    (CALLBACK + "/", "GET", "app.api.connections_api"),
    (CALLBACK + "/extra", "GET", "app.api.connections_api"),
    ("/api/v1/connections/apollo/start", "GET", "app.api.connections_api"),
    ("/api/v1/connections/apollo/status", "GET", "app.api.connections_api"),
    ("/api/v1/connections/apollo/connect", "POST", "app.api.connections_api"),
    ("/api/v1/connections/other/callback", "GET", "app.api.connections_api"),
    ("/connections/apollo/callback", "GET", "app.api.connections_api"),
])
def test_public_exception_does_not_cover_siblings_or_other_handlers(core_http, path, method, module):
    assert not core_http.independent_auth(_request(path, method), module)


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    # Prevent settings from reading a checkout .env or an existing data vault.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", "synthetic-boundary-assertion-key-over-32-bytes")

    def no_network(*_args, **_kwargs):
        raise AssertionError("Unexpected live HTTP call")

    async def no_async_network(*_args, **_kwargs):
        raise AssertionError("Unexpected live HTTP call")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_network)
    main, client, saved = _gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(main.settings, "company_os_url", "http://core.internal:3300")
    monkeypatch.setattr(main.settings, "console_origin", "https://app.supymem.com")
    monkeypatch.setattr(main.settings, "app_env", "production")
    try:
        yield main, client
    finally:
        client.close()
        _close_gateway(saved)


def _upstream(monkeypatch, response, *, callback=False):
    calls = []

    async def receive(request):
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    real_client = httpx.AsyncClient

    def client(**kwargs):
        if callback:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls


@pytest.mark.parametrize("method,body,media", [
    ("GET", b"discarded-get-body", "application/json"),
    ("POST", b"List-Unsubscribe=One-Click", "application/x-www-form-urlencoded"),
    ("POST", b'{"token":"synthetic-signed-token"}', "application/json"),
])
def test_unsubscribe_is_public_and_preserves_its_exact_transport(gateway, monkeypatch, method, body, media):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200, text="Confirmed", headers={
        "Content-Type": "text/html", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    }))
    query = "t=synthetic-signed-token&t=duplicate&workspace_id=wsp_attacker"
    response = client.request(method, f"{UNSUBSCRIBE}?{query}", content=body, headers={
        **ATTACKER_HEADERS, "Content-Type": media,
    }, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == "Confirmed"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == method
    assert str(request.url) == f"http://core.internal:3300{UNSUBSCRIBE}?{query}"
    assert request.content == (body if method == "POST" else b"")
    assert request.headers["content-type"] == media
    for name, value in ATTACKER_HEADERS.items():
        assert value not in request.headers.values()
        if name.lower() not in {"x-subject", "x-session-id", "x-request-id"}:
            assert name not in request.headers
    assert not request.headers.get("X-Subject")
    assert request.headers["X-Session-Id"] == "anonymous"


def test_unsubscribe_uses_the_public_ip_rate_limit(gateway, monkeypatch):
    main, client = gateway
    checks = []
    monkeypatch.setattr(main.limiter, "check_or_raise", lambda key, **limits: checks.append((key, limits)))
    _upstream(monkeypatch, httpx.Response(200))
    assert client.get(UNSUBSCRIBE).status_code == 200
    assert client.post(UNSUBSCRIBE, content=b"List-Unsubscribe=One-Click").status_code == 200
    assert len(checks) == 2
    assert checks[0][0] == checks[1][0]
    assert checks[0][1] == checks[1][1] == {"rate": 0.5, "capacity": 20}


@pytest.mark.parametrize("header_case", ["title", "lower", "mixed"])
def test_unsubscribe_rate_limit_errors_are_fixed_and_private(gateway, monkeypatch, header_case):
    main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))

    def exhausted(*_args, **_kwargs):
        error_headers = {
            "Retry-After": "7", "Cache-Control": "public, max-age=3600",
            "Referrer-Policy": "unsafe-url", "X-Private": "private-limit-header",
        }
        if header_case == "lower":
            error_headers = {key.lower(): value for key, value in error_headers.items()}
        elif header_case == "mixed":
            error_headers = {key.swapcase(): value for key, value in error_headers.items()}
        raise HTTPException(429, detail="private-limit-detail", headers=error_headers)

    monkeypatch.setattr(main.limiter, "check_or_raise", exhausted)
    response = client.post(
        UNSUBSCRIBE, params={"t": "private-query-token"},
        content=b"List-Unsubscribe=One-Click", headers=ATTACKER_HEADERS,
    )
    assert response.status_code == 429
    assert response.json() == {"code": "unsubscribe_rate_limited"}
    assert response.headers["retry-after"] == "7"
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert response.headers.get_list("referrer-policy") == ["no-referrer"]
    assert not calls
    for value in ["private-limit-detail", "private-limit-header", "private-query-token", *ATTACKER_HEADERS.values()]:
        assert value not in response.text + str(response.headers)


@pytest.mark.parametrize("size", [8192, 8193])
def test_unsubscribe_post_body_limit_is_enforced_before_upstream(gateway, monkeypatch, size):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.post(UNSUBSCRIBE, content=b"x" * size)
    assert response.status_code == (200 if size == 8192 else 413)
    assert len(calls) == (1 if size == 8192 else 0)


def test_chunked_unsubscribe_cannot_bypass_the_body_limit(gateway, monkeypatch):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.post(UNSUBSCRIBE, content=iter([b"x" * 4096, b"x" * 4097]))
    assert response.status_code == 413
    assert not calls


@pytest.mark.parametrize("headers,expected", [
    ({"Content-Length": "not-a-length"}, 413),
    ({"Content-Encoding": "gzip"}, 415),
])
def test_unsubscribe_rejects_ambiguous_body_framing(gateway, monkeypatch, headers, expected):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.post(UNSUBSCRIBE, content=b"synthetic", headers=headers)
    assert response.status_code == expected
    assert not calls


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unsubscribe_oversized_query_is_rejected_before_upstream(gateway, monkeypatch, method):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.request(method, UNSUBSCRIBE, params={"token": "x" * 16385}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_unsubscribe"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert not calls


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_unsubscribe_never_relays_upstream_redirects(gateway, monkeypatch, status):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(status, headers={
        "Location": "https://evil.example/?token=synthetic-signed-token",
    }))
    response = client.get(UNSUBSCRIBE, follow_redirects=False)
    assert response.status_code == 502
    assert response.json() == {"code": "unsubscribe_unavailable"}
    assert "location" not in response.headers
    assert "synthetic-signed-token" not in response.text + str(response.headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(calls) == 1


def test_unsubscribe_privacy_headers_override_upstream_caching(gateway, monkeypatch):
    _main, client = gateway
    _upstream(monkeypatch, httpx.Response(200, headers={
        "Cache-Control": "public, max-age=3600", "Referrer-Policy": "unsafe-url",
    }))
    response = client.get(UNSUBSCRIBE, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("location", [
    "https://app.supymem.com/connections?provider=apollo&oauth=connected",
    "https://app.supymem.com/connections?workspace=wsp_abc123&venture=ven_abc123&provider=apollo&oauth=failed",
])
def test_apollo_callback_is_public_but_forwards_no_browser_identity(gateway, monkeypatch, location):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(302, headers={"Location": location}), callback=True)
    pairs = [("state", "one-use-state"), ("state", "duplicate-state"), ("code", "private-code")]
    response = client.get(CALLBACK, params=pairs, headers=ATTACKER_HEADERS, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == location
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"
    assert str(request.url.copy_with(query=None)) == f"http://core.internal:3300{CALLBACK}"
    assert request.url.params.multi_items() == pairs
    assert request.headers["accept"] == "application/json"
    for name in ATTACKER_HEADERS:
        assert name not in request.headers


@pytest.mark.parametrize("location", [
    "https://evil.example/connections?provider=apollo&oauth=connected",
    "https://app.supymem.com.evil.example/connections?provider=apollo&oauth=connected",
    "http://app.supymem.com/connections?provider=apollo&oauth=connected",
    "https://attacker@app.supymem.com/connections?provider=apollo&oauth=connected",
    "https://app.supymem.com/auth/consume?provider=apollo&oauth=connected",
    "https://app.supymem.com/connections?provider=x&oauth=connected",
    "https://app.supymem.com/connections?provider=apollo&provider=apollo&oauth=connected",
    "https://app.supymem.com/connections?provider=apollo&oauth=maybe",
    "https://app.supymem.com/connections?provider=apollo&oauth=connected&code=private-code",
    "https://app.supymem.com/connections?provider=apollo&oauth=connected#private-code",
    "https://app.supymem.com/connections?workspace=../foreign&provider=apollo&oauth=connected",
    "https://app.supymem.com/connections?workspace=wsp_abc123&workspace=wsp_abc123&provider=apollo&oauth=connected",
])
def test_apollo_callback_rejects_untrusted_returns(gateway, monkeypatch, location):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(303, headers={"Location": location}))
    response = client.get(CALLBACK, follow_redirects=False)
    assert response.status_code == 502
    assert response.json() == {"error": {"code": "invalid_oauth_return"}}
    assert "location" not in response.headers
    assert len(calls) == 1


@pytest.mark.parametrize("upstream,expected,code", [
    (httpx.Response(400, json={"error": "private-code"}), 400, "oauth_callback_rejected"),
    (httpx.Response(500, text="private-code"), 502, "oauth_callback_unavailable"),
    (httpx.ConnectError("https://core/?code=private-code"), 502, "oauth_callback_unavailable"),
])
def test_apollo_upstream_failures_are_fixed_and_private(gateway, monkeypatch, upstream, expected, code):
    _main, client = gateway
    _upstream(monkeypatch, upstream)
    response = client.get(CALLBACK, params={"code": "private-code"}, follow_redirects=False)
    assert response.status_code == expected
    assert response.json() == {"error": {"code": code}}
    assert "private-code" not in response.text + str(response.headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_apollo_oversized_query_is_rejected_before_upstream(gateway, monkeypatch):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.get(CALLBACK, params={"state": "x" * 16385}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_oauth_callback"}}
    assert not calls


@pytest.mark.parametrize("method,path,expected", [
    ("POST", CALLBACK, 401),
    ("HEAD", CALLBACK, 401),
    ("GET", CALLBACK + "/extra", 401),
    ("GET", "/api/v1/connections/apollo/start", 401),
    ("GET", "/api/v1/connections/apollo/status", 401),
    ("GET", "/api/v1/connections/other/callback", 401),
    ("GET", "/api/v1/public/unsubscribe", 401),
    ("GET", UNSUBSCRIBE + "/extra", 404),
    ("POST", "/public/other", 404),
    ("PUT", UNSUBSCRIBE, 405),
    ("HEAD", UNSUBSCRIBE, 405),
])
def test_gateway_does_not_open_neighboring_routes(gateway, monkeypatch, method, path, expected):
    _main, client = gateway
    calls = _upstream(monkeypatch, httpx.Response(200))
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == expected
    assert not calls
