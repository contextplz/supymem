"""Independent work for one venture must not be serialized at the worker lease."""

import pytest

from tests.unit.jobs.test_lease import factory


@pytest.mark.parametrize("idea", ["Returns routing for wholesalers", "Music practice reminders for beginners"])
def test_independent_venture_jobs_are_registered_and_leased_concurrently(factory, monkeypatch, idea):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.worker import main as worker
    from app.worker.handlers import HANDLERS, install_default_handlers
    from core.enums import JobStatus

    monkeypatch.setattr(worker, "CONCURRENCY", 3)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    kinds = ["research.deep", "experiment.beliefs.tick"]
    install_default_handlers()
    assert all(kind in HANDLERS for kind in kinds)
    with factory() as session:
        first = enqueue(session, kinds[0], {"idea": idea}, venture_id="venture_under_test")
        second = enqueue(session, kinds[1], {"idea": idea}, venture_id="venture_under_test")
        session.commit()
        claimed = worker._lease_available(session, "worker", kinds, set(), set(), {"interactive", "bulk"})
        assert {job.id for job in claimed} == {first, second}
        assert all(session.get(Job, job_id).status == JobStatus.LEASED.value for job_id in (first, second))
        assert all(session.get(Job, job_id).started_at is not None for job_id in (first, second))


@pytest.mark.parametrize("idea", ["Returns routing for wholesalers", "Music practice reminders for beginners"])
def test_independent_job_starts_while_same_venture_research_is_running(factory, monkeypatch, idea):
    import asyncio

    from app.core.jobs import enqueue, lease
    from app.worker import main as worker
    from app.worker.handlers import HANDLERS, install_default_handlers
    from app.core import local_worker

    install_default_handlers()
    monkeypatch.setattr(local_worker, "capability_enabled", lambda name: True)
    monkeypatch.setattr(worker, "CONCURRENCY", 3)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    with factory() as session:
        first = enqueue(session, "research.deep", {"idea": idea}, venture_id="venture_under_test")
        second = enqueue(session, "experiment.beliefs.tick", {"idea": idea}, venture_id="venture_under_test")
        research = lease(session, "worker", kinds=["research.deep"])[0]
        session.commit()
        running = {(research.id, research.attempts)}
        independent = worker._lease_available(session, "worker", sorted(HANDLERS), running, running,
                                             {"interactive", "bulk"})
        session.commit()
    assert [job.id for job in independent] == [second]

    async def run():
        started = asyncio.Event()
        release = asyncio.Event()

        async def research_handler(payload):
            assert payload["idea"] == idea
            started.set()
            await release.wait()

        async def independent_handler(payload):
            assert payload["idea"] == idea
            assert started.is_set() and not release.is_set()

        monkeypatch.setitem(HANDLERS, "research.deep", research_handler)
        monkeypatch.setitem(HANDLERS, "experiment.beliefs.tick", independent_handler)
        from app.tenancy import Scope
        monkeypatch.setattr(worker, "_job_scope", lambda job: Scope("none", job.venture_id, "system"))
        running.update((job.id, job.attempts) for job in independent)
        task = asyncio.create_task(worker._run_one(factory, research, running, running))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.wait_for(worker._run_one(factory, independent[0], running, running), timeout=5)
            with factory() as session:
                from app.db.models import Job
                assert session.get(Job, second).status == "succeeded"
                assert session.get(Job, first).status == "leased"
        finally:
            release.set()
            await task

    asyncio.run(run())
