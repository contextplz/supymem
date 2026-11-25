"""Lease, dedupe replace, retry, two-worker race (PG when available)."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker
from core.clock import utcnow
from core.enums import JobStatus

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.company_os_harness import load_company_os_app
from tests.unit.db.test_scale_indexes import scale_engine as scale_engine

load_company_os_app()

from app.core.jobs import (  # noqa: E402 - app namespace selected above
    FatalError,
    RetryableError,
    enqueue,
    fail_job,
    lease,
    reclaim_stale,
)
from app.db.models import Base, Job  # noqa: E402
from app.db.session import make_engine  # noqa: E402


@pytest.fixture()
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lease.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=Session)


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Family trail planning"])
def test_local_failed_build_job_writes_private_diagnostics(factory, monkeypatch, tmp_path, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    with factory() as session:
        job_id = enqueue(session, "pagebuilder.build", {"domain": domain})
        session.commit()
        job, = lease(session, "local_worker", kinds=["pagebuilder.build"])
        try:
            raise FatalError(domain)
        except FatalError as error:
            assert fail_job(session, job_id, error, worker_id="local_worker", attempt=job.attempts) == "failed_dead"
        session.commit()
    path = tmp_path / "logs/errors.jsonl"
    assert path.exists()
    entry, = [json.loads(line) for line in path.read_text().splitlines()]
    assert entry["code"] == "local_job_failed" and entry["error_type"] == "FatalError"
    assert entry["frames"][0].startswith(__name__ + ":")
    assert domain not in path.read_text()


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
def test_released_page_job_stops_at_retry_limit(factory, monkeypatch, domain, interrupted, exhausted):
    from app.core.jobs import release_lease
    from app.pagebuilder.local import failure_reason

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    clock = utcnow()
    with factory() as session:
        job_id = enqueue(session, "pagebuilder.build", {"domain": domain, "_runtime": {"state": "running"}},
                         max_attempts=3, run_after=clock)
        if exhausted:
            row = session.get(Job, job_id)
            row.status, row.attempts = "retry_wait", 9
        session.commit()
        if not exhausted:
            for attempt in range(1, 4):
                claimed, = lease(session, "page_worker", kinds=["pagebuilder.build"], now=clock)
                assert claimed.attempts == attempt
                release_lease(session, job_id, now=clock, worker_id="page_worker", attempt=attempt,
                              interrupted=interrupted)
                session.commit()
                assert session.get(Job, job_id).status == ("retry_wait" if attempt < 3 else "failed_dead")
        assert lease(session, "page_worker", kinds=["pagebuilder.build"], now=clock) == []
        row = session.get(Job, job_id)
        assert row.status == "failed_dead" and row.finished_at is not None
        assert row.attempts == (9 if exhausted else 3)
        assert row.locked_by is None and row.locked_at is None
        assert row.last_error == "pagebuilder_retry_limit"
        assert json.loads(row.payload_json)["_runtime"]["state"] == "failed"
        assert "Open Build activity" in failure_reason(row.last_error)
        assert "Your saved pages are unchanged" in failure_reason(row.last_error)


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
def test_healthy_coding_order_polls_do_not_use_page_retries(factory, monkeypatch, domain):
    from app.build.device_orders import KIND
    from app.core.jobs import release_lease
    from app.db.models import VentureDocument

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    clock = utcnow()
    with factory() as session:
        job_id = enqueue(session, "pagebuilder.build", {"domain": domain, "_actor_subject": "usr_owner",
            "tool_build": {"order_id": "dwo_owned"}}, workspace_id="wsp_owned", venture_id="ven_owned",
            max_attempts=3, run_after=clock)
        order = VentureDocument(id="dwo_owned", venture_id="ven_owned", kind=KIND, title=domain,
            created_at=clock, updated_at=clock, lease_expires_at=clock + timedelta(minutes=10),
            provenance_json=json.dumps({"state": "claimed", "workspace_id": "wsp_owned", "actor_id": "usr_owner",
                "manifest": {"coding_loop_verification": {"operation": "workspace_loop_v1"}},
                "task_snapshot": {"pagebuilder_job_id": job_id}}))
        session.add(order)
        session.commit()
        for poll in range(8):
            now = clock + timedelta(seconds=15 * poll)
            claimed, = lease(session, "page_worker", kinds=["pagebuilder.build"], now=now)
            release_lease(session, job_id, now=now, worker_id="page_worker", attempt=claimed.attempts,
                          interrupted=False)
            session.commit()
            assert session.get(Job, job_id).status == "retry_wait"
            assert session.get(Job, job_id).attempts == 1
        order.lease_expires_at = clock - timedelta(seconds=1)
        session.commit()
        for attempt in (2, 3):
            now = clock + timedelta(minutes=3)
            claimed, = lease(session, "page_worker", kinds=["pagebuilder.build"], now=now)
            assert claimed.attempts == attempt
            release_lease(session, job_id, now=now, worker_id="page_worker", attempt=attempt, interrupted=False)
            session.commit()
        assert session.get(Job, job_id).status == "failed_dead"
        assert session.get(Job, job_id).last_error == "pagebuilder_retry_limit"


def test_interactive_lease_precedes_bulk_and_logs_queue_wait(factory, caplog):
    with factory() as session:
        bulk = enqueue(session, "gtm.journey.prepare", {}, priority=0)
        interactive = enqueue(session, "idea.clarify_turn", {}, priority=9)
        clock = utcnow()
        session.get(Job, interactive).created_at = clock - timedelta(seconds=12)
        session.commit()
        with caplog.at_level("INFO", logger="app.jobs"):
            claimed = lease(session, "worker", n=1, now=clock)
        assert [job.id for job in claimed] == [interactive]
        record = next(record for record in caplog.records if record.message == "job_queue_wait")
        assert record.job_class == "interactive" and record.queue_wait_s == 12
        assert session.get(Job, bulk).status == JobStatus.SCHEDULED.value


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
@pytest.mark.parametrize("priority", [0, 10])
@pytest.mark.parametrize("workspace_id", [None, "workspace"])
def test_retry_preserves_priority_and_queue_order(factory, monkeypatch, domain, priority, workspace_id):
    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
    clock = utcnow()
    with factory() as session:
        original = enqueue(session, "email.send", {"domain": domain}, priority=priority, run_after=clock,
                           workspace_id=workspace_id)
        session.get(Job, original).created_at = clock - timedelta(minutes=1)
        session.commit()
        claimed, = lease(session, "first", kinds=["email.send"], now=clock)
        assert claimed.id == original
        fail_job(session, original, RetryableError("temporary"), now=clock)
        later = enqueue(session, "email.send", {"domain": domain}, priority=10, workspace_id=workspace_id)
        future = enqueue(session, "email.send", {"domain": domain}, priority=0,
                         run_after=clock + timedelta(hours=1), workspace_id=workspace_id)
        peer = enqueue(session, "email.send", {"domain": domain}, priority=20,
                       workspace_id="peer" if workspace_id else None)
        session.commit()
        reclaimed = lease(session, "recovery", kinds=["email.send"], n=3,
                          now=clock + timedelta(minutes=5))
        expected = [peer, original, later] if workspace_id else [original, later, peer]
        assert [job.id for job in reclaimed] == expected
        assert next(job for job in reclaimed if job.id == original).attempts == 2
        assert session.get(Job, future).status == JobStatus.SCHEDULED.value


@pytest.mark.parametrize("domain", ["Warehouse procurement", "Weekend cooking"])
def test_equal_timestamp_claims_do_not_sort_random_job_ids(factory, monkeypatch, domain):
    from app.core import jobs

    clock = utcnow()
    identities = iter(["job_z_first", "job_a_second"])
    monkeypatch.setattr(jobs, "new_id", lambda prefix: next(identities))
    monkeypatch.setattr(jobs, "utcnow", lambda: clock)
    with factory() as session:
        first = enqueue(session, "email.send", {"domain": domain}, workspace_id="workspace", run_after=clock)
        second = enqueue(session, "email.send", {"domain": domain}, workspace_id="workspace", run_after=clock)
        for job_id in (first, second):  # Postgres stamps created_at server-side; pin a true tie
            session.get(Job, job_id).created_at = clock
        session.commit()
        assert session.get(Job, first).created_at == session.get(Job, second).created_at
        claimed = lease(session, "worker", kinds=["email.send"], n=2, now=clock)
        assert [job.id for job in claimed] == [first, second]


def test_workspace_caps_are_independent_per_class(scale_engine, monkeypatch):
    from sqlalchemy import MetaData

    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
    metadata = MetaData()
    Job.__table__.to_metadata(metadata)
    metadata.create_all(scale_engine)
    factory = sessionmaker(bind=scale_engine)
    with factory() as session:
        for workspace, domain in (("operations", "Fleet maintenance"), ("consumer", "Home fitness")):
            for number in range(3):
                enqueue(session, "future.bulk", {"domain": domain, "number": number}, workspace_id=workspace)
            for kind in ("chat.respond", "idea.clarify_turn", "chat.respond"):
                enqueue(session, kind, {"domain": domain}, workspace_id=workspace)
        assert len(lease(session, "bulk", n=8, job_classes=["bulk"])) == 4
        session.commit()
        interactive = lease(session, "interactive", n=8, job_classes=["interactive"])
        assert len(interactive) == 4
        assert {job.kind for job in interactive} == {"chat.respond", "idea.clarify_turn"}
        assert {job.payload["domain"] for job in interactive} == {"Fleet maintenance", "Home fitness"}
        assert lease(session, "mixed", n=8) == []
        for job in interactive:
            session.get(Job, job.id).status = JobStatus.SUCCEEDED.value
        session.commit()
        assert len(lease(session, "mixed", n=8)) == 2
        assert lease(session, "bulk", n=8, job_classes=["bulk"]) == []


@pytest.mark.parametrize("domains", [("Inventory planning", "Pet training"), ("Clinic scheduling", "Home composting")])
def test_workspace_rotation_cap_and_parallel_claims(scale_engine, monkeypatch, domains):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import MetaData
    from threading import Barrier

    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
    metadata = MetaData()
    Job.__table__.to_metadata(metadata)
    metadata.create_all(scale_engine)
    factory = sessionmaker(bind=scale_engine, class_=Session)
    with factory() as session:
        for workspace, domain, count in (("busy", domains[0], 12), ("peer", domains[1], 4)):
            for number in range(count):
                enqueue(session, "future.kind", {"domain": domain, "number": number}, workspace_id=workspace)
        claimed = lease(session, "first", n=4)
        assert [job.workspace_id for job in claimed] == ["busy", "peer", "busy", "peer"]
        assert {job.payload["domain"] for job in claimed} == set(domains)
        assert lease(session, "full", n=10) == []
        for job in claimed:
            session.get(Job, job.id).status = JobStatus.SUCCEEDED.value
        session.commit()
    barrier = Barrier(2)

    def claim(worker):
        with factory() as session:
            barrier.wait(timeout=10)
            result = lease(session, worker, n=4)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["left", "right"]))
    leased = [job for batch in results for job in batch]
    assert len({job.id for job in leased}) == len(leased) == 4
    assert [job.workspace_id for job in leased].count("busy") == 2
    assert [job.workspace_id for job in leased].count("peer") == 2


@pytest.mark.parametrize("domains", [("Warehouse replenishment", "Family cycling"), ("Dental appointments", "Home gardening")])
def test_founder_cap_and_rotation_include_sibling_workspaces(scale_engine, monkeypatch, domains):
    from collections import Counter
    from app.core.jobs import enqueue, lease
    from app.db.models import Base, Person, Workspace

    Base.metadata.create_all(scale_engine)
    factory = sessionmaker(bind=scale_engine)
    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
    now = utcnow()
    owners = {"busy-first": "busy-owner", "busy-second": "busy-owner", "peer": "peer-owner"}
    with factory() as session:
        for owner, domain in zip(("busy-owner", "peer-owner"), domains):
            session.add(Person(id=owner, display_name=domain, created_at=now))
        session.flush()
        for workspace, owner in owners.items():
            session.add(Workspace(id=workspace, kind="company", name=workspace, entry_mode="idea",
                owner_id=owner, created_at=now, updated_at=now))
        session.flush()
        for workspace, owner in owners.items():
            for number in range(3):
                enqueue(session, "gtm.gmail.read", {"domain": domains[owner == "peer-owner"], "number": number},
                    workspace_id=workspace, priority=number)
        session.commit()
        claimed = lease(session, "founder-worker", kinds=["gtm.gmail.read"], n=6, now=utcnow())
        assert Counter(owners[job.workspace_id] for job in claimed) == {"busy-owner": 2, "peer-owner": 2}
        assert [owners[job.workspace_id] for job in claimed] == ["busy-owner", "peer-owner", "busy-owner", "peer-owner"]
        assert {job.payload["domain"] for job in claimed} == set(domains)
        assert [(job.workspace_id, job.payload["number"]) for job in claimed] == [
            ("busy-first", 0), ("peer", 0), ("busy-second", 0), ("peer", 1)]
        from app.worker import main as worker
        monkeypatch.setattr(worker, "CONCURRENCY", 6)
        assert worker._lease_available(session, "next-worker", ["gtm.gmail.read"], set(), set(), {"bulk"}) == []


@pytest.mark.parametrize("schema", ["fresh", "jobs_only"])
def test_queue_fifo_priority_and_retry_fencing_across_schemas(tmp_path, monkeypatch, schema):
    from sqlalchemy import MetaData
    from app.connections.composio_pacing import RateLimited
    from app.core.jobs import RetryableError, complete_job, enqueue, fail_job, heartbeat, lease, reclaim_stale
    from app.db.models import Base, Job
    from app.db.session import make_engine

    monkeypatch.setenv("JOB_WORKSPACE_INFLIGHT_CAP", "2")
    engine = make_engine(f"sqlite:///{tmp_path / 'queue-contract.db'}")
    if schema == "fresh":
        Base.metadata.create_all(engine)
    else:
        metadata = MetaData()
        Job.__table__.to_metadata(metadata)
        metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with factory() as session:
            for workspace, domain in (("first", "Equipment inspections"), ("second", "Local walking groups")):
                for number, priority in enumerate((2, 0, 0)):
                    enqueue(session, "gtm.gmail.read", {"domain": domain, "number": number},
                        workspace_id=workspace, priority=priority, max_attempts=3)
            session.commit()
            now = utcnow()
            claimed = lease(session, "initial", kinds=["gtm.gmail.read"], n=4, now=now)
            session.commit()
            assert [(job.workspace_id, job.payload["number"]) for job in claimed] == [
                ("first", 1), ("second", 1), ("first", 2), ("second", 2)]
            assert lease(session, "capped", kinds=["gtm.gmail.read"], n=4, now=now) == []
            session.commit()
            original = claimed[0]
            assert fail_job(session, original.id, RateLimited(120), now=now,
                worker_id=original.worker_id, attempt=original.attempts) == "retry_wait"
            session.commit()
            due = session.get(Job, original.id).run_after.replace(tzinfo=now.tzinfo)
            assert due >= now + timedelta(seconds=120)
            assert session.get(Job, original.id).max_attempts == 4
            for job in claimed[1:]:
                heartbeat(session, job.id, now=due, worker_id=job.worker_id, attempt=job.attempts)
            session.commit()
            replacement = lease(session, "replacement", kinds=["gtm.gmail.read"], n=1, now=due)[0]
            session.commit()
            assert replacement.id == original.id and replacement.attempts == original.attempts + 1
            heartbeat(session, original.id, now=due + timedelta(seconds=1),
                worker_id=original.worker_id, attempt=original.attempts)
            complete_job(session, original.id, worker_id=original.worker_id, attempt=original.attempts)
            assert fail_job(session, original.id, RetryableError("stale"),
                worker_id=original.worker_id, attempt=original.attempts) is None
            session.commit()
            row = session.get(Job, original.id)
            assert row.status == "leased" and row.locked_by == "replacement"
            assert row.locked_at.replace(tzinfo=now.tzinfo) == due
            assert reclaim_stale(session, now=due + timedelta(seconds=91)) == 4
            session.commit()
            recovered = lease(session, "recovered", kinds=["gtm.gmail.read"], n=4, now=due + timedelta(seconds=91))
            session.commit()
            assert len({job.id for job in recovered}) == 4
            assert next(job for job in recovered if job.id == original.id).attempts == 3
    finally:
        engine.dispose()


def test_lease_classes_default_bulk_and_preserve_due_cancel_kind_guards(factory):
    with factory() as session:
        unknown = enqueue(session, "future.kind", {})
        cancelled = enqueue(session, "idea.clarify_turn", {})
        later = enqueue(session, "idea.validate", {})
        clock = utcnow()
        session.get(Job, cancelled).cancel_requested = True
        session.get(Job, later).run_after = clock + timedelta(minutes=1)
        session.commit()
        assert lease(session, "worker", job_classes=["interactive"], now=clock) == []
        assert lease(session, "worker", kinds=[], now=clock) == []
        assert lease(session, "worker", job_classes=[], now=clock) == []
        assert [job.id for job in lease(session, "worker", job_classes=["bulk"], now=clock)] == [unknown]
        with pytest.raises(ValueError):
            lease(session, "worker", job_classes=["typo"])


def test_reserved_worker_slot_stays_available_after_bulk_saturates(factory, monkeypatch):
    from app.worker import main as worker

    monkeypatch.setattr(worker, "CONCURRENCY", 3)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    classes = {"interactive", "bulk"}
    kinds = ["gtm.journey.prepare", "idea.clarify_turn"]
    with factory() as session:
        for number in range(4):
            enqueue(session, "gtm.journey.prepare", {"number": number})
        session.commit()
        claimed = worker._lease_available(session, "worker", kinds, set(), set(), classes)
        assert len(claimed) == 2 and all(job.kind == "gtm.journey.prepare" for job in claimed)
        running = {(job.id, job.attempts) for job in claimed}
        assert worker._lease_available(session, "worker", kinds, running, running, classes) == []
        interactive = enqueue(session, "idea.clarify_turn", {})
        session.commit()
        assert [job.id for job in worker._lease_available(session, "worker", kinds, running, running, classes)] == [interactive]


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouse operators", "Watering reminders for home gardeners"])
def test_due_idea_refresh_is_leased_despite_continuous_interactive_work(factory, monkeypatch, idea):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.worker import main as worker
    from app.worker.catalog import enabled_kinds

    monkeypatch.delenv("WORKER_KINDS", raising=False)
    monkeypatch.setattr(worker, "CONCURRENCY", 2)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    clock = utcnow()
    monkeypatch.setattr("app.core.jobs.utcnow", lambda: clock)
    with factory() as session:
        refresh = enqueue(session, "idea.page_refresh", {"idea": idea}, run_after=clock + timedelta(minutes=5))
        for _index in range(8):
            enqueue(session, "idea.clarify_turn", {"idea": idea})
        session.commit()
        kinds = sorted(enabled_kinds())
        assert "idea.page_refresh" in kinds
        early = worker._lease_available(session, "worker", kinds, set(), set(), {"interactive", "bulk"})
        assert len(early) == 2 and all(job.kind == "idea.clarify_turn" for job in early)
        for job in early:
            session.get(Job, job.id).status = "succeeded"
        session.commit()
        monkeypatch.setattr("app.core.jobs.utcnow", lambda: clock + timedelta(minutes=5))
        due = worker._lease_available(session, "worker", kinds, set(), set(), {"interactive", "bulk"})
        assert len(due) == 2 and refresh in {job.id for job in due}
        assert all(job.payload["idea"] == idea for job in due)


def test_bulk_only_worker_uses_all_slots(factory, monkeypatch):
    from app.worker import main as worker

    monkeypatch.setattr(worker, "CONCURRENCY", 2)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    with factory() as session:
        for number in range(2):
            enqueue(session, "gtm.journey.prepare", {"number": number})
        enqueue(session, "idea.clarify_turn", {})
        session.commit()
        claimed = worker._lease_available(session, "worker", None, set(), set(), {"bulk"})
        assert len(claimed) == 2 and all(job.kind == "gtm.journey.prepare" for job in claimed)


@pytest.mark.parametrize("idea", ["Scheduling for dental practices", "Meal planning for home cooks"])
def test_kind_restricted_bulk_worker_does_not_reserve_unusable_interactive_slot(factory, monkeypatch, idea):
    from app.worker import main as worker

    monkeypatch.setattr(worker, "CONCURRENCY", 3)
    monkeypatch.setattr(worker, "INTERACTIVE_SLOTS", 1)
    with factory() as session:
        ids = {enqueue(session, "gtm.journey.prepare", {"idea": idea}, workspace_id="workspace_under_test",
                       venture_id="venture_under_test") for _ in range(3)}
        excluded = enqueue(session, "idea.clarify_turn", {"idea": idea})
        session.commit()
        claimed = worker._lease_available(session, "worker", ["gtm.journey.prepare"], set(), set(),
                                          {"interactive", "bulk"})
        assert {job.id for job in claimed} == ids
        assert all(job.payload["idea"] == idea for job in claimed)
        assert session.get(Job, excluded).status == JobStatus.SCHEDULED.value


def test_interactive_only_worker_does_not_lease_bulk(factory, monkeypatch):
    from app.worker import main as worker

    monkeypatch.setattr(worker, "CONCURRENCY", 2)
    with factory() as session:
        enqueue(session, "gtm.journey.prepare", {})
        interactive = enqueue(session, "idea.validate", {})
        session.commit()
        claimed = worker._lease_available(session, "worker", None, set(), set(), {"interactive"})
        assert [job.id for job in claimed] == [interactive]


def test_dedupe_replaces_while_scheduled(factory) -> None:
    with factory() as session:
        first = enqueue(session, "credits.sweep", {"n": 1}, dedupe_key="credits.sweep:w1")
        second = enqueue(session, "credits.sweep", {"n": 2}, dedupe_key="credits.sweep:w1")
        session.commit()
        assert first == second
        row = session.get(Job, first)
        assert row is not None
        assert '"n": 2' in row.payload_json


@pytest.mark.parametrize("idea", ["Inventory counts for pharmacies", "Trail planning for hikers"])
@pytest.mark.parametrize("status", ["leased", "retry_wait", "scheduled"])
def test_dedupe_never_replaces_started_work(factory, idea, status):
    import json
    from app.chat.store import CLIENT_REQUEST_ID

    token = CLIENT_REQUEST_ID.set("research-turn")
    try:
        with factory() as session:
            job_id = enqueue(session, "research.deep", {"idea": idea}, venture_id="venture_under_test",
                             dedupe_key="research-under-test")
            row = session.get(Job, job_id)
            row.status, row.attempts, row.locked_by = status, 1, "worker"
            session.commit()
            original = row.payload_json
    finally:
        CLIENT_REQUEST_ID.reset(token)
    token = CLIENT_REQUEST_ID.set("next-turn")
    try:
        with factory() as session:
            assert enqueue(session, "research.deep", {"idea": idea, "changed": True},
                           venture_id="venture_under_test", dedupe_key="research-under-test") == job_id
            session.commit()
            row = session.get(Job, job_id)
            assert row.payload_json == original
            assert json.loads(row.payload_json)["_chat_request_nonce"] == "research-turn"
            assert (row.status, row.attempts, row.locked_by, row.cancel_requested) == (status, 1, "worker", False)
    finally:
        CLIENT_REQUEST_ID.reset(token)


def test_retryable_then_fatal(factory) -> None:
    with factory() as session:
        job_id = enqueue(session, "credits.sweep", {}, max_attempts=2)
        session.commit()
    with factory() as session:
        claimed = lease(session, "w1", kinds=["credits.sweep"], n=1)
        assert claimed and claimed[0].id == job_id
        status = fail_job(session, job_id, RetryableError("temp"))
        session.commit()
        assert status == JobStatus.RETRY_WAIT.value
    with factory() as session:
        row = session.get(Job, job_id)
        assert row is not None
        row.run_after = utcnow()
        row.scheduled_for = row.run_after
        session.commit()
    with factory() as session:
        claimed = lease(session, "w1", kinds=["credits.sweep"], n=1)
        assert claimed
        status = fail_job(session, claimed[0].id, FatalError("done"))
        session.commit()
        assert status == JobStatus.FAILED_DEAD.value


def test_stale_lock_re_leased(factory) -> None:
    from datetime import timedelta

    with factory() as session:
        job_id = enqueue(session, "ops.signal_sweep", {})
        session.commit()
    with factory() as session:
        lease(session, "dead", kinds=["ops.signal_sweep"], n=1)
        session.commit()
    with factory() as session:
        row = session.get(Job, job_id)
        assert row is not None
        row.locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with factory() as session:
        n = reclaim_stale(session)
        session.commit()
        assert n == 1
        claimed = lease(session, "alive", kinds=["ops.signal_sweep"], n=1)
        session.commit()
        assert claimed and claimed[0].id == job_id


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL unset")
def test_two_worker_race_pg(pg_database) -> None:
    # Fixture owns a random fully migrated database; never touch admin DB jobs.
    _, engine = pg_database
    fac = sessionmaker(bind=engine, class_=Session)
    with fac() as session:
        enqueue(session, "credits.sweep", {"race": 1}, dedupe_key="race:one")
        session.commit()
    with fac() as a, fac() as b:
        left = lease(a, "wa", kinds=["credits.sweep"], n=1)
        right = lease(b, "wb", kinds=["credits.sweep"], n=1)
        a.commit()
        b.commit()
    ids = [j.id for j in left + right]
    assert len(ids) == 1


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL unset")
def test_stale_retry_skips_contended_row_and_preserves_lease_attempts_pg(pg_database):
    from datetime import timedelta
    from sqlalchemy import select, text
    _, engine = pg_database
    fac = sessionmaker(bind=engine, class_=Session)
    clock = utcnow()
    with fac() as session:
        held = enqueue(session, "ops.signal_sweep", {}, max_attempts=3)
        free = enqueue(session, "ops.signal_sweep", {}, max_attempts=3)
        session.flush()
        for job_id in (held, free):
            row = session.get(Job, job_id)
            row.status, row.attempts = "leased", 1
            row.locked_at, row.locked_by = clock - timedelta(seconds=120), "old-worker"
        session.commit()
    with fac() as owner:
        owner.scalar(select(Job).where(Job.id == held).with_for_update())
        with fac() as contender:
            # Bound a buggy bulk UPDATE deterministically; never hang the runner.
            contender.execute(text("SET LOCAL lock_timeout = '150ms'"))
            contender.execute(text("SET LOCAL statement_timeout = '1000ms'"))
            assert reclaim_stale(contender, now=clock) == 1
            contender.commit()
        with fac() as reader:
            locked = reader.get(Job, held)
            recovered = reader.get(Job, free)
            assert locked.status == "leased" and locked.locked_by == "old-worker" and locked.attempts == 1
            assert recovered.status == "retry_wait" and recovered.locked_by is None and recovered.attempts == 1
        owner.commit()
    with fac() as next_worker:
        assert reclaim_stale(next_worker, now=clock) == 1
        jobs = lease(next_worker, "replacement-worker", kinds=["ops.signal_sweep"], n=2, now=clock)
        assert {job.id for job in jobs} == {held, free}
        assert all(job.attempts == 2 for job in jobs)  # Reclaim did not increment.
        next_worker.commit()


def test_queue_wait_counts_from_when_a_deferred_job_became_due(factory, caplog):
    with factory() as session:
        job = enqueue(session, "email.send", {}, priority=10)
        clock = utcnow()
        row = session.get(Job, job)
        row.created_at, row.run_after = clock - timedelta(minutes=45), clock - timedelta(seconds=5)
        session.commit()
        with caplog.at_level("INFO", logger="app.jobs"):
            lease(session, "worker", n=1, now=clock)
        record = next(record for record in caplog.records if record.message == "job_queue_wait")
        assert record.queue_wait_s == 5
