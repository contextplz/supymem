"""Real Alembic upgrades; optionally exercise isolated local PostgreSQL databases.

Run with PR13_TEST_POSTGRES_URL pointing at a localhost PostgreSQL admin database
for PostgreSQL coverage. The test creates/drops uniquely named databases only;
without that setting it uses temporary SQLite files.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "services/company_os"
HEAD = "s013cleanupfromu6"
MERGE = "u6v7w8x9y0z1"
PLATFORM_TABLES = (
    "import_runs", "source_snapshots", "build_runs", "build_artifacts",
    "content_generation_runs", "content_publication_runs", "credit_ledger",
)


def _upgrade(url: str, revision: str) -> None:
    env = dict(os.environ, COMPANY_OS_DB_URL=url, CHANNELS_DRY_RUN="true")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=SERVICE, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@contextmanager
def _database(tmp_path: Path):
    configured = os.environ.get("PR13_TEST_POSTGRES_URL")
    if not configured:
        yield f"sqlite:///{tmp_path / (uuid.uuid4().hex + '.db')}"
        return
    url = sa.make_url(configured)
    assert url.get_backend_name() == "postgresql"
    assert url.host in {"localhost", "127.0.0.1", "::1"}, "Local test PostgreSQL only"
    name = "pr13_migration_" + uuid.uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    try:
        yield url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def _schema(engine):
    inspector = sa.inspect(engine)
    return {
        table: {
            "columns": sorted((c["name"], str(c["type"]), c["nullable"], c["default"])
                              for c in inspector.get_columns(table)),
            "foreign_keys": sorted((f["name"] or "", tuple(f["constrained_columns"]),
                                    f["referred_table"], tuple(f["referred_columns"]))
                                   for f in inspector.get_foreign_keys(table)),
            "checks": sorted((c["name"] or "", c["sqltext"])
                             for c in inspector.get_check_constraints(table)),
            "unique": sorted((c["name"] or "", tuple(c["column_names"]))
                             for c in inspector.get_unique_constraints(table)),
            "indexes": sorted((c["name"], tuple(c["column_names"]), bool(c["unique"]))
                              for c in inspector.get_indexes(table)),
        }
        for table in inspector.get_table_names()
    }


def _triggers(conn):
    if conn.dialect.name == "postgresql":
        return conn.execute(sa.text(
            "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname='public' ORDER BY 1, 2"
        )).all()
    return conn.execute(sa.text(
        "SELECT tbl_name, name, sql FROM sqlite_master WHERE type='trigger' ORDER BY 1, 2"
    )).all()


def test_pr13_single_head():
    config = Config(str(SERVICE / "alembic.ini"))
    config.set_main_option("script_location", str(SERVICE / "app/db/migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [HEAD]
    assert scripts.get_revision(HEAD).down_revision == MERGE
    assert set(scripts.get_revision(MERGE).down_revision) == {
        "s011importcleanup", "t5u6v7w8x9y0",
    }


def test_recorded_live_u6_without_cleanup_gains_table_without_rewriting_parents(tmp_path):
    """Simulate live staging: version_num is u6, s011importcleanup never ran."""
    with _database(tmp_path) as url:
        engine = sa.create_engine(url)
        try:
            _upgrade(url, "s010importintake")
            _upgrade(url, "t5u6v7w8x9y0")
            with engine.begin() as conn:
                present = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
                assert set(present) == {"s010importintake", "t5u6v7w8x9y0"}
                conn.execute(sa.text("DELETE FROM alembic_version"))
                conn.execute(sa.text(
                    "INSERT INTO alembic_version (version_num) VALUES ('u6v7w8x9y0z1')"
                ))
            inspector = sa.inspect(engine)
            assert "import_object_cleanups" not in inspector.get_table_names()
            _upgrade(url, "head")
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD]
            assert "import_object_cleanups" in sa.inspect(engine).get_table_names()
            _upgrade(url, "head")
            assert "import_object_cleanups" in sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()


def _cleanup_snapshot(url: str):
    engine = sa.create_engine(url)
    try:
        schema = _schema(engine)["import_object_cleanups"]
        with engine.connect() as conn:
            triggers = [row for row in _triggers(conn) if row[0] == "import_object_cleanups"]
        return schema, triggers
    finally:
        engine.dispose()


def test_s013_cleanup_schema_matches_full_graph_s011_path(tmp_path):
    """Live-u6 s013 DDL must match databases that created the table via s011."""
    with _database(tmp_path) as full_url:
        _upgrade(full_url, "head")
        full = _cleanup_snapshot(full_url)
    with _database(tmp_path) as live_url:
        _upgrade(live_url, "s010importintake")
        _upgrade(live_url, "t5u6v7w8x9y0")
        engine = sa.create_engine(live_url)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("DELETE FROM alembic_version"))
                conn.execute(sa.text(
                    "INSERT INTO alembic_version (version_num) VALUES ('u6v7w8x9y0z1')"
                ))
            assert "import_object_cleanups" not in sa.inspect(engine).get_table_names()
        finally:
            engine.dispose()
        _upgrade(live_url, "head")
        live = _cleanup_snapshot(live_url)
    assert live == full


def test_pr13_upgrade_paths_converge_and_preserve_platform(tmp_path):
    schemas = []
    for previous in ("s010importintake", "t5u6v7w8x9y0", None):
        with _database(tmp_path) as url:
            engine = sa.create_engine(url)
            try:
                before = None
                triggers = []
                if previous:
                    _upgrade(url, previous)
                if previous == "s010importintake":
                    before = _schema(engine)
                    with engine.begin() as conn:
                        triggers = _triggers(conn)
                        conn.execute(sa.text(
                            "INSERT INTO gtm_contacts (id,workspace_id,email,stage_changed_at,created_at) "
                            "VALUES ('migration-contact','migration-workspace','migration@example.invalid',"
                            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                        ))
                        conn.execute(sa.text(
                            "INSERT INTO gtm_touches (id,contact_id,direction,body_text,created_at) "
                            "VALUES ('migration-touch','migration-contact','in','keep this history',CURRENT_TIMESTAMP)"
                        ))
                _upgrade(url, "head")
                after = _schema(engine)
                assert {"import_runs", "source_snapshots", "gtm_outreach_settings",
                        "gtm_god_plans", "marketing_ad_campaigns", "gtm_interviews",
                        "import_object_cleanups"} <= after.keys()
                with engine.connect() as conn:
                    assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD]
                    if before:
                        for table in PLATFORM_TABLES:
                            if table in before:
                                assert after[table] == before[table], table
                        after_triggers = _triggers(conn)
                        before_snapshot = [
                            t for t in triggers if t[0] in before
                        ]
                        after_snapshot = [
                            t for t in after_triggers if t[0] in before
                        ]
                        assert after_snapshot == before_snapshot
                        assert conn.execute(sa.text(
                            "SELECT workspace_id,body_text FROM gtm_touches WHERE id='migration-touch'"
                        )).one() == ("migration-workspace", "keep this history")
                        assert conn.execute(sa.text(
                            "SELECT email FROM gtm_contacts WHERE id='migration-contact'"
                        )).scalar_one() == "migration@example.invalid"
                _upgrade(url, "head")
                assert _schema(engine) == after
                schemas.append(after)
            finally:
                engine.dispose()
    assert schemas[0] == schemas[1] == schemas[2]

