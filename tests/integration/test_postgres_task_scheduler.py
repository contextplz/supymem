"""Actual PostgreSQL scheduler claims; never starts a worker or a paid handler."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import (
    concurrent, pg_database as module_pg_database, service_modules,
)


@pytest.fixture()
def pg_database(tmp_path_factory):
    yield from module_pg_database.__wrapped__(tmp_path_factory)


def test_two_workers_fire_each_due_maintenance_window_once(pg_database, monkeypatch):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.db.models import Job
        from app.db.task_models import MaintenanceSchedule
        from app.tasks import scheduler, service

        monkeypatch.setattr(service, "fire_due", lambda *args, **kwargs: [])
        factory = sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 8, 12, 4, tzinfo=timezone.utc)

        def fire():
            with factory() as session:
                result = scheduler.run_due(session, now=now)
                session.commit()
                return result

        results = concurrent(fire, count=2)
        assert sum(result["maintenance_due"] for result in results) == 1
        assert sum(len(result["ticks"]) for result in results) == 7
        with factory() as session:
            schedules = session.scalars(select(MaintenanceSchedule)).all()
            assert len(schedules) == 8
            assert all(row.next_fire_at > now.replace(tzinfo=None) for row in schedules)
            jobs = session.scalars(select(Job)).all()
            assert len(jobs) == len({job.dedupe_key for job in jobs})
            assert {job.kind for job in jobs} >= {"ops.signal_sweep", "ops.brain_cycle", "learning.cycle", "retention.purge"}


def test_locked_schedule_is_skipped_then_fires_after_unlock(pg_database, monkeypatch):
    _, engine = pg_database
    with service_modules("company_os"):
        from app.db.task_models import MaintenanceSchedule
        from app.tasks import scheduler, service

        monkeypatch.setattr(service, "fire_due", lambda *args, **kwargs: [])
        factory = sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 8, 12, 5, tzinfo=timezone.utc)
        with factory() as session:
            scheduler.run_due(session, now=now)
            session.get(MaintenanceSchedule, "maintenance").next_fire_at = now.replace(tzinfo=None)
            session.commit()
        with factory() as blocker:
            row = blocker.scalars(select(MaintenanceSchedule).where(
                MaintenanceSchedule.tick == "maintenance").with_for_update()).one()
            assert row.next_fire_at == now.replace(tzinfo=None)
            with factory() as runner:
                result = scheduler.run_due(runner, now=now)
                runner.commit()
            assert not result["maintenance_due"]
            blocker.commit()
        with factory() as session:
            result = scheduler.run_due(session, now=now)
            session.commit()
        assert result["maintenance_due"]
