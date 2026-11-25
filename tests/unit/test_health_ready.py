"""Liveness is cheap; readiness is honest about the database."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.company_os_harness import company_os_client, load_company_os_app, local_owner_headers

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "services" / "gateway"
WARDEN = ROOT / "services" / "warden"


def _dead_sqlite_url(tmp_path: Path) -> str:
    """A SQLite URL whose parent path is a file, so connect fails immediately."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    return f"sqlite:///{blocker / 'cos.db'}"


def _stamp_alembic(url: str, revision: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": revision},
        )
    engine.dispose()


@pytest.mark.parametrize("database", ["invoice_audits", "family_walks"], ids=["b2b", "consumer"])
@pytest.mark.parametrize("mode", ["hosted", "local"])
def test_company_os_health_ok_ready_503_when_db_unavailable(
    tmp_path: Path, monkeypatch, database, mode
) -> None:
    directory = tmp_path / database
    directory.mkdir()
    dead = _dead_sqlite_url(directory)
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", dead)
    monkeypatch.setenv("DATABASE_URL", dead)
    monkeypatch.setenv("DB_URL", dead)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    main = load_company_os_app()
    with TestClient(main.create_app(), base_url="http://localhost", client=("127.0.0.1", 50000), headers=local_owner_headers()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        ready = client.get("/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert body["checks"]["database"]["ok"] is False
        assert body["checks"]["database"]["hard"] is True
        if mode == "local":
            protected = client.get("/auth/session")
            assert protected.status_code == 503
            assert protected.json()["detail"]["code"] == "local_auth_not_ready"
            with TestClient(client.app, base_url="http://localhost", client=("192.0.2.1", 50000)) as remote:
                denied = remote.get("/health")
                assert denied.status_code == 403
                assert denied.json()["detail"]["code"] == "local_only"


def test_company_os_ready_200_when_db_and_schema_ok(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "cos.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("WARDEN_URL", "http://127.0.0.1:1")
    main = load_company_os_app()
    from app.health import expected_schema_revision
    import app.health as health_mod

    health_mod.expected_schema_revision.cache_clear()
    migrations = Path(health_mod.__file__).resolve().parent / "db" / "migrations" / "env.py"
    assert migrations.is_file(), "schema probe must read app/db/migrations, not app/app/..."
    rev = expected_schema_revision()
    assert rev, "alembic heads must be readable"
    _stamp_alembic(url, rev)
    with company_os_client(main.create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        ready = client.get("/ready")
        assert ready.status_code == 200, ready.json()
        body = ready.json()
        assert body["ready"] is True
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["schema"]["ok"] is True
        from app.core.local_mode import is_local

        if is_local():
            assert set(body["checks"]) == {"database", "schema"}
            assert body["degraded"] is False
        else:
            assert body["checks"]["warden"]["hard"] is False
            assert body["degraded"] is True


@pytest.mark.skipif(not GATEWAY.is_dir(), reason="Hosted gateway is not in the public local release")
def test_gateway_health_ok_ready_503_when_db_unavailable(tmp_path: Path, monkeypatch) -> None:
    from tests.warden_harness import purge_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path))  # directory → sqlite connect fails
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WARDEN_URL", "http://127.0.0.1:1")
    load_company_os_app()
    headers = local_owner_headers()
    purge_app_modules()
    sys.path = [
        p
        for p in sys.path
        if "services/company_os" not in p and "services/warden" not in p
    ]
    sys.path.insert(0, str(GATEWAY))
    from app.main import create_app

    with TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 50000), headers=headers) as client:
        health = client.get("/health")
        assert health.status_code == 200
        ready = client.get("/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["ready"] is False
        assert body["checks"]["users_db"]["ok"] is False
        assert body["checks"]["company_os"]["hard"] is True


@pytest.mark.skipif(not GATEWAY.is_dir(), reason="Hosted gateway is not in the public local release")
def test_gateway_ready_200_when_deps_healthy(tmp_path: Path, monkeypatch) -> None:
    from tests.warden_harness import purge_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "test-pass")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("GATEWAY_USERS_DB", str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv("COMPANY_OS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("WARDEN_URL", "http://127.0.0.1:1")
    load_company_os_app()
    headers = local_owner_headers()
    purge_app_modules()
    sys.path = [
        p
        for p in sys.path
        if "services/company_os" not in p and "services/warden" not in p
    ]
    sys.path.insert(0, str(GATEWAY))

    class _Resp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def get(self, url: str):
            return _Resp()

    from app.main import create_app

    with patch("app.health.httpx.Client", _FakeClient):
        with TestClient(create_app(), base_url="http://localhost", client=("127.0.0.1", 50000), headers=headers) as client:
            health = client.get("/health")
            assert health.status_code == 200
            ready = client.get("/ready")
            assert ready.status_code == 200, ready.json()
            body = ready.json()
            assert body["ready"] is True
            assert body["checks"]["users_db"]["ok"] is True
            assert body["checks"]["company_os"]["ok"] is True


@pytest.mark.skipif(not WARDEN.is_dir(), reason="Hosted Warden is not in the public local release")
def test_warden_health_ok_ready_503_when_db_unavailable(tmp_path: Path, monkeypatch) -> None:
    from tests.warden_harness import prefer_warden_path, purge_app_modules, restore_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    headers = local_owner_headers()
    saved = purge_app_modules()
    old = prefer_warden_path()
    try:
        from app.main import create_app

        app = create_app(db_url=_dead_sqlite_url(tmp_path))
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), headers=headers) as client:
            health = client.get("/health")
            assert health.status_code == 200
            ready = client.get("/ready")
            assert ready.status_code == 503
            body = ready.json()
            assert body["ready"] is False
            assert body["checks"]["database"]["ok"] is False
    finally:
        sys.path[:] = old
        restore_app_modules(saved)


@pytest.mark.skipif(not WARDEN.is_dir(), reason="Hosted Warden is not in the public local release")
def test_warden_ready_200_when_db_healthy(tmp_path: Path, monkeypatch) -> None:
    from tests.warden_harness import prefer_warden_path, purge_app_modules, restore_app_modules

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    load_company_os_app()
    headers = local_owner_headers()
    saved = purge_app_modules()
    old = prefer_warden_path()
    try:
        from app.main import create_app

        app = create_app(db_url=f"sqlite:///{tmp_path / 'warden.db'}")
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), headers=headers) as client:
            health = client.get("/health")
            assert health.status_code == 200
            ready = client.get("/ready")
            assert ready.status_code == 200, ready.json()
            body = ready.json()
            assert body["ready"] is True
            assert body["checks"]["database"]["ok"] is True
            assert body["checks"]["schema"]["ok"] is True
    finally:
        sys.path[:] = old
        restore_app_modules(saved)
