"""Gateway auth: session for /api/*; /openai/v1 is Bearer (Hermes), not a cookie."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "services" / "gateway"


@pytest.fixture()
def client(tmp_path, monkeypatch, isolate_app_imports):
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1:3100")
    # Prefer gateway app package
    for k in list(sys.modules):
        if k == "app" or k.startswith("app."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "services/company_os" not in p and "services/warden" not in p]
    if str(GATEWAY) in sys.path:
        sys.path.remove(str(GATEWAY))
    sys.path.insert(0, str(GATEWAY))

    from app.config import settings

    settings.console_passphrase = "test-pass"
    settings.session_secret = "test-session-secret-at-least-32-bytes"
    settings.console_origin = "http://127.0.0.1:3100"
    # This suite exercises the gateway's own auth surface and its offline fallback store.
    # Point the core at an unroutable address so a dev stack on :3300 cannot influence it.
    settings.company_os_url = "http://127.0.0.1:1"

    # Re-hash passphrase
    import app.auth as auth_mod
    from argon2 import PasswordHasher

    auth_mod._PASS_HASH = PasswordHasher().hash("test-pass")
    auth_mod.settings.console_passphrase = "test-pass"

    from app.main import create_app
    import app.main as main_mod
    from app.vault import Vault
    import app.vault as vault_mod

    vault_mod.vault = Vault(path=tmp_path / "vault.enc", secret="test-session-secret-at-least-32-bytes")

    # Venture persistence and authorization belong to Core.  This gateway-auth
    # suite supplies an explicit Core boundary rather than reviving the former
    # process-local _VENTURES fallback.
    from fastapi.responses import JSONResponse

    async def core_ventures(path, request, _session):
        if request.method == "POST":
            return JSONResponse({"id": "ven_test"}, status_code=200)
        return JSONResponse({"items": []}, status_code=200)

    monkeypatch.setattr(main_mod, "forward", core_ventures)

    app = create_app()
    with TestClient(app) as c:
        yield c


def test_health_unauthenticated(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200


def test_auth_session_unauthenticated(client: TestClient) -> None:
    r = client.get("/auth/session")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False


def test_api_requires_session(client: TestClient) -> None:
    r = client.get("/api/ventures")
    assert r.status_code == 401


def test_entry_requires_session_not_500(client: TestClient) -> None:
    r = client.post(
        "/api/entry",
        json={
            "text": "a marketplace for restoring vintage synthesizers",
            "stage": "scratch",
            "explicit_mode": "idea",
            "mode": "guided",
            "start": True,
            "confirm": True,
        },
    )
    assert r.status_code == 401
    assert r.json().get("detail", {}).get("code") == "unauthenticated"


def test_openai_is_bearer_not_session(client: TestClient) -> None:
    missing = client.post(
        "/openai/v1/chat/completions",
        json={"model": "supymem-flash", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_api_key"

    forwarded = client.post(
        "/openai/v1/chat/completions",
        json={"model": "supymem-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer sk_app_test.secret"},
    )
    # Core is pointed at an unroutable address in this suite — key was accepted.
    assert forwarded.status_code == 503
    assert forwarded.json()["code"] == "core_restarting"


def test_events_requires_session(client: TestClient) -> None:
    r = client.get("/events")
    assert r.status_code == 401


def test_login_and_access(client: TestClient) -> None:
    r = client.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": "http://127.0.0.1:3100"},
    )
    assert r.status_code == 200
    csrf = r.json()["csrf"]
    r2 = client.get("/api/ventures", headers={"Origin": "http://127.0.0.1:3100"})
    assert r2.status_code == 200
    r3 = client.post(
        "/api/ventures",
        json={"name": "X", "raw_idea": "idea"},
        headers={"Origin": "http://127.0.0.1:3100", "X-CSRF-Token": csrf},
    )
    assert r3.status_code == 200
    assert r3.json()["id"].startswith("ven_")


def test_session_returns_csrf_after_login(client: TestClient) -> None:
    login = client.post(
        "/auth/login",
        json={"passphrase": "test-pass"},
        headers={"Origin": "http://127.0.0.1:3100"},
    )
    assert login.status_code == 200
    sess = client.get("/auth/session", headers={"Origin": "http://127.0.0.1:3100"})
    assert sess.status_code == 200
    csrf = sess.json().get("csrf")
    assert csrf
    created = client.post(
        "/api/ventures",
        json={"name": "Y", "raw_idea": "another idea"},
        headers={"Origin": "http://127.0.0.1:3100", "X-CSRF-Token": csrf},
    )
    assert created.status_code == 200
