"""Real PostgreSQL migration and concurrent callback-consumption acceptance."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    concurrent,
    pg_database as pg_database,
    service_modules,
)


@pytest.fixture()
def oauth_db(pg_database, tmp_path, monkeypatch):
    url, engine = pg_database
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "local-test-oauth-vault-secret-more-than-32-bytes")
    monkeypatch.setenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "1")
    with service_modules("company_os"):
        from app.state import set_factory
        from app.authz.workspace import ensure_subject_person
        from app.workspaces.service import create_workspace
        from app.content.oauth import attempts
        from app.tenancy import bind

        # Use the migrated schema directly. create_all cannot hide missing fields.
        set_factory(sessionmaker(engine, expire_on_commit=False))
        actor = "usr_pg_oauth_" + uuid.uuid4().hex[:12]
        ensure_subject_person(actor)
        workspace = create_workspace(kind="company", name="OAuth test", owner_id=actor)
        with bind(workspace_id=workspace.id, venture_id="", actor=actor):
            state = attempts.create(workspace_id=workspace.id, platform="x",
                                    redirect_uri="https://api.example.test/api/v1/content/oauth/x/callback",
                                    return_to="https://console.example.test/connections",
                                    auth_method="native", verifier="v" * 64)
        yield attempts, state, workspace.id, actor, engine


def test_pg_callback_nonce_has_one_consumer_and_clears_encrypted_verifier(oauth_db):
    attempts, state, wid, actor, engine = oauth_db
    def consume():
        try:
            return attempts.consume(state, "x")
        except attempts.OAuthError:
            return None
    results = concurrent(consume)
    successes = [row for row in results if row]
    assert len(successes) == 1
    assert successes[0]["workspace_id"] == wid and successes[0]["actor_subject"] == actor
    assert attempts.verifier(successes[0]) == "v" * 64
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, code_verifier, consumed_at FROM content_oauth_states WHERE id=:id"), {"id": attempts.digest(state)}).one()
        assert row.id != state and row.code_verifier is None and row.consumed_at is not None


def test_pg_platform_expiry_and_legacy_state_fail_closed(oauth_db):
    from core.clock import utcnow
    attempts, state, _, _, engine = oauth_db
    with pytest.raises(attempts.OAuthError):
        attempts.consume(state, "linkedin")
    with engine.begin() as conn:
        conn.execute(text("UPDATE content_oauth_states SET expires_at=:expiry WHERE id=:id"),
                     {"expiry": utcnow() - timedelta(minutes=1), "id": attempts.digest(state)})
    with pytest.raises(attempts.OAuthError):
        attempts.consume(state, "x")
    with engine.begin() as conn:
        conn.execute(text("UPDATE content_oauth_states SET expires_at=:expiry, actor_subject=NULL WHERE id=:id"),
                     {"expiry": utcnow() + timedelta(minutes=1), "id": attempts.digest(state)})
    with pytest.raises(attempts.OAuthError):
        attempts.consume(state, "x")


def test_pg_managed_receipt_is_immutable_and_required(oauth_db):
    attempts, state, _, _, engine = oauth_db
    with engine.begin() as conn:
        conn.execute(text("UPDATE content_oauth_states SET auth_method='composio' WHERE id=:id"), {"id": attempts.digest(state)})
    with pytest.raises(attempts.OAuthError, match="not ready"):
        attempts.consume(state, "x")
    attempts.bind_connection(state, user_id="bound-user", auth_config_id="bound-config", connection_id="bound-account")
    with pytest.raises(attempts.OAuthError):
        attempts.bind_connection(state, user_id="foreign-user", auth_config_id="foreign-config", connection_id="foreign-account")
    row = attempts.consume(state, "x")
    assert row["provider_user_id"] == "bound-user" and row["connected_account_id"] == "bound-account"


def test_pg_oauth_migration_roundtrip_invalidates_legacy_plaintext(oauth_db, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from tests.integration.test_postgres_auth_security import ROOT

    attempts, state, wid, _, engine = oauth_db
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    command.downgrade(cfg, "s003mergecontent")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO content_oauth_states(id,workspace_id,platform,code_verifier,redirect_uri,expires_at,created_at) VALUES ('legacy-state',:wid,'x','old-plaintext-verifier','https://api.example.test/callback',CURRENT_TIMESTAMP + interval '15 minutes',CURRENT_TIMESTAMP)"), {"wid": wid})
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT code_verifier,consumed_at,actor_subject FROM content_oauth_states WHERE id='legacy-state'")).one()
        assert row.code_verifier is None and row.consumed_at is not None and row.actor_subject is None
        assert conn.execute(text("SELECT id FROM workspaces WHERE id=:wid"), {"wid": wid}).scalar_one() == wid
