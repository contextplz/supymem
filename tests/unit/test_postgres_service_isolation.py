"""Background enrichment must finish before its import and database custody end."""

import os
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from threading import Event, Thread, enumerate as enumerate_threads
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.mark.parametrize("domain", ["warehouse inventory", "home gardening"])
def test_migrations_do_not_expose_fresh_database_to_background_work(domain, tmp_path, monkeypatch):
    from alembic import command
    from tests.integration import test_postgres_auth_security as fixtures

    parent_url = f"sqlite:///{tmp_path / (domain + '.db')}"
    monkeypatch.setenv("TEST_POSTGRES_URL", "postgresql://localhost/postgres")
    monkeypatch.setenv("COMPANY_OS_DB_URL", parent_url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    admin, engine = MagicMock(), MagicMock()
    monkeypatch.setattr(fixtures, "create_engine", lambda *args, **kwargs: admin)
    entered, released, finished = Event(), Event(), Event()
    observed, migrations = [], []
    migrated_path = tmp_path / "migrated.db"

    def background_work():
        try:
            entered.set()
            assert released.wait(10)
            observed.append(os.environ["COMPANY_OS_DB_URL"])
        finally:
            finished.set()

    def upgrade(*args, **kwargs):
        migrations.append((args, kwargs))
        released.set()
        assert finished.wait(10)
        if "env" in kwargs:
            child_env = {**kwargs["env"], "COMPANY_OS_DB_URL": f"sqlite:///{migrated_path}"}
            result = subprocess.run(args[0], **{**kwargs, "env": child_env}, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr

    monkeypatch.setattr(command, "upgrade", upgrade)
    monkeypatch.setattr(fixtures, "subprocess", SimpleNamespace(run=upgrade), raising=False)
    background = None
    database = None
    try:
        with service_modules("company_os"):
            from app.db import session
            from app.chat.proactive import _spawn

            monkeypatch.setattr(session, "make_engine", lambda *args, **kwargs: engine)
            monkeypatch.setattr(fixtures, "service_modules", lambda name: nullcontext())
            _spawn(background_work)
            assert entered.wait(10)
            background = next(thread for thread in enumerate_threads() if thread.name == "since-you-left")
            database = fixtures.pg_database.__wrapped__(
                SimpleNamespace(mktemp=lambda name: tmp_path / name))
            db_url, actual_engine = next(database)
            assert observed == [parent_url]
            assert actual_engine is engine
            args, kwargs = migrations[0]
            assert args[0] == [sys.executable, "-m", "alembic", "-c",
                str(fixtures.ROOT / "services/company_os/alembic.ini"), "upgrade", "head"]
            assert kwargs["env"]["COMPANY_OS_DB_URL"] == db_url
            assert kwargs["env"]["PYTHONPATH"] == os.pathsep.join([
                str(fixtures.ROOT / "packages"), str(fixtures.ROOT / "services/company_os")])
            assert kwargs["cwd"] == fixtures.ROOT / "services/company_os"
            assert kwargs["check"] is True and kwargs["timeout"] > 0
            with sqlite3.connect(migrated_path) as migrated:
                assert migrated.execute("SELECT version_num FROM alembic_version").fetchone() == (fixtures.migration_head(),)
                assert migrated.execute("SELECT COUNT(*) FROM architecture_decision_records").fetchone() == (0,)
    finally:
        released.set()
        if background is not None:
            background.join(timeout=10)
        if database is not None:
            database.close()
    assert background is not None and not background.is_alive()
    engine.dispose.assert_called_once()
    admin.dispose.assert_called_once()
    statements = [str(call.args[0]) for call in admin.connect.return_value.__enter__.return_value.execute.call_args_list]
    assert statements == [f"CREATE DATABASE {db_url.rsplit('/', 1)[1]}",
        f"DROP DATABASE IF EXISTS {db_url.rsplit('/', 1)[1]} WITH (FORCE)"]


@pytest.mark.parametrize("domain", ["warehouse inventory", "home gardening"])
def test_service_cleanup_drains_stopped_enrichment_before_disposal(domain, tmp_path, monkeypatch):
    released, entered = Event(), Event()
    original_join = Thread.join
    background = None

    def join(thread, *args, **kwargs):
        if thread is background:
            released.set()
        return original_join(thread, *args, **kwargs)

    monkeypatch.setattr(Thread, "join", join)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    saved_state = sys.modules.get("app.state")
    observed = []
    try:
        with service_modules("company_os"):
            from app.research.prior_art import graveyard
            import app.state as state

            def run(did, payload, snapshot, selected, stopped):
                entered.set()
                assert released.wait(10)
                from app import state as current_state

                observed.append((current_state, os.environ["COMPANY_OS_DB_URL"], snapshot["offer"], stopped.is_set()))
                return {"startups": []}

            def dispose():
                assert future.done(), "Engine was disposed while enrichment still used app imports"
                assert observed == [(state, "sqlite:///:memory:", domain, True)]

            monkeypatch.setattr(graveyard.ex, "mutate", lambda *args: True)
            monkeypatch.setattr(graveyard, "run", run)
            state._engine = SimpleNamespace(dispose=dispose)
            future, stopped = graveyard.start("operation", {}, {"offer": domain}, None)
            assert entered.wait(10)
            background = next(thread for thread in enumerate_threads()
                              if thread.name == "research-graveyard")
            graveyard.collect("operation", {}, (future, stopped))
            assert stopped.is_set() and not future.done()
        assert future.result(timeout=10) == {"startups": []}
        assert not background.is_alive()
        assert sys.modules.get("app.state") is saved_state
    finally:
        released.set()
        if background is not None:
            original_join(background, timeout=10)
