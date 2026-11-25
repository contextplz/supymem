"""Chaos: a dead worker's lease is reclaimed so another worker can take the job."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.jobs import enqueue, lease, reclaim_stale
from app.db.models import Base
from app.db.session import make_engine
from core.clock import utcnow


def test_killed_worker_lease_is_reclaimed(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'chaos.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        job_id = enqueue(session, "credits.sweep", {"n": 1})
        session.commit()
    with factory() as session:
        claimed = lease(session, "dead-worker", kinds=["credits.sweep"], n=1)
        assert claimed and claimed[0].id == job_id
        session.commit()
    with factory() as session:
        from app.db.models import Job

        row = session.get(Job, job_id)
        assert row is not None
        row.locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with factory() as session:
        n = reclaim_stale(session)
        session.commit()
        assert n == 1
        claimed = lease(session, "alive-worker", kinds=["credits.sweep"], n=1)
        session.commit()
        assert claimed and claimed[0].id == job_id
