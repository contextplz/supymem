"""Lazy durable content dispatch and canonical scope; no legacy content run."""
import asyncio
from datetime import datetime, timezone
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def worker():
    with service_modules("company_os"):
        from app.worker import handlers, main
        yield handlers, main


@pytest.mark.parametrize("domain", ["Fleet maintenance for delivery teams", "Garden journals for home growers"])
def test_only_exact_new_handlers_registered_and_imported_on_dispatch(worker, monkeypatch, domain):
    handlers, _ = worker
    from app.core import local_worker
    monkeypatch.setattr(local_worker, "capability_enabled", lambda name: True)
    monkeypatch.setattr(handlers, "HANDLERS", {})
    monkeypatch.setattr(handlers, "enabled_kinds", lambda: {"content.topics", "content.draft", "content.auto_publish"})
    handlers.install_default_handlers()
    assert set(handlers.HANDLERS) == {"content.topics", "content.draft"}
    calls = []
    module = ModuleType("app.content.generation")
    module.run_topics = lambda payload: calls.append(("topics", payload))
    module.run_draft = lambda payload: calls.append(("draft", payload))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    from app.tenancy import bind

    for kind in ("topics", "draft"):
        with bind(workspace_id="wsp_test", venture_id="ven_test"):
            asyncio.run(handlers.dispatch("content." + kind, {"receipt_id": "receipt-" + kind, "domain": domain}))
    assert calls == [("topics", {"receipt_id": "receipt-topics", "domain": domain}),
                     ("draft", {"receipt_id": "receipt-draft", "domain": domain})]


@pytest.mark.parametrize("kind", ["content.topics", "content.draft", "gtm.god.run", "pagebuilder.build"])
def test_content_requires_canonical_workspace_and_venture_authority(worker, monkeypatch, kind):
    handlers, main = worker
    from app.authz import workspace
    from app.workspaces import service
    monkeypatch.setattr(handlers, "hosted_mode", lambda: True)
    job = SimpleNamespace(kind=kind, workspace_id=None, venture_id=None, payload={})
    with pytest.raises(main.FatalError, match="job_scope_missing"):
        main._job_scope(job)
    job.workspace_id = "workspace-a"
    with pytest.raises(main.FatalError, match="job_scope_missing"):
        main._job_scope(job)
    job.workspace_id, job.venture_id = "workspace-a", "venture-a"
    job.payload = {"_actor_subject": "founder-a"}
    monkeypatch.setattr(workspace, "assert_workspace_access", lambda wid, actor: None)
    monkeypatch.setattr(workspace, "assert_venture_access", lambda vid, actor: (vid, "workspace-other"))
    monkeypatch.setattr(workspace, "subject_person_id", lambda actor: actor)
    monkeypatch.setattr(service, "role_of", lambda wid, actor: "maintainer")
    with pytest.raises(main.FatalError, match="job_scope_mismatch"):
        main._job_scope(job)
    monkeypatch.setattr(workspace, "assert_venture_access", lambda vid, actor: (vid, "workspace-a"))
    assert main._job_scope(job).actor == "founder-a"
    monkeypatch.setattr(service, "role_of", lambda wid, actor: "viewer")
    with pytest.raises(main.FatalError, match="job_authority_revoked"):
        main._job_scope(job)


@pytest.mark.parametrize("kind,error", [("content.topics", "content_operation_in_progress"), ("pagebuilder.build", "pagebuilder_operation_in_progress")])
def test_duplicate_content_delivery_cannot_finish_or_fail_another_attempt(worker, monkeypatch, kind, error):
    _, main = worker
    from app.db.models import Job
    from app.db.task_models import TaskEvent, TaskStep
    from app.tasks import usage
    from app.tenancy import Scope
    factory, engine = _owner_factory(kind)
    monkeypatch.setattr(usage, "db", lambda: factory)
    monkeypatch.setattr(main, "_job_scope", lambda job: Scope("workspace-a", "venture-a", "founder-a"))
    calls = []
    async def in_progress(kind, payload):
        calls.append(payload)
        raise main.FatalError(error)
    monkeypatch.setattr(main, "dispatch", in_progress)
    monkeypatch.setattr(main, "_funnel_outcome", lambda *a: calls.append("unexpected funnel"))
    with factory() as session:
        before = _job_snapshot(session.get(Job, "job-content"))
        events_before = session.scalar(select(func.count()).select_from(TaskEvent))
    job = SimpleNamespace(id="job-content", kind=kind, worker_id="worker-current", attempts=1,
        workspace_id="workspace-a", venture_id="venture-a", payload={"_actor_subject": "founder-a"})
    running = {(job.id, job.attempts)}
    try:
        asyncio.run(main._run_one(factory, job, running))
        assert running == set() and calls == []
        with factory() as session:
            assert _job_snapshot(session.get(Job, job.id)) == before
            assert session.scalar(select(func.count()).select_from(TaskEvent)) == events_before
            assert session.scalar(select(func.count()).select_from(TaskStep)) == 0
    finally:
        engine.dispose()


def _owner_factory(kind):
    """A real scoped lease owned by attempt 2; the stale delivery claims 1."""
    from app.db.models import Base, Job
    from app.db.task_models import TaskEvent, TaskRun, TaskStep
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Job.__table__, TaskRun.__table__, TaskStep.__table__, TaskEvent.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(Job(id="job-content", kind=kind, workspace_id="workspace-a", venture_id="venture-a",
            payload_json=json.dumps({"workspace_id": "workspace-a", "venture_id": "venture-a",
                "_actor_subject": "founder-a"}), status="leased", attempts=2, max_attempts=3,
            locked_by="worker-current", locked_at=now, started_at=now, created_at=now))
        session.commit()
    return factory, engine


def _job_snapshot(row):
    return {key: getattr(row, key) for key in ("status", "attempts", "locked_by", "locked_at",
        "payload_json", "finished_at", "last_error", "cancel_requested")}


@pytest.mark.parametrize("kind,error", [("content.topics", "content_operation_in_progress"), ("pagebuilder.build", "pagebuilder_operation_in_progress")])
def test_same_owned_attempt_duplicate_preserves_live_owner_receipts(worker, monkeypatch, kind, error):
    _, main = worker
    from app.db.models import Job
    from app.db.task_models import TaskEvent
    from app.tasks import usage
    from app.tenancy import Scope
    factory, engine = _owner_factory(kind)
    monkeypatch.setattr(usage, "db", lambda: factory)
    monkeypatch.setattr(main, "_job_scope", lambda job: Scope("workspace-a", "venture-a", "founder-a"))
    monkeypatch.setattr(main, "_funnel_outcome", lambda *a: None)
    calls = []
    async def in_progress(kind, payload):
        calls.append(payload)
        raise main.FatalError(error)
    monkeypatch.setattr(main, "dispatch", in_progress)
    with factory() as session:
        before = _job_snapshot(session.get(Job, "job-content"))
    job = SimpleNamespace(id="job-content", kind=kind, worker_id="worker-current", attempts=2,
        workspace_id="workspace-a", venture_id="venture-a", payload={"_actor_subject": "founder-a"})
    running = {(job.id, job.attempts)}
    try:
        asyncio.run(main._run_one(factory, job, running))
        assert running == {(job.id, job.attempts)} and len(calls) == 1
        with factory() as session:
            assert _job_snapshot(session.get(Job, job.id)) == before
            assert session.scalar(select(func.count()).select_from(TaskEvent)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize("domain", ["freight-dispatch.example", "family-gardening.example"])
def test_local_database_bootstrap_does_not_reopen_database_for_bound_venture(worker, monkeypatch, domain):
    from app import state
    from app.tenancy import bind

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    open_database = state.db

    def fail_reentrant_open():
        pytest.fail("SQLite startup tried to reopen its database under the schema lock")

    monkeypatch.setattr(state, "db", fail_reentrant_open)
    with bind(workspace_id="workspace-a", venture_id=domain), open_database()() as session:
        assert session.scalar(select(1)) == 1


def test_pagebuilder_registered_lazily_on_existing_worker(worker, monkeypatch):
    handlers, _ = worker
    monkeypatch.setattr(handlers, "HANDLERS", {})
    monkeypatch.setattr(handlers, "enabled_kinds", lambda: {"pagebuilder.build"})
    handlers.install_default_handlers()
    assert set(handlers.HANDLERS) == {"pagebuilder.build"}
    calls = []
    module = ModuleType("app.pagebuilder.jobs")
    module.run = lambda payload: calls.append(payload)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    payload = {"_job_id": "job-pagebuilder"}
    from app.tenancy import bind

    with bind(workspace_id="wsp_test", venture_id="ven_test"):
        asyncio.run(handlers.dispatch("pagebuilder.build", payload))
    assert calls == [payload]
