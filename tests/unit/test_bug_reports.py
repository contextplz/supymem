"""Crash reporter queues offline; hosted ingest + founder admin ACL."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.credit_lease import generate_keypair
from tests.company_os_harness import COMPANY_OS, load_company_os_app


class BoomTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, json={"id": "bug_ok", "kind": "crash", "status": "new"})


@pytest.mark.parametrize("local", [False, True])
def test_crash_reporter_queues_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bool) -> None:
    monkeypatch.setenv("SUPYMEM_MODE", "local" if local else "hosted")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.0")
    from desktop_runtime.crash_report import enqueue_crash, queue_dir

    path = enqueue_crash(stack="panic: boom\n  at update.rs:12")
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "crash"
    assert payload["anonymous"] is True
    assert payload["include_account_id"] is False
    assert "email" not in payload or not payload.get("email")
    if local:
        assert payload["stack"] == ""
    else:
        assert "boom" in payload["stack"]
    assert payload["app_version"] == "0.1.0"

    assert any(queue_dir(tmp_path).iterdir())
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (queue_dir(tmp_path).stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("stack", ["Freight invoice processing", "Family recipe organizer"])
def test_crash_report_uses_the_running_desktop_version(tmp_path, monkeypatch, stack):
    from desktop_runtime import __version__
    from desktop_runtime.crash_report import app_version, enqueue_crash

    monkeypatch.setenv("SUPYMEM_APP_VERSION", " 1.2.3-rc.4 ")
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "1.2.2")
    monkeypatch.setenv("SUPYMEM_VERSION", "1.2.1")
    payload = json.loads(enqueue_crash(stack=stack, root=tmp_path).read_text(encoding="utf-8"))
    assert payload["app_version"] == "1.2.3-rc.4"
    monkeypatch.setenv("SUPYMEM_APP_VERSION", " ")
    assert app_version() == "1.2.2"
    monkeypatch.delenv("COMPANY_OS_APP_VERSION")
    assert app_version() == "1.2.1"
    monkeypatch.delenv("SUPYMEM_VERSION")
    assert app_version() == __version__


def test_crash_records_stay_local_and_queue_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from desktop_runtime import crash_report

    assert not hasattr(crash_report, "flush_queue")
    assert not hasattr(crash_report, "start_flush_loop")
    for index in range(55):
        crash_report.enqueue_crash(stack=f"local error {index}")
    assert len(list((tmp_path / "crash_queue").glob("*.json"))) == 50


@pytest.fixture()
def consumer_env(tmp_path, monkeypatch):
    if not (COMPANY_OS / "app/api/consumer_api.py").is_file():
        pytest.skip("Hosted account bug reports are not in the public local release.")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/bugs.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACCOUNT_SESSION_SECRET", "account-session-secret-32bytes!!")
    monkeypatch.setenv("FOUNDER_ADMIN_EMAILS", "founder@supymem.com")
    monkeypatch.setenv("BUG_REPORTS_INGEST_ENABLED", "1")
    priv, pub = generate_keypair()
    monkeypatch.setenv("CREDIT_LEASE_PRIVATE_KEY", priv)
    monkeypatch.setenv("CREDIT_LEASE_PUBLIC_KEY", pub)
    load_company_os_app()
    from app import state
    from app.credits.policy import invalidate
    from app.consumer.cognito import FakeCognitoClient, set_cognito_client

    state.reset_db_factory()
    state.db()
    invalidate()
    fake = FakeCognitoClient()
    set_cognito_client(fake)
    yield fake
    set_cognito_client(None)
    invalidate()


@pytest.fixture()
def client(consumer_env) -> TestClient:
    from app.api.consumer_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _signup(client: TestClient, email: str) -> str:
    client.post("/api/auth/v2/cognito/email-otp/start", json={"email": email})
    res = client.post(
        "/api/auth/v2/cognito/email-otp/verify",
        json={"email": email, "code": "246801"},
    )
    return res.json()["session_token"]


def test_user_report_api_rejects_unsigned(client: TestClient) -> None:
    denied = client.post(
        "/api/v1/reports/user",
        json={"title": "Broken chat", "body": "It froze after send."},
    )
    assert denied.status_code == 401


def test_admin_list_403_for_non_founder(client: TestClient) -> None:
    user_token = _signup(client, "user@example.com")
    denied = client.get(
        "/api/v1/founder/reports",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert denied.status_code == 403


def test_founder_can_read_and_filter_reports(client: TestClient) -> None:
    user_token = _signup(client, "user@example.com")
    founder_token = _signup(client, "founder@supymem.com")

    crash = client.post(
        "/api/v1/reports/crash",
        json={
            "app_version": "0.1.0",
            "os": "macOS-15.0-arm64",
            "stack": "panic at src-tauri/src/update.rs:12",
            "occurred_at": "2026-08-24T12:00:00Z",
            "email": "should-not-store@example.com",
            "account_id": "csu_forged",
            "include_account_id": True,
        },
    )
    assert crash.status_code == 200
    crash_row = crash.json()
    assert crash_row["kind"] == "crash"
    assert crash_row["status"] == "new"
    assert crash_row["anonymous"] is True
    assert crash_row["account_id"] is None
    assert crash_row.get("contact") is None

    user = client.post(
        "/api/v1/reports/user",
        headers={"Authorization": f"Bearer {user_token}"},
        json={
            "title": "Chat stuck",
            "body": "The composer never returned.",
            "contact": "ada@example.com",
            "app_version": "0.1.0",
            "os": "macOS",
            "include_account_id": True,
        },
    )
    assert user.status_code == 200
    assert user.json()["kind"] == "user"
    assert user.json()["account_id"]
    assert user.json()["contact"] == "ada@example.com"

    listed = client.get(
        "/api/v1/founder/reports",
        headers={"Authorization": f"Bearer {founder_token}"},
    )
    assert listed.status_code == 200
    kinds = {row["kind"] for row in listed.json()["items"]}
    assert kinds == {"crash", "user"}

    crashes = client.get(
        "/api/v1/founder/reports?kind=crash",
        headers={"Authorization": f"Bearer {founder_token}"},
    )
    assert crashes.status_code == 200
    assert all(row["kind"] == "crash" for row in crashes.json()["items"])

    detail = client.get(
        f"/api/v1/founder/reports/{crash_row['id']}",
        headers={"Authorization": f"Bearer {founder_token}"},
    )
    assert detail.status_code == 200
    assert "panic" in (detail.json().get("stack") or "")

    patched = client.patch(
        f"/api/v1/founder/reports/{crash_row['id']}",
        headers={"Authorization": f"Bearer {founder_token}"},
        json={"status": "ack"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "ack"

    user_patch = client.patch(
        f"/api/v1/founder/reports/{crash_row['id']}",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"status": "closed"},
    )
    assert user_patch.status_code == 403


def test_crash_opt_in_attaches_account_id_from_session(client: TestClient) -> None:
    token = _signup(client, "ada@example.com")
    from app.consumer.session import parse_session

    uid = parse_session(token).user_id
    res = client.post(
        "/api/v1/reports/crash",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "app_version": "0.1.0",
            "os": "linux",
            "stack": "boom",
            "include_account_id": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["account_id"] == uid
    assert res.json()["anonymous"] is False


def test_no_sentry_dsn_in_desktop_reporter() -> None:
    root = Path(__file__).resolve().parents[2]
    reporter = (root / "packages" / "desktop_runtime" / "crash_report.py").read_text(
        encoding="utf-8"
    )
    rust = (root / "src-tauri" / "src" / "crash.rs").read_text(encoding="utf-8")
    assert "sentry.io" not in reporter.lower()
    assert "sentry.io" not in rust.lower()
    assert "SENTRY_DSN" not in reporter
    assert "SENTRY_DSN" not in rust


def test_safe_logs_skip_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    (tmp_path / "hosted_account.json").write_text('{"session_token":"secret"}', encoding="utf-8")
    (tmp_path / "runtime.log").write_text("ok line\n", encoding="utf-8")
    from desktop_runtime.crash_report import collect_safe_logs

    logs = collect_safe_logs(tmp_path)
    assert "ok line" in logs
    assert "secret" not in logs
    assert "session_token" not in logs
