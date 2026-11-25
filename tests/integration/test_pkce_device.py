"""PKCE device approve → token → bearer on /api and /events."""

from __future__ import annotations

import base64
import hashlib
import importlib
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

GW = Path(__file__).resolve().parents[2] / "services" / "gateway"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_pkce_and_bearer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_MAIL_SINK", str(tmp_path / "mail.jsonl"))
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "changeme")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me-32b!!")
    monkeypatch.setenv("APP_ENV", "local")
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    sys.path.insert(0, str(GW))
    try:
        main = importlib.import_module("app.main")
        client = TestClient(main.create_app())
        r = client.post(
            "/auth/signup",
            json={"email": "dev@example.com", "password": "password1234"},
        )
        assert r.status_code == 200, r.text
        verifier = secrets.token_urlsafe(32)
        approve = client.post(
            "/auth/device/approve",
            json={"code_challenge": _challenge(verifier), "device_name": "Test Mac"},
            headers={"X-CSRF-Token": r.json()["csrf"]},
        )
        assert approve.status_code == 200, approve.text
        code = approve.json()["code"]
        client.post("/auth/logout")
        tok = client.post(
            "/auth/device/token",
            json={"code": code, "code_verifier": verifier},
        )
        assert tok.status_code == 200, tok.text
        access = tok.json()["access_token"]
        assert tok.json()["expires_in"] == 900
        session = client.get("/auth/session", headers={"Authorization": f"Bearer {access}"})
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["subject"] == tok.json()["subject"]
        async def accepted_forward(path, request, session, **kwargs):
            assert path == "ventures"
            assert session.subject == tok.json()["subject"]
            return JSONResponse({"items": []})
        monkeypatch.setattr(main, "forward", accepted_forward)
        assert client.get("/api/ventures", headers={"Authorization": f"Bearer {access}"}).status_code == 200
        # Authentication succeeds before the required venture-id validation.
        assert client.get("/events", headers={"Authorization": f"Bearer {access}"}, params={"once": "true"}).status_code == 422
        # A lost refresh reply can be recovered with the same successor.
        refresh = tok.json()["refresh_token"]
        device_id = tok.json()["device_id"]
        first = client.post(
            "/auth/device/refresh",
            json={"device_id": device_id, "refresh_token": refresh},
        )
        assert first.status_code == 200
        reused = client.post(
            "/auth/device/refresh",
            json={"device_id": device_id, "refresh_token": refresh},
        )
        assert reused.status_code == 200
        assert reused.json()["refresh_token"] == first.json()["refresh_token"]
        devices = importlib.import_module("app.device_auth")
        clock = devices._now
        later = devices._now() + timedelta(seconds=601)
        monkeypatch.setattr(devices, "_now", lambda: later)
        expired = client.post(
            "/auth/device/refresh",
            json={"device_id": device_id, "refresh_token": refresh},
        )
        assert expired.status_code == 401
        assert expired.json()["error"]["code"] == "invalid_refresh"
        assert devices.device_person(device_id) == tok.json()["subject"]
        # The unreceived successor is retired: whoever holds it trips reuse detection.
        monkeypatch.setattr(devices, "_now", clock)
        rejected = client.post("/auth/device/refresh", json={
            "device_id": device_id, "refresh_token": first.json()["refresh_token"],
        })
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "token_reuse"
        assert devices.device_person(device_id) is None
    finally:
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if str(GW) in sys.path:
            sys.path.remove(str(GW))


@pytest.fixture()
def sqlite_device(tmp_path, monkeypatch):
    from tests.integration.test_postgres_auth_security import service_modules
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("COMPANY_OS_DB_URL", raising=False)
    with service_modules("gateway"):
        devices = importlib.import_module("app.device_auth")
        device = devices.create_device(person_id="usr_test", name="Retry Mac")
        yield devices, device


def test_sqlite_refresh_retry_is_same_successor_and_closes_on_next_rotate(sqlite_device, monkeypatch):
    devices, device = sqlite_device
    initial = devices.rotate_refresh(device, previous=None)
    second = devices.rotate_refresh(device, previous=initial)
    assert devices.rotate_refresh(device, previous=initial) == second
    assert devices.rotate_refresh(device, previous=initial) == second
    third = devices.rotate_refresh(device, previous=second)
    assert not devices.revoke_device(device, initial)
    assert devices.device_person(device) == "usr_test"
    assert third != second
    assert devices.rotate_refresh(device, previous=second) == third
    with pytest.raises(ValueError, match="invalid_refresh"):
        devices.rotate_refresh(device, previous=initial)
    assert devices.device_person(device) == "usr_test"
    access = devices.issue_access_jwt("usr_test", device, refresh_token=third)
    assert devices.load_bearer(access).subject == "usr_test"
    later = devices._now() + timedelta(seconds=601)
    monkeypatch.setattr(devices, "_now", lambda: later)
    with pytest.raises(ValueError, match="token_reuse"):
        devices.rotate_refresh(device, previous=second)
    with pytest.raises(ValueError, match="revoked"):
        devices.rotate_refresh(device, previous=third)


@pytest.mark.parametrize("seconds", [600, 601])
def test_sqlite_refresh_retry_window_is_fixed(sqlite_device, monkeypatch, seconds):
    devices, device = sqlite_device
    at = datetime(2026, 10, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(devices, "_now", lambda: at)
    initial = devices.rotate_refresh(device, previous=None)
    second = devices.rotate_refresh(device, previous=initial)
    monkeypatch.setattr(devices, "_now", lambda: at + timedelta(seconds=599))
    assert devices.rotate_refresh(device, previous=initial) == second
    monkeypatch.setattr(devices, "_now", lambda: at + timedelta(seconds=seconds))
    if seconds == 600:
        assert devices.rotate_refresh(device, previous=initial) == second
    else:
        with pytest.raises(ValueError, match="invalid_refresh"):
            devices.rotate_refresh(device, previous=initial)
        assert devices.device_person(device) == "usr_test"
        with pytest.raises(ValueError, match="token_reuse"):
            devices.rotate_refresh(device, previous=second)
        assert devices.device_person(device) is None


@pytest.mark.parametrize("received", [False, True])
def test_sqlite_late_previous_refresh_revokes_only_after_successor_bearer_used(sqlite_device, monkeypatch, received):
    devices, device = sqlite_device
    initial = devices.rotate_refresh(device, previous=None)
    old_access = devices.issue_access_jwt("usr_test", device, refresh_token=initial)
    second = devices.rotate_refresh(device, previous=initial)
    access = devices.issue_access_jwt("usr_test", device, refresh_token=second)
    assert devices.load_bearer(old_access).subject == "usr_test"
    if received:
        assert devices.load_bearer(access).subject == "usr_test"
    later = devices._now() + timedelta(seconds=601)
    monkeypatch.setattr(devices, "_now", lambda: later)
    with pytest.raises(ValueError, match="token_reuse" if received else "invalid_refresh"):
        devices.rotate_refresh(device, previous=initial)
    assert devices.device_person(device) == (None if received else "usr_test")


@pytest.mark.parametrize("state", ["initial", "rotated", "retired"])
def test_random_refresh_cannot_revoke_known_device_or_change_credentials(sqlite_device, monkeypatch, state):
    devices, device = sqlite_device
    initial = devices.rotate_refresh(device, previous=None)
    access = devices.issue_access_jwt("usr_test", device, refresh_token=initial)
    if state != "initial":
        second = devices.rotate_refresh(device, previous=initial)
    if state == "retired":
        later = devices._now() + timedelta(seconds=601)
        monkeypatch.setattr(devices, "_now", lambda: later)
        with pytest.raises(ValueError, match="invalid_refresh"):
            devices.rotate_refresh(device, previous=initial)
        with devices._connect() as connection:
            row = connection.execute("SELECT * FROM desktop_devices WHERE id=?", (device,)).fetchone()
            assert row["refresh_token_hash"] is None
            assert row["previous_refresh_token_hash"] == devices._hash_token(second)
    main = importlib.import_module("app.main")
    events = []
    monkeypatch.setattr(devices, "_security_event", lambda *event: events.append(event))
    with TestClient(main.create_app()) as client:
        bearer = {"Authorization": f"Bearer {access}"}
        assert client.get("/auth/session", headers=bearer).json()["authenticated"] is True
        with devices._connect() as connection:
            before = dict(connection.execute("SELECT * FROM desktop_devices WHERE id=?", (device,)).fetchone())
        rejected = client.post("/auth/device/refresh", json={
            "device_id": device, "refresh_token": secrets.token_urlsafe(32),
        })
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "invalid_refresh"
        with devices._connect() as connection:
            after = dict(connection.execute("SELECT * FROM desktop_devices WHERE id=?", (device,)).fetchone())
        assert after == before
        assert events == []
        assert devices.device_person(device) == "usr_test"
        assert client.get("/auth/session", headers=bearer).json()["authenticated"] is True


def test_genuine_previous_refresh_reuse_revokes_device_and_access(sqlite_device, monkeypatch):
    devices, device = sqlite_device
    initial = devices.rotate_refresh(device, previous=None)
    second = devices.rotate_refresh(device, previous=initial)
    access = devices.issue_access_jwt("usr_test", device, refresh_token=second)
    main = importlib.import_module("app.main")
    with TestClient(main.create_app()) as client:
        bearer = {"Authorization": f"Bearer {access}"}
        assert client.get("/auth/session", headers=bearer).json()["authenticated"] is True
        later = devices._now() + timedelta(seconds=601)
        monkeypatch.setattr(devices, "_now", lambda: later)
        rejected = client.post("/auth/device/refresh", json={
            "device_id": device, "refresh_token": initial,
        })
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "token_reuse"
        assert devices.device_person(device) is None
        assert client.get("/auth/session", headers=bearer).status_code == 401


def test_expired_bearer_after_sleep_or_deploy_is_401_and_refresh_keeps_device(sqlite_device, monkeypatch):
    # Mac slept past its 15-minute access token; its first request after wake
    # lands on a freshly deployed gateway task. Released Mac apps discard the
    # device on a 200 authenticated:false, so a rejected bearer must be a 401.
    devices, device = sqlite_device
    refresh = devices.rotate_refresh(device, previous=None)
    real_now = devices._now
    monkeypatch.setattr(devices, "_now", lambda: real_now() - timedelta(minutes=16))
    stale = {"Authorization": f"Bearer {devices.issue_access_jwt('usr_test', device, refresh_token=refresh)}"}
    monkeypatch.setattr(devices, "_now", real_now)
    main = importlib.import_module("app.main")
    with TestClient(main.create_app()) as client:
        assert client.get("/auth/session", headers=stale).status_code == 401
        assert client.get("/auth/session", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
        assert client.get("/auth/session").json()["authenticated"] is False  # cookie clients unchanged
        renewed = client.post("/auth/device/refresh", json={"device_id": device, "refresh_token": refresh})
        assert renewed.status_code == 200
        fresh = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
        assert client.get("/auth/session", headers=fresh).json()["subject"] == "usr_test"
    assert devices.device_person(device) == "usr_test"


@pytest.mark.parametrize("elapsed", [0, 601])
def test_sqlite_logout_can_revoke_after_lost_refresh_reply(sqlite_device, monkeypatch, elapsed):
    devices, device = sqlite_device
    initial = devices.rotate_refresh(device, previous=None)
    devices.rotate_refresh(device, previous=initial)
    later = devices._now() + timedelta(seconds=elapsed)
    monkeypatch.setattr(devices, "_now", lambda: later)
    assert devices.revoke_device(device, initial)
    assert devices.device_person(device) is None


@pytest.mark.parametrize("stage", ["device_person", "rotate_refresh", "issue_access_jwt"])
def test_refresh_availability_failure_is_retryable_503(sqlite_device, monkeypatch, stage):
    devices, device = sqlite_device
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-longer-than-32-bytes")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-passphrase")
    main = importlib.import_module("app.main")
    initial = devices.rotate_refresh(device, previous=None)
    original = getattr(devices, stage)
    def unavailable(*args, **kwargs):
        raise RuntimeError("private backend details")
    monkeypatch.setattr(devices, stage, unavailable)
    with TestClient(main.create_app(), raise_server_exceptions=False) as client:
        body = {"device_id": device, "refresh_token": initial}
        response = client.post("/auth/device/refresh", json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "device_refresh_unavailable"
        assert "private backend" not in response.text
        monkeypatch.setattr(devices, stage, original)
        recovered = client.post("/auth/device/refresh", json=body)
        assert recovered.status_code == 200
        assert devices.rotate_refresh(device, previous=initial) == recovered.json()["refresh_token"]


def test_sqlite_existing_device_schema_upgrades_without_losing_token(tmp_path, monkeypatch):
    import sqlite3
    from tests.integration.test_postgres_auth_security import service_modules
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE desktop_devices (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, name TEXT NOT NULL, platform TEXT, refresh_token_hash TEXT, revoked_at TEXT)")
        connection.execute("INSERT INTO desktop_devices VALUES ('dev_legacy','usr_legacy','Legacy Mac','macos','digest',NULL)")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(path))
    with service_modules("gateway"):
        users = importlib.import_module("app.users")
        with users._connect() as connection:
            row = connection.execute("SELECT * FROM desktop_devices WHERE id='dev_legacy'").fetchone()
            assert row["refresh_token_hash"] == "digest"
            assert row["previous_refresh_token_hash"] is None and row["refresh_rotated_at"] is None
            assert row["refresh_token_used"] == 1


def test_retry_across_workers_uses_shared_signer_and_key_change_is_not_revocation(sqlite_device, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    devices, device = sqlite_device
    def signer():
        return ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
    monkeypatch.setenv("JWT_SIGNING_KEY_ES256", signer())
    first = devices.rotate_refresh(device, previous=None)
    second = devices.rotate_refresh(device, previous=first)
    monkeypatch.setattr(devices, "_LOCAL_KEY", None)
    assert devices.rotate_refresh(device, previous=first) == second
    monkeypatch.setenv("JWT_SIGNING_KEY_ES256", signer())
    with pytest.raises(RuntimeError, match="refresh_recovery_unavailable"):
        devices.rotate_refresh(device, previous=first)
    assert devices.device_person(device) == "usr_test"
    assert devices.rotate_refresh(device, previous=second) != second
