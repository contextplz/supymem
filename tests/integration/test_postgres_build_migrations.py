"""Real old-head upgrade, safe empty rollback, and corrupt-history preflight."""
from contextlib import contextmanager
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.integration.test_postgres_auth_security import ROOT, pg_database as pg_database, service_modules


@contextmanager
def prior_build_schema(pg_database, monkeypatch):
    base, _ = pg_database
    admin = create_engine(make_url(base).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "supymem_build_schema_" + uuid.uuid4().hex[:12]
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        with service_modules("company_os"):
            from alembic import command
            from alembic.config import Config
            monkeypatch.setenv("COMPANY_OS_DB_URL", url)
            cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
            command.upgrade(cfg, "s005ideaclarify")
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO ventures(id,workspace_id,name,raw_idea,stage,autonomy_level,created_at,updated_at) VALUES ('v','w','legacy','legacy','draft','L1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO apps(id,workspace_id,venture_id,slug,name,kind,state,build_mode,deploy_target,created_at,updated_at) VALUES ('a','w','v','legacy-app','Legacy','service','draft','supymem','supymem',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO build_runs(id,app_id,workspace_id,venture_id,trigger,agent,state,spec,credits_used) VALUES ('r','a','w','v','legacy','fixture','succeeded','{}',0)"))
                connection.execute(text("INSERT INTO build_events(build_run_id,seq,ts,kind,payload) VALUES ('r',5,CURRENT_TIMESTAMP,'legacy','{}')"))
            yield engine, cfg, command
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def test_build_migration_roundtrip_preserves_legacy_provenance(pg_database, monkeypatch):
    with prior_build_schema(pg_database, monkeypatch) as (engine, cfg, command):
        for _ in range(2):
            command.upgrade(cfg, "head")
            with engine.connect() as connection:
                assert tuple(connection.execute(text("SELECT runtime_version,event_seq,state FROM build_runs WHERE id='r'")).one()) == (0, 5, "succeeded")
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "s006buildruntime"
            command.downgrade(cfg, "s005ideaclarify")
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("fault", ["scope", "duplicate_events"])
def test_build_migration_refuses_corrupt_history_without_silent_repair(pg_database, monkeypatch, fault):
    with prior_build_schema(pg_database, monkeypatch) as (engine, cfg, command):
        with engine.begin() as connection:
            if fault == "scope":
                connection.execute(text("UPDATE build_runs SET workspace_id='foreign' WHERE id='r'"))
            else:
                connection.execute(text("INSERT INTO build_events(build_run_id,seq,ts,kind,payload) VALUES ('r',5,CURRENT_TIMESTAMP,'duplicate','{}')"))
        with pytest.raises(RuntimeError, match="requires_reconciliation"):
            command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "s005ideaclarify"


def test_build_migration_cannot_erase_managed_history(pg_database, monkeypatch):
    with prior_build_schema(pg_database, monkeypatch) as (engine, cfg, command):
        command.upgrade(cfg, "head")
        with engine.begin() as connection:
            connection.execute(text("UPDATE build_runs SET runtime_version=1,closed_at=CURRENT_TIMESTAMP WHERE id='r'"))
        with pytest.raises(RuntimeError, match="requires_forward_migration"):
            command.downgrade(cfg, "s005ideaclarify")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "s006buildruntime"
            assert connection.execute(text("SELECT runtime_version FROM build_runs WHERE id='r'")).scalar_one() == 1
