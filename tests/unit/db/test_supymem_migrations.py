"""Supymem foundation migration upgrades and downgrades on SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tests.integration.test_postgres_auth_security import service_modules
from tests.unit.db.test_scale_indexes import scale_engine as scale_engine


HISTORY_DOWNGRADES = (
    ("s057inboxbrief", "inbox_briefing_state", "inbox_briefing_history_prevents_downgrade"),
    ("s057inboxbrief", "inbox_briefing_items", "inbox_briefing_history_prevents_downgrade"),
    ("s058memsearch", "memory_core_profiles", "memory_search_history_prevents_downgrade"),
    ("s058memsearch", "memory_search_jobs", "memory_search_history_prevents_downgrade"),
    ("s058memsearch", "memory_embedding_cache", "memory_search_history_prevents_downgrade"),
    ("s058memsearch", "venture_facts", "memory_search_history_prevents_downgrade"),
    ("s059scaleidx", "provider_admission_fences", "provider_admission_active_prevents_downgrade"),
    ("s061codingconsent", "coding_agent_consent", "coding_agent_consent_history_prevents_downgrade"),
)


def _late_migrations():
    import importlib.util

    migrations = []
    for revision in ("s057inboxbrief", "s058memsearch", "s059scaleidx", "s061codingconsent", "s062spend"):
        spec = importlib.util.spec_from_file_location(revision, Path(__file__).resolve().parents[3] /
            f"services/company_os/app/db/migrations/versions/{revision}.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migrations.append(migration)
    return migrations


def _seed_late_history(connection, table, domain):
    import json
    from datetime import datetime
    from sqlalchemy import text

    scope = {"workspace": "ws_task_migrate", "venture": "ven_task_migrate", "domain": domain}
    if table == "coding_agent_consent":
        from app.coding_agents_consent import TABLES
        connection.execute(TABLES[0].insert().values(id=domain, founder_id=domain, provider="codex",
            consented_at=datetime(2026, 10, 1)))
    elif table.startswith("inbox_briefing_"):
        from app.inbox.store import TABLES
        values = dict(id=domain, founder_id=domain, workspace_id=scope["workspace"], mailbox_id=domain)
        if table == "inbox_briefing_items":
            values.update(message_id=domain, sender_email="fixture@example.invalid", subject=domain,
                snippet=domain, received_at=datetime(2026, 10, 1), category="reply", reason=domain)
        connection.execute(next(item for item in TABLES if item.name == table).insert().values(**values))
    elif table == "venture_facts":
        connection.execute(text("INSERT INTO venture_facts "
            "(id,workspace_id,venture_id,person_id,subject,predicate,value,value_json,source_json,source_digest,"
            "dedupe_key,confidence,valid_from,recorded_at) VALUES "
            "(:domain,:workspace,:venture,:domain,'topic','interest',:domain,:value,'{}',:domain,:domain,1,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {**scope, "value": json.dumps(domain)})
    elif table == "memory_embedding_cache":
        connection.execute(text("INSERT INTO memory_embedding_cache VALUES "
            "(:workspace,:venture,'fixture',:domain,'[0.1]')"), scope)
    elif table == "provider_admission_fences":
        connection.execute(text("INSERT INTO provider_admission_fences (scope,token) VALUES (:domain,:domain)"), scope)
    else:
        connection.execute(text(f"INSERT INTO {table} (workspace_id,venture_id,person_id) "
            "VALUES (:workspace,:venture,:domain)"), scope)


@pytest.mark.parametrize("revision,table,error", HISTORY_DOWNGRADES)
@pytest.mark.parametrize("domain", ["Industrial sensor maintenance", "Home birdwatching"])
def test_late_history_downgrade_refusal_preserves_rows_stamp_schema_and_indexes(tmp_path, revision, table, error, domain):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    migrations = _late_migrations()
    engine = create_engine(f"sqlite:///{tmp_path / 'late-history.db'}")
    selected = next(index for index, migration in enumerate(migrations) if migration.revision == revision)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.execute(text("CREATE TABLE workspaces (id TEXT PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE ventures (id TEXT PRIMARY KEY)"))
            connection.execute(text("INSERT INTO workspaces VALUES ('ws_task_migrate')"))
            connection.execute(text("INSERT INTO ventures VALUES ('ven_task_migrate')"))
            connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
                "kind TEXT, status TEXT, cancel_requested BOOLEAN, priority INTEGER, created_at TIMESTAMP, started_at TIMESTAMP)"))
            connection.execute(text("INSERT INTO jobs (id,kind,status) VALUES (:domain,'fixture','pending')"), {"domain": domain})
            connection.execute(text("CREATE TABLE gtm_email_events (email_account_id TEXT, event_type TEXT, created_at TIMESTAMP)"))
            connection.execute(text("CREATE TABLE credit_ledger (account_id TEXT, created_at TEXT, amount_micro INTEGER)"))
            connection.execute(text("INSERT INTO credit_ledger VALUES (:domain,'2026-10-01',-25)"), {"domain": domain})
            connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('s062spend')"))
            for migration in migrations:
                migration.upgrade()
            schema = connection.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all()
            for migration in reversed(migrations[selected:]):
                migration.downgrade()
                connection.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": migration.down_revision})
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == migrations[selected].down_revision
            assert connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name=:table"), {"table": table}) == 0
            for migration in migrations[selected:]:
                migration.upgrade()
                connection.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": migration.revision})
            assert connection.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all() == schema
            _seed_late_history(connection, table, domain)
            rows = connection.execute(text(f"SELECT * FROM {table}")).all()
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
            with Operations.context(context), pytest.raises(RuntimeError, match=error):
                for migration in reversed(migrations[selected:]):
                    migration.downgrade()
                    connection.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": migration.down_revision})
            connection.rollback()
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s062spend"
            assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows
            assert connection.execute(text("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")).all() == schema
            assert connection.scalar(text("SELECT id FROM jobs")) == domain
            assert connection.execute(text("SELECT * FROM credit_ledger")).all() == [(domain, "2026-10-01", -25)]
    finally:
        engine.dispose()


@pytest.mark.parametrize("revision,table,error", HISTORY_DOWNGRADES)
@pytest.mark.parametrize("domain", ["Industrial sensor maintenance", "Home birdwatching"])
@pytest.mark.parametrize("populated", [False, True])
def test_postgres_history_guards_lock_before_check_without_autocommit(monkeypatch, revision, table, error, domain, populated):
    from types import SimpleNamespace
    import sqlalchemy as sa

    migration = next(item for item in _late_migrations() if item.revision == revision)
    statements = []

    def execute(statement):
        statement = str(statement)
        statements.append(statement)
        if statement.startswith("LOCK TABLE"):
            return None
        assert statements[0].startswith("LOCK TABLE ")
        assert "IN ACCESS EXCLUSIVE MODE" in statements[0]
        assert table in statements[0].split(" IN ")[0].replace(",", "").split()
        if statement.startswith(("DROP", "ALTER")):
            assert not populated
            return None
        assert statement.startswith("SELECT")
        has_rows = populated and f"FROM {table}" in statement
        return SimpleNamespace(first=lambda: (domain,) if has_rows else None, scalar=lambda: has_rows)

    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=execute,
        scalar=lambda statement: execute(statement).scalar())
    monkeypatch.setattr(sa, "inspect", lambda bind: SimpleNamespace(get_table_names=lambda: [item[1] for item in HISTORY_DOWNGRADES]))
    monkeypatch.setattr(sa.Table, "drop", lambda item, bind, **kwargs: execute(f"DROP TABLE {item.name}"))
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda: connection, execute=execute,
        get_context=lambda: SimpleNamespace(dialect=connection.dialect,
            autocommit_block=lambda: pytest.fail("rollback guards must not release locks before DDL"))))
    if populated:
        with pytest.raises(RuntimeError, match=error):
            migration.downgrade()
        assert not any(statement.startswith(("DROP", "DELETE", "COMMIT")) for statement in statements)
    else:
        migration.downgrade()
        assert any(statement == f"DROP TABLE {table}" for statement in statements)
        assert not any(statement.startswith(("DELETE", "COMMIT")) for statement in statements)


@pytest.mark.parametrize("revision,index,table,columns", [
    ("s059scaleidx", "ix_jobs_workspace_claim", "jobs", "id"),
    ("s059scaleidx", "ix_jobs_workspace_inflight", "jobs", "id"),
    ("s059scaleidx", "ix_jobs_workspace_rotation", "jobs", "id"),
    ("s059scaleidx", "ix_gtm_email_events_mailbox_type_latest", "gtm_email_events", "event_type"),
    ("s062spend", "ix_credit_ledger_account_created", "credit_ledger", "created_at, account_id"),
])
@pytest.mark.parametrize("domain", ["Industrial sensor maintenance", "Home birdwatching"])
def test_valid_wrong_index_definition_refuses_upgrade(tmp_path, monkeypatch, revision, index, table, columns, domain):
    from types import SimpleNamespace
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    migration = next(item for item in _late_migrations() if item.revision == revision)
    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-index.db'}")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
                "kind TEXT, status TEXT, cancel_requested BOOLEAN, priority INTEGER, created_at TIMESTAMP, started_at TIMESTAMP)"))
            connection.execute(text("INSERT INTO jobs (id,kind,status) VALUES (:domain,'fixture','pending')"), {"domain": domain})
            connection.execute(text("CREATE TABLE gtm_email_events (email_account_id TEXT, event_type TEXT, created_at TIMESTAMP)"))
            connection.execute(text("CREATE TABLE credit_ledger (account_id TEXT, created_at TEXT, amount_micro INTEGER)"))
            connection.execute(text("INSERT INTO credit_ledger VALUES (:domain,'2026-10-01',-25)"), {"domain": domain})
            connection.execute(text(f"CREATE INDEX {index} ON {table} ({columns})"))
            definition = connection.scalar(text("SELECT sql FROM sqlite_master WHERE name=:index"), {"index": index})
            with pytest.raises(RuntimeError, match="index_definition_mismatch"):
                migration.upgrade()
            assert connection.scalar(text("SELECT sql FROM sqlite_master WHERE name=:index"), {"index": index}) == definition
            assert connection.scalar(text("SELECT id FROM jobs")) == domain
            assert connection.execute(text("SELECT * FROM credit_ledger")).all() == [(domain, "2026-10-01", -25)]
    finally:
        engine.dispose()

    expected = (next(definition for item, definition in zip(migration.INDEXES, migration._POSTGRES_DEFINITIONS)
        if item[0] == index) if revision == "s059scaleidx" else "btree (account_id, created_at)")
    correct = f"CREATE INDEX {index} ON public.{table} USING {expected}".replace(" IS false", " IS FALSE")
    variants = [
        (True, f"CREATE INDEX {index} ON public.{table} USING btree ({columns})"),
        (False, correct.replace(f"public.{table}", "public.other_history")),
        (True, correct.replace("CREATE INDEX", "CREATE UNIQUE INDEX")),
        (True, correct.replace(" WHERE ", " INCLUDE (id) WHERE ") if " WHERE " in correct else correct + " INCLUDE (id)"),
        (True, correct.partition(" WHERE ")[0] + " WHERE false"),
    ]
    if "'chat.respond'" in correct:
        variants.extend((True, correct.replace("'chat.respond'", value))
                        for value in ("'CHAT.RESPOND'", "'chat. respond'"))
    for expected_table, definition in [(True, correct), *variants]:
        def catalog(statement, parameters=None):
            assert "pg_get_indexdef(indexrelid)" in str(statement)
            return SimpleNamespace(first=lambda: (True, expected_table, definition))

        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=catalog)
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        if definition == correct and expected_table:
            assert (migration._index_valid(index, table, expected) if revision == "s059scaleidx"
                    else migration._index_valid()) is True
        else:
            with pytest.raises(RuntimeError, match="index_definition_mismatch"):
                if revision == "s059scaleidx":
                    migration._index_valid(index, table, expected)
                else:
                    migration._index_valid()


def test_memory_search_migration_does_not_rewrite_existing_memories(scale_engine):
    import importlib.util
    import json
    from sqlalchemy import event, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    if scale_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL heap rewrite and concurrent index validation")
    spec = importlib.util.spec_from_file_location("memory_online_migration", Path(__file__).resolve().parents[3] /
        "services/company_os/app/db/migrations/versions/s058memsearch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with scale_engine.begin() as connection:
        connection.execute(text("CREATE TABLE workspaces (id TEXT PRIMARY KEY, owner_id TEXT)"))
        connection.execute(text("CREATE TABLE ventures (id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE credit_policies (key TEXT PRIMARY KEY, value_json TEXT)"))
        connection.execute(text("CREATE TABLE institutional_memories (id TEXT PRIMARY KEY, workspace_id TEXT, "
            "venture_id TEXT, record_json TEXT, dedupe_key TEXT, version INTEGER NOT NULL DEFAULT 1)"))
        for table in ("chat_messages", "chat_conversations", "idea_revisions", "clarifier_turns"):
            connection.execute(text(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, venture_id TEXT)"))
        for domain in ("Factory scheduling", "Personal photography"):
            for number in range(40):
                identity = f"{domain}:{number}"
                connection.execute(text("INSERT INTO institutional_memories (id,workspace_id,venture_id,record_json,dedupe_key) "
                    "VALUES (:id,'workspace','venture',:record,:id)"), {"id": identity, "record": json.dumps({
                        "subject": "audience", "statement": domain, "author": "person", "kind": "insight"})})
        before = connection.scalar(text("SELECT relfilenode FROM pg_class WHERE oid='institutional_memories'::regclass"))
    indexes = []

    def record_index(connection, cursor, statement, parameters, context, many):
        if statement.startswith("CREATE INDEX") and "institutional_memories" in statement:
            assert "CONCURRENTLY" in statement
            assert connection.connection.driver_connection.autocommit
            indexes.append(statement)

    event.listen(scale_engine, "before_cursor_execute", record_index)
    with scale_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration.upgrade()
        connection.commit()
        assert connection.scalar(text("SELECT relfilenode FROM pg_class WHERE oid='institutional_memories'::regclass")) == before
        assert "search_document" not in {column["name"] for column in inspect(connection).get_columns("institutional_memories")}
        assert connection.scalar(text("SELECT count(*) FROM institutional_memories WHERE NOT report_subjects_ready")) == 80
        assert connection.scalar(text("SELECT count(*) FROM pg_index WHERE indrelid='institutional_memories'::regclass AND NOT indisvalid")) == 0
        assert any("to_tsvector" in index for index in indexes)


@pytest.fixture(autouse=True)
def _current_company_os_modules():
    # Editable primary checkouts and prior service tests must not supply the
    # modules executed by this checkout's migration environment.
    with service_modules("company_os"):
        yield


def test_routines_migration_preserves_both_founders_and_refuses_history_loss():
    from runpy import run_path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    migration = run_path(str(Path(__file__).resolve().parents[3] /
        "services/company_os/app/db/migrations/versions/s063routines.py"))
    assert migration["revision"] == "s063routines" and migration["down_revision"] == "s062spend"
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.execute(text("CREATE TABLE tasks (id TEXT PRIMARY KEY, workspace_id TEXT, venture_id TEXT, "
                "owner_person_id TEXT, kind TEXT, create_key TEXT, goal TEXT)"))
            history = [("stock", "Warehouse forecasting"), ("recipe", "Household meal planning")]
            for identity, domain in history:
                connection.execute(text("INSERT INTO tasks VALUES (:id, :id, :id, :id, 'scheduled', :key, :goal)"),
                    {"id": identity, "key": f"routine:{identity}", "goal": domain})
            connection.execute(text("INSERT INTO tasks VALUES ('ordinary', 'other', 'other', 'other', 'one_time', NULL, 'Saved task')"))
            migration["upgrade"]()
            migration["upgrade"]()
            before = connection.execute(text("SELECT * FROM tasks ORDER BY id")).all()
            with pytest.raises(RuntimeError, match="routine_history_prevents_downgrade"):
                migration["downgrade"]()
            assert connection.execute(text("SELECT * FROM tasks ORDER BY id")).all() == before
            assert "ix_tasks_owner_schedule" in {index["name"] for index in inspect(connection).get_indexes("tasks")}
            connection.execute(text("UPDATE tasks SET create_key = 'routine-run:' || id WHERE id != 'ordinary'"))
            with pytest.raises(RuntimeError, match="routine_history_prevents_downgrade"):
                migration["downgrade"]()
            connection.execute(text("DELETE FROM tasks WHERE id != 'ordinary'"))
            migration["downgrade"]()
            assert connection.scalar(text("SELECT count(*) FROM tasks")) == 1
            assert "ix_tasks_owner_schedule" not in {index["name"] for index in inspect(connection).get_indexes("tasks")}
    finally:
        engine.dispose()


def test_coding_consent_migration_follows_scale_head_without_changing_unrelated_history(monkeypatch):
    from runpy import run_path
    from alembic import op
    from sqlalchemy import create_engine, inspect, text

    migration = run_path(str(Path(__file__).resolve().parents[3] /
                             "services/company_os/app/db/migrations/versions/s061codingconsent.py"))
    assert migration["revision"] == "s061codingconsent"
    assert migration["down_revision"] == "s059scaleidx"
    engine = create_engine("sqlite://")
    history = [("usr_stock_planner", "Inventory forecasts"), ("usr_recipe_club", "Family recipes")]
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(op, "get_bind", lambda: connection)
            connection.execute(text("CREATE TABLE unrelated_history (founder_id TEXT PRIMARY KEY, record TEXT)"))
            for founder_id, record in history:
                connection.execute(text("INSERT INTO unrelated_history VALUES (:founder_id, :record)"),
                                   {"founder_id": founder_id, "record": record})
            migration["upgrade"]()
            assert "coding_agent_consent" in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT founder_id, record FROM unrelated_history ORDER BY founder_id DESC")).all() == history
            migration["downgrade"]()
            assert "coding_agent_consent" not in inspect(connection).get_table_names()
            assert connection.execute(text("SELECT founder_id, record FROM unrelated_history ORDER BY founder_id DESC")).all() == history
    finally:
        engine.dispose()


def _alembic_cfg(url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "services" / "company_os" / "alembic.ini"))
    cfg.set_main_option(
        "script_location",
        str(root / "services" / "company_os" / "app" / "db" / "migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.mark.parametrize("domain", ["Freight brokerage", "Family recipe planning"])
@pytest.mark.parametrize("custom_directory", [False, True])
@pytest.mark.parametrize("async_engine", [False, True])
def test_local_sqlite_first_start_migrates_and_preserves_append_only_history(
    tmp_path, monkeypatch, domain, custom_directory, async_engine,
):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone

    from app.db.models import APPEND_ONLY_TABLES, Venture
    from app.db.session import make_async_engine, make_engine, resolve_database_url
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from sqlalchemy import create_engine, inspect, select, text
    from sqlalchemy.exc import IntegrityError
    from app.core import local_secrets

    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {}})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "SUPYMEM_DATA_DIR", "COMPANY_OS_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    from app.config import data_root

    directory = data_root()
    if custom_directory:
        monkeypatch.setenv("SUPYMEM_DATA_DIR", "~/data 100%/" + domain)
        directory = tmp_path / "data 100%" / domain
    path = directory / "database.sqlite3"
    factory = make_async_engine if async_engine else make_engine
    with ThreadPoolExecutor(max_workers=2) as startup:
        first = startup.submit(factory)
        second = startup.submit(factory)
        engines = [first.result(timeout=90), second.result(timeout=90)]
    for engine in engines:
        try:
            assert Path(engine.url.database) == path
            assert path.is_file()
            assert directory.stat().st_mode & 0o077 == 0
        finally:
            if async_engine:
                asyncio.run(engine.dispose())
            else:
                engine.dispose()

    raw_engine = create_engine(resolve_database_url())
    try:
        with raw_engine.begin() as connection:
            from tests.integration.test_postgres_auth_security import migration_head

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == migration_head()
            guarded = APPEND_ONLY_TABLES | {
                "idea_revisions", "build_events", "agent_runtime_steps", "app_usage_records",
            }
            triggers = set(connection.scalars(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
            assert {f"{table}_no_{operation}" for table in guarded for operation in ("update", "delete")} <= triggers
            assert "idea_revisions" in inspect(connection).get_table_names()
            now = datetime(2026, 11, 1, tzinfo=timezone.utc)
            connection.execute(Venture.__table__.insert().values(
                id=domain, name=domain, raw_idea=domain, stage="draft", created_at=now, updated_at=now,
            ))
            connection.execute(IdeaSession.__table__.insert().values(
                id=domain, workspace_id=domain, venture_id=domain, case="know", created_at=now, updated_at=now,
            ))
            connection.execute(IdeaRevision.__table__.insert().values(
                id=domain, session_id=domain, workspace_id=domain, venture_id=domain,
                rev=1, source="founder", summary=domain, created_at=now,
            ))
        for statement in ("UPDATE idea_revisions SET summary='changed'", "DELETE FROM idea_revisions"):
            with pytest.raises(IntegrityError, match="append.only"), raw_engine.begin() as connection:
                connection.execute(text(statement))
        restarted = factory()
        if async_engine:
            asyncio.run(restarted.dispose())
        else:
            restarted.dispose()
        with raw_engine.begin() as connection:
            assert connection.scalar(select(IdeaRevision.summary)) == domain
            connection.execute(IdeaRevision.__table__.insert().values(
                id=domain + "-next", session_id=domain, workspace_id=domain, venture_id=domain,
                rev=2, source="founder", summary=domain, created_at=now,
            ))
            assert connection.scalar(text("SELECT count(*) FROM idea_revisions")) == 2
    finally:
        raw_engine.dispose()


@pytest.mark.parametrize("domain", ["Freight brokerage", "Family recipe planning"])
@pytest.mark.parametrize("explicit_url", [False, True])
def test_local_alembic_migrates_fresh_file_with_append_only_guards(tmp_path, monkeypatch, domain, explicit_url):
    from app.db.session import resolve_database_url
    from sqlalchemy import create_engine, text

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path / domain))
    monkeypatch.delenv("COMPANY_OS_DATA_DIR", raising=False)
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL"):
        monkeypatch.delenv(name, raising=False)
    if explicit_url:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.db'}")
    url = resolve_database_url()
    assert url
    command.upgrade(_alembic_cfg(url), "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            triggers = set(connection.scalars(text("SELECT name FROM sqlite_master WHERE type='trigger'")))
            assert {"idea_revisions_no_update", "idea_revisions_no_delete"} <= triggers
    finally:
        engine.dispose()


@pytest.mark.parametrize("domain", ["Freight brokerage", "Family recipe planning"])
def test_local_sqlite_failed_migration_preserves_existing_data(tmp_path, monkeypatch, domain):
    from app.cli_local import LocalMaintenanceError
    from app.db.session import make_engine
    from sqlalchemy import create_engine, text

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    url = f"sqlite:///{tmp_path / 'existing.db'}"
    raw_engine = create_engine(url)
    with raw_engine.begin() as connection:
        connection.execute(text("CREATE TABLE existing_history (domain TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO existing_history VALUES (:domain)"), {"domain": domain})

    def fail_migration(config, revision):
        assert revision == "head"
        config.attributes["connection"].execute(text("DELETE FROM existing_history"))
        raise RuntimeError("fixture migration failure")

    monkeypatch.setattr(command, "upgrade", fail_migration)
    try:
        with pytest.raises(LocalMaintenanceError, match="automatic backup was restored"):
            make_engine(url)
        with raw_engine.connect() as connection:
            assert connection.scalar(text("SELECT domain FROM existing_history")) == domain
    finally:
        raw_engine.dispose()


@pytest.mark.parametrize("url_name", ["COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL"])
def test_local_postgres_url_keeps_existing_driver_and_pooling(tmp_path, monkeypatch, url_name):
    import asyncio

    from app.db.session import make_async_engine, make_engine, resolve_database_url

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path / "unused"))
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    url = "postgresql://127.0.0.1/local_fixture"
    monkeypatch.setenv(url_name, url)
    assert resolve_database_url() == url
    sync_engine, async_engine = make_engine(), make_async_engine()
    try:
        assert sync_engine.url.drivername == "postgresql+psycopg"
        assert async_engine.url.drivername == "postgresql+psycopg_async"
        assert sync_engine.pool.size() == async_engine.pool.size()
        assert not (tmp_path / "unused").exists()
    finally:
        sync_engine.dispose()
        asyncio.run(async_engine.dispose())


def test_memory_search_upgrade_is_additive_idempotent_and_refuses_fact_loss(tmp_path):
    import importlib.util
    import pytest
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("memory_search_migration", Path(__file__).resolve().parents[3] /
        "services/company_os/app/db/migrations/versions/s058memsearch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path}/memory-search.db")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        connection.execute(text("CREATE TABLE workspaces(id TEXT PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE ventures(id TEXT PRIMARY KEY)"))
        migration.upgrade()
        migration.upgrade()
        assert {"memory_core_profiles", "memory_search_jobs", "memory_embedding_cache", "venture_facts"} <= set(inspect(connection).get_table_names())
        for identity, domain in (("logistics", "Freight dispatch"), ("gardens", "Balcony seeds")):
            connection.execute(text("""INSERT INTO venture_facts
                (id,workspace_id,venture_id,person_id,subject,predicate,value,value_json,source_json,source_digest,dedupe_key,
                confidence,valid_from,recorded_at) VALUES (:id,:id,:id,:id,'idea.audience','is',:value,'{}','{}','hash',:id,
                1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""), {"id": identity, "value": domain})
        with pytest.raises(RuntimeError, match="memory_search_history_prevents_downgrade"):
            migration.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM venture_facts")) == 2
    engine.dispose()


def test_memory_search_missing_vector_extension_keeps_fulltext(caplog, monkeypatch):
    import importlib.util
    from contextlib import nullcontext
    from types import SimpleNamespace
    from sqlalchemy.exc import DBAPIError
    spec = importlib.util.spec_from_file_location("memory_search_migration", Path(__file__).resolve().parents[3] /
        "services/company_os/app/db/migrations/versions/s058memsearch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements = []
    class Connection:
        dialect = SimpleNamespace(name="postgresql")
        def begin_nested(self):
            return nullcontext()
        def execute(self, statement):
            statements.append(str(statement))
            if "CREATE EXTENSION" in str(statement):
                raise DBAPIError(str(statement), {}, RuntimeError("extension absent"))
        def scalar(self, statement, parameters):
            return False
    monkeypatch.setattr(migration.op, "get_bind", lambda: Connection())
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(autocommit_block=nullcontext))
    migration.upgrade()
    assert any("USING gin" in statement for statement in statements)
    assert not any("USING hnsw" in statement for statement in statements)
    assert "memory_search_vector_unavailable_fulltext_only" in caplog.text


def test_memory_search_content_updates_invalidate_background_embeddings(monkeypatch):
    import importlib.util
    from contextlib import nullcontext
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("memory_search_migration", Path(__file__).resolve().parents[3] /
        "services/company_os/app/db/migrations/versions/s058memsearch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements = []
    class Connection:
        dialect = SimpleNamespace(name="postgresql")
        def begin_nested(self):
            return nullcontext()
        def execute(self, statement):
            statements.append(str(statement))
        def scalar(self, statement, parameters):
            return False
    monkeypatch.setattr(migration.op, "get_bind", lambda: Connection())
    monkeypatch.setattr(migration.op, "get_context", lambda: SimpleNamespace(autocommit_block=nullcontext))
    migration.upgrade()
    assert sum("USING hnsw" in statement for statement in statements) == 2
    assert any("NEW.embedding := NULL; NEW.embedding_hash := NULL" in statement for statement in statements)
    assert any("BEFORE UPDATE OF value,subject,source_json ON venture_facts" in statement for statement in statements)
    assert any("BEFORE UPDATE OF record_json ON institutional_memories" in statement for statement in statements)


def test_composio_reconnect_status_migration_preserves_unrelated_accounts(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path / 'composio-project.db'}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s054leadlibrary")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for account_id, domain in (("dispatch", "Warehouse planning"), ("trails", "Trail journaling")):
                connection.execute(text("INSERT INTO content_accounts "
                    "(id,workspace_id,platform,external_account,status,scopes_json,extra_json,daily_cap,posts_today,health,created_at,updated_at) "
                    "VALUES (:id,:id,'linkedin',:domain,'active','[]','{}',3,0,'ok',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
                    {"id": account_id, "domain": domain})
        command.upgrade(cfg, "head")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s063routines"
            connection.execute(text("UPDATE content_accounts SET status='needs_reconnect'"))
        command.downgrade(cfg, "s054leadlibrary")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT external_account,status FROM content_accounts ORDER BY id")).all() == [
                ("Warehouse planning", "expired"), ("Trail journaling", "expired")]
    finally:
        engine.dispose()


def test_default_off_memory_does_not_advertise_unmigrated_fact_schema(monkeypatch):
    monkeypatch.setenv("COMPANY_OS_MEMORY_FACTS_ENABLED", "0")
    from app.db.models import ALL_TABLE_NAMES, Base
    from alembic.script import ScriptDirectory
    assert "venture_facts" not in ALL_TABLE_NAMES and "venture_facts" not in Base.metadata.tables
    assert len(ScriptDirectory.from_config(_alembic_cfg("sqlite://")).get_heads()) == 1


@pytest.mark.parametrize("upgrade_path", ["alembic", "runtime"])
def test_multi_gmail_upgrade_keeps_history_and_legacy_dedupe_with_append_only_guards(tmp_path, monkeypatch, upgrade_path):
    from sqlalchemy import MetaData, Table, create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError
    from core.clock import utcnow

    url = f"sqlite:///{tmp_path / ('multi-gmail-' + upgrade_path + '.db')}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s055composioproject")
    engine = create_engine(url)
    now = utcnow()
    contacts = Table("gtm_contacts", MetaData(), autoload_with=engine)
    touches = Table("gtm_touches", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        for contact_id, email in (("tools", "buyer@buildtools.example"), ("recipes", "cook@homecooking.example")):
            connection.execute(contacts.insert().values(id=contact_id, workspace_id="workspace", email=email,
                                                        stage_changed_at=now, created_at=now))
            connection.execute(touches.insert().values(id="old-" + contact_id, contact_id=contact_id, direction="out",
                                                       subject=email, simulated=0, created_at=now))
        original = connection.execute(text("SELECT id,contact_id,subject FROM gtm_touches ORDER BY id")).all()
        for table in ("gtm_touches", "gtm_email_events", "connection_events", "credit_ledger"):
            connection.execute(text(f"CREATE TRIGGER test_{table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'history is append-only'); END"))
    if upgrade_path == "alembic":
        command.upgrade(cfg, "s056multigmail")
    else:
        from app.gtm.supymem.schema import ensure_supymem_schema
        ensure_supymem_schema(engine)
        ensure_supymem_schema(engine)
    try:
        for table, column in (("workspaces", "default_gmail_mailbox_id"), ("ventures", "default_gmail_mailbox_id"),
                              ("gtm_touches", "email_account_id"), ("content_oauth_states", "gmail_intent"),
                              ("content_oauth_states", "gmail_mailbox_id")):
            assert next(value for value in inspect(engine).get_columns(table) if value["name"] == column)["nullable"]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id,contact_id,subject FROM gtm_touches ORDER BY id")).all() == original
            assert connection.scalar(text("SELECT COUNT(*) FROM gtm_touches WHERE email_account_id IS NOT NULL")) == 0
        for mailbox_id in ("tools", "recipes", None):
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO gtm_touches (id,workspace_id,venture_id,contact_id,direction,message_id,email_account_id,created_at) "
                                        "VALUES (:id,'workspace','venture','tools','in','same-message',:mailbox,:now)"),
                                   {"id": "reply-" + str(mailbox_id), "mailbox": mailbox_id, "now": now})
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(text("INSERT INTO gtm_touches (id,workspace_id,venture_id,contact_id,direction,message_id,email_account_id,created_at) "
                                            "VALUES (:id,'workspace','venture','tools','in','same-message',:mailbox,:now)"),
                                       {"id": "duplicate-" + str(mailbox_id), "mailbox": mailbox_id, "now": now})
        with pytest.raises(IntegrityError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(text("UPDATE gtm_touches SET subject='changed' WHERE id='old-tools'"))
        if upgrade_path == "alembic":
            with pytest.raises(RuntimeError, match="must not be discarded"):
                command.downgrade(cfg, "s055composioproject")
    finally:
        engine.dispose()


def test_shared_learning_upgrade_preserves_existing_data_and_has_no_personal_fields(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    url = f"sqlite:///{tmp_path / 'shared-learning.db'}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s049refreshuse")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO desktop_devices (id,person_id,name,platform,refresh_token_hash) VALUES ('existing','person','Existing','macos','digest')"))
    command.upgrade(cfg, "head")
    schema = inspect(engine)
    tables = {"gtm_shared_email_patterns", "gtm_shared_address_validity", "gtm_shared_email_outcomes", "gtm_public_research_cache"}
    assert tables <= set(schema.get_table_names())
    for table in tables - {"gtm_public_research_cache"}:
        assert not {"email", "address", "workspace_id", "name", "draft", "body"} & {column["name"] for column in schema.get_columns(table)}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT refresh_token_hash FROM desktop_devices WHERE id='existing'")) == "digest"
    command.downgrade(cfg, "s049refreshuse")
    assert not tables & set(inspect(engine).get_table_names())
    engine.dispose()


def test_chat_history_migration_preserves_messages_and_limits_folder(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError

    url = f"sqlite:///{tmp_path / 'chat-history.db'}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s050gtmlearning")
    engine = create_engine(url)
    try:
        scope_checks = {check["sqltext"] for check in inspect(engine).get_check_constraints("chat_conversations")}
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO ventures (id,name,raw_idea,stage,autonomy_level,created_at,updated_at) "
                                    "VALUES ('ven_history','History','idea','draft','L1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO chat_conversations (id,venture_id,visibility,title,creation_key,created_at,updated_at) "
                                    "VALUES ('cnv_history','ven_history','legacy_shared','Earlier shared chat','legacy:ven_history',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO chat_messages (id,venture_id,conversation_id,role,kind,body_md,created_at) "
                                    "VALUES ('msg_history','ven_history','cnv_history','user','text','Original message Ω',CURRENT_TIMESTAMP)"))
            original = connection.execute(text("SELECT * FROM chat_messages")).all()
        command.upgrade(cfg, "head")
        columns = {column["name"]: column for column in inspect(engine).get_columns("chat_conversations")}
        assert all(columns[name]["nullable"] for name in ("pinned_at", "archived_at", "folder"))
        assert scope_checks <= {check["sqltext"] for check in inspect(engine).get_check_constraints("chat_conversations")}
        with engine.begin() as connection:
            assert connection.execute(text("SELECT pinned_at,archived_at,folder FROM chat_conversations")).one() == (None, None, None)
            assert connection.execute(text("SELECT * FROM chat_messages")).all() == original
            connection.execute(text("UPDATE chat_conversations SET folder=:folder"), {"folder": "x" * 60})
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(text("UPDATE chat_conversations SET folder=:folder"), {"folder": "x" * 61})
        command.downgrade(cfg, "s050gtmlearning")
        assert not {"pinned_at", "archived_at", "folder"} & {column["name"] for column in inspect(engine).get_columns("chat_conversations")}
        assert scope_checks == {check["sqltext"] for check in inspect(engine).get_check_constraints("chat_conversations")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM chat_messages")).all() == original
    finally:
        engine.dispose()


def test_upgrade_and_downgrade_sqlite(tmp_path, monkeypatch) -> None:
    from app.db.supymem_models import SUPYMEM_TABLE_NAMES

    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "e4f5a6b7c8d9")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "s042walletbridge")
    from sqlalchemy import create_engine, inspect

    engine = create_engine(url)
    assert "lease_expires_at" not in {column["name"] for column in inspect(engine).get_columns("venture_documents")}
    assert "ix_venture_documents_lease" not in {index["name"] for index in inspect(engine).get_indexes("venture_documents")}
    engine.dispose()
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(url))
    names = set(insp.get_table_names())
    missing = SUPYMEM_TABLE_NAMES - names
    assert not missing, missing
    assert {"venture_waitlists", "venture_waitlist_entries", "chat_proposals", "app_runtime_intents"} <= names
    job_cols = {c["name"] for c in insp.get_columns("jobs")}
    assert {"dedupe_key", "locked_by", "workspace_id", "effect_key"} <= job_cols
    res_cols = {c["name"] for c in insp.get_columns("credit_reservations")}
    assert "parent_id" in res_cols


def test_head_downgrade_reaches_managed_qa_history_guard(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path / 'managed-qa.db'}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO qa_runs (id,app_id,build_run_id,workspace_id,venture_id,state,suites,passed,failed,created_at,"
                "runtime_version,managed_build_id,generation,source_artifact_id,source_sha256,source_manifest_digest,"
                "contract_digest,input_digest) VALUES ('qa_guard','app_guard','run_guard','ws_guard','ven_guard',"
                "'queued','[]',0,0,CURRENT_TIMESTAMP,1,'run_guard',1,'source_guard',:digest,:digest,:digest,:digest)"
            ), {"digest": "a" * 64})
        with pytest.raises(RuntimeError, match="managed_qa_history_requires_forward_migration"):
            command.downgrade(cfg, "s007contentgen")
        assert {"ck_qa_managed_receipt", "ck_qa_runtime_counts"} <= {
            constraint["name"] for constraint in inspect(engine).get_check_constraints("qa_runs")}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT input_digest FROM qa_runs WHERE id='qa_guard'")) == "a" * 64
    finally:
        engine.dispose()


def test_deployed_s013_history_converges_to_release_head(tmp_path, monkeypatch) -> None:
    """The recorded production head must upgrade without recreating cleanup DDL."""
    db = tmp_path / "s013.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s013cleanupfromu6")
    command.upgrade(cfg, "head")

    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(cfg)
    assert scripts.get_current_head() == "s063routines"
    assert scripts.get_revision("s063routines").down_revision == "s062spend"
    assert scripts.get_revision("s062spend").down_revision == "s061codingconsent"
    assert scripts.get_revision("s059scaleidx").down_revision == "s058memsearch"
    assert scripts.get_revision("s058memsearch").down_revision == "s057inboxbrief"
    assert scripts.get_revision("s057inboxbrief").down_revision == "s056multigmail"
    assert scripts.get_revision("s056multigmail").down_revision == "s055composioproject"
    assert scripts.get_revision("s055composioproject").down_revision == "s054leadlibrary"
    assert scripts.get_revision("s054leadlibrary").down_revision == "s053waitlistprops"
    assert scripts.get_revision("s053waitlistprops").down_revision == "s052referralrewards"
    assert scripts.get_revision("s052referralrewards").down_revision == "s051chathistory"
    assert scripts.get_revision("s051chathistory").down_revision == "s050gtmlearning"
    assert scripts.get_revision("s049refreshuse").down_revision == "s048gatewayidentities"
    assert scripts.get_revision("s048gatewayidentities").down_revision == "s047credentialepoch"
    assert scripts.get_revision("s047credentialepoch").down_revision == "s046lootdrop"
    assert scripts.get_revision("s046lootdrop").down_revision == "s045tasks"
    assert scripts.get_revision("s045tasks").down_revision == "s044grantthreshold"

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s063routines"
        schema = inspect(engine)
        assert next(index for index in schema.get_indexes("tasks") if index["name"] == "ix_tasks_owner_schedule")["column_names"] == [
            "workspace_id", "venture_id", "owner_person_id", "kind"]
        assert {"lead_library_entries", "lead_library_tags", "lead_library_usage"} <= set(schema.get_table_names())
        entry_columns = {column["name"]: column for column in schema.get_columns("lead_library_entries")}
        assert entry_columns["external_lead_id"]["nullable"] is False
        assert entry_columns["role_title"]["nullable"] is True
        assert entry_columns["imported_at"]["nullable"] is False
        assert entry_columns["fit_score"]["nullable"] is True
        assert entry_columns["search_text"]["nullable"] is False
        assert "TEXT" in str(entry_columns["person_name"]["type"]).upper()
        assert "FLOAT" in str(entry_columns["confidence"]["type"]).upper()
        assert "INT" in str(entry_columns["fit_score"]["type"]).upper()
        assert "DATE" in str(entry_columns["imported_at"]["type"]).upper() or "TIME" in str(entry_columns["imported_at"]["type"]).upper()
        assert any(check["name"] == "ck_lead_library_entry_status" for check in schema.get_check_constraints("lead_library_entries"))
        entry_indexes = {index["name"]: index["column_names"] for index in schema.get_indexes("lead_library_entries")}
        assert entry_indexes["ix_lead_library_entries_type_country"] == ["lead_type", "country_code"]
        assert entry_indexes["ix_lead_library_entries_function_seniority"] == ["function", "seniority"]
        assert entry_indexes["ix_lead_library_entries_status_rank"] == ["status", "fit_score"]
        assert entry_indexes["ix_lead_library_entries_email_hash"] == ["email_hash"]
        assert entry_indexes["ix_lead_library_entries_company_domain"] == ["company_domain"]
        unique_columns = [constraint["column_names"] for constraint in schema.get_unique_constraints("lead_library_entries")]
        unique_indexes = [index["column_names"] for index in schema.get_indexes("lead_library_entries") if index["unique"]]
        assert ["external_lead_id"] in unique_columns + unique_indexes
        assert schema.get_pk_constraint("lead_library_tags")["constrained_columns"] == ["entry_id", "tag_type", "tag"]
        assert {index["name"]: index["column_names"] for index in schema.get_indexes("lead_library_tags")}["ix_lead_library_tags_lookup"] == ["tag_type", "tag", "entry_id"]
        usage_columns = {column["name"]: column for column in schema.get_columns("lead_library_usage")}
        assert usage_columns["workspace_id"]["nullable"] is False
        assert usage_columns["venture_id"]["nullable"] is True
        assert usage_columns["contacted_at"]["nullable"] is False
        usage_indexes = {index["name"]: index["column_names"] for index in schema.get_indexes("lead_library_usage")}
        assert usage_indexes["ix_lead_library_usage_entry_time"] == ["entry_id", "contacted_at"]
        assert usage_indexes["ix_lead_library_usage_workspace"] == ["workspace_id", "venture_id"]
        assert {"chat_conversations", "chat_context_checkpoints", "institutional_memories", "memory_working_entries"} <= set(schema.get_table_names())
        assert {"conversation_id", "author_person_id"} <= {column["name"] for column in schema.get_columns("chat_messages")}
        assert {"installed_at", "qualified_at", "reward_ledger_id"} <= {column["name"] for column in schema.get_columns("beta_code_redemptions")}
        assert "last_nudged_at" in {column["name"] for column in schema.get_columns("person_referral_codes")}
        assert "venture_id" in {column["name"] for column in schema.get_columns("site_versions")}
        touch_columns = {column["name"]: column for column in schema.get_columns("gtm_touches")}
        assert touch_columns["venture_id"]["nullable"] is True
        assert touch_columns["provider_thread_id"]["nullable"] is True
        reply_index = next(index for index in schema.get_indexes("gtm_touches")
                           if index["name"] == "uq_gtm_touch_venture_reply")
        assert reply_index["unique"] == 1
        assert reply_index["column_names"] == ["workspace_id", "venture_id", "contact_id", "message_id"]
        assert "direction = 'in'" in str(reply_index["dialect_options"]["sqlite_where"])
    finally:
        engine.dispose()
    command.downgrade(cfg, "s053waitlistprops")
    downgraded = create_engine(url)
    try:
        assert {"lead_library_entries", "lead_library_tags", "lead_library_usage"}.isdisjoint(inspect(downgraded).get_table_names())
        with downgraded.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "s053waitlistprops"
    finally:
        downgraded.dispose()


def test_federated_identity_migration_keeps_passwords_separate_and_refuses_data_loss(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path}/gateway-identities.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("gateway_identities")}
        assert columns == {"id", "email", "email_verified", "created_at"}
        assert any(index["column_names"] == ["email"] for index in inspect(engine).get_indexes("gateway_identities"))
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO gateway_identities VALUES "
                "('usr_cog_kept', 'kept@example.test', true, '2026-10-12 10:00:00')"))
        with pytest.raises(RuntimeError, match="gateway_identity_history_prevents_downgrade"):
            command.downgrade(cfg, "s047credentialepoch")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT email_verified FROM gateway_identities WHERE id='usr_cog_kept'")) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("domain", ["Inventory forecasting", "Family recipe journal"])
@pytest.mark.parametrize("valid", [None, False, True])
def test_spend_index_is_concurrent_retry_safe_and_preserves_sqlite(tmp_path, monkeypatch, domain, valid):
    import importlib.util
    from contextlib import contextmanager
    from types import SimpleNamespace

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("spend_migration",
        root / "services/company_os/app/db/migrations/versions/s062spend.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "s062spend"
    assert migration.down_revision == "s061codingconsent"
    engine = create_engine(f"sqlite:///{tmp_path}/spend.db")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE credit_ledger (account_id TEXT, created_at TEXT, amount_micro INTEGER)"))
            connection.execute(text("INSERT INTO credit_ledger VALUES (:domain, '2026-10-01', -250000)"), {"domain": domain})
            before = connection.execute(text("SELECT * FROM credit_ledger")).all()
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                indexes = inspect(connection).get_indexes("credit_ledger")
                assert [(index["name"], index["column_names"]) for index in indexes] == [
                    ("ix_credit_ledger_account_created", ["account_id", "created_at"])]
                migration.downgrade()
                assert inspect(connection).get_indexes("credit_ledger") == []
                migration.upgrade()
            assert connection.execute(text("SELECT * FROM credit_ledger")).all() == before
    finally:
        engine.dispose()

    statements, blocks = [], []
    in_autocommit = False
    index_valid = valid

    @contextmanager
    def autocommit_block():
        nonlocal in_autocommit
        assert not in_autocommit
        in_autocommit = True
        blocks.append("enter")
        try:
            yield
        finally:
            blocks.append("exit")
            in_autocommit = False

    def execute(statement):
        nonlocal index_valid
        assert in_autocommit
        statement = str(statement)
        statements.append(statement)
        if statement.startswith("SELECT"):
            assert "SELECT indisvalid," in statement and "FROM pg_index" in statement
            assert "pg_get_indexdef(indexrelid)" in statement
            assert "indrelid = to_regclass('credit_ledger')" in statement
            assert "to_regclass('ix_credit_ledger_account_created')" in statement
            return SimpleNamespace(first=lambda: None if index_valid is None else (index_valid, True,
                "CREATE INDEX ix_credit_ledger_account_created ON public.credit_ledger USING btree (account_id, created_at)"))
        if statement.startswith("DROP"):
            assert statement == "DROP INDEX CONCURRENTLY IF EXISTS ix_credit_ledger_account_created"
            index_valid = None
        else:
            assert statement == ("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_credit_ledger_account_created "
                                 "ON credit_ledger (account_id, created_at)")
            if index_valid is None:
                index_valid = True

    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=execute)
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=lambda: connection,
        get_context=lambda: SimpleNamespace(autocommit_block=autocommit_block), execute=execute))
    migration.upgrade()
    migration.upgrade()
    assert blocks == ["enter", "exit", "enter", "exit"]
    assert [statement.split()[0] for statement in statements] == (
        ["SELECT", "DROP", "CREATE", "SELECT", "SELECT", "CREATE", "SELECT"] if valid is False else
        ["SELECT", "CREATE", "SELECT", "SELECT", "CREATE", "SELECT"])
    assert index_valid is True


def test_credential_epoch_migrates_existing_users_and_guards_rollback(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("credential_epoch_migration",
        root / "services/company_os/app/db/migrations/versions/s047credentialepoch.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path}/epoch.db")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE gateway_users (id TEXT PRIMARY KEY)"))
            connection.execute(text("INSERT INTO gateway_users VALUES ('usr_legacy')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                migration.upgrade()
                assert connection.scalar(text("SELECT session_epoch FROM gateway_users")) == 0
                migration.downgrade()
                migration.upgrade()
                connection.execute(text("UPDATE gateway_users SET session_epoch=1"))
                with pytest.raises(RuntimeError, match="restore_revoked_sessions"):
                    migration.downgrade()
            assert "session_epoch" in {column["name"] for column in inspect(connection).get_columns("gateway_users")}
    finally:
        engine.dispose()


def test_accepted_build_backfills_venture_stage(tmp_path, monkeypatch) -> None:
    """An accepted immutable receipt must repair pre-stage-tracking ventures."""
    import json

    from sqlalchemy import create_engine, text

    db = tmp_path / "stage.db"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "s015mergereleaseheads")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO ventures (id, workspace_id, name, raw_idea, stage, autonomy_level, created_at, updated_at) "
            "VALUES ('ven_backfill', 'wsp_backfill', 'Backfill', 'idea', 'draft', 'L1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO build_runs "
            "(id, app_id, workspace_id, venture_id, trigger, agent, state, spec, credits_used, runtime_version, event_seq) "
            "VALUES ('run_backfill', 'app_backfill', 'wsp_backfill', 'ven_backfill', 'founder', 'managed', 'succeeded', :spec, 0, 1, 0)"
        ), {"spec": json.dumps({"lifecycle": {"review": {"decision": "accept", "status": "accepted"}}})})
    command.upgrade(cfg, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT stage FROM ventures WHERE id = 'ven_backfill'"
        )).scalar_one() == "readiness_review"
    engine.dispose()


def test_zero_cost_device_custody_blocks_downgrade(tmp_path, monkeypatch):
    import pytest
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path / 'zero-hold.db'}"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        initial_head = conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_credit_pool_identity'")) == 1
        conn.execute(text("INSERT INTO credit_reservations "
            "(id, account_id, is_pool, amount_micro, status, purpose, clamped, expires_at, created_at) "
            "VALUES ('zero_custody', 'account', 0, 0, 'settled', 'device.work_order', 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    with pytest.raises(RuntimeError, match="zero_cost_device_custody_prevents_downgrade"):
        command.downgrade(cfg, "s027usererrors")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT amount_micro FROM credit_reservations WHERE id='zero_custody'")) == 0
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == initial_head


def test_refresh_grace_upgrade_preserves_existing_device_credentials(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    url = f"sqlite:///{tmp_path / 'existing-device.db'}"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "s040errorreports")
    engine = create_engine(url)
    try:
        assert "previous_refresh_token_hash" not in {column["name"] for column in inspect(engine).get_columns("desktop_devices")}
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO desktop_devices (id,person_id,name,platform,refresh_token_hash) VALUES ('dev_existing','usr_existing','Existing Mac','macos','existing-digest')"))
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM desktop_devices WHERE id='dev_existing'")).mappings().one()
            assert row["refresh_token_hash"] == "existing-digest"
            assert row["previous_refresh_token_hash"] is None and row["refresh_rotated_at"] is None
            assert row["refresh_token_used"] == 1
    finally:
        engine.dispose()
