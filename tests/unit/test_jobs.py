"""Job queue: visibility timeout reclaim + poison queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.jobs import JobQueue, JobSpec
from app.db.models import Base, Job
from app.db.session import make_engine
from core.enums import JobStatus


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture()
def queue(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    clock = FakeClock()
    q = JobQueue(engine, visibility_timeout_s=5, clock=clock)
    return q, clock, engine


def test_crashed_job_retried_after_visibility_timeout(queue) -> None:
    q, clock, _engine = queue
    job_id = q.enqueue(JobSpec(kind="demo", payload={"x": 1}, max_attempts=3))
    claimed = q.claim()
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.attempts == 1
    # Crash: leave as running, do not complete/fail.
    assert q.status(job_id) == JobStatus.RUNNING.value

    # Before visibility timeout — should not reclaim.
    clock.advance(2)
    assert q.claim() is None
    assert q.status(job_id) == JobStatus.RUNNING.value

    # After visibility timeout — reclaim exactly once.
    clock.advance(4)
    claimed2 = q.claim()
    assert claimed2 is not None
    assert claimed2.id == job_id
    assert claimed2.attempts == 2
    q.complete(job_id)
    assert q.status(job_id) == JobStatus.DONE.value


def test_permanently_failing_job_lands_in_poison_queue(queue) -> None:
    q, clock, engine = queue
    job_id = q.enqueue(JobSpec(kind="bad", payload={}, max_attempts=3))
    statuses: list[str] = []
    for _ in range(3):
        claimed = q.claim()
        assert claimed is not None
        assert claimed.id == job_id
        status = q.fail(job_id, "boom")
        statuses.append(status)
        # Advance past backoff so next claim can proceed.
        clock.advance(60)
    assert statuses[-1] == JobStatus.POISON.value
    assert q.status(job_id) == JobStatus.POISON.value
    assert q.claim() is None

    # Confirm row persisted as poison
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        row = session.get(Job, job_id)
        assert row is not None
        assert row.status == JobStatus.POISON.value
        assert row.attempts == 3
        assert row.last_error == "boom"
