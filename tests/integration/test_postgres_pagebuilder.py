"""Migrated PostgreSQL: concurrent intents, restart leases and credit receipts."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules
from tests.qa_storage_fixtures import MemoryS3
from tests.unit.pagebuilder.test_api import seed, install_pipeline_stub


@pytest.fixture()
def pagebuilder_db(pg_database, tmp_path, monkeypatch):
    url, engine = pg_database
    for key, value in {"COMPANY_OS_DB_URL": url, "COMPANY_OS_DATA_DIR": str(tmp_path),
        "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"), "SUPYMEM_PAGEBUILDER_ENABLED": "1",
        "LLM_OFFLINE": "0", "LLM_API_KEY": "synthetic-test-only", "LLM_BASE_URL": "https://provider.example.invalid",
        "LLM_PROVIDER": "azure_foundry", "LLM_DEPLOYMENT_PRO": "DeepSeek-V4-Pro",
        "PUBLIC_GATEWAY_URL": "https://gateway.supymem.com", "APP_ENV": "local"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.state import set_factory
        factory = sessionmaker(engine, expire_on_commit=False)
        set_factory(factory)  # No create_all: exercise the existing migrated schema.
        seeded = seed(factory, actor="pagebuilder_pg_" + uuid.uuid4().hex[:12])
        yield seeded
        from app.db.models import Job
        with factory() as session:
            session.execute(update(Job).where(Job.workspace_id == seeded["wid"], Job.status.in_(["scheduled", "pending", "retry_wait"]))
                .values(status="cancelled"))
            session.commit()


def test_concurrent_start_single_durable_intent(pagebuilder_db):
    from app.pagebuilder import api
    d = pagebuilder_db
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: api.start(d["vid"], d["actor"])["job_id"], range(4)))
    assert len(set(ids)) == 1
    assert api.status(d["vid"], d["actor"], ids[0])["state"] == "queued"


@pytest.mark.parametrize("winner", ["choice", "research"])
def test_research_admission_serializes_with_page_choice(pagebuilder_db, monkeypatch, winner):
    """Isolated PG fixture: real transactions, normal admission, no worker/provider I/O."""
    from fastapi import HTTPException
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.db import session as sessions
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.research.service import start
    from app.tenancy import bind

    d = pagebuilder_db
    save_page_choice(d["vid"], PageChoiceBody(choice="build"), x_subject=d["actor"])
    real_gate, role = sessions.serialize_writes, threading.local()
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    loser_pid = []

    def gated(session, *, key=None):
        if key != "research-page-choice:" + d["vid"] or not getattr(role, "name", None):
            return real_gate(session, key=key)
        if role.name == winner:
            real_gate(session, key=key)
            held.set()
            assert release.wait(15), "winning transaction was not released"
        else:
            loser_pid.append(session.connection().connection.driver_connection.info.backend_pid)
            attempted.set()
            real_gate(session, key=key)

    monkeypatch.setattr(sessions, "serialize_writes", gated)

    def request(name):
        role.name = name
        if name == "choice":
            try:
                return save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
            except HTTPException as exc:
                assert exc.status_code == 409
                return exc.status_code
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            return start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-pg-race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, winner)
        try:
            acquired = held.wait(5)
            if not acquired and first.done():
                first.result()  # Surface the original worker error before the acquisition assertion.
            assert acquired, "winner did not acquire the transaction gate"
            other = "research" if winner == "choice" else "choice"
            second = pool.submit(request, other)
            assert attempted.wait(5), "loser did not attempt the transaction gate"
            deadline = time.monotonic() + 5
            with d["factory"]() as observer:
                while time.monotonic() < deadline:
                    if observer.scalar(text("SELECT count(*) FROM pg_locks WHERE pid = :pid "
                                            "AND locktype = 'advisory' AND NOT granted"), {"pid": loser_pid[0]}):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("loser was not blocked by the real PostgreSQL advisory lock")
            assert not second.done()
        finally:
            release.set()
        results = {winner: first.result(timeout=10), other: second.result(timeout=10)}

    assert results["choice"] == ({"choice": "skip"} if winner == "choice" else 409)
    with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
        repeated = start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-pg-race")
    assert repeated["job_id"] == results["research"]["job_id"]
    with pytest.raises(HTTPException) as stale:
        save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    assert stale.value.status_code == 409
    with d["factory"]() as session:
        saved = session.get(VentureDocument, f"page_choice:{d['vid']}")
        assert json.loads(saved.body_md) == {"choice": "skip" if winner == "choice" else "build"}
        jobs = session.scalars(select(Job).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"])).all()
        assert [(job.id, job.kind, job.status) for job in jobs] == [(repeated["job_id"], "research.deep", "scheduled")]
        idea = session.get(IdeaSession, d["sid"])
        assert (idea.state, idea.current_revision_id, idea.confirmed_revision_id) == ("confirmed", d["rid"], d["rid"])


def test_research_gate_does_not_invert_direction_correction_locks(pagebuilder_db, monkeypatch):
    """Correction keeps Idea→Venture; research waits, then rejects its stale confirmation."""
    from sqlalchemy.orm import Session
    from app.api.ventures import PageChoiceBody, save_page_choice
    from app.db import session as sessions
    from app.db.models import Job, VentureDocument
    from app.db.supymem_models import IdeaSession
    from app.idea.service import confirm, start_direction_correction
    from app.research.service import ResearchPreflightBlocked, start
    from app.tenancy import bind

    d = pagebuilder_db
    save_page_choice(d["vid"], PageChoiceBody(choice="skip"), x_subject=d["actor"])
    real_get, real_gate = Session.get, sessions.serialize_writes
    role = threading.local()
    held, attempted, release = threading.Event(), threading.Event(), threading.Event()
    research_pid = []

    def paused_get(session, entity, ident, **kwargs):
        row = real_get(session, entity, ident, **kwargs)
        if (getattr(role, "name", None) == "correction" and entity is IdeaSession
                and ident == d["sid"] and kwargs.get("with_for_update")):
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            held.set()
            assert release.wait(15), "correction row lock was not released"
        return row

    def observed_gate(session, *, key=None):
        real_gate(session, key=key)
        if getattr(role, "name", None) == "research" and key == "research-page-choice:" + d["vid"]:
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            research_pid.append(session.connection().connection.driver_connection.info.backend_pid)
            attempted.set()

    monkeypatch.setattr(Session, "get", paused_get)
    monkeypatch.setattr(sessions, "serialize_writes", observed_gate)

    def request(name):
        role.name = name
        with bind(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"]):
            if name == "correction":
                return start_direction_correction(d["sid"], "know", venture_id=d["vid"], revision_id=d["rid"])
            with pytest.raises(ResearchPreflightBlocked, match="research_confirmation_required"):
                start(d["vid"], expected_revision_id=d["rid"], request_key="page-choice-pg-correction")

    with ThreadPoolExecutor(max_workers=2) as pool:
        correction_future = pool.submit(request, "correction")
        try:
            assert held.wait(5), "correction did not lock the original IdeaSession"
            research_future = pool.submit(request, "research")
            assert attempted.wait(5), "research did not acquire its advisory gate"
            deadline = time.monotonic() + 5
            with d["factory"]() as observer:
                while time.monotonic() < deadline:
                    if observer.scalar(text("SELECT count(*) FROM pg_locks WHERE pid = :pid "
                                            "AND locktype = 'transactionid' AND NOT granted"), {"pid": research_pid[0]}):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("research was not waiting on the correction's real IdeaSession row lock")
            assert not research_future.done()
        finally:
            release.set()
        correction = correction_future.result(timeout=10)
        research_future.result(timeout=10)

    assert correction["id"] != d["sid"] and correction["state"] == "clarifying"
    with pytest.raises(RuntimeError, match="not_ready_to_confirm"):
        confirm(correction["id"], revision_id=d["rid"], person_id=d["actor"])
    with d["factory"]() as session:
        original, pending = session.get(IdeaSession, d["sid"]), session.get(IdeaSession, correction["id"])
        assert (original.state, original.current_revision_id, original.confirmed_revision_id) == ("confirmed", d["rid"], d["rid"])
        assert (pending.state, pending.current_revision_id, pending.confirmed_revision_id) == (
            "clarifying", correction["current_revision"]["id"], None)
        assert not session.scalar(select(Job.id).where(Job.workspace_id == d["wid"], Job.venture_id == d["vid"]))
        assert json.loads(session.get(VentureDocument, f"page_choice:{d['vid']}").body_md) == {"choice": "skip"}


@pytest.fixture
def pagebuilder_artifact_store(pagebuilder_db, monkeypatch):
    """Actual immutable storage implementation; only the S3 SDK transport is local."""
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore

    client = MemoryS3()
    config = ArtifactStoreConfig("test-artifacts", "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    store = S3ArtifactStore(config, client)
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    return store, client


def test_reclaimed_worker_fences_then_completes_with_metering(pagebuilder_db, pagebuilder_artifact_store, monkeypatch):
    from app.core.jobs import FatalError, lease
    from app.credits.accounts import get_account
    from app.db.models import Job, VentureDocument
    from app.pagebuilder import api, native_reference, runtime
    from app.pagebuilder.worker import run_one
    from core.clock import utcnow
    d = pagebuilder_db
    install_pipeline_stub(monkeypatch)
    from llm_gateway import sole

    calls, transmit = [], sole.transmit

    async def counted_transmit(**kwargs):
        calls.append(kwargs["purpose"])
        return await transmit(**kwargs)

    monkeypatch.setattr(sole, "transmit", counted_transmit)
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        first = lease(session, "crashed-pagebuilder", kinds=[api.KIND], n=1)[0]
        session.get(Job, first.id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with d["factory"]() as session:
        replacement = lease(session, "replacement-pagebuilder", kinds=[api.KIND], n=1)[0]
        session.commit()
    assert replacement.id == first.id and replacement.attempts == 2
    old = {**first.payload, "_job_id": first.id, "_worker_id": first.worker_id, "_job_attempt": first.attempts}
    with pytest.raises(FatalError, match="pagebuilder_lease_lost"):
        runtime.job_model_call(first.id, old)("code", "Generate this page")
    assert not calls
    asyncio.run(run_one(d["factory"], replacement))
    assert api.result(d["vid"], d["actor"], started["job_id"])["state"] == "ready"
    account = get_account(d["vid"])
    assert account.spent_micro > 0 and account.reserved_micro == 0
    with d["factory"]() as session:
        row = session.get(Job, started["job_id"])
        doc = json.loads(row.payload_json)
        assert row.status == "succeeded" and row.attempts == 2
        assert all(op["state"] == "completed" for op in doc["_runtime"]["operations"].values())
        references = session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == d["vid"], VentureDocument.kind == native_reference.KIND)).all()
        assert len(references) == 1
        saved = json.loads(references[0].provenance_json)
        assert saved["state"] == "stored"
    assert calls == ["pagebuilder.brief_direction_plan", "pagebuilder.capability_review_1", "pagebuilder.code"]
    assert api.result(d["vid"], d["actor"], started["job_id"])["publish_ready"] is False
    store, client = pagebuilder_artifact_store
    binding, receipt = saved["binding"], saved["receipt"]
    assert (binding["job_id"], binding["variant"], binding["workspace_id"], binding["venture_id"]) == (
        started["job_id"], 0, d["wid"], d["vid"])
    blob = store.read_import(receipt, native_reference._storage_scope(binding), kind="raw",
                             object_digest=binding["reference_sha256"])
    assert hashlib.sha256(blob).hexdigest() == receipt["sha256"] == binding["reference_sha256"]
    assert len(blob) == receipt["size_bytes"] == binding["reference_size_bytes"]
    assert receipt["version_id"] != "null"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert set(archive.namelist()) == {"source.zip", "manifest.json", "SPEC.md", "REFERENCE.json"}
        assert hashlib.sha256(archive.read("source.zip")).hexdigest() == binding["source_sha256"]
        assert hashlib.sha256(archive.read("SPEC.md")).hexdigest() == binding["spec_sha256"]
    native_reference.persist_job(started["job_id"])  # Storage replay reads the saved immutable version.
    assert len(calls) == 3 and len(client.objects) == 1
    assert len([call for call in client.calls if call[0] == "put"]) == 1


def test_saved_provider_receipt_settles_after_process_recovery(pagebuilder_db, monkeypatch):
    from app.credits.accounts import get_account
    from app.credits.reserve import protect_outcome, reserve
    from app.core.jobs import lease
    from app.pagebuilder import api, runtime
    d = pagebuilder_db
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        job = lease(session, "receipt-test-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    assert job.id == started["job_id"]
    quote = json.loads(runtime.read(job.id).payload_json)["_runtime"]["quote"]
    request_id = f"{job.id}:variant_0:code:{job.attempts}"
    hold = reserve(account_id=quote["account_id"], amount_micro=1000, purpose="pagebuilder.code",
        workspace_id=d["wid"], venture_id=d["vid"], model=quote["model"], provider=quote["provider"],
        request_id=request_id)
    protect_outcome(hold)
    runtime.journal.save_operation(job.id, "variant_0:code", {"state": "responded", "attempt": job.attempts,
        "response": {"content": "saved code"},
        "usage": {"reservation_id": hold.reservation_id, "account_id": hold.account_id, "estimate_micro": hold.amount_micro,
            "workspace_id": d["wid"], "venture_id": d["vid"], "purpose": "pagebuilder.code",
            "model": quote["model"], "provider": quote["provider"], "provider_identity": quote["provider_identity"],
            "source": "platform", "request_id": request_id, "task_invocation": None,
            "actual_micro": 300, "provider_actual_micro": 300, "prompt_tokens": 100, "completion_tokens": 100,
            "cached_tokens": 0, "total_tokens": 200}})
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    call = runtime.job_model_call(job.id, payload)
    assert call("code", "Generate this page") == "saved code"
    assert call("code", "Generate this page") == "saved code"
    account = get_account(d["vid"])
    assert account.spent_micro == 300 and account.reserved_micro == 0


@pytest.mark.parametrize("renderer_down", [False, True])
@pytest.mark.parametrize("journal_state", ["calling", "uncertain"])
def test_uncertain_crashed_job_never_redials_or_expires_protected_hold(pagebuilder_db, monkeypatch, renderer_down, journal_state):
    from app.core.jobs import FatalError, lease
    from app.credits.accounts import get_account
    from app.credits.reserve import sweep_expired
    from app.db.models import CreditReservation, Job
    from app.pagebuilder import api, runtime, rendering
    from app.pagebuilder.worker import run_one
    from core.clock import utcnow
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError
    d = pagebuilder_db
    install_pipeline_stub(monkeypatch)
    calls = []
    class AbruptProviderCrash(BaseException):
        pass
    async def unknown(**kwargs):
        calls.append(1)
        if journal_state == "calling":
            raise AbruptProviderCrash("Synthetic process exit after protected dispatch")
        raise ProviderCallError("provider_error", "Synthetic unknown transport outcome", status=None)
    monkeypatch.setattr(sole, "transmit", unknown)
    started = api.start(d["vid"], d["actor"])
    with d["factory"]() as session:
        first = lease(session, "unknown-crashed-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    assert first.id == started["job_id"]
    payload = {**first.payload, "_job_id": first.id, "_worker_id": first.worker_id, "_job_attempt": first.attempts}
    rendering.require_renderer()  # Real initial preflight, before the first paid attempt.
    expected = AbruptProviderCrash if journal_state == "calling" else FatalError
    failure = "Synthetic process exit" if journal_state == "calling" else "pagebuilder_outcome_uncertain"
    with pytest.raises(expected, match=failure):
        runtime.job_model_call(first.id, payload)("brief_direction_plan", "Design this venture")
    assert runtime.journal.operation(first.id, "variant_0:brief_direction_plan")["state"] == journal_state
    held = get_account(d["vid"]).reserved_micro
    with d["factory"]() as session:
        session.get(Job, first.id).locked_at = utcnow() - timedelta(seconds=120)
        session.commit()
    with d["factory"]() as session:
        replacement = lease(session, "unknown-replacement-worker", kinds=[api.KIND], n=1)[0]
        session.commit()
    renderer_attempts = []
    if renderer_down:
        def unavailable():
            renderer_attempts.append(True)
            raise ValueError("pagebuilder_renderer_unavailable")
        monkeypatch.setattr(rendering, "require_renderer", unavailable)
    asyncio.run(run_one(d["factory"], replacement))
    receipt = api.status(d["vid"], d["actor"], first.id)
    assert receipt["state"] == "failed" and receipt["error"] == "pagebuilder_outcome_uncertain"
    assert receipt["receipt"]["uncertain_outcomes"] == 1 and len(calls) == 1
    assert renderer_attempts == []
    operation = runtime.journal.operation(first.id, "variant_0:brief_direction_plan")
    with d["factory"]() as session:
        hold = session.get(CreditReservation, operation["usage"]["reservation_id"])
        assert hold.status == "reconciling"
        expiry = hold.expires_at
    sweep_expired(now=expiry + timedelta(seconds=1))
    assert get_account(d["vid"]).reserved_micro == held and held > 0


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_free_build_failure_requeue_and_paid_revision_cap(pagebuilder_db, paid, explicit):
    """The venture lock keeps repeated unpaid retries on one durable intent."""
    from app.db.models import Job
    from app.pagebuilder import api
    from sqlalchemy import select
    d = pagebuilder_db
    ids = []
    for _ in range(3):
        started = api.build(d["vid"], d["actor"], new_version_of=ids[-1] if explicit and ids else None)
        ids.append(started["job_id"])
        with d["factory"]() as session:
            row = session.get(Job, started["job_id"])
            doc = json.loads(row.payload_json)
            doc["_runtime"].update(state="failed", error="pagebuilder_qa_failed" if paid else "pagebuilder_renderer_unavailable",
                calls_used=int(paid), operations={"variant_0:code": {"state": "completed", "usage": {"credits_micro": 5}}} if paid else {})
            row.payload_json, row.status, row.attempts = api.dump(doc), "failed_dead", 3
            session.commit()
    found = api.build(d["vid"], d["actor"], new_version_of=ids[-1] if explicit else None)
    # Undelivered paid failures no longer exhaust three ready versions; proven free failures still reuse their intent.
    assert found["state"] == "queued"
    assert (found["job_id"] not in ids) if paid else (found["job_id"] == ids[-1])
    with d["factory"]() as session:
        rows = session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()
        assert len(rows) == (4 if paid else 1)
        if not paid:
            assert found["requeued"] is True
            assert rows[0].attempts == 0 and rows[0].status == "scheduled"
