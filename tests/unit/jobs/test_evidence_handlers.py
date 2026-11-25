"""Actual outbox/lease rows with an explicit synthetic S04 callee, no stage qualification."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
SCOPE = dict(workspace_id="ws_evidence", venture_id="ven_evidence", actor="usr_evidence")
EVENT = "synthetic:event_evidence"


@pytest.fixture()
def evidence_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/evidence.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.core import jobs
    from app.credits import _txn
    from app.db.models import Base, Person, Venture, VentureDocument, Workspace, WorkspaceMember
    from app.state import set_factory
    from app.tenancy import bind
    from app.worker import handlers

    monkeypatch.setattr(jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    engine = create_engine(f"sqlite:///{tmp_path}/evidence.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    set_factory(factory)
    with factory() as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_evidence", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_evidence", owner_id="usr_evidence", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(WorkspaceMember(id="member_evidence", workspace_id="ws_evidence", person_id="usr_evidence",
            role="owner", created_at=moment))
        session.add(Venture(id="ven_evidence", workspace_id="ws_evidence", name="Synthetic", raw_idea="Synthetic",
            stage="draft", created_at=moment, updated_at=moment))
        session.add(VentureDocument(id="event_evidence", venture_id="ven_evidence", kind="synthetic_stage_source",
            title="Synthetic committed source", provenance_json=json.dumps({"workspace_id": "ws_evidence",
                "source": "waitlist", "valid": True, "test": False, "auto": False, "state": "LIVE"}),
            created_at=moment, updated_at=moment))
        session.commit()
    calls = []
    module = ModuleType("app.ventures.stage")

    def evidence_context(vid, event_key, workspace_id=None):
        """Explicit stub, reading a real committed row instead of asserted payload identity."""
        with factory() as session:
            row = session.get(VentureDocument, event_key.removeprefix("synthetic:"))
            venture = session.get(Venture, vid)
            source = json.loads(row.provenance_json) if row else {}
            if (not event_key.startswith("synthetic:") or not row or not venture or row.venture_id != vid
                    or source.get("workspace_id") != workspace_id or venture.workspace_id != workspace_id
                    or source.get("source") not in {"waitlist", "signup", "human_reply", "publication"}
                    or not source.get("valid") or source.get("test") or source.get("auto")
                    or source.get("source") == "publication" and source.get("state") != "LIVE"):
                raise ValueError("stage_evidence_invalid")
            workspace = session.get(Workspace, workspace_id)
            return dict(venture_id=vid, workspace_id=workspace_id, owner_person_id=workspace.owner_id)

    def reassess_evidence(vid, event_key, workspace_id):
        evidence_context(vid, event_key, workspace_id)
        calls.append((vid, event_key, workspace_id))
        with factory() as session:
            receipt_id = "synthetic_receipt:" + event_key
            receipt = session.get(VentureDocument, receipt_id)
            if receipt is None:
                session.get(Venture, vid).stage = "validating"
                session.add(VentureDocument(id=receipt_id, venture_id=vid, kind="synthetic_stage_receipt",
                    title="Synthetic stage receipt", provenance_json="{}", created_at=NOW, updated_at=NOW))
                session.commit()
            return {"event_key": event_key, "synthetic": True}

    module.evidence_context = evidence_context
    module.reassess_evidence = reassess_evidence
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(handlers, "HANDLERS", {})
    monkeypatch.setattr(handlers, "enabled_kinds", lambda: {"milestone.reassess"})
    handlers.install_default_handlers()
    yield SimpleNamespace(factory=factory, engine=engine, module=module, handlers=handlers, jobs=jobs, bind=bind, calls=calls)
    engine.dispose()


@pytest.mark.parametrize("domain", ["Quality checks for machine shops", "Birdwatching notes for beginners"])
@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("nested", [True, False])
def test_failure_report_waits_for_its_transaction(evidence_db, monkeypatch, domain, commit, nested):
    from contextlib import nullcontext
    from app.analytics import error_events
    from app.chat import recovery
    from app.db.models import Venture

    calls = []

    def report(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("Synthetic report failure")

    monkeypatch.setattr(error_events, "log_error_event", report)
    with evidence_db.factory() as session:
        session.get(Venture, "ven_evidence").name = domain
        session.flush()
        with session.begin_nested() if nested else nullcontext():
            recovery.record("ven_evidence", kind="research_failed", key=domain,
                            failure={"step": "research.deep", "code": "research_interrupted"},
                            workspace_id="ws_evidence", session=session)
        assert not calls
        session.commit() if commit else session.rollback()
        session.commit()
    assert len(calls) == int(commit)
    with evidence_db.factory() as session:
        assert (session.get(Venture, "ven_evidence").name == domain) is commit


def enqueue(d, *, commit=True):
    with d.factory() as session, d.bind(**SCOPE):
        identifier = d.jobs.enqueue(session, "milestone.reassess", {"event_key": EVENT},
            workspace_id=SCOPE["workspace_id"], venture_id=SCOPE["venture_id"],
            dedupe_key="stage.evidence:" + EVENT, max_attempts=2)
        if commit:
            session.commit()
        return identifier


def claim(d):
    with d.factory() as session:
        job = d.jobs.lease(session, "worker_evidence", kinds=["milestone.reassess"], now=NOW)[0]
        session.commit()
    return {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def invoke(d, payload, *, scope=None):
    with d.bind(**(scope or SCOPE)):
        return d.handlers._milestone_reassess(payload)


def snapshot(d):
    from app.db.models import Job, Venture, VentureDocument
    with d.factory() as session:
        tasks = session.scalar(text("SELECT COUNT(*) FROM tasks")) if inspect(d.engine).has_table("tasks") else 0
        return (session.scalar(select(func.count()).select_from(Job)), session.get(Venture, "ven_evidence").stage,
            tasks,
            session.scalar(select(func.count()).select_from(VentureDocument).where(VentureDocument.kind == "synthetic_stage_receipt")))


def test_conditional_registration_requires_both_actual_callable_dependencies(evidence_db, monkeypatch):
    d = evidence_db
    assert set(d.handlers.HANDLERS) == {"milestone.reassess"}
    monkeypatch.setattr(d.handlers, "HANDLERS", {})
    monkeypatch.setitem(sys.modules, "app.ventures.stage", None)
    d.handlers.install_default_handlers()
    assert d.handlers.HANDLERS == {}
    monkeypatch.setitem(sys.modules, "app.ventures.stage", d.module)
    monkeypatch.setattr(d.module, "reassess_evidence", None)
    d.handlers.install_default_handlers()
    assert d.handlers.HANDLERS == {}


def test_missing_dependency_cannot_return_success_for_direct_dispatch(evidence_db, monkeypatch):
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    monkeypatch.setitem(sys.modules, "app.ventures.stage", None)
    with pytest.raises(d.jobs.FatalError, match="stage_reassessment_unavailable"):
        invoke(d, payload)
    assert snapshot(d) == (1, "draft", 0, 0) and d.calls == []


def test_outbox_dedupes_exact_source_and_rollback_publishes_no_job(evidence_db):
    d = evidence_db
    enqueue(d, commit=False)
    assert snapshot(d) == (0, "draft", 0, 0)
    first, second = enqueue(d), enqueue(d)
    assert first == second and snapshot(d) == (1, "draft", 0, 0)
    payload = claim(d)
    assert set(payload) == {"event_key", "workspace_id", "venture_id", "_actor_subject", "_job_id", "_worker_id", "_job_attempt"}
    invoke(d, payload)
    invoke(d, payload)  # Same lease: the source callee owns deterministic event idempotence.
    assert snapshot(d) == (1, "validating", 0, 1)
    assert d.calls == [("ven_evidence", EVENT, "ws_evidence")] * 2


def test_registered_dispatch_preserves_existing_worker_scope(evidence_db):
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    with d.bind(**SCOPE):
        asyncio.run(d.handlers.dispatch("milestone.reassess", payload))
    assert snapshot(d) == (1, "validating", 0, 1)


@pytest.mark.parametrize("changes", [
    {"_job_id": "missing"}, {"_worker_id": "foreign_worker"}, {"_job_attempt": 2}, {"_job_attempt": True},
    {"event_key": "synthetic:foreign"}, {"owner_person_id": "asserted"}, {"workspace_id": "foreign"},
])
def test_asserted_payload_or_stale_attempt_cannot_reassess(evidence_db, changes):
    d = evidence_db
    enqueue(d)
    payload = {**claim(d), **changes}
    with pytest.raises(d.jobs.FatalError, match="stage_evidence_invalid"):
        invoke(d, payload)
    assert snapshot(d) == (1, "draft", 0, 0) and d.calls == []


@pytest.mark.parametrize("change", ["kind", "completed", "expired", "key", "attempts"])
def test_handler_checks_actual_persisted_lease_and_outbox_contract(evidence_db, change):
    from app.db.models import Job
    d = evidence_db
    identifier = enqueue(d)
    payload = claim(d)
    with d.factory() as session:
        row = session.get(Job, identifier)
        if change == "kind":
            row.kind = "credits.sweep"
        elif change == "completed":
            row.status = "succeeded"
        elif change == "expired":
            row.locked_at = NOW - timedelta(seconds=91)
        elif change == "key":
            row.dedupe_key = "wrong_source"
        elif change == "attempts":
            row.max_attempts = 3
        session.commit()
    with pytest.raises(d.jobs.FatalError, match="stage_evidence_invalid"):
        invoke(d, payload)
    assert snapshot(d) == (1, "draft", 0, 0) and d.calls == []


@pytest.mark.parametrize("change", ["owner", "archived", "blocked", "settings", "venture"])
def test_fresh_custody_and_membership_are_required(evidence_db, change):
    from app.db.models import Venture, Workspace
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    with d.factory() as session:
        workspace = session.get(Workspace, "ws_evidence")
        if change == "owner":
            workspace.owner_id = "foreign_owner"
        elif change == "archived":
            workspace.archived_at = NOW
        elif change == "blocked":
            workspace.settings_json = '{"blocked":true}'
        elif change == "settings":
            workspace.settings_json = '[]'
        elif change == "venture":
            session.get(Venture, "ven_evidence").workspace_id = "foreign_workspace"
        session.commit()
    with pytest.raises(d.jobs.FatalError, match="stage_reassessment_scope_mismatch"):
        invoke(d, payload)
    assert snapshot(d) == (1, "draft", 0, 0) and d.calls == []


@pytest.mark.parametrize("legacy", [False, True])
def test_schema_compatible_cancellation_guard_preserves_real_lease_checks(evidence_db, monkeypatch, legacy):
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    original = d.jobs._leased_row
    def compatible_row(*args, **kwargs):
        row = original(*args, **kwargs)
        if row is None:
            return None
        # The actual persisted lease is validated first. This proxy models only
        # the old/new Job metadata ABI, without claiming a real old-schema run.
        fields = {key: getattr(row, key) for key in ("kind", "locked_at", "payload_json", "max_attempts",
            "dedupe_key", "idempotency_key", "workspace_id", "venture_id")}
        if not legacy:
            fields["cancel_requested"] = True
        return SimpleNamespace(**fields)
    monkeypatch.setattr(d.jobs, "_leased_row", compatible_row)
    if legacy:
        invoke(d, payload)
        assert snapshot(d) == (1, "validating", 0, 1)
    else:
        with pytest.raises(d.jobs.FatalError, match="stage_evidence_invalid"):
            invoke(d, payload)
        assert snapshot(d) == (1, "draft", 0, 0)


@pytest.mark.parametrize("membership", ["deleted", "viewer"])
def test_canonical_implicit_owner_authority_does_not_require_a_membership_role(evidence_db, membership):
    from app.db.models import WorkspaceMember
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    with d.factory() as session:
        row = session.get(WorkspaceMember, "member_evidence")
        if membership == "deleted":
            session.delete(row)
        else:
            row.role = "viewer"
        session.commit()
    invoke(d, payload)
    assert snapshot(d) == (1, "validating", 0, 1)


@pytest.mark.parametrize("scope", [
    {**SCOPE, "workspace_id": "foreign"}, {**SCOPE, "venture_id": "foreign"}, {**SCOPE, "actor": "foreign"},
])
def test_existing_worker_scope_cannot_be_rebound_from_job_payload(evidence_db, scope):
    d = evidence_db
    enqueue(d)
    with pytest.raises(d.jobs.FatalError, match="stage_reassessment_scope_mismatch"):
        invoke(d, claim(d), scope=scope)
    assert d.calls == []


@pytest.mark.parametrize("change", ["deleted", "foreign", "invalid", "test", "auto", "nonlive"])
def test_canonical_source_failures_are_fatal_before_reassessment(evidence_db, change):
    from app.db.models import VentureDocument
    d = evidence_db
    enqueue(d)
    payload = claim(d)
    with d.factory() as session:
        row = session.get(VentureDocument, "event_evidence")
        data = json.loads(row.provenance_json)
        if change == "deleted":
            session.delete(row)
        elif change == "foreign":
            row.venture_id = "foreign"
        elif change == "invalid":
            data["valid"] = False
        elif change == "test":
            data["test"] = True
        elif change == "auto":
            data["auto"] = True
        elif change == "nonlive":
            data.update(source="publication", state="PREVIEW")
        row.provenance_json = json.dumps(data)
        session.commit()
    with pytest.raises(d.jobs.FatalError, match="stage_evidence_invalid"):
        invoke(d, payload)
    assert snapshot(d) == (1, "draft", 0, 0) and d.calls == []


@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError])
def test_database_failure_uses_existing_bounded_retry_and_rolls_back(evidence_db, monkeypatch, error_type):
    from app.db.models import Job, Venture
    d = evidence_db
    identifier = enqueue(d)
    payload = claim(d)
    def failed_reassessment(*args, **kwargs):
        with d.factory() as session:
            session.get(Venture, "ven_evidence").stage = "validating"
            session.flush()
            raise error_type("synthetic database failure", {}, RuntimeError("synthetic"))
    monkeypatch.setattr(d.module, "reassess_evidence", failed_reassessment)
    for attempt in (1, 2):
        with pytest.raises(d.jobs.RetryableError, match="stage_reassessment_database_retryable") as raised:
            invoke(d, payload)
        with d.factory() as session:
            status = d.jobs.fail_job(session, identifier, raised.value, worker_id="worker_evidence", attempt=attempt, now=NOW)
            session.commit()
        assert status == ("retry_wait" if attempt == 1 else "failed_dead")
        assert snapshot(d) == (1, "draft", 0, 0)
        if attempt == 1:
            with d.factory() as session:
                row = session.get(Job, identifier)
                row.run_after = row.scheduled_for = NOW
                session.commit()
            payload = claim(d)
    assert d.calls == []
