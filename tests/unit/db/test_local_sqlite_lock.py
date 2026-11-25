import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.company_os_harness import load_company_os_app
from tests.unit.test_idea_page import env as idea_env


@pytest.fixture
def local_engines(tmp_path, monkeypatch):
    load_company_os_app()
    from app.db import local_sqlite
    from app.db.models import Job
    from app.db.session import make_engine

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_sqlite, "migrate_sqlite", local_sqlite.ensure_sqlite_parent)
    url = f"sqlite:///{tmp_path}/shared.sqlite3"
    api = make_engine(url)
    worker = make_engine(url)
    Job.__table__.create(api)
    with api.begin() as connection:
        connection.execute(text("CREATE TABLE writes (domain TEXT PRIMARY KEY, count INTEGER NOT NULL)"))
    yield api, worker
    api.dispose()
    worker.dispose()


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
def test_api_savepoints_and_worker_writes_contend_without_snapshot_lock(local_engines, domain):
    from app.core.jobs import enqueue
    from app.db.models import Job

    api, worker = local_engines
    with api.begin() as connection:
        connection.execute(text("INSERT INTO writes VALUES (:domain, 0)"), {"domain": domain})
    worker_started = threading.Event()
    deadline = time.monotonic() + 3

    def allow_worker_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT jobs."):
            worker_started.set()
            time.sleep(0.02)

    event.listen(api, "after_cursor_execute", allow_worker_write)

    def api_role():
        factory = sessionmaker(api)
        count = 0
        while time.monotonic() < deadline:
            with factory() as session:
                with session.begin_nested():
                    enqueue(session, "idea.page_refresh", {"domain": domain}, dedupe_key=f"{domain}:{count}")
                session.commit()
            count += 1
            time.sleep(0.01)
        return count

    def worker_role():
        assert worker_started.wait(5)
        count = 0
        while time.monotonic() < deadline:
            with worker.begin() as connection:
                connection.execute(text("UPDATE writes SET count = count + 1 WHERE domain = :domain"), {"domain": domain})
            count += 1
            time.sleep(0.01)
        return count

    with ThreadPoolExecutor(max_workers=2) as executor:
        api_result = executor.submit(api_role)
        worker_result = executor.submit(worker_role)
        api_count, worker_count = api_result.result(timeout=40), worker_result.result(timeout=40)
    event.remove(api, "after_cursor_execute", allow_worker_write)
    assert api_count > 0 and worker_count > 0
    with api.connect() as connection:
        assert len(connection.execute(select(Job.id)).all()) == api_count
        assert connection.scalar(text("SELECT count FROM writes WHERE domain = :domain"), {"domain": domain}) == worker_count


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
def test_enqueued_savepoint_stays_inside_the_callers_rollback(local_engines, domain):
    from app.core.jobs import enqueue
    from app.db.models import Job

    api, worker = local_engines
    with sessionmaker(api)() as session:
        with session.begin_nested():
            enqueue(session, "idea.page_refresh", {"domain": domain})
        session.rollback()
    with worker.connect() as connection:
        assert connection.scalar(select(Job.id)) is None


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
def test_concurrent_enqueue_dedupes_before_either_role_reads(local_engines, domain):
    from app.core.jobs import enqueue
    from app.db.models import Job

    start = threading.Barrier(2)

    def enqueue_role(engine):
        start.wait(timeout=5)
        with sessionmaker(engine)() as session:
            job_id = enqueue(session, "idea.page_refresh", {"domain": domain}, dedupe_key=domain)
            session.commit()
            return job_id

    def allow_competing_reader(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT jobs."):
            time.sleep(0.03)

    for engine in local_engines:
        event.listen(engine, "after_cursor_execute", allow_competing_reader)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue_role, local_engines))
    for engine in local_engines:
        event.remove(engine, "after_cursor_execute", allow_competing_reader)
    assert results[0] == results[1]
    with local_engines[0].connect() as connection:
        assert len(connection.execute(select(Job.id)).all()) == 1


def test_sync_and_async_connections_share_durable_wal_settings(local_engines):
    from app.db.session import make_async_engine

    api, worker = local_engines
    for engine in (api, worker):
        assert isinstance(engine.pool, NullPool)
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert connection.scalar(text("PRAGMA busy_timeout")) == 30000
            assert connection.scalar(text("PRAGMA synchronous")) == 2
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    async def check_async():
        engine = make_async_engine(str(api.url))
        try:
            assert isinstance(engine.pool, NullPool)
            async with engine.connect() as connection:
                assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
                assert await connection.scalar(text("PRAGMA busy_timeout")) == 30000
                assert await connection.scalar(text("PRAGMA synchronous")) == 2
                assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            async with engine.begin() as connection:
                async with connection.begin_nested():
                    await connection.execute(text("INSERT INTO writes VALUES ('async', 1)"))
                await connection.rollback()
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM writes")) == 0
        finally:
            await engine.dispose()

    asyncio.run(check_async())


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
def test_nested_reader_does_not_repeat_wal_startup(local_engines, domain):
    api, worker = local_engines
    statements = []

    @event.listens_for(worker, "do_connect")
    def trace_connection(dialect, record, args, kwargs):
        connection = dialect.connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with api.begin() as writer:
        writer.execute(text("INSERT INTO writes VALUES (:domain, 1)"), {"domain": domain})
    count = text("SELECT count FROM writes WHERE domain = :domain")
    with worker.connect() as reader:
        assert reader.scalar(count, {"domain": domain}) == 1
    statements.clear()

    with api.begin() as writer:
        writer.execute(text("UPDATE writes SET count = 2 WHERE domain = :domain"), {"domain": domain})
        with worker.connect() as reader:
            assert reader.scalar(count, {"domain": domain}) == 1
    assert not any(statement.startswith("PRAGMA journal_mode") for statement in statements)


@pytest.mark.parametrize("filename", ["working_memory.sqlite3", "institutional_memory.sqlite3", "local_spend.sqlite3"])
@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
def test_raw_sqlite_store_configuration_keeps_wal_and_full_durability(tmp_path, filename, domain):
    load_company_os_app()
    from app.db.session import _apply_sqlite_pragmas

    connection = sqlite3.connect(tmp_path / filename)
    try:
        _apply_sqlite_pragmas(connection)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        with connection:
            connection.execute("CREATE TABLE saved (domain TEXT)")
            connection.execute("INSERT INTO saved VALUES (?)", (domain,))
        assert connection.execute("SELECT domain FROM saved").fetchone()[0] == domain
    finally:
        connection.close()


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
@pytest.mark.parametrize("retryable", [True, False])
def test_wal_activation_retries_only_sqlite_lock_errors(tmp_path, domain, retryable):
    load_company_os_app()
    from app.db.session import _apply_sqlite_pragmas

    connection = sqlite3.connect(tmp_path / "startup.sqlite3")
    with connection:
        connection.execute("CREATE TABLE saved (domain TEXT)")
        connection.execute("INSERT INTO saved VALUES (?)", (domain,))
    real_cursor = connection.cursor()
    cursor = Mock(wraps=real_cursor)
    attempts = []

    def execute(statement):
        if statement == "PRAGMA journal_mode=WAL":
            attempts.append(statement)
            if len(attempts) == 1:
                error = sqlite3.OperationalError("fixture SQLite setup failure")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY if retryable else sqlite3.SQLITE_IOERR
                raise error
        return real_cursor.execute(statement)

    cursor.execute.side_effect = execute
    dbapi = Mock()
    dbapi.cursor.return_value = cursor
    try:
        if retryable:
            _apply_sqlite_pragmas(dbapi, None)
            assert len(attempts) == 2
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        else:
            with pytest.raises(sqlite3.OperationalError, match="fixture SQLite setup failure"):
                _apply_sqlite_pragmas(dbapi, None)
            assert len(attempts) == 1
        cursor.close.assert_called_once()
        assert connection.execute("SELECT domain FROM saved").fetchone()[0] == domain
    finally:
        connection.close()


@pytest.mark.parametrize("domain", ["Factory equipment diagnostics", "Family trail journals"])
@pytest.mark.parametrize("custom_directory", [False, True])
@pytest.mark.parametrize("async_engine", [False, True])
def test_concurrent_first_start_migrates_with_isolated_backup_settings(tmp_path, monkeypatch, domain, custom_directory, async_engine):
    load_company_os_app()
    from app.core import local_secrets
    from tests.unit.db.test_supymem_migrations import test_local_sqlite_first_start_migrates_and_preserves_append_only_history

    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {}})
    test_local_sqlite_first_start_migrates_and_preserves_append_only_history(
        tmp_path, monkeypatch, domain, custom_directory, async_engine,
    )


@pytest.mark.parametrize("venture_id,audience", [("ven_a", "Freight dispatchers"), ("ven_b", "Weekend hikers")])
def test_idea_enqueue_failure_is_visible_and_retry_queues_saved_work(idea_env, monkeypatch, venture_id, audience, caplog):
    from app.chat import milestones
    from app.core import jobs
    from app.db.models import Base, ChatMessage, Venture

    Base.metadata.create_all(idea_env.factory.kw["bind"])
    monkeypatch.setattr(milestones, "db", lambda: idea_env.factory)
    monkeypatch.setattr(milestones, "manager_speaker", lambda venture_id: {"name": "Charlie", "title": "Manager"})
    monkeypatch.setattr("app.email.triggers.on_milestone", lambda *args, **kwargs: None)
    original = jobs.enqueue

    def unavailable(*args, **kwargs):
        raise OperationalError("INSERT", {"secret": "must-not-be-logged"}, Exception("database is locked"))

    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    with idea_env.factory() as session:
        venture = session.get(Venture, venture_id)
        workspace_id = venture.workspace_id
        venture.raw_idea = f"An offer for {audience}"
        session.commit()
        monkeypatch.setattr(jobs, "enqueue", unavailable)
        assert idea_env.module.schedule(session, workspace_id, venture_id, actor="usr_a", now=now, requested=True) is False
        session.commit()
    with idea_env.factory() as session:
        failure = session.scalar(select(ChatMessage).where(ChatMessage.venture_id == venture_id))
        assert failure is not None
        assert "your" in failure.body_md.lower() and "Retry" in failure.body_md
        retry = next(action for action in json.loads(failure.actions_json) if action.get("label") == "Retry")
        assert retry["action"] == idea_env.module.JOB_KIND and retry["payload"] == {}
        assert session.scalar(select(idea_env.job.id)) is None
        assert session.get(Venture, venture_id).raw_idea == f"An offer for {audience}"
    assert "must-not-be-logged" not in caplog.text
    monkeypatch.setattr(jobs, "enqueue", original)
    assert idea_env.module.request_refresh(venture_id, actor="usr_a", requested=True) is True
    with idea_env.factory() as session:
        assert len(session.scalars(select(idea_env.job)).all()) == 1


@pytest.mark.parametrize("venture_id,audience", [("ven_a", "Freight dispatchers"), ("ven_b", "Weekend hikers")])
def test_idea_model_call_releases_the_database_for_other_writers(idea_env, monkeypatch, venture_id, audience):
    from app.credits import metered
    from app.db import local_sqlite
    from app.db.models import Venture
    from app.db.session import make_engine
    from app.ventures import state_brief

    monkeypatch.setattr(local_sqlite, "migrate_sqlite", local_sqlite.ensure_sqlite_parent)
    engine = make_engine(str(idea_env.factory.kw["bind"].url))
    monkeypatch.setattr(state_brief, "get_venture_state_brief", lambda *args, **kwargs: {"idea": {"audience": audience}})
    monkeypatch.setattr(state_brief, "invalidate_venture_state_brief", lambda venture_id: None)

    def api_write():
        with engine.begin() as connection:
            connection.execute(Venture.__table__.update().where(Venture.id == venture_id).values(raw_idea=audience))

    async def model(*args, **kwargs):
        await asyncio.to_thread(api_write)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"summary": audience})))])

    monkeypatch.setattr(metered, "llm", model)
    try:
        asyncio.run(idea_env.module.refresh({"venture_id": venture_id, "_requested": True}))
        assert idea_env.module.read(venture_id)["page"]["summary"] == audience
        with idea_env.factory() as session:
            assert session.get(Venture, venture_id).raw_idea == audience
    finally:
        engine.dispose()


@pytest.mark.parametrize("venture_id,workspace_id,audience", [
    ("ven_a", "wsp_a", "Factory equipment diagnostics"),
    ("ven_b", "wsp_b", "Family trail journals"),
])
def test_local_idea_refresh_does_not_depend_on_the_hosted_flag(idea_env, monkeypatch, venture_id, workspace_id, audience):
    from app.core.local_secrets import update_settings
    from app.db.models import Venture

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("IDEA_PAGE_REFRESH", "off")
    with idea_env.factory() as session:
        session.get(Venture, venture_id).raw_idea = audience
        assert idea_env.module.schedule(session, workspace_id, venture_id, actor="usr_a") is False
        assert idea_env.module.schedule(session, workspace_id, venture_id, actor="usr_a", requested=True) is True
        update_settings("capabilities", {"background_work": True}, {})
        assert idea_env.module.schedule(session, workspace_id, venture_id, actor="usr_a") is False
        assert idea_env.module.schedule(session, workspace_id, venture_id, actor="usr_a", requested=True) is True
        session.commit()
        assert session.scalars(select(idea_env.job.id)).one()
