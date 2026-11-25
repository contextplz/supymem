import asyncio
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.company_os_harness import load_company_os_app
from tests.unit.autopilot.test_outreach_send_admission import _emulate_pg_advisory_locks
from tests.unit.db.test_scale_indexes import scale_engine as scale_engine


def test_default_pools_and_nested_send_fences_release_connections(scale_engine, monkeypatch):
    from contextlib import ExitStack
    from sqlalchemy.orm import sessionmaker

    load_company_os_app()
    from app.autopilot import send_admission
    from app.db.session import make_async_engine, make_engine

    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("DB_MAX_OVERFLOW", raising=False)
    monkeypatch.delenv("RDS_PROXY_ENDPOINT", raising=False)
    spec = importlib.util.spec_from_file_location("gateway_default_pool", Path(__file__).resolve().parents[3] /
        "services/gateway/app/users_pg.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    url = "postgresql+psycopg://user:secret@unused.invalid/defaults"
    for engine in (make_engine(url), make_async_engine(url), gateway._engine(url)):
        assert engine.pool.size() == 10 and engine.pool._max_overflow == 20
        if hasattr(engine, "sync_engine"):
            asyncio.run(engine.dispose())
        else:
            engine.dispose()
    with scale_engine.begin() as connection:
        connection.execute(text("CREATE TABLE provider_admission_fences (scope TEXT PRIMARY KEY, "
            "token TEXT NOT NULL, acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    factory = sessionmaker(bind=scale_engine)
    monkeypatch.setattr(send_admission, "db", lambda: factory)
    if scale_engine.dialect.name == "sqlite":
        _emulate_pg_advisory_locks(scale_engine)
        monkeypatch.setattr(scale_engine.dialect, "name", "postgresql")
    with pytest.raises(RuntimeError, match="provider interrupted"):
        with ExitStack() as owners:
            for domain in ("Equipment inspection", "Indoor herb growing"):
                for sender in range(3):
                    scope = f"{domain}:{sender}"
                    assert owners.enter_context(send_admission.try_send_admission(scope))
                    assert owners.enter_context(send_admission.try_send_admission("company:" + scope))
                    assert scale_engine.pool.checkedout() == 0
                    with send_admission.try_send_admission(scope) as duplicate:
                        assert not duplicate
                    with factory() as session:
                        assert session.scalar(text("SELECT count(*) FROM provider_admission_fences")) >= 2
                assert scale_engine.pool.checkedout() == 0
            raise RuntimeError("provider interrupted")
    with factory() as session:
        assert session.scalar(text("SELECT count(*) FROM provider_admission_fences")) == 0


@pytest.mark.parametrize("domain", ["Industrial scheduling", "Household gardening"])
def test_main_ledger_pricing_share_pool_and_configure_proxy(tmp_path, monkeypatch, domain):
    load_company_os_app()
    from app import state
    from app.credits._txn import session_factory
    from app.db.session import make_async_engine, make_engine, make_session_factory
    from app.llm import pricing

    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "1")
    monkeypatch.delenv("RDS_PROXY_ENDPOINT", raising=False)
    url = "postgresql+psycopg://user:secret@direct.invalid:5433/ledger?sslmode=require"
    direct = make_engine(url)
    assert direct.url.host == "direct.invalid"
    assert direct.pool.size() == 3 and direct.pool._max_overflow == 1
    direct.dispose()
    monkeypatch.setenv("RDS_PROXY_ENDPOINT", "proxy.invalid")
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("gateway_scale_pool", root / "services/gateway/app/users_pg.py")
    gateway = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gateway)
    for engine in (make_engine(url), make_async_engine(url), gateway._engine(url)):
        assert engine.url.host == "proxy.invalid"
        assert engine.url.password == "secret" and engine.url.port == 5433
        assert engine.url.database == "ledger" and engine.url.query["sslmode"] == "require"
        assert engine.pool.size() == 3 and engine.pool._max_overflow == 1
        if hasattr(engine, "sync_engine"):
            asyncio.run(engine.dispose())
        else:
            engine.dispose()
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with pytest.raises(ValueError):
        make_engine(url)
    engine = make_engine(f"sqlite:///{tmp_path / 'shared.db'}")
    factory = make_session_factory(engine)
    monkeypatch.setattr(state, "_factory", factory)
    monkeypatch.setattr(pricing, "_pg_engine", None)
    monkeypatch.setattr(pricing, "_pg_ready", False)
    monkeypatch.setattr(pricing, "_pg_url", lambda: url)
    assert state.db() is session_factory() is factory
    assert pricing._postgres_engine() is pricing._postgres_engine() is engine
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO llm_usage_events (id, workspace_id, at, purpose) "
                                "VALUES ('usage', 'workspace', CURRENT_TIMESTAMP, :domain)"), {"domain": domain})
        assert connection.scalar(text("SELECT purpose FROM llm_usage_events")) == domain

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(state, "db", unavailable)
    assert pricing._postgres_engine() is None
    engine.dispose()
