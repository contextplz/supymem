"""Scheduled portfolio consumers bind each venture's DB-owned workspace."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture
def portfolio(tmp_path, monkeypatch):
    with service_modules("company_os"):
        from app.db.models import Venture, Workspace
        from app.db import session as db_session
        from app import state
        from app.ventures import lifecycle
        from app.tenancy import current
        from core.clock import utcnow

        engine = create_engine(f"sqlite:///{tmp_path}/portfolio.db")
        Venture.__table__.create(engine)
        Workspace.__table__.create(engine)
        factory = sessionmaker(engine)
        with factory() as session:
            for suffix in ("one", "two"):
                session.add(Workspace(id=f"wsp_{suffix}", name=suffix, kind="venture",
                    entry_mode="idea", owner_id="founder", settings_json="{}",
                    created_at=utcnow(), updated_at=utcnow()))
                session.add(Venture(id=f"ven_{suffix}", workspace_id=f"wsp_{suffix}",
                    name=suffix, raw_idea=suffix, stage="live", created_at=utcnow(), updated_at=utcnow()))
            session.commit()
        monkeypatch.setattr(state, "db", lambda: factory)
        monkeypatch.setattr(lifecycle, "db", lambda: factory)
        monkeypatch.setattr(lifecycle, "_heal_names", lambda *args: False)
        monkeypatch.setattr(db_session, "resolve_database_url", lambda: "postgresql://test")

        def project(row):
            assert (current().workspace_id, current().venture_id) == (row.workspace_id, row.id)
            return {"id": row.id, "workspace_id": row.workspace_id, "stage": row.stage}

        monkeypatch.setattr(lifecycle, "_ser_active_scoped", project)
        yield lifecycle, factory
        engine.dispose()


@pytest.mark.parametrize("kind", ["ops.brain_cycle", "ops.slate"])
def test_scheduled_jobs_project_two_workspaces(portfolio, monkeypatch, kind):
    from app.gbrain import cycle
    from app.memory.scope import bound_memory_scope
    from app.tenancy import bind, current
    from app.will import driver
    from app.worker import handlers, main

    seen = []

    def visit(vid):
        assert current().workspace_id == vid.replace("ven_", "wsp_")
        assert current().venture_id == vid
        seen.append(vid)
        return 0

    async def drive(ven):
        with bound_memory_scope(ven["id"]):
            visit(ven["id"])
        return "started", ven["id"]

    monkeypatch.setattr(cycle, "_cycle_one", visit)
    monkeypatch.setattr(driver, "_drive_one", drive)
    scope = main._job_scope(SimpleNamespace(kind=kind, workspace_id=None, venture_id=None, payload={}))
    with bind(workspace_id=scope.workspace_id, venture_id=scope.venture_id, actor=scope.actor):
        asyncio.run(handlers._ops_brain_cycle({}) if kind == "ops.brain_cycle" else handlers._ops_slate({}))
        assert current() == scope
    assert set(seen) == {"ven_one", "ven_two"}


def test_scope_mismatch_still_refuses_users_and_scoped_system(portfolio, monkeypatch):
    from app.authz import workspace
    from app.memory.scope import bound_memory_scope
    from app.tenancy import bind, current

    monkeypatch.setattr(workspace, "accessible_workspace_ids", lambda actor: {"wsp_one"})
    for wid, vid, actor in [("wsp_one", "", "usr_alice"), ("none", "", "usr_alice"),
                            ("none", "ven_one", "system"), ("wsp_one", "", "system")]:
        with bind(workspace_id=wid, venture_id=vid, actor=actor):
            with pytest.raises(PermissionError, match="memory_consumer_scope_mismatch"):
                with bound_memory_scope("ven_two"):
                    pytest.fail("foreign scope admitted")
    with bind(workspace_id="none", venture_id="", actor="usr_alice"):
        with bound_memory_scope("ven_one"):
            assert current().workspace_id == "wsp_one"
    with bind(workspace_id="none", venture_id="", actor="system"):
        with pytest.raises(PermissionError, match="memory_consumer_scope_mismatch"):
            with bound_memory_scope("ven_one", workspace_id="wsp_two"):
                pytest.fail("caller workspace overrode DB ownership")


def test_list_active_skips_one_failed_projection(portfolio, monkeypatch, caplog):
    lifecycle, _ = portfolio
    from app.tenancy import bind

    project = lifecycle._ser_active_scoped

    def broken(row):
        if row.id == "ven_two":
            raise ValueError("private customer detail")
        return project(row)

    monkeypatch.setattr(lifecycle, "_ser_active_scoped", broken)
    with bind(workspace_id="none", venture_id="", actor="system"):
        assert [row["id"] for row in lifecycle.list_active()] == ["ven_one"]
    assert "active venture projection skipped" in caplog.text
    assert "private customer detail" not in caplog.text
