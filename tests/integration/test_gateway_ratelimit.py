import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from tests.integration.test_gateway_b1_security import _close_gateway, _gateway


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    main, client, saved = _gateway(tmp_path, monkeypatch)
    try:
        yield main, client
    finally:
        client.close()
        _close_gateway(saved)


def assert_rate_limited(response, *, code="rate_limited"):
    assert response.status_code == 429, response.text
    assert int(response.headers["Retry-After"]) > 0
    assert (response.json().get("error") or response.json().get("detail"))["code"] == code


@pytest.mark.parametrize("path", ["/auth/login", "/auth/password-reset", "/auth/signup", "/auth/magic-link", "/auth/verification/resend"])
def test_auth_requests_per_account(gateway, path):
    _main, client = gateway
    for _attempt in range(5):
        response = client.post(path, json={"email": "test@example.com", "password": "long-password-1234"})
        assert response.status_code != 429
    assert_rate_limited(client.post(path, json={"email": "TEST@example.com", "password": "long-password-1234"}))


@pytest.mark.parametrize("path", ["/auth/login", "/auth/password-reset"])
def test_account_limit_survives_ip_rotation(gateway, monkeypatch, path):
    main, client = gateway
    monkeypatch.setattr(main.settings, "trusted_proxy_hops", 1)
    for index in range(5):
        assert client.post(path, json={"email": "test@example.com", "password": "synthetic-invalid-password"},
                           headers={"X-Forwarded-For": f"198.51.100.{index}"}).status_code != 429
    assert_rate_limited(client.post("/auth/magic-link", json={"email": " TEST@example.com "},
                                   headers={"X-Forwarded-For": "198.51.100.99"}))


def test_admin_login_uses_same_ip_and_account_budgets(gateway):
    _main, client = gateway
    for _attempt in range(5):
        assert client.post("/admin/session", json={"email": "admin@example.com", "password": "synthetic", "totp": "123456"}).status_code != 429
    assert_rate_limited(client.post("/admin/session", json={"email": "admin@example.com", "password": "synthetic", "totp": "123456"}), code="login_locked")


def test_ip_limit_survives_email_rotation_and_spoofed_xff(gateway, monkeypatch):
    main, client = gateway
    monkeypatch.setenv("RATE_LIMIT_AUTH_IP_60", "10")
    monkeypatch.setattr(main.settings, "trusted_proxy_hops", 1)
    for index in range(10):
        assert client.post("/auth/password-reset", json={"email": f"test{index}@example.com"},
                           headers={"X-Forwarded-For": f"198.51.100.{index}, 203.0.113.1"}).status_code != 429
    assert_rate_limited(client.post("/auth/password-reset", json={"email": "fresh@example.com"},
                                   headers={"X-Forwarded-For": "new-forged, 203.0.113.1"}))


def test_beta_code_enumeration_is_limited(gateway, monkeypatch):
    main, client = gateway

    async def upstream(*args, **kwargs):
        return JSONResponse({"code": "invalid_code"}, status_code=400)

    monkeypatch.setattr(main, "forward", upstream)
    for index in range(10):
        assert client.post("/public/beta/verify", json={"code": f"CODE{index}"}).status_code == 400
    assert_rate_limited(client.post("/public/beta/verify", json={"code": "NEWCODE"}))


def test_device_refresh_limit_does_not_rotate_or_revoke(gateway, monkeypatch):
    _main, client = gateway
    from app import device_auth

    monkeypatch.setenv("RATE_LIMIT_DEVICE_REFRESH_3600", "1")
    monkeypatch.setattr(device_auth, "device_person", lambda _id: "usr_test")
    monkeypatch.setattr(device_auth, "issue_access_jwt", lambda *args, **kwargs: "access")
    calls = []
    monkeypatch.setattr(device_auth, "rotate_refresh", lambda *args, **kwargs: calls.append(args) or "refresh")
    body = {"device_id": "dev_test", "refresh_token": "r" * 32}
    assert client.post("/auth/device/refresh", json=body).status_code == 200
    assert_rate_limited(client.post("/auth/device/refresh", json=body))
    assert len(calls) == 1


def test_subject_and_ai_caps_include_non_proxy_routes(gateway, monkeypatch):
    _main, client = gateway
    from app.auth import require_session
    from app.session import issue_session

    token, _session = issue_session("founder")
    monkeypatch.setenv("RATE_LIMIT_AI_60", "1")
    for index in range(2):
        request = Request({"type": "http", "method": "POST", "path": "/api/ventures/ven_test/chat",
                           "headers": [(b"cookie", f"cos_session={token}".encode())]})
        if index == 0:
            assert require_session(request).subject == "founder"
        else:
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as denied:
                require_session(request)
            assert denied.value.status_code == 429


def test_public_limit_and_signed_webhook_exemption(gateway, monkeypatch):
    main, client = gateway
    monkeypatch.setenv("RATE_LIMIT_PUBLIC_WRITE_60", "1")

    async def upstream(*args, **kwargs):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(main, "forward", upstream)
    for _attempt in range(3):  # telemetry replays have their own bucket
        assert client.post("/e", json={}).status_code == 200
    assert client.post("/w/wsp_test/interest", json={}).status_code == 200
    assert_rate_limited(client.post("/w/wsp_test/interest", json={}))
    for _attempt in range(3):
        assert client.post("/webhooks/email/resend", json={}, headers={"svix-signature": "signed"}).status_code == 200


def test_three_founder_journeys_from_one_ip_are_never_limited(gateway, monkeypatch):
    """Mirrors scripts/qa/run_founder_live_journeys.py plus a hosted account journey, three times from one IP."""
    main, client = gateway
    from app import users

    async def upstream(*args, **kwargs):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(main, "forward", upstream)
    monkeypatch.setattr(main.settings, "console_passphrase", "operator-qa-passphrase")
    origin = {"Origin": "http://127.0.0.1:3100"}
    statuses = []

    def call(method, path, **kwargs):
        response = client.request(method, path, headers=origin, **kwargs)
        statuses.append((method, path, response.status_code))
        return response

    def page_load():
        # Unauthenticated loopback consoles: every ensureSession() caller re-probes the default.
        call("GET", "/auth/session")
        for _caller in range(8):
            call("POST", "/auth/login", json={"passphrase": "changeme"})
        call("GET", "/auth/invite/status")

    for journey in range(3):
        client.cookies.clear()
        call("GET", "/ready")
        page_load()
        call("POST", "/auth/login", json={"passphrase": "operator-qa-passphrase"})
        call("GET", "/auth/session")
        for _event in range(20):
            call("POST", "/api/telemetry", json={"events": []})
        client.cookies.clear()
        email = f"founder{journey}@example.test"
        page_load()
        call("POST", "/auth/signup", json={"email": email, "password": "long-password-1234"})
        user = users.get_user_by_email(email)
        call("POST", "/auth/verify-email", json={"token": users.issue_token(user.id, "email_verify")})
        call("POST", "/auth/login", json={"email": email, "password": "long-password-1234"})
        call("GET", "/auth/session")
        for _fetch in range(10):
            call("GET", "/w/wsp_test/site/")
        call("POST", "/public/waitlists/ven_test", json={"email": f"lead{journey}@example.test"})
        for _event in range(20):
            call("POST", "/e", json={})
    assert not [entry for entry in statuses if entry[2] == 429], statuses
    assert ("POST", "/auth/login", 200) in statuses
