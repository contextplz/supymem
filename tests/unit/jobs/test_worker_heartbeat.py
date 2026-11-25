"""Lease renewal survives a blocked loop; stale attempts cannot close newer work."""

import asyncio
import json
import time
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def worker_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import bootstrap, state
    from app.core import jobs, ratelimit
    from app.db.models import Base, Job
    from app.db.session import make_engine
    from app.tenancy import Scope
    from app.worker import handlers, health, main

    engine = make_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(bootstrap, "bootstrap", lambda: None)
    monkeypatch.setattr(health, "write", lambda *_: None)
    monkeypatch.setattr(main, "install_default_handlers", lambda: None)
    monkeypatch.setattr(main, "_recover_orphaned_builds", lambda: 0)
    monkeypatch.setattr(main, "_start_health", lambda _: None)
    monkeypatch.setattr(main, "enabled_kinds", lambda: {"idea.validate"})
    monkeypatch.setattr(main, "_job_scope", lambda _: Scope("none", "", "system"))
    monkeypatch.setattr(ratelimit, "check_job", lambda _: None)
    monkeypatch.setattr(handlers, "require_capability", lambda _: None)
    yield SimpleNamespace(factory=factory, Job=Job, jobs=jobs, main=main, handlers=handlers)
    engine.dispose()


@pytest.mark.parametrize("domain", ["Factory purchase orders", "Birdwatching plans"])
def test_local_worker_rejects_managed_jobs_without_losing_queued_payload(worker_db, monkeypatch, domain):
    import os
    from unittest.mock import Mock

    import boto3
    from app.core.job_status import _view

    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    aws = Mock(side_effect=AssertionError("Local jobs must not use AWS"))
    monkeypatch.setattr(boto3, "client", aws)
    data = worker_db
    kinds = {"infra.provision": {"schema_version": 1, "runtime_intent_id": domain},
             "build.watch": {"runtime_version": 1, "build_run_id": domain}}
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    with data.factory() as session:
        for kind, payload in kinds.items():
            data.jobs.enqueue(session, kind, payload)
        session.commit()
        claimed = data.jobs.lease(session, "local-worker", kinds=list(kinds), n=len(kinds))
        snapshots = {job.id: session.get(data.Job, job.id).payload_json for job in claimed}
        session.commit()
    assert len(claimed) == len(kinds)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for job in claimed:
        asyncio.run(data.main._run_one(data.factory, job, {(job.id, job.attempts)}))
        with data.factory() as session:
            saved = session.get(data.Job, job.id)
            assert saved.status == "failed_dead"
            view = _view(saved, session)
            assert "retry through your connected coding tool" in view["failure_reason"].lower()
            assert view["error_code"] == data.jobs.LOCAL_MANAGED_BUILD_CODE
            assert saved.payload_json == snapshots[job.id]
            with pytest.raises(ValueError, match="retry through your connected coding tool"):
                data.jobs.enqueue(session, job.kind, kinds[job.kind])
    aws.assert_not_called()


@pytest.mark.parametrize("domain", ["Factory purchase orders", "Birdwatching plans"])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("deferred", ["capability", "paused"])
def test_deferred_job_refunds_attempt_and_releases_owned_lease(worker_db, monkeypatch, domain, autoflush, deferred):
    from app.core import local_worker
    from app.tasks import service
    from core import clock

    data = worker_db
    now = clock.utcnow()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(clock, "_clock", lambda: now)
    data.factory.configure(autoflush=autoflush)
    monkeypatch.setattr(data.main, "dispatch", lambda *args: pytest.fail("Deferred work was dispatched"))
    monkeypatch.setattr(local_worker, "blocked_reason", lambda _: "Open Settings > Capabilities and switch on Landing pages." if deferred == "capability" else None)
    if deferred == "paused":
        monkeypatch.setattr(service, "before_job", lambda *args: False)
        monkeypatch.setattr(service, "job_step", lambda *args: (SimpleNamespace(state="paused"), None, None))
    kind = "pagebuilder.build" if deferred == "capability" else "tasks.verify"
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, kind, {"domain": domain}, max_attempts=1)
        session.commit()
        job, = data.jobs.lease(session, "deferring-worker", kinds=[kind], now=now)
        session.commit()
    asyncio.run(data.main._run_one(data.factory, job, {(job.id, job.attempts)}))
    with data.factory() as session:
        row = session.get(data.Job, job_id)
        assert row.status == "retry_wait" and row.attempts == 0
        assert row.locked_by is None and row.locked_at is None
        assert row.run_after == (now + timedelta(minutes=1)).replace(tzinfo=None)
        assert data.jobs.lease(session, "next-worker", kinds=[kind], now=now) == []
        resumed, = data.jobs.lease(session, "next-worker", kinds=[kind], now=now + timedelta(minutes=1))
        assert resumed.id == job_id and resumed.attempts == 1


@pytest.mark.parametrize("idea", ["Inventory alerts for workshops", "Practice plans for pianists"])
def test_local_in_process_worker_finishes_a_scheduled_sqlite_job(worker_db, monkeypatch, caplog, idea, tmp_path):
    from app.core import local_worker
    from app.db.task_models import MaintenanceSchedule
    from app.worker import health
    from core.clock import utcnow

    data = worker_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "1")
    monkeypatch.setenv("COS_WORKER_HEALTH_PATH", str(tmp_path / "worker-health.json"))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    monkeypatch.setattr(data.main, "POLL_S", 0.01)
    monkeypatch.setattr(data.main, "_run_maintenance", lambda: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Local workers must not bind a health port or replace API signal handlers")

    monkeypatch.setattr(data.main, "_start_health", forbidden)
    monkeypatch.setattr(health, "write", forbidden)
    from app import bootstrap
    monkeypatch.setattr(bootstrap, "bootstrap", forbidden)
    due = utcnow() + timedelta(milliseconds=50)
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "idea.validate", {"idea": idea},
            run_after=due)
        session.commit()

    async def exercise():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", forbidden)
        executor = loop._default_executor
        finished = asyncio.Event()

        async def dispatch(kind, payload):
            assert kind == "idea.validate" and payload["idea"] == idea
            assert payload["_job_id"] == job_id
            assert utcnow() >= due
            finished.set()

        monkeypatch.setattr(data.main, "dispatch", dispatch)
        async with local_worker.lifespan():
            await asyncio.wait_for(finished.wait(), timeout=5)
            assert health.healthy()
        assert not health.healthy()
        assert executor is None or loop._default_executor is executor

    asyncio.run(exercise())
    with data.factory() as session:
        job = session.get(data.Job, job_id)
        assert job.status == "succeeded" and job.finished_at is not None
        assert job.locked_by is None and job.attempts == 1
        assert session.query(MaintenanceSchedule).count() > 0
    assert not any(record.message == "database scheduler failed" for record in caplog.records)


@pytest.mark.parametrize("domain", ["invoiceharbor.example", "trailnest.example"])
def test_job_failed_log_has_bounded_redacted_diagnostics(worker_db, monkeypatch, caplog, domain):
    from core.logging import JsonFormatter

    data = worker_db
    secret, email, phone = "sk-proj-" + "aB3dE5" * 8, "founder@" + domain, "415-555-0198"
    message = f"Missing audience; email={email}; phone={phone}; token={secret}; " + "detail " * 200
    def fail(depth):
        if depth:
            return fail(depth - 1)
        raise ValueError(message)
    async def dispatch(_kind, _payload):
        fail(12)
    monkeypatch.setattr(data.main, "dispatch", dispatch)
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "idea.validate", {})
        session.commit()
        job = data.jobs.lease(session, "diagnostic-worker")[0]
        session.commit()
    asyncio.run(data.main._run_one(data.factory, job, {(job.id, job.attempts)}))
    records = [record for record in caplog.records if record.getMessage() == "job failed"]
    assert len(records) == 1
    record = records[0]
    assert record.error_type == "ValueError" and record.error_code == "handler_failed:ValueError"
    assert record.exception_message.startswith("Missing audience") and len(record.exception_message) <= 512
    assert len(record.traceback) <= 2048 and len(record.traceback.splitlines()) == 8
    assert "test_worker_heartbeat.py:" in record.traceback and " in fail" in record.traceback
    serialized = JsonFormatter().format(record)
    assert all(value not in serialized for value in (secret, email, phone, "raise ValueError(message)", str(__file__)))
    with data.factory() as session:
        saved = session.get(data.Job, job_id)
        assert saved.last_error == "handler_failed:ValueError" and saved.status == "retry_wait"


@pytest.mark.parametrize("idea", ["Invoice matching for finance teams", "Hiking plans for families"])
def test_background_reply_keeps_its_request_nonce_without_changing_stop_identity(worker_db, monkeypatch, idea):
    from app.chat.store import CLIENT_REQUEST_ID, REPLY_REQUEST_ID, request_citations

    data = worker_db
    observed = []
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "idea.validate", {"idea": idea, "_chat_request_nonce": "earlier-option"})
        session.commit()

    def reply(payload):
        assert payload["idea"] == idea
        assert CLIENT_REQUEST_ID.get() is None
        observed.append(request_citations())
        with data.factory() as session:
            child = data.jobs.enqueue(session, "idea.validate", {})
            assert json.loads(session.get(data.Job, child).payload_json)["_chat_request_nonce"] == "earlier-option"
            session.rollback()

    monkeypatch.setattr(data.handlers, "HANDLERS", {"idea.validate": reply})
    asyncio.run(data.main.run_worker(once=True))
    assert observed == [[{"client_request_id": "earlier-option"}]]
    assert REPLY_REQUEST_ID.get() is None and CLIENT_REQUEST_ID.get() is None
    with data.factory() as session:
        assert session.get(data.Job, job_id).status == "succeeded"


@pytest.mark.parametrize("domain", ["freightledger.example", "birdwalk.example"])
@pytest.mark.parametrize("fatal", [False, True])
def test_json_exception_redacts_quoted_keys_and_unshaped_vault_values(worker_db, monkeypatch, caplog, domain, fatal):
    from app.boundary.detectors import vault as vault_detector
    from app.connectors import secrets
    from app.db.models import Person, Workspace
    from app.secrets.boundary import register_vault_boundary
    from core.clock import utcnow
    from core.logging import JsonFormatter

    data, now = worker_db, utcnow()
    workspace_id, actor = "ws_" + domain, "usr_" + domain
    with data.factory() as session:
        session.add(Person(id=actor, display_name=domain, created_at=now))
        session.flush()
        session.add(Workspace(id=workspace_id, kind="company", name=domain, entry_mode="idea", owner_id=actor,
            created_at=now, updated_at=now))
        session.commit()
    vault = secrets.LocalSecretStore()
    credential, vault_value = "OpaqueCredentialFor" + domain.split(".")[0], "VaultOnlyValueFor" + domain.split(".")[0]
    vault.put(workspace_id=workspace_id, provider="test", key="api_key", value=vault_value)
    monkeypatch.setattr(secrets, "store", lambda: vault)
    monkeypatch.setattr(vault_detector, "_providers", [])
    register_vault_boundary()
    message = json.dumps({"api_key": credential, "context": vault_value, "reason": "provider rejected request"})

    async def dispatch(_kind, _payload):
        raise (data.jobs.FatalError if fatal else ValueError)(message)

    monkeypatch.setattr(data.main, "dispatch", dispatch)
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "tasks.verify", {}, workspace_id=workspace_id)
        session.commit()
        job = data.jobs.lease(session, "json-diagnostic-worker")[0]
        session.commit()
    asyncio.run(data.main._run_one(data.factory, job, {(job.id, job.attempts)}))
    records = [record for record in caplog.records if record.getMessage() == "job failed"]
    assert len(records) == 1
    assert "provider rejected request" in records[0].exception_message
    serialized = JsonFormatter().format(records[0])
    assert credential not in serialized and vault_value not in serialized
    with data.factory() as session:
        saved = session.get(data.Job, job_id)
        assert saved.last_error == (message if fatal else "handler_failed:ValueError")
        assert saved.status == ("failed_dead" if fatal else "retry_wait")


@pytest.mark.parametrize("blocking_async", [False, True], ids=["sync-handler", "blocked-event-loop"])
@pytest.mark.parametrize("kind", ["idea.validate", "idea.clarify_turn"])
def test_blocking_handler_keeps_lease_and_completes(worker_db, monkeypatch, blocking_async, kind):
    data = worker_db
    monkeypatch.setattr(data.main, "enabled_kinds", lambda: {kind})
    monkeypatch.setattr(data.jobs, "STALE_LOCK_S", 0.3)
    reclaim = data.jobs.reclaim_stale
    monkeypatch.setattr(data.jobs, "reclaim_stale", lambda session, **kwargs:
                        reclaim(session, stale_s=data.jobs.STALE_LOCK_S, **kwargs))
    monkeypatch.setattr(data.main, "HEARTBEAT_S", 0.015)
    started, stop = Event(), Event()
    observations, reclaimed, errors = [], [], []
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, kind, {})
        session.commit()

    def block(payload):
        started.set()
        time.sleep(data.jobs.STALE_LOCK_S * 3)

    async def block_loop(payload):
        block(payload)

    monkeypatch.setattr(data.handlers, "HANDLERS", {kind: block_loop if blocking_async else block})

    def watchdog():
        try:
            assert started.wait(5)
            while not stop.wait(0.04):
                with data.factory() as session:
                    reclaimed.append(data.jobs.reclaim_stale(session))
                    session.commit()
                    row = session.get(data.Job, job_id)
                    observations.append((row.status, row.attempts, row.locked_at))
        except Exception as exc:
            errors.append(exc)

    watcher = Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        asyncio.run(data.main.run_worker(once=True))
    finally:
        stop.set()
        watcher.join(5)
    assert not watcher.is_alive() and not errors
    assert len(observations) >= 3 and not any(reclaimed)
    assert all(status in {"leased", "succeeded"} and attempt == 1 for status, attempt, _ in observations)
    renewals = [locked_at for _, _, locked_at in observations if locked_at is not None]
    assert (max(renewals) - min(renewals)).total_seconds() > data.jobs.STALE_LOCK_S
    with data.factory() as session:
        row = session.get(data.Job, job_id)
        assert (row.status, row.attempts, row.last_error) == ("succeeded", 1, None)


@pytest.mark.parametrize("idea", ["Inventory alerts for workshops", "Practice plans for pianists"])
def test_missing_setup_does_not_consume_the_final_page_attempt(worker_db, monkeypatch, idea):
    from app.core import local_worker
    from core.clock import utcnow

    data = worker_db
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    reason = "Open Settings > Keys and connections > Coding tools, connect your tool, then retry."
    monkeypatch.setattr(local_worker, "blocked_reason", lambda *_: reason)
    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "pagebuilder.build", {"idea": idea}, max_attempts=3)
        session.get(data.Job, job_id).attempts = 2
        session.commit()
        job, = data.jobs.lease(session, "page_worker", now=utcnow())
        session.commit()
    asyncio.run(data.main._run_one(data.factory, job, set()))
    with data.factory() as session:
        row = session.get(data.Job, job_id)
        assert (row.status, row.attempts, row.last_error) == ("retry_wait", 2, reason)
        assert row.finished_at is None


@pytest.mark.parametrize("replacement_worker", ["same-worker", "different-worker"])
@pytest.mark.parametrize("fatal", [False, True], ids=["retryable", "fatal"])
def test_stale_attempt_cannot_mutate_new_lease(worker_db, replacement_worker, fatal):
    data = worker_db
    from core.clock import utcnow

    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "idea.validate", {})
        session.commit()
        old = data.jobs.lease(session, "same-worker")[0]
        session.get(data.Job, job_id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
        new = data.jobs.lease(session, replacement_worker)[0]
        session.commit()
        before = session.get(data.Job, job_id).locked_at
    ownership = {"worker_id": old.worker_id, "attempt": old.attempts}
    error = data.jobs.FatalError("research_lease_lost") if fatal else data.jobs.RetryableError("provider_retryable")
    with data.factory() as session:
        assert data.jobs.fail_job(session, job_id, error, **ownership) is None
        data.jobs.complete_job(session, job_id, **ownership)
        data.jobs.release_lease(session, job_id, **ownership)
        data.jobs.heartbeat(session, job_id, **ownership)
        session.commit()
        row = session.get(data.Job, job_id)
        assert (row.status, row.locked_by, row.attempts, row.locked_at, row.last_error, row.finished_at) == (
            "leased", replacement_worker, new.attempts, before, None, None)
        data.jobs.complete_job(session, job_id, worker_id=new.worker_id, attempt=new.attempts)
        session.commit()
        assert session.get(data.Job, job_id).status == "succeeded"


def test_lost_research_fence_does_not_remove_new_attempt_or_emit_terminal_receipt(worker_db, monkeypatch):
    data = worker_db
    from app.analytics import error_events
    from app.research.execution import fence
    from core.clock import utcnow

    with data.factory() as session:
        job_id = data.jobs.enqueue(session, "idea.validate", {})
        session.commit()
        old = data.jobs.lease(session, "same-worker")[0]
        session.commit()
    running = {(job_id, old.attempts)}
    receipts = []
    monkeypatch.setattr(error_events, "record_job_failure", lambda *args, **kwargs: receipts.append(kwargs))

    async def lose_lease(kind, payload):
        with data.factory() as session:
            session.get(data.Job, job_id).locked_at = utcnow() - timedelta(seconds=120)
            session.commit()
            new = data.jobs.lease(session, "same-worker")[0]
            session.commit()
        running.add((job_id, new.attempts))
        with data.factory() as session:
            fence(session, payload)

    monkeypatch.setattr(data.main, "dispatch", lose_lease)
    asyncio.run(data.main._run_one(data.factory, old, running))
    assert running == {(job_id, old.attempts + 1)} and receipts == []
    with data.factory() as session:
        row = session.get(data.Job, job_id)
        assert (row.status, row.locked_by, row.attempts, row.last_error, row.finished_at) == (
            "leased", old.worker_id, old.attempts + 1, None, None)


def test_inline_stale_attempt_cannot_fail_newer_lease(worker_db, monkeypatch):
    data = worker_db
    from core.clock import utcnow

    async def lose_lease(kind, payload):
        with data.factory() as session:
            row = session.query(data.Job).filter_by(status="leased").one()
            row.locked_at = utcnow() - timedelta(seconds=120)
            session.commit()
            data.jobs.lease(session, "inline")[0]
            session.commit()
        raise data.jobs.FatalError("research_lease_lost")

    monkeypatch.setattr(data.handlers, "dispatch", lose_lease)
    monkeypatch.setattr(data.handlers, "install_default_handlers", lambda: None)
    with pytest.raises(data.jobs.FatalError, match="research_lease_lost"):
        asyncio.run(data.jobs.run_inline("idea.validate", {}))
    with data.factory() as session:
        row = session.query(data.Job).one()
        assert (row.status, row.locked_by, row.attempts, row.last_error, row.finished_at) == (
            "leased", "inline", 2, None, None)
