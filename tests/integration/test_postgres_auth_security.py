"""Hosted migrations and auth concurrency against an isolated local PostgreSQL DB.

Set TEST_POSTGRES_URL to a loopback PostgreSQL admin connection. Each run creates
and drops its own random database; remote hosts are explicitly refused.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, enumerate as enumerate_threads

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
SECRET = "postgres-boundary-secret-more-than-32-bytes"


def migration_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


@contextmanager
def service_modules(name: str):
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "app" or k.startswith("app.")}
    old_path = sys.path[:]
    sys.path.insert(0, str(ROOT / "services" / name))
    try:
        yield
    finally:
        # A stopped enrichment can still import app.state; drain it before changing modules or DB URLs.
        for thread in enumerate_threads():
            if thread.name == "research-graveyard":
                thread.join(timeout=120)
                assert not thread.is_alive(), "Research enrichment outlived its test service context"
        state = sys.modules.get("app.state")
        engine = getattr(state, "_engine", None)
        if engine is not None:
            engine.dispose()
        for key in list(sys.modules):
            if key == "app" or key.startswith("app."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)
        sys.path[:] = old_path


@pytest.fixture(scope="module")
def pg_database(tmp_path_factory):
    raw = os.environ.get("TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("TEST_POSTGRES_URL is required for real PostgreSQL acceptance")
    url = make_url(raw)
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL auth acceptance refuses non-loopback hosts")
    admin_url = url.set(drivername="postgresql+psycopg", database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = f"supymem_auth_test_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    db_url = admin_url.set(database=name).render_as_string(hide_password=False)
    with service_modules("company_os"):
        from app.db.session import make_engine
        # Exercise the same connection invariants as production. Raw engines
        # otherwise inherit the developer's local PostgreSQL session timezone.
        engine = make_engine(db_url)
    try:
        # Parent background work must never see the fresh database before migrations finish.
        env = dict(os.environ)
        env["COMPANY_OS_DB_URL"] = db_url
        env["COMPANY_OS_DATA_DIR"] = str(tmp_path_factory.mktemp("pg-auth-migrations"))
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "packages"), str(ROOT / "services/company_os")])
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ROOT / "services/company_os/alembic.ini"), "upgrade", "head"],
            env=env, cwd=ROOT / "services/company_os", check=True, timeout=120,
        )
        yield db_url, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture()
def gateway(pg_database, tmp_path, monkeypatch):
    db_url, engine = pg_database
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("GATEWAY_USERS_DB", db_url)
    monkeypatch.setenv("COMPANY_OS_DB_URL", db_url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with service_modules("gateway"):
        users = importlib.import_module("app.users")
        devices = importlib.import_module("app.device_auth")
        user = users.create_user(f"{uuid.uuid4().hex}@example.test", "Pg-security-only-password-2026")
        yield users, devices, user, engine


def concurrent(call, count=8):
    barrier = Barrier(count)

    def run(_):
        barrier.wait(timeout=15)
        return call()

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_migrations_include_core_and_gateway_columns(pg_database):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.db.models import Base

        schema = inspect(engine)
        actual = set(schema.get_table_names())
        missing_tables = set(Base.metadata.tables) - actual
        assert not missing_tables
        missing_columns = {
            table.name: set(table.columns.keys()) - {col["name"] for col in schema.get_columns(table.name)}
            for table in Base.metadata.sorted_tables
        }
        assert not {name: cols for name, cols in missing_columns.items() if cols}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_head()


@pytest.mark.parametrize("prior_head", ["s002credits", "p0k1l2m3n4o5"])
def test_upgrade_from_either_branch_preserves_existing_person(pg_database, monkeypatch, prior_head):
    db_url, _ = pg_database
    admin_url = make_url(db_url).set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    name = f"supymem_branch_test_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        monkeypatch.setenv("COMPANY_OS_DB_URL", url)
        with service_modules("company_os"):
            from alembic import command
            from alembic.config import Config

            cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
            command.upgrade(cfg, prior_head)
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO people (id,display_name,created_at) VALUES (:id,:name,:now)"),
                             {"id": "usr_preserved", "name": "Existing founder", "now": datetime.now(timezone.utc)})
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT display_name FROM people WHERE id='usr_preserved'")).scalar_one() == "Existing founder"
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_head()
        assert {"gateway_users", "desktop_bug_reports", "community_plans", "credit_accounts"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def test_auth_token_has_one_concurrent_consumer(gateway):
    users, _, user, _ = gateway
    token = users.issue_token(user.id, "password_reset")
    result = concurrent(lambda: users.consume_token(token, "password_reset"))
    assert result.count(user.id) == 1
    assert result.count(None) == len(result) - 1


def test_auth_token_purpose_and_expiry_are_enforced(gateway):
    users, _, user, _ = gateway
    token = users.issue_token(user.id, "email_verify")
    assert users.consume_token(token, "password_reset") is None
    assert users.consume_token(token, "email_verify") == user.id
    expired = users.issue_token(user.id, "magic_link", ttl_seconds=-1)
    assert users.consume_token(expired, "magic_link") is None


def test_password_reset_revokes_sibling_reset_and_magic_link_tokens(gateway):
    users, _devices, user, _engine = gateway
    first = users.issue_token(user.id, "password_reset")
    second = users.issue_token(user.id, "password_reset")
    magic = users.issue_token(user.id, "magic_link")
    assert users.consume_token(first, "password_reset", password="replacement-password") == user.id
    assert users.consume_token(second, "password_reset", password="another-password") is None
    assert users.consume_token(magic, "magic_link") is None
    assert users.verify_credentials(user.email, "replacement-password") is not None


def test_concurrent_password_resets_have_one_winner(gateway):
    users, _devices, user, _engine = gateway
    tokens = [users.issue_token(user.id, "password_reset") for _ in range(2)]
    barrier = Barrier(2)

    def reset(token):
        barrier.wait(timeout=10)
        return users.consume_token(token, "password_reset", password="replacement-password")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reset, tokens))
    assert results.count(user.id) == 1
    assert results.count(None) == 1
    assert users.get_user(user.id).session_epoch == 1


def test_pg_device_code_accepts_fresh_timestamp_and_rejects_reuse(gateway):
    _, devices, user, _ = gateway
    device = devices.create_device(person_id=user.id, name="Postgres test Mac")
    verifier = "s" * 43
    code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
    assert devices.exchange_code(code=code, code_verifier="wrong" * 9) is None
    result = devices.exchange_code(code=code, code_verifier=verifier)
    assert result is not None
    assert result[:2] == (user.id, device)
    assert devices.exchange_code(code=code, code_verifier=verifier) is None


def test_pg_device_code_has_one_concurrent_consumer(gateway):
    _, devices, user, _ = gateway
    device = devices.create_device(person_id=user.id, name="Concurrent test Mac")
    verifier = "c" * 43
    code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
    results = concurrent(lambda: devices.exchange_code(code=code, code_verifier=verifier))
    assert sum(result is not None for result in results) == 1


def test_pg_device_code_expiry_is_enforced(gateway):
    users, devices, user, engine = gateway
    device = devices.create_device(person_id=user.id, name="Expired test Mac")
    verifier = "e" * 43
    code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
    with engine.begin() as conn:
        conn.execute(text("UPDATE auth_codes SET expires_at = :past WHERE code_hash = :h"),
                     {"past": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1), "h": users._hash_token(code)})
    assert devices.exchange_code(code=code, code_verifier=verifier) is None


def test_refresh_replay_commits_revocation(gateway, monkeypatch):
    _, devices, user, engine = gateway
    device = devices.create_device(person_id=user.id, name="Replay test Mac")
    first = devices.rotate_refresh(device, previous=None)
    access = devices.issue_access_jwt(user.id, device)
    assert devices.load_bearer(access).subject == user.id
    second = devices.rotate_refresh(device, previous=first)
    assert devices.rotate_refresh(device, previous=first) == second
    third = devices.rotate_refresh(device, previous=second)
    assert devices.load_bearer(devices.issue_access_jwt(user.id, device, refresh_token=third)).subject == user.id
    with pytest.raises(ValueError, match="invalid_refresh"):
        devices.rotate_refresh(device, previous=first)
    later = devices._now() + timedelta(seconds=601)
    monkeypatch.setattr(devices, "_now", lambda: later)
    with pytest.raises(ValueError, match="token_reuse"):
        devices.rotate_refresh(device, previous=second)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT revoked_at FROM desktop_devices WHERE id=:id"), {"id": device}).scalar_one() is not None
        assert conn.execute(
            text("SELECT count(*) FROM security_events WHERE kind='token_reuse' AND detail LIKE :device"),
            {"device": f"%{device}%"},
        ).scalar_one() == 1
    with pytest.raises(ValueError, match="revoked"):
        devices.rotate_refresh(device, previous=second)
    assert devices.load_bearer(access) is None


def test_uninitialized_device_cannot_be_claimed_with_random_refresh(gateway):
    _, devices, user, _ = gateway
    device = devices.create_device(person_id=user.id, name="Unclaimed test Mac")
    with pytest.raises(ValueError, match="invalid_refresh"):
        devices.rotate_refresh(device, previous="invented-refresh")
    assert devices.device_person(device) == user.id


def test_access_token_must_match_device_owner(gateway):
    _, devices, user, _ = gateway
    device = devices.create_device(person_id=user.id, name="Owner-bound test Mac")
    assert devices.load_bearer(devices.issue_access_jwt("usr_other", device)) is None
    assert devices.load_bearer(devices.issue_access_jwt(user.id, "dev_missing")) is None


def test_gateway_accepts_bare_postgres_urls(pg_database):
    db_url, _ = pg_database
    with service_modules("gateway"):
        from app.users_pg import _engine

        url = make_url(db_url).set(drivername="postgresql").render_as_string(hide_password=False)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            engine.dispose()


def test_federated_gateway_profile_is_readable_by_core_without_password_or_email_collision(gateway):
    users, _devices, password_user, engine = gateway
    subject = "usr_cog_shared_profile"
    users.record_verified_identity(subject, password_user.email)
    profile = users.get_user(subject)
    assert profile.email_verified and profile.email == password_user.email
    assert users.get_user_by_email(password_user.email).id == password_user.id
    assert users.verify_credentials(password_user.email, "Pg-security-only-password-2026").id == password_user.id
    users.record_verified_identity(subject, "updated@example.test")
    assert users.get_user(subject).created_at == profile.created_at
    with service_modules("company_os"):
        from app.admin.catalog import _user_profiles
        from app.db.models import Person
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            session.add(Person(id=subject, display_name="Founder", email=None, created_at=datetime.now(timezone.utc)))
            session.flush()
            row = _user_profiles(session, [session.get(Person, subject)])[subject]
            assert row["email"] == "updated@example.test"
            assert row["verification_status"] == "verified"
            assert row["last_active_at"] is None


def test_gateway_http_device_exchange_refresh_and_revocation(gateway, monkeypatch):
    _, devices, user, _ = gateway
    monkeypatch.setenv("SESSION_SECRET", "postgres-http-session-secret-more-than-32-bytes")
    monkeypatch.setenv("CONSOLE_PASSPHRASE", "postgres-local-test-passphrase")
    from fastapi import Depends
    from fastapi.testclient import TestClient

    from app.auth import require_session
    from app.main import create_app

    device = devices.create_device(person_id=user.id, name="HTTP test Mac")
    verifier = "h" * 43
    code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
    app = create_app()

    @app.get("/_test_device_session")
    def private_session(session=Depends(require_session)):
        return {"subject": session.subject}

    with TestClient(app) as client:
        exchanged = client.post("/auth/device/token", json={"code": code, "code_verifier": verifier})
        assert exchanged.status_code == 200, exchanged.text
        credentials = exchanged.json()
        bearer = {"Authorization": f"Bearer {credentials['access_token']}"}
        assert client.get("/_test_device_session", headers=bearer).json() == {"subject": user.id}
        reused_code = client.post("/auth/device/token", json={"code": code, "code_verifier": verifier})
        assert reused_code.status_code == 400
        body = {"device_id": device, "refresh_token": credentials["refresh_token"]}
        refreshed = client.post("/auth/device/refresh", json=body)
        assert refreshed.status_code == 200, refreshed.text
        replay = client.post("/auth/device/refresh", json=body)
        assert replay.status_code == 200
        assert replay.json()["refresh_token"] == refreshed.json()["refresh_token"]
        latest = refreshed.json()["refresh_token"]
        current = client.post("/auth/device/refresh", json={"device_id": device, "refresh_token": latest})
        assert current.status_code == 200
        stale = client.post("/auth/device/refresh", json=body)
        assert stale.status_code == 401
        assert stale.json()["error"]["code"] == "invalid_refresh"
        assert client.get("/_test_device_session", headers=bearer).status_code == 200
        current_bearer = {"Authorization": f"Bearer {current.json()['access_token']}"}
        assert client.get("/_test_device_session", headers=current_bearer).status_code == 200
        later = devices._now() + timedelta(seconds=601)
        monkeypatch.setattr(devices, "_now", lambda: later)
        rejected = client.post("/auth/device/refresh", json={"device_id": device, "refresh_token": latest})
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "token_reuse"
        assert client.get("/_test_device_session", headers=bearer).status_code == 401


def test_concurrent_refresh_retries_return_same_successor_without_revoking(gateway):
    _, devices, user, engine = gateway
    device = devices.create_device(person_id=user.id, name="Concurrent refresh Mac")
    first = devices.rotate_refresh(device, previous=None)
    access = devices.issue_access_jwt(user.id, device)

    def rotate():
        try:
            return "ok", devices.rotate_refresh(device, previous=first)
        except ValueError as exc:
            return "error", str(exc)

    result = concurrent(rotate)
    assert all(status == "ok" for status, _ in result)
    assert len({value for _, value in result}) == 1
    assert devices.load_bearer(access).subject == user.id
    with engine.connect() as conn:
        assert conn.execute(text("SELECT revoked_at FROM desktop_devices WHERE id=:id"), {"id": device}).scalar_one() is None


def test_signed_core_boundary_uses_migrated_postgres(pg_database, tmp_path, monkeypatch):
    db_url, _ = pg_database
    for key, value in {
        "APP_ENV": "local", "LLM_OFFLINE": "1", "COMPANY_OS_DB_URL": db_url,
        "COMPANY_OS_DATA_DIR": str(tmp_path), "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"),
        "COMPANY_OS_REQUIRE_SIGNED_CALLER": "1", "COS_INTERNAL_ASSERTION_SECRET": SECRET,
        "COMPANY_OS_VAULT_SECRET": "postgres-test-vault-secret-at-least-32-bytes",
        "ADMIN_REQUIRE_TOTP": "1", "ADMIN_BOOTSTRAP_EMAILS": "ops@example.test",
    }.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from fastapi.testclient import TestClient

        from app.main import create_app
        from core.assertion import reset_nonce_store, sign_assertion

        reset_nonce_store()

        def signed(path, subject, method="GET"):
            return {"X-COS-Assertion": sign_assertion(SECRET, sub=subject, email="", role="user", path=path, method=method)}

        with TestClient(create_app()) as client:
            ids = []
            for person in ("usr_pg_alice", "usr_pg_bob"):
                response = client.post("/workspaces", json={"name": person, "kind": "company"}, headers=signed("/workspaces", person, "POST"))
                assert response.status_code == 201, response.text
                ids.append(response.json()["id"])
            path = f"/workspaces/{ids[0]}"
            assertion = signed(path, "usr_pg_alice")
            assert client.get(path, headers=assertion).status_code == 200
            assert client.get(path, headers=assertion).status_code == 401
            assert client.get(path, headers=signed(path, "usr_pg_bob")).status_code == 404
            assert client.get(path, headers={"X-Subject": "usr_pg_alice"}).status_code == 401
            listed = client.get("/workspaces", headers=signed("/workspaces", "usr_pg_alice")).json()
            assert ids[0] in {item["id"] for item in listed["items"]}
            assert ids[1] not in {item["id"] for item in listed["items"]}


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/Los_Angeles"])
def test_credential_expiry_is_independent_of_postgres_session_timezone(gateway, zone):
    from sqlalchemy import event
    users, devices, user, _ = gateway
    engine = users._pg_engine()
    def set_zone(connection, *_args):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('TimeZone', %s, false)", (zone,))
        connection.commit()
    event.listen(engine, "checkout", set_zone)
    try:
        token = users.issue_token(user.id, "password_reset")
        assert users.consume_token(token, "password_reset") == user.id
        assert users.consume_token(token, "password_reset") is None
        expired = users.issue_token(user.id, "password_reset", ttl_seconds=-1)
        assert users.consume_token(expired, "password_reset") is None
        device = devices.create_device(person_id=user.id, name="Timezone test")
        verifier = "z" * 43
        code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
        assert devices.exchange_code(code=code, code_verifier=verifier)[:2] == (user.id, device)
        assert devices.exchange_code(code=code, code_verifier=verifier) is None
        expired_code = devices.store_auth_code(person_id=user.id, device_id=device, code_challenge=devices._s256(verifier))
        with engine.begin() as connection:
            connection.execute(text("UPDATE auth_codes SET expires_at=:past WHERE code_hash=:hash"),
                {"past": datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1), "hash": users._hash_token(expired_code)})
        assert devices.exchange_code(code=expired_code, code_verifier=verifier) is None
    finally:
        event.remove(engine, "checkout", set_zone)
