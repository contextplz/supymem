"""Outreach admission: real owned rows and a fake provider, never external email."""
from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from tests.unit.autopilot.test_outreach import _contacts, _on, _run, env as env, mail as mail


def _emulate_pg_advisory_locks(engine):
    locks = {}

    def connect(connection, record, proxy):
        def acquire(key):
            owner = locks.get(key)
            if owner is not None and owner is not connection:
                return False
            locks[key] = connection
            return True
        connection.create_function("pg_try_advisory_xact_lock", 1, acquire)

    def release(connection):
        owner = connection.connection.dbapi_connection
        for key in [key for key, holder in locks.items() if holder is owner]:
            del locks[key]

    event.listen(engine, "checkout", connect)
    event.listen(engine, "rollback", release)
    event.listen(engine, "commit", release)


@pytest.fixture
def postgres_admission(tmp_path, monkeypatch):
    from app.autopilot import send_admission

    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    _emulate_pg_advisory_locks(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE provider_admission_fences (scope TEXT PRIMARY KEY, "
            "token TEXT NOT NULL, acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"))
    engine.dialect.name = "postgresql"
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(send_admission, "db", lambda: factory)
    yield factory, engine
    engine.dispose()


@pytest.mark.parametrize("domain", ["fleet-inspection.example", "home-gardening.example"])
@pytest.mark.parametrize("old_first", [True, False])
def test_mixed_version_senders_share_production_advisory_lock(env, postgres_admission, domain, old_first):
    from app.autopilot.send_admission import try_send_admission

    factory, engine = postgres_admission
    key = int.from_bytes(hashlib.sha256(("outreach-send:" + domain).encode()).digest()[:8], "big", signed=True)
    if old_first:
        with factory() as old:
            assert old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
            with try_send_admission(domain) as admitted:
                assert not admitted
            assert not old.scalar(text("SELECT count(*) FROM provider_admission_fences"))
    else:
        with try_send_admission(domain) as admitted:
            assert admitted
            assert engine.pool.checkedout() == 0
            with factory() as old:
                assert not old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
            with try_send_admission(domain + "-other") as separate:
                assert separate
    with try_send_admission(domain) as recovered:
        assert recovered
    with factory() as session:
        assert not session.scalar(text("SELECT count(*) FROM provider_admission_fences"))


@pytest.mark.parametrize("domain", ["fleet-inspection.example", "home-gardening.example"])
def test_orphaned_fence_recovered_only_without_production_lock(env, postgres_admission, domain):
    from app.autopilot.send_admission import try_send_admission

    factory, _ = postgres_admission
    key = int.from_bytes(hashlib.sha256(("outreach-send:" + domain).encode()).digest()[:8], "big", signed=True)
    with factory() as session:
        session.execute(text("INSERT INTO provider_admission_fences (scope,token) VALUES (:scope,'killed-owner')"),
            {"scope": domain})
        session.commit()
    with factory() as old:
        assert old.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
        with try_send_admission(domain) as blocked:
            assert not blocked
        assert old.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
            {"scope": domain}) == "killed-owner"
    with try_send_admission(domain) as recovered:
        assert recovered
        with factory() as session:
            assert session.scalar(text("SELECT token FROM provider_admission_fences WHERE scope=:scope"),
                {"scope": domain}) != "killed-owner"
        with try_send_admission(domain) as duplicate:
            assert not duplicate
    with factory() as session:
        assert not session.scalar(text("SELECT count(*) FROM provider_admission_fences"))


def test_sqlite_admission_is_nonblocking_scoped_and_released_on_exception(env):
    from app.autopilot.send_admission import try_send_admission

    _, venture = env
    with pytest.raises(RuntimeError, match="owned interruption"):
        with try_send_admission(venture) as owner:
            assert owner is True
            with try_send_admission(venture) as duplicate:
                assert duplicate is False
            with try_send_admission(venture + "-other") as independent:
                assert independent is True
            raise RuntimeError("owned interruption")
    with try_send_admission(venture) as recovered:
        assert recovered is True


@pytest.mark.parametrize("admitted", [False, True])
def test_postgres_admission_commits_and_closes_before_context_body(env, monkeypatch, admitted):
    from app.autopilot import send_admission

    trace = []
    class Session:
        def __enter__(self):
            trace.append("open")
            return self
        def __exit__(self, *args):
            trace.append("close")
        def get_bind(self):
            lock = SimpleNamespace(detach=lambda: None, execute=lambda *args:
                SimpleNamespace(scalar=lambda: admitted))
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: nullcontext(lock))
        def execute(self, statement, parameters):
            assert set(parameters) == {"scope", "token"}
            trace.append(str(statement))
            return SimpleNamespace(scalar=lambda: parameters["token"] if admitted else None)
        def commit(self):
            trace.append("commit")
    monkeypatch.setattr(send_admission, "db", lambda: Session)
    with send_admission.try_send_admission(env[1]) as got:
        assert got is admitted
        assert trace[0] == "open" and trace[-2:] == ["commit", "close"]
    assert trace[-1] == "close"


def test_admission_closes_on_pg_failure_before_provider_authority(env, monkeypatch):
    from app.autopilot import send_admission

    closed = []
    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
        def get_bind(self):
            lock = SimpleNamespace(detach=lambda: None, execute=lambda *args:
                SimpleNamespace(scalar=lambda: True))
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: nullcontext(lock))
        def execute(self, *args):
            raise RuntimeError("owned database unavailable")
    monkeypatch.setattr(send_admission, "db", lambda: Session)
    with pytest.raises(RuntimeError, match="owned database unavailable"):
        with send_admission.try_send_admission(env[1]):
            pytest.fail("database uncertainty cannot admit delivery")
    assert closed == [True]


@pytest.mark.parametrize("distinct_batch", [False, True])
def test_concurrent_approved_batches_dispatch_once_and_do_not_exceed_daily_cap(mail, monkeypatch, distinct_batch):
    client, venture, sent = mail
    from app.autopilot import outreach

    _on(client, venture, allow_send=True, send_max_per_day=1)
    _contacts(venture, 1, domain="owned-first")
    _run(outreach.draft_batch(venture, review_only=True))
    first_batch = outreach.drafts(venture, "draft")[0]["batch_id"]
    second_batch = first_batch
    if distinct_batch:
        _contacts(venture, 1, domain="owned-second")
        _run(outreach.draft_batch(venture, review_only=True))
        second_batch = next(d["batch_id"] for d in outreach.drafts(venture, "draft")
                            if d["batch_id"] != first_batch)
    started, finish = threading.Event(), threading.Event()
    def fake_send(box, to, subject, body, thread_id):
        sent.append({"to": to})
        started.set()
        assert finish.wait(5), "owned delivery was not released"
        return {"provider_message_id": "owned-message", "thread_id": "owned-thread"}
    monkeypatch.setattr(outreach, "_send_one", fake_send)

    async def concurrent_confirmation():
        owner = asyncio.create_task(outreach.send_batch(venture, {
            "approved_by_founder": True, "batch_id": first_batch}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            blocked = await outreach.send_batch(venture, {
                "approved_by_founder": True, "batch_id": second_batch})
            assert blocked["kind"] == "wait" and "already in progress" in blocked["body_md"]
            assert len(sent) == 1 and outreach.sends_today(venture) == 0
        finally:
            finish.set()
            await owner
    _run(concurrent_confirmation())
    assert len(sent) == 1 and outreach.sends_today(venture) == 1
    assert len(outreach.drafts(venture, "sent")) == 1
    assert len(outreach.drafts(venture, "draft")) == int(distinct_batch)
    _run(outreach.send_batch(venture, {"approved_by_founder": True, "batch_id": second_batch}))
    assert len(sent) == 1 and outreach.sends_today(venture) == 1


def test_cancelled_wait_keeps_inflight_provider_reservation_and_blocks_another_batch(mail, monkeypatch):
    client, venture, sent = mail
    from app.autopilot import outreach

    _on(client, venture, allow_send=True, send_max_per_day=1)
    batches = []
    for domain in ("owned-cancelled", "owned-next"):
        _contacts(venture, 1, domain=domain)
        _run(outreach.draft_batch(venture, review_only=True))
        batches.append(next(d["batch_id"] for d in outreach.drafts(venture, "draft")
                            if d["batch_id"] not in batches))
    started, finish, finished = threading.Event(), threading.Event(), threading.Event()
    def fake_send(box, to, subject, body, thread_id):
        sent.append({"to": to})
        started.set()
        assert finish.wait(5)
        finished.set()
        return {"provider_message_id": "owned-late-receipt", "thread_id": "owned-thread"}
    monkeypatch.setattr(outreach, "_send_one", fake_send)

    async def cancel_then_confirm():
        owner = asyncio.create_task(outreach.send_batch(venture, {
            "approved_by_founder": True, "batch_id": batches[0]}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            # Cancellation releases admission, but cannot undo the provider
            # thread or its persisted reservation. Neither a replay nor a
            # distinct batch may consume a second slot while it is in flight.
            assert len(outreach.drafts(venture, "delivery_pending")) == 1
            assert outreach.sends_today(venture) == 0 and outreach.send_slots_used_today(venture) == 1
            for batch, kind in zip(batches, ("needs_you", "wait")):
                result = await outreach.send_batch(venture, {"approved_by_founder": True, "batch_id": batch})
                assert result["kind"] == kind
            assert len(sent) == 1 and not finished.is_set()
        finally:
            finish.set()
            assert await asyncio.to_thread(finished.wait, 5)
    _run(cancel_then_confirm())
    assert len(sent) == 1 and len(outreach.drafts(venture, "delivery_pending")) == 1
    assert outreach.sends_today(venture) == 0 and outreach.send_slots_used_today(venture) == 1
