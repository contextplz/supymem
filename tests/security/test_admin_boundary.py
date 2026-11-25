"""Threat #4 / #5 / #6: admin paths are not proxyable; assertions bind and expire."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.assertion import (
    MIN_SECRET_BYTES,
    PathMismatch,
    Replayed,
    sign_assertion,
    verify_assertion,
)

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "services" / "gateway"
SECRET = "s" * MIN_SECRET_BYTES
ORIGIN = "http://127.0.0.1:3100"


def _wipe_app_modules() -> None:
    for k in list(sys.modules):
        if k == "app" or k.startswith("app."):
            del sys.modules[k]


def _gateway_path() -> None:
    sys.path[:] = [
        p
        for p in sys.path
        if "services/company_os" not in p and "services/warden" not in p
    ]
    if str(GATEWAY) in sys.path:
        sys.path.remove(str(GATEWAY))
    sys.path.insert(0, str(GATEWAY))


@pytest.fixture()
def gw(tmp_path, monkeypatch, isolate_app_imports):
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("CONSOLE_ORIGIN", ORIGIN)
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.com")
    _wipe_app_modules()
    _gateway_path()

    from app.config import settings

    settings.console_passphrase = "test-pass"
    settings.session_secret = "test-session-secret-at-least-32-bytes"
    settings.console_origin = ORIGIN
    settings.company_os_url = "http://127.0.0.1:1"
    settings.cos_internal_assertion_secret = SECRET

    import app.auth as auth_mod
    from argon2 import PasswordHasher

    auth_mod._PASS_HASH = PasswordHasher().hash("test-pass")
    auth_mod.settings.console_passphrase = "test-pass"

    from app.ratelimit import limiter

    limiter.reset()

    from app.main import create_app
    from app.vault import Vault
    import app.vault as vault_mod

    vault_mod.vault = Vault(
        path=tmp_path / "vault.enc", secret="test-session-secret-at-least-32-bytes"
    )
    app = create_app()
    with TestClient(app) as c:
        yield c


def _login_user(client: TestClient) -> str:
    r = client.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200
    return r.json()["csrf"]


def test_non_admin_cannot_reach_admin_via_api_proxy(gw: TestClient) -> None:
    csrf = _login_user(gw)
    with patch("app.main.forward", new_callable=AsyncMock) as fwd:
        r = gw.post(
            "/api/admin/credits/grant",
            json={"email": "x@y.com", "credits": 5000, "reason": "nope"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert r.status_code == 404
        fwd.assert_not_called()


def test_non_admin_cannot_reach_internal_via_api_proxy(gw: TestClient) -> None:
    csrf = _login_user(gw)
    with patch("app.main.forward", new_callable=AsyncMock) as fwd:
        r = gw.post(
            "/api/internal/ops/run-cycle",
            json={"cycle": "morning"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        assert r.status_code == 404
        fwd.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api//admin/x",
        "/api/ADMIN/x",
        "/api/admin/../admin/x",
        "/api/admin%2f%2e%2e%2fadmin/x",
        "/api/%61dmin/x",
        "/api/internal/ops/run-cycle",
        "/api/_/secret",
        "/api/admin%2fx",
    ],
)
def test_blocked_prefix_bypass_shapes(gw: TestClient, path: str) -> None:
    csrf = _login_user(gw)
    with patch("app.main.forward", new_callable=AsyncMock) as fwd:
        r = gw.get(path, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert r.status_code == 404, path
        fwd.assert_not_called()


def test_stolen_normal_session_cookie_is_not_admin(gw: TestClient) -> None:
    _login_user(gw)
    r = gw.get("/admin/session", headers={"Origin": ORIGIN})
    assert r.status_code == 401
    r2 = gw.get("/admin/credits/grant", headers={"Origin": ORIGIN})
    assert r2.status_code == 401


def test_admin_login_error_does_not_leak_email_existence(gw: TestClient) -> None:
    unknown = gw.post(
        "/admin/session",
        json={"email": "nobody@example.com", "password": "aaaaaaaa", "totp": "123456"},
        headers={"Origin": ORIGIN},
    )
    from app import users as users_mod

    users_mod.create_user("known@example.com", "password1234")
    known = gw.post(
        "/admin/session",
        json={"email": "known@example.com", "password": "wrong-pass", "totp": "123456"},
        headers={"Origin": ORIGIN},
    )
    assert unknown.status_code == 401
    assert known.status_code == 401
    assert unknown.json() == known.json()
    assert unknown.json()["error"]["code"] == "invalid_credentials"


def test_admin_login_requires_totp(gw: TestClient) -> None:
    from app import users as users_mod

    users_mod.create_user("ops@example.com", "password1234")
    missing = gw.post(
        "/admin/session",
        json={"email": "ops@example.com", "password": "password1"},
        headers={"Origin": ORIGIN},
    )
    # totp defaults to "" → generic failure, never 200
    assert missing.status_code == 401
    empty = gw.post(
        "/admin/session",
        json={"email": "ops@example.com", "password": "password1", "totp": ""},
        headers={"Origin": ORIGIN},
    )
    assert empty.status_code == 401
    assert empty.json()["error"]["code"] == "invalid_credentials"


def test_admin_login_rate_limited_and_locks(gw: TestClient) -> None:
    body = {"email": "ops@example.com", "password": "wrong", "totp": "000000"}
    last = None
    for _ in range(5):
        last = gw.post("/admin/session", json=body, headers={"Origin": ORIGIN})
        assert last.status_code == 401
    locked = gw.post("/admin/session", json=body, headers={"Origin": ORIGIN})
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "login_locked"


def test_admin_session_refresh_restores_csrf_then_signout_revokes(gw: TestClient) -> None:
    """A console reload must not strand its cross-subdomain CSRF token."""
    from datetime import datetime, timedelta, timezone

    import httpx

    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    gw.cookies.set("cos_admin", "admin-token")
    gw.cookies.set("cos_admin_csrf", "csrf-after-refresh")

    async def company_os(method, path, **_kwargs):
        if method == "GET" and path.startswith("/internal/admin/sessions/"):
            return httpx.Response(200, json={"admin_id": "adm_1", "email": "ops@example.com",
                                             "role": "super_admin", "expires_at": expires})
        if method == "DELETE" and path.startswith("/internal/admin/sessions/"):
            return httpx.Response(200, json={"ok": True})
        raise AssertionError((method, path))

    with patch("app.admin_auth._cos_request", side_effect=company_os) as core:
        refreshed = gw.get("/admin/session", headers={"Origin": ORIGIN})
        assert refreshed.status_code == 200
        assert refreshed.json()["csrf"] == "csrf-after-refresh"
        assert "no-store" in refreshed.headers["cache-control"]
        signed_out = gw.delete("/admin/session", headers={"Origin": ORIGIN,
                                                          "X-CSRF-Token": refreshed.json()["csrf"]})
        assert signed_out.status_code == 200
        # TestClient's manually seeded cookie has no domain and its jar does
        # not apply a host-scoped deletion; verify the server's clear header.
        assert "cos_admin=\"\"" in signed_out.headers["set-cookie"]
        assert core.call_count == 2


def test_admin_signout_does_not_claim_success_when_revocation_fails(gw: TestClient) -> None:
    import httpx

    gw.cookies.set("cos_admin", "admin-token")
    gw.cookies.set("cos_admin_csrf", "csrf")
    with patch("app.admin_auth._cos_request", return_value=httpx.Response(503)):
        response = gw.delete("/admin/session", headers={"Origin": ORIGIN, "X-CSRF-Token": "csrf"})
    assert response.status_code == 503
    assert "cos_admin=" not in response.headers.get("set-cookie", "")


def test_replayed_assertion_is_rejected() -> None:
    from core.assertion import LruNonceStore

    token = sign_assertion(
        SECRET,
        sub="adm_1",
        email="ops@example.com",
        role="super_admin",
        path="/admin/session",
        method="GET",
    )
    store = LruNonceStore()
    verify_assertion(
        SECRET, token, path="/admin/session", method="GET", nonce_store=store
    )
    with pytest.raises(Replayed):
        verify_assertion(
            SECRET, token, path="/admin/session", method="GET", nonce_store=store
        )


def test_assertion_bound_to_path_and_method() -> None:
    token = sign_assertion(
        SECRET,
        sub="adm_1",
        email="ops@example.com",
        role="super_admin",
        path="/admin/session",
        method="GET",
    )
    with pytest.raises(PathMismatch):
        verify_assertion(
            SECRET, token, path="/admin/credits/grant", method="POST"
        )


def test_path_normalisation_blocks_bypass_shapes() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gw_proxy_guard", GATEWAY / "app" / "proxy_guard.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    blocked = [
        "admin/x",
        "/admin/x",
        "ADMIN/x",
        "//admin/x",
        "admin/../admin/x",
        "admin%2f%2e%2e%2fadmin/x",
        "%61dmin/x",
        "internal/ops/run-cycle",
        "_/secret",
        "api/admin/credits/grant",
    ]
    for raw in blocked:
        assert mod.is_blocked_api_path(raw), raw
    assert not mod.is_blocked_api_path("ventures")
    # The Warden policy plane is admin/system only; founders never reach it via the catch-all.
    for raw in ("warden/kill-switch", "api/warden/policy/autonomy", "WARDEN/quota/consume", "%77arden/audit"):
        assert mod.is_blocked_api_path(raw), raw


def test_underscore_prefixed_routes_are_blocked() -> None:
    """A leading underscore marks an internal route, not just the bare "_" segment."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "proxy_guard_underscore", GATEWAY / "app" / "proxy_guard.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for raw in ("_hidden", "_debug/vars", "api/_hidden", "%5fhidden"):
        assert mod.is_blocked_api_path(raw), raw


def test_admin_route_requires_assertion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "test-vault-secret-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.com")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    main = load_company_os_app()
    from app.admin.identity import create_admin

    create_admin("ops@example.com", "super_admin", created_by="test")
    with TestClient(main.create_app()) as c:
        missing = c.get("/admin/session")
        assert missing.status_code == 401
        token = sign_assertion(
            SECRET,
            sub="usr_ops",
            email="ops@example.com",
            role="super_admin",
            path="/admin/session",
            method="GET",
        )
        ok = c.get("/admin/session", headers={"X-COS-Assertion": token})
        assert ok.status_code == 200
        body = ok.json()
        assert body["email"] == "ops@example.com"
        assert body["role"] == "super_admin"
        replay = c.get("/admin/session", headers={"X-COS-Assertion": token})
        assert replay.status_code == 401
        grant_token = sign_assertion(
            SECRET,
            sub="usr_ops",
            email="ops@example.com",
            role="super_admin",
            path="/admin/credits/grant",
            method="POST",
        )
        mismatched = c.get("/admin/session", headers={"X-COS-Assertion": grant_token})
        assert mismatched.status_code == 401


def test_boot_refuses_when_boundary_required_but_secret_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.delenv("COS_INTERNAL_ASSERTION_SECRET", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    # app.main.create_app() runs at import; a missing secret must refuse to boot.
    with pytest.raises(RuntimeError, match="COS_INTERNAL_ASSERTION_SECRET"):
        load_company_os_app().create_app()


def test_totp_default_follows_deployment_posture(monkeypatch) -> None:
    """Forgetting ADMIN_REQUIRE_TOTP must not silently disable MFA when hosted."""
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.admin.authn import admin_require_totp

    monkeypatch.delenv("ADMIN_REQUIRE_TOTP", raising=False)
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    assert admin_require_totp() is True
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "0")
    assert admin_require_totp() is False
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    assert admin_require_totp() is True


def test_boot_refuses_hosted_deployment_with_totp_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "test-vault-secret-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.com")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "0")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    with pytest.raises(RuntimeError, match="ADMIN_REQUIRE_TOTP"):
        load_company_os_app().create_app()


def test_boot_refuses_hosted_deployment_with_default_vault_key(tmp_path, monkeypatch) -> None:
    """Default vault key means a DB reader can decrypt TOTP secrets and forge codes."""
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "ops@example.com")
    for name in ("COMPANY_OS_VAULT_SECRET", "SESSION_SECRET", "COMPANY_OS_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/cos.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from tests.company_os_harness import load_company_os_app

    with pytest.raises(RuntimeError, match="COMPANY_OS_VAULT_SECRET"):
        load_company_os_app().create_app()


def test_revoked_admin_session_is_rejected(gw: TestClient, monkeypatch) -> None:
    from app import users as users_mod
    from app import admin_auth

    users_mod.create_user("ops@example.com", "password1234")
    store: dict = {}

    class _Resp:
        def __init__(self, status_code: int, data: dict):
            self.status_code = status_code
            self._data = data

        def json(self) -> dict:
            return self._data

    async def fake_cos(method: str, path: str, json_body=None):
        if method == "POST" and path.endswith("/login"):
            store["hash"] = json_body["token_hash"]
            return _Resp(
                200,
                {
                    "admin_id": "adm_1",
                    "email": "ops@example.com",
                    "role": "super_admin",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            )
        if "/sessions/" in path:
            token_hash = path.rsplit("/", 1)[-1]
            if store.get("revoked") or token_hash != store.get("hash"):
                return _Resp(401, {})
            if method == "DELETE":
                store["revoked"] = True
                return _Resp(200, {"ok": True})
            return _Resp(
                200,
                {
                    "admin_id": "adm_1",
                    "email": "ops@example.com",
                    "role": "super_admin",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            )
        return _Resp(404, {})

    monkeypatch.setattr(admin_auth, "_cos_request", fake_cos)
    login = gw.post(
        "/admin/session",
        json={"email": "ops@example.com", "password": "password1234", "totp": "123456"},
        headers={"Origin": ORIGIN},
    )
    assert login.status_code == 200
    csrf = login.json()["csrf"]
    who = gw.get("/admin/session", headers={"Origin": ORIGIN})
    assert who.status_code == 200
    assert who.json()["email"] == "ops@example.com"
    gone = gw.delete(
        "/admin/session",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert gone.status_code == 200
    again = gw.get("/admin/session", headers={"Origin": ORIGIN})
    assert again.status_code == 401


@pytest.mark.parametrize("app_env", ["production", "staging"])
def test_hosted_founders_cannot_use_shared_passphrase(gw, monkeypatch, app_env):
    from app.config import settings
    import app.main as main

    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(settings, "staging_operator_passphrase_login_enabled", False)
    with patch.object(main, "issue_session", side_effect=AssertionError("shared session must not be issued")):
        response = gw.post("/auth/login", json={"passphrase": "test-pass"}, headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert response.json() == {"error": {"code": "local_passphrase_unavailable"}}
    assert not response.cookies
