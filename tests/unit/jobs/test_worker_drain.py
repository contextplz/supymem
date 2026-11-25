"""SIGTERM: stop leasing, let in-flight jobs finish; leftovers are released for the next worker."""

import asyncio
import os
import signal
from threading import Event

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.jobs import enqueue, lease  # noqa: E402
from app.db.models import Base, Job  # noqa: E402
from app.db.session import make_engine  # noqa: E402
from app.worker import main as worker  # noqa: E402

KIND = "tasks.verify"


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_local_worker_retries_concurrent_sqlite_writer_without_blocking_its_release(tmp_path, monkeypatch, idea, caplog):
    from sqlalchemy import event, text
    from sqlalchemy.exc import OperationalError
    from app.tenancy import Scope
    import app.bootstrap as boot
    import app.state as state

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.delenv("WORKER_HEALTH_PORT", raising=False)
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    engine = make_engine(f"sqlite:///{tmp_path / 'contention.db'}")
    Base.metadata.create_all(engine)
    event.listen(engine, "checkout", lambda connection, *_: connection.execute("PRAGMA busy_timeout=25"))
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        job_id = enqueue(session, KIND, {"idea": idea})
        session.commit()
    monkeypatch.setattr(boot, "bootstrap", lambda: None)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(worker, "install_default_handlers", lambda: None)
    monkeypatch.setattr(worker, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(worker, "enabled_kinds", lambda: {KIND})
    monkeypatch.setattr(worker, "_job_scope", lambda _: Scope("none", "", "system"))
    monkeypatch.setattr(worker, "_heartbeat_loop", lambda *args: None)
    monkeypatch.setattr(worker, "POLL_S", 0.01)
    calls = []
    contention = Event()
    original = worker._lease_available

    def claim(*args):
        try:
            return original(*args)
        except OperationalError:
            contention.set()
            raise

    monkeypatch.setattr(worker, "_lease_available", claim)

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(worker, "_scheduler_loop", idle)

    async def exercise():
        stop = asyncio.Event()
        holder = factory()
        holder.execute(text("UPDATE jobs SET status=status WHERE 0=1"))

        async def release():
            assert await asyncio.to_thread(contention.wait, 2)
            holder.rollback()

        async def dispatch(kind, payload):
            calls.append((kind, payload["idea"]))
            stop.set()

        monkeypatch.setattr(worker, "dispatch", dispatch)
        releasing = asyncio.create_task(release())
        try:
            await asyncio.wait_for(worker.run_worker(stop=stop), 3)
        finally:
            await releasing
            holder.close()

    try:
        asyncio.run(exercise())
        assert calls == [(KIND, idea)]
        assert "database busy" in caplog.text
        with factory() as session:
            saved = session.get(Job, job_id)
            assert saved.status == "succeeded" and saved.attempts == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_duplicate_delivery_exit_frees_capacity_without_releasing_or_replaying_lease(tmp_path, monkeypatch, idea):
    from app.tenancy import Scope
    import app.bootstrap as boot
    import app.state as state
    from app.worker import health

    engine = make_engine(f"sqlite:///{tmp_path / 'duplicate.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        duplicate = enqueue(session, "content.topics", {"idea": idea})
        independent = enqueue(session, KIND, {"idea": idea})
        session.commit()
    monkeypatch.setattr(boot, "bootstrap", lambda: None)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(health, "write", lambda *_: None)
    monkeypatch.setattr(worker, "install_default_handlers", lambda: None)
    monkeypatch.setattr(worker, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(worker, "_start_health", lambda _: None)
    monkeypatch.setattr(worker, "enabled_kinds", lambda: {"content.topics", KIND})
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    monkeypatch.setattr(worker, "_job_scope", lambda _: Scope("none", "", "system"))
    monkeypatch.setattr(worker, "CONCURRENCY", 1)
    monkeypatch.setattr(worker, "POLL_S", 0.01)
    monkeypatch.setattr(worker, "GRACE_S", 0.1)
    calls = []

    async def dispatch(kind, payload):
        calls.append((kind, payload["idea"]))
        if kind == "content.topics":
            raise worker.FatalError("content_operation_in_progress")
        with factory() as session:
            row = session.get(Job, duplicate)
            assert row.status == "leased" and row.attempts == 1
            assert row.finished_at is None and row.last_error is None
        os.kill(os.getpid(), signal.SIGTERM)

    async def idle(stop):
        await stop.wait()

    async def run():
        await asyncio.wait_for(worker.run_worker(), timeout=3)

    monkeypatch.setattr(worker, "dispatch", dispatch)
    monkeypatch.setattr(worker, "_scheduler_loop", idle)
    monkeypatch.setattr(worker, "_redis_listener", lambda wake: asyncio.sleep(10))
    try:
        asyncio.run(run())
        assert calls == [("content.topics", idea), (KIND, idea)]
        with factory() as session:
            assert session.get(Job, duplicate).status == "leased"
            assert session.get(Job, independent).status == "succeeded"
    finally:
        engine.dispose()


def _run(tmp_path, monkeypatch, *, job_seconds, grace, seen=None, kind=KIND, calling=False):
    engine = make_engine(f"sqlite:///{tmp_path / 'w.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        job_id = enqueue(session, kind, {})
        session.commit()

    async def slow(kind, payload):
        if calling:
            from app.db.models import Venture
            from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession
            from core.clock import utcnow
            with factory() as session:
                job = session.get(Job, job_id)
                now = utcnow()
                session.add(Venture(id="v", workspace_id="w", name="Test", raw_idea="Test", stage="idea", created_at=now, updated_at=now))
                session.flush()
                session.add(IdeaSession(id="s", workspace_id="w", venture_id="v", case="know", created_at=now, updated_at=now))
                session.flush()
                session.add(IdeaRevision(id="r", session_id="s", workspace_id="w", venture_id="v", rev=1, source="founder", created_at=now))
                session.flush()
                session.add(IdeaClarificationRun(id="drain-clarify", job_id=job_id, session_id="s",
                    workspace_id="w", venture_id="v", revision_id="r", state="calling",
                    attempt=job.attempts, worker_id=job.locked_by, snapshot="{}", usage="{}",
                    created_at=utcnow(), updated_at=utcnow()))
                session.commit()
        if seen is not None:
            seen.append(asyncio.get_running_loop()._default_executor)
        os.kill(os.getpid(), signal.SIGTERM)  # a deploy arrives while the job is in flight
        await asyncio.sleep(job_seconds)

    import app.bootstrap as boot
    import app.state as state
    from app.worker import health
    monkeypatch.setattr(boot, "bootstrap", lambda: None)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(health, "write", lambda *_: None)
    monkeypatch.setattr(worker, "install_default_handlers", lambda: None)
    monkeypatch.setattr(worker, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(worker, "_start_health", lambda _id: None)
    monkeypatch.setattr(worker, "enabled_kinds", lambda: {kind})
    monkeypatch.setattr(worker, "dispatch", slow)
    monkeypatch.setattr(worker, "GRACE_S", grace)
    monkeypatch.setattr(worker, "POLL_S", 0.05)
    if calling:
        from app.tenancy import Scope
        monkeypatch.setattr(worker, "_job_scope", lambda _: Scope("none", "", "system"))

    async def idle(stop):
        await stop.wait()

    monkeypatch.setattr(worker, "_scheduler_loop", idle)
    async def run():
        await asyncio.wait_for(worker.run_worker(), timeout=10)

    asyncio.run(run())
    return factory, job_id


def test_in_flight_job_finishes_within_grace(tmp_path, monkeypatch):
    factory, job_id = _run(tmp_path, monkeypatch, job_seconds=0.2, grace=5)
    with factory() as session:
        assert session.get(Job, job_id).status in {"succeeded", "done"}


def test_thread_pool_fits_concurrent_sync_jobs(tmp_path, monkeypatch):
    # The default pool (cpu + 4) let slow email sends starve interactive turns in production.
    seen = []
    _run(tmp_path, monkeypatch, job_seconds=0.05, grace=5, seen=seen)
    assert seen[0]._max_workers >= worker.CONCURRENCY * 2


def test_job_outliving_grace_is_released_as_retryable(tmp_path, monkeypatch):
    factory, job_id = _run(tmp_path, monkeypatch, job_seconds=5, grace=0.2)
    with factory() as session:
        row = session.get(Job, job_id)
        assert (row.status, row.locked_by) == ("retry_wait", None)
        assert len(lease(session, "next-worker", kinds=[KIND])) == 1


def test_clarification_call_outliving_grace_is_not_released_mid_call(tmp_path, monkeypatch):
    factory, job_id = _run(tmp_path, monkeypatch, job_seconds=5, grace=0.2,
        kind="idea.clarify_turn", calling=True)
    with factory() as session:
        row = session.get(Job, job_id)
        assert row.status == "leased" and row.locked_by is not None
    with factory() as session:
        assert len(lease(session, "next-worker", kinds=["idea.clarify_turn"])) == 0


def test_worker_dispatches_interactive_while_bulk_is_blocked(tmp_path, monkeypatch):
    import app.bootstrap as boot
    import app.state as state
    from app.worker import health

    engine = make_engine(f"sqlite:///{tmp_path / 'reserved.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    with factory() as session:
        for number in range(3):
            enqueue(session, KIND, {"number": number})
        session.commit()
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    monkeypatch.setattr(boot, "bootstrap", lambda: None)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(health, "write", lambda *_: None)
    monkeypatch.setattr(worker, "install_default_handlers", lambda: None)
    monkeypatch.setattr(worker, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(worker, "_start_health", lambda _id: None)
    monkeypatch.setattr(worker, "enabled_kinds", lambda: {KIND, "idea.clarify_turn"})
    monkeypatch.setattr(worker, "CONCURRENCY", 3)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    monkeypatch.setattr(worker, "POLL_S", 0.01)

    async def run():
        active = []
        blocked = asyncio.Event()
        finished = asyncio.Event()

        async def dispatch(kind, payload):
            active.append(kind)
            if kind == KIND:
                if active.count(KIND) == 2:
                    blocked.set()
                await finished.wait()
            else:
                assert active.count(KIND) == 2
                finished.set()

        async def scheduler(stop):
            await blocked.wait()
            with factory() as session:
                interactive = enqueue(session, "idea.clarify_turn", {}, workspace_id="ws_latency")
                session.commit()
            await finished.wait()
            stop.set()
            return interactive

        monkeypatch.setattr(worker, "dispatch", dispatch)
        monkeypatch.setattr(worker, "_scheduler_loop", scheduler)
        await asyncio.wait_for(worker.run_worker(), 5)
        assert active == [KIND, KIND, "idea.clarify_turn"]
        with factory() as session:
            rows = session.query(Job).all()
            assert sum(row.status in {"succeeded", "done"} for row in rows) == 3
            assert sum(row.status == "scheduled" for row in rows) == 1

    asyncio.run(run())
