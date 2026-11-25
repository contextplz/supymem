import importlib.util
import os
from pathlib import Path
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import case, create_engine, event, func, literal, select, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("scale_indexes", ROOT / "services/company_os/app/db/migrations/versions/s059scaleidx.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture(params=["sqlite", "postgresql"])
def scale_engine(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'indexes.db'}")
        yield engine
        engine.dispose()
        return
    raw = os.environ.get("TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("TEST_POSTGRES_URL is required")
    parsed = make_url(raw)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}, "Refuse a non-loopback test database"
    schema = "scale_test_" + uuid.uuid4().hex
    admin = create_engine(parsed, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(parsed, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("domain", ["Freight operations", "Weekend cooking"])
def test_scale_indexes_concurrent_roundtrip_preserves_history(scale_engine, monkeypatch, domain):
    statements = []
    event.listen(scale_engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    with scale_engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
                                "kind TEXT, status TEXT, cancel_requested BOOLEAN, priority INTEGER, created_at TIMESTAMP, started_at TIMESTAMP)"))
        connection.execute(text("CREATE TABLE gtm_email_events (email_account_id TEXT, event_type TEXT, created_at TIMESTAMP)"))
        connection.execute(text("INSERT INTO jobs (id, workspace_id, kind, status, cancel_requested, priority, created_at) "
                                "VALUES (:domain, 'workspace', 'chat.respond', 'pending', false, 1, CURRENT_TIMESTAMP)"), {"domain": domain})
    with scale_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            with context.begin_transaction():
                migration.upgrade()
            connection.commit()
            with context.begin_transaction():
                migration.upgrade()
            connection.commit()
            assert connection.scalar(text("SELECT id FROM jobs")) == domain
            from tests.company_os_harness import load_company_os_app

            load_company_os_app()
            from app.core.jobs import CLAIMABLE, INTERACTIVE_KINDS
            from app.db.models import Job

            class_order = case((Job.kind.in_([literal(kind, literal_execute=True) for kind in sorted(INTERACTIVE_KINDS)]),
                                literal(0, literal_execute=True)), else_=literal(1, literal_execute=True))
            query = select(Job.id).where(func.coalesce(Job.workspace_id, Job.venture_id, Job.id) == "workspace",
                Job.status.in_([literal(status, literal_execute=True) for status in sorted(CLAIMABLE)]),
                Job.cancel_requested.is_(False)).order_by(class_order, Job.priority, Job.created_at, Job.id).limit(1)
            sql = str(query.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
            if connection.dialect.name == "sqlite":
                plan = str(connection.execute(text("EXPLAIN QUERY PLAN " + sql)).all())
                assert "ix_jobs_workspace_claim" in plan and "TEMP B-TREE" not in plan
            if connection.dialect.name == "postgresql":
                assert connection.scalar(text("SELECT count(*) FROM pg_index WHERE indrelid = 'jobs'::regclass AND NOT indisvalid")) == 0
            connection.commit()
            with context.begin_transaction():
                migration.downgrade()
            connection.commit()
            assert connection.scalar(text("SELECT id FROM jobs")) == domain
    creates = [statement for statement in statements if statement.startswith("CREATE INDEX")]
    assert len(creates) == 8
    assert all(("CONCURRENTLY" in statement) == (scale_engine.dialect.name == "postgresql") for statement in creates)
    assert migration.down_revision == "s058memsearch"


@pytest.mark.parametrize("domain", ["Industrial calibration", "Personal running"])
def test_postgres_downgrade_keeps_history_guards_transactional(tmp_path, monkeypatch, domain):
    from types import SimpleNamespace

    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
            "kind TEXT, status TEXT, cancel_requested BOOLEAN, priority INTEGER, created_at TIMESTAMP, started_at TIMESTAMP)"))
        connection.execute(text("CREATE TABLE gtm_email_events (email_account_id TEXT, event_type TEXT, created_at TIMESTAMP)"))
        connection.execute(text("CREATE TABLE qa_runs (id TEXT PRIMARY KEY, runtime_version INTEGER)"))
        connection.execute(text("INSERT INTO qa_runs VALUES (:domain, 1)"), {"domain": domain})
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('s062spend')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
    guard_spec = importlib.util.spec_from_file_location("qa_history_guard",
        ROOT / "services/company_os/app/db/migrations/versions/s008_static_qa_receipts.py")
    guard = importlib.util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guard)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
            bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                scalar=connection.scalar, execute=connection.execute)
            monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
            monkeypatch.setattr(context, "autocommit_block", lambda: pytest.fail("downgrades must not commit before history guards"))
            connection.exec_driver_sql("BEGIN")
            with Operations.context(context), pytest.raises(RuntimeError, match="managed_qa_history_requires_forward_migration"):
                connection.execute(text("UPDATE alembic_version SET version_num='s059scaleidx'"))
                migration.downgrade()
                guard.downgrade()
            connection.rollback()
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s062spend"
            assert connection.scalar(text("SELECT id FROM qa_runs")) == domain
            assert connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='provider_admission_fences'")) == 1
            assert connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='index' AND name LIKE 'ix_jobs_workspace_%'")) == 3
    finally:
        engine.dispose()


def test_retry_rebuilds_invalid_concurrent_indexes(scale_engine):
    from sqlalchemy.exc import IntegrityError

    if scale_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent index validation")
    with scale_engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
            "kind TEXT, status TEXT, cancel_requested BOOLEAN, priority INTEGER, created_at TIMESTAMP, started_at TIMESTAMP)"))
        connection.execute(text("CREATE TABLE gtm_email_events (email_account_id TEXT, event_type TEXT, created_at TIMESTAMP)"))
        for domain in ("Industrial calibration", "Personal running"):
            for number in range(2):
                connection.execute(text("INSERT INTO jobs VALUES (:id,:domain,NULL,'chat.respond','pending',"
                    "false,1,CURRENT_TIMESTAMP,NULL)"), {"id": f"{domain}:{number}", "domain": domain})
    with scale_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.autocommit_block():
            with pytest.raises(IntegrityError):
                connection.execute(text("CREATE UNIQUE INDEX CONCURRENTLY ix_jobs_workspace_claim ON jobs (workspace_id)"))
            assert connection.scalar(text("SELECT NOT indisvalid FROM pg_index "
                "WHERE indexrelid='ix_jobs_workspace_claim'::regclass"))
        connection.commit()
        with Operations.context(context), context.begin_transaction():
            migration.upgrade()
        connection.commit()
        assert connection.scalar(text("SELECT indisvalid AND NOT indisunique FROM pg_index "
            "WHERE indexrelid='ix_jobs_workspace_claim'::regclass"))
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 4
