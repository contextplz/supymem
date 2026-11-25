"""Worker-slot regression checks using local providers and persisted jobs."""
import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.test_email_engine import env as env, _needs_context


@pytest.fixture(params=[
    ("Ledger review", "Compliance evidence for payroll teams"),
    ("Kitchen planner", "Weekly recipes for home cooks"),
], ids=["b2b", "consumer"])
def queued_email(env, monkeypatch, request):
    from app.db.models import Venture, Workspace
    from app.email import service
    from app.worker.handlers import install_default_handlers

    _, state = env
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("SUPYMEM_EMAIL_TEMPLATE_ALLOWLIST", raising=False)
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "true")
    install_default_handlers()
    context = _needs_context(state)
    with state.db()() as session:
        venture = session.get(Venture, "ven_email_usr_a")
        venture.name, venture.raw_idea = request.param
        session.get(Workspace, venture.workspace_id).name = venture.name
        session.commit()
    message_id = service.send_email("usr_a", "needs_you", context, ref="worker-pacing")
    assert message_id
    return state, message_id


@pytest.mark.parametrize("cancel_during_admission", [False, True], ids=["busy", "cancel"])
def test_busy_send_fence_reschedules_the_same_lease_without_spending_attempts(queued_email, monkeypatch, cancel_during_admission):
    from app.core import jobs
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    from app.email import providers, service
    from app.worker import main as worker

    state, message_id = queued_email
    clock = service.utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: clock)
    monkeypatch.setattr(jobs, "utcnow", lambda: clock)
    queries, outgoing = [], []
    admitted = False
    original_lock = service._lock

    def execute(statement, parameters):
        queries.append(str(statement))
        assert str(statement) == "SELECT pg_try_advisory_xact_lock(:key)"
        assert parameters["key"] == int.from_bytes(
            service.hashlib.sha256(b"email:global_send").digest()[:8], "big", signed=True)
        if cancel_during_admission:
            with state.db()() as session:
                session.get(Job, job_id).cancel_requested = True
                session.commit()
        return SimpleNamespace(scalar=lambda: admitted)

    postgres = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")), execute=execute)

    def lock(session, scope, **kwargs):
        return original_lock(postgres if scope == "global_send" else session, scope, **kwargs)

    def no_sleep(_seconds):
        pytest.fail("Email workers must schedule contention, not sleep")

    monkeypatch.setattr(service, "_lock", lock)
    monkeypatch.setattr(time, "sleep", no_sleep)
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: outgoing.append(message) or "local_receipt")
    with state.db()() as session:
        job = session.scalar(select(Job).where(Job.dedupe_key == "email.send:" + message_id))
        job_id, maximum = job.id, job.max_attempts
        job.attempts = maximum - 1
        session.commit()

    for _round in range(2):
        with state.db()() as session:
            claimed = jobs.lease(session, "pacing-worker", kinds=["email.send"], now=clock)[0]
            session.commit()
        running = {(claimed.id, claimed.attempts)}
        asyncio.run(worker._run_one(state.db(), claimed, running))
        assert running == set() and outgoing == []
        with state.db()() as session:
            job = session.get(Job, job_id)
            if cancel_during_admission:
                assert job.status == "cancelled" and job.locked_by is None and job.finished_at is not None
                assert jobs.lease(session, "late-worker", kinds=["email.send"], now=clock + timedelta(hours=1)) == []
                assert len(queries) == 1
                return
            assert job.status == "retry_wait" and job.locked_by is None
            assert job.attempts == maximum - 1 and job.max_attempts == maximum
            assert job.run_after == job.scheduled_for == clock + timedelta(seconds=15)
            assert jobs.lease(session, "early-worker", kinds=["email.send"], now=clock) == []
            assert session.get(EmailMessage, message_id).status == "queued"
            assert len(session.scalars(select(Job).where(Job.kind == "email.send")).all()) == 1
            clock = job.run_after
    assert len(queries) == 2
    admitted = True
    with state.db()() as session:
        claimed = jobs.lease(session, "pacing-worker", kinds=["email.send"], now=clock)[0]
        session.commit()
    asyncio.run(worker._run_one(state.db(), claimed, {(claimed.id, claimed.attempts)}))
    with state.db()() as session:
        assert session.get(Job, job_id).status == "succeeded"
        assert session.get(EmailMessage, message_id).status == "sent"
    assert len(outgoing) == 1 and outgoing[0].idempotency_key


def test_paused_send_uses_run_after_even_when_redis_wake_fails(queued_email, monkeypatch):
    import redis
    from app.core import jobs
    from app.db.models import Job
    from app.db.supymem_models import EmailMessage
    from app.email import providers, service
    from app.worker import main as worker

    state, message_id = queued_email
    clock = service.utcnow()
    monkeypatch.setattr(service, "utcnow", lambda: clock)
    monkeypatch.setenv("SUPYMEM_EMAIL_LIFECYCLE_ENABLED", "false")
    monkeypatch.setenv("REDIS_URL", "redis://unused.invalid/0")
    options, wakes, outgoing = [], [], []

    def publish(channel, message):
        wakes.append((channel, message))
        raise redis.TimeoutError("local simulated timeout")

    def client(_url, **kwargs):
        options.append(kwargs)
        return SimpleNamespace(publish=publish)

    monkeypatch.setattr(redis.Redis, "from_url", client)
    monkeypatch.setitem(providers.PROVIDERS, "log", lambda message: outgoing.append(message) or "local_receipt")
    with state.db()() as session:
        claimed = jobs.lease(session, "paused-worker", kinds=["email.send"])[0]
        session.commit()
    running = {(claimed.id, claimed.attempts)}
    asyncio.run(worker._run_one(state.db(), claimed, running))
    assert running == set() and outgoing == []
    assert wakes == [(jobs.WAKE_CHANNEL, "1")] and len(options) == 1
    assert options[0]["socket_connect_timeout"] == options[0]["socket_timeout"] == 0.2
    assert options[0]["retry"].get_retries() == 0
    with state.db()() as session:
        assert session.get(Job, claimed.id).status == "succeeded"
        row = session.get(EmailMessage, message_id)
        assert row.status == "queued" and row.error == "lifecycle_disabled"
        later = session.scalar(select(Job).where(Job.id != claimed.id, Job.kind == "email.send"))
        assert later.run_after == clock + timedelta(hours=1) and later.attempts == 0
