"""Warden's policy plane answers only signed system/admin callers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.assertion import reset_nonce_store, sign_assertion
from tests.company_os_harness import ROOT

SECRET = "w" * 48  # test-only signing key (built at runtime, not a credential)


@pytest.fixture()
def warden(tmp_path: Path, monkeypatch):
    if not (ROOT / "services/warden/app/main.py").is_file():
        pytest.skip("Hosted Warden is not in the public local release.")
    from tests.warden_harness import prefer_warden_path, purge_app_modules, restore_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    reset_nonce_store()
    saved = purge_app_modules()
    old = prefer_warden_path()
    try:
        from app.main import create_app

        with TestClient(create_app(db_url=f"sqlite:///{tmp_path / 'warden.db'}")) as client:
            yield client, monkeypatch
    finally:
        sys.path[:] = old
        restore_app_modules(saved)


def _sig(path: str, method: str, role: str = "system") -> dict[str, str]:
    return {"X-COS-Assertion": sign_assertion(SECRET, sub="t", email="", role=role, path=path, method=method)}


def test_unsigned_mutations_are_refused(warden) -> None:
    client, _ = warden
    assert client.get("/health").status_code == 200
    assert client.post("/kill-switch", json={"enabled": True, "actor": "x"}).status_code == 401
    assert client.post("/policy/autonomy", json={"level": "L4", "actor": "x"}).status_code == 401
    assert client.post("/policy/caps", json={"caps": {}, "actor": "x"}).status_code == 401
    assert client.get("/audit").status_code == 401


def test_user_role_assertion_is_forbidden(warden) -> None:
    client, _ = warden
    res = client.post("/kill-switch", json={"enabled": True, "actor": "x"}, headers=_sig("/kill-switch", "POST", "user"))
    assert res.status_code == 403


def test_system_assertion_is_accepted_and_path_bound(warden) -> None:
    client, _ = warden
    assert client.get("/kill-switch", headers=_sig("/kill-switch", "GET")).status_code == 200
    # An assertion for one route cannot be replayed onto another.
    assert client.post("/kill-switch", json={"enabled": True}, headers=_sig("/check", "POST")).status_code == 401


def test_hosted_without_secret_fails_closed(warden) -> None:
    client, monkeypatch = warden
    monkeypatch.delenv("COS_INTERNAL_ASSERTION_SECRET")
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/kill-switch").status_code == 503
    assert client.get("/health").status_code == 200


def test_company_os_client_signs_warden_calls(tmp_path, monkeypatch) -> None:
    import asyncio

    import httpx

    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    load_company_os_app()
    from app.core.warden_client import WardenClient

    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-cos-assertion", ""))
        if request.url.path == "/check":
            return httpx.Response(200, json={"decision": "allow", "reason": "ok"})
        return httpx.Response(200, json={"kill_switch": False})

    client = WardenClient("http://warden", transport=httpx.MockTransport(handler))
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert asyncio.run(client.check(capability="x")).decision == "deny"
    assert not seen

    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    assert asyncio.run(client.check(capability="x")).decision == "allow"
    assert asyncio.run(client.kill_switch()) is False
    assert len(seen) == 2 and all(seen)
