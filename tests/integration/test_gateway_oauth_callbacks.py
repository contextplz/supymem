"""Provider returns work without a session without creating an open proxy."""

import importlib

import httpx
import pytest

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    main, client, saved = _gateway(tmp_path, monkeypatch)
    callbacks = importlib.import_module("app.oauth_callbacks")
    monkeypatch.setattr(main.settings, "company_os_url", "http://core.internal:3300")
    monkeypatch.setattr(main.settings, "console_origin", "https://app.supymem.com")
    monkeypatch.setattr(main.settings, "app_env", "production")
    try:
        yield client, callbacks
    finally:
        _close_gateway(saved)


def _upstream(monkeypatch, callbacks, response):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(callbacks.httpx, "AsyncClient", Client)
    return calls


@pytest.mark.parametrize("provider", ["linkedin", "reddit", "x", "gmail"])
def test_exact_public_callback_forwards_only_to_core(gateway, monkeypatch, provider):
    client, callbacks = gateway
    location = f"https://app.supymem.com/connections?provider={provider}&oauth=connected"
    calls = _upstream(monkeypatch, callbacks, httpx.Response(303, headers={"Location": location}))
    core_path = (
        "/api/v1/ai-gtm/gmail/callback" if provider == "gmail"
        else f"/api/v1/content/oauth/{provider}/callback"
    )
    response = client.get(
        core_path, params={"code": "private-code", "state": "one-use-state"},
        headers={"X-Workspace-ID": "foreign", "Authorization": "Bearer attacker"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == location
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert calls == [(f"http://core.internal:3300{core_path}", {
        "params": [("code", "private-code"), ("state", "one-use-state")],
        "headers": {"Accept": "application/json"},
    })]


@pytest.mark.parametrize("location", [
    "https://evil.example/connections?provider=x&oauth=connected",
    "https://app.supymem.com.evil.example/connections?provider=x&oauth=connected",
    "http://app.supymem.com/connections?provider=x&oauth=connected",
    "https://app.supymem.com/connections?provider=x&oauth=connected&code=secret",
    "https://app.supymem.com/connections?provider=x&provider=x&oauth=connected",
    "https://app.supymem.com/connections?provider=gmail&oauth=connected",
    "https://app.supymem.com/connections?provider=x&oauth=connected#secret",
    "https://app.supymem.com/auth/consume?provider=x&oauth=connected",
    "https://attacker@app.supymem.com/connections?provider=x&oauth=connected",
    "https://app.supymem.com/connections?workspace=wsp_abc123&workspace=wsp_abc123&provider=x&oauth=connected",
    "https://app.supymem.com/connections?workspace=../evil&provider=x&oauth=connected",
    "https://app.supymem.com/connections?account=wsp_abc123&provider=x&oauth=connected",
    "https://app.supymem.com/connections?workspace=wsp_abc123&provider=x&oauth=maybe",
])
def test_callback_rejects_unexpected_redirect(gateway, monkeypatch, location):
    client, callbacks = gateway
    _upstream(monkeypatch, callbacks, httpx.Response(303, headers={"Location": location}))
    response = client.get("/api/v1/content/oauth/x/callback", follow_redirects=False)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_oauth_return"
    assert "location" not in response.headers


def test_no_broad_authentication_exception(gateway, monkeypatch):
    client, callbacks = gateway
    calls = _upstream(monkeypatch, callbacks, httpx.Response(200))
    assert client.get("/api/v1/content/oauth/unknown/callback").status_code == 404
    for path in ("/api/v1/content/oauth/x/status", "/api/v1/ai-gtm/gmail/connect"):
        assert client.get(path).status_code == 401
    assert client.post("/api/v1/content/oauth/x/callback").status_code == 401
    assert not calls


@pytest.mark.parametrize("upstream,expected", [
    (httpx.Response(400, json={"error": "private-code"}), 400),
    (httpx.Response(500, text="private-code"), 502),
    (httpx.ConnectError("https://core/?code=private-code"), 502),
])
def test_provider_error_body_is_not_relayed(gateway, monkeypatch, upstream, expected):
    client, callbacks = gateway
    _upstream(monkeypatch, callbacks, upstream)
    response = client.get("/api/v1/content/oauth/x/callback", follow_redirects=False)
    assert response.status_code == expected
    assert "private-code" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("location", [
    "https://app.supymem.com/connections?workspace=wsp_testmuseums&venture=ven_testbooks&provider=gmail&oauth=connected",
    "https://app.supymem.com/connections?workspace=wsp_testmuseums&provider=gmail&oauth=failed",
])
def test_gmail_callback_accepts_scoped_return(gateway, monkeypatch, location):
    client, callbacks = gateway
    _upstream(monkeypatch, callbacks, httpx.Response(303, headers={"Location": location}))
    response = client.get("/api/v1/ai-gtm/gmail/callback", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == location
