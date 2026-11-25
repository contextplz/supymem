"""Real PostgreSQL workers cannot enqueue duplicate jobs for one tick window."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    concurrent, pg_database as pg_database, service_modules,
)


def test_concurrent_tick_dispatch_dedupes_each_kind(pg_database, monkeypatch):
    url, engine = pg_database
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    with service_modules("company_os"):
        from app.api import internal_tick
        from app.db.models import Job
        from app.state import set_factory

        factory = sessionmaker(engine, expire_on_commit=False)
        set_factory(factory)
        monkeypatch.setattr(internal_tick, "publish_wake", lambda: None)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 2, 12, 5, tzinfo=timezone.utc)

        monkeypatch.setattr(internal_tick, "datetime", Clock)
        results = concurrent(lambda: internal_tick.dispatch_tick("5m"), count=4)
        assert all(result["window"] == "20261002T1205" for result in results)
        assert {"credits.sweep", "autopilot.tick"} <= set(results[0]["enqueued"])
        for kind in results[0]["enqueued"]:
            with factory() as session:
                jobs = session.scalars(select(Job).where(
                    Job.dedupe_key == f"{kind}:20261002T1205"
                )).all()
                assert len(jobs) == 1
                assert {result["ids"][result["enqueued"].index(kind)] for result in results} == {jobs[0].id}
