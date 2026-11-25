"""Migrated local PostgreSQL scope/immutability tests; provider/storage calls remain absent."""
import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DBAPIError
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db


@pytest.fixture()
def intake(journey_db):
    from app.infra.apps import create_draft
    from app.db.import_models import ImportRun
    from app.db.supymem_models import FounderRequest
    from core.clock import utcnow
    from core.ids import new_id
    data = dict(journey_db)
    app = create_draft(workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"],
                       name="Import integrity", slug="import-" + uuid.uuid4().hex)
    now = utcnow()
    run_id, request_id = new_id("imp"), new_id("frq")
    with data["factory"]() as session:
        session.add(FounderRequest(id=request_id, workspace_id=data["wid"], venture_id=data["vid"], source="import",
            text="Controlled test source", intent="import", intent_category="import", classification="unknown", outcome="pending", created_at=now))
        session.flush()
        session.add(ImportRun(id=run_id, workspace_id=data["wid"], venture_id=data["vid"], app_id=app["id"], actor_subject=data["actor"],
            request_id=request_id, source_kind="upload_zip", idempotency_hash="a" * 64, input_digest="b" * 64,
            policy_version="import-intake-v1", state="awaiting_upload", requested_source_json="{}", reserved_bytes=100,
            acquired_bytes=0, retained_bytes=0, created_at=now, updated_at=now, expires_at=now + timedelta(minutes=15)))
        session.commit()
    return {**data, "app_id": app["id"], "import_id": run_id}


def test_real_migration_and_source_identity_constraints(intake):
    from app.db.import_models import ImportRun, SourceSnapshot
    from core.clock import utcnow
    data = intake
    row = dict(id="snapshot_" + uuid.uuid4().hex, workspace_id=data["wid"], venture_id=data["vid"], app_id=data["app_id"],
        import_run_id=data["import_id"], normalized_archive_sha256=hashlib.sha256(b"source").hexdigest(), size_bytes=6,
        manifest_digest="c" * 64, manifest_json="[]", storage_receipt_json="{}", source_provenance_json="{}",
        analysis_policy_version="import-intake-v1", created_at=utcnow())
    with data["factory"]() as session:
        session.add(SourceSnapshot(**{**row, "venture_id": "foreign-venture"}))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(SourceSnapshot(**row))
        session.flush()
        run = session.get(ImportRun, data["import_id"])
        run.source_snapshot_id = row["id"]
        run.state = "report_complete"
        session.commit()
    for column in ("manifest_json", "storage_receipt_json"):
        with data["factory"]() as session:
            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(text(f"UPDATE source_snapshots SET {column} = :changed WHERE id=:id"),
                                {"id": row["id"], "changed": "[1]"})
                session.commit()
    with data["factory"]() as session:
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(text("UPDATE import_runs SET actor_subject='another-member' WHERE id=:id"), {"id": data["import_id"]})
            session.commit()
    with data["factory"]() as session:
        saved = session.get(ImportRun, data["import_id"])
        assert saved.source_snapshot_id == row["id"] and saved.state == "report_complete"
        assert json.loads(session.get(SourceSnapshot, row["id"]).manifest_json) == []

@pytest.fixture()
def flow(intake, monkeypatch):
    from app.imports import intake as service, runner
    from app.imports.analysis import analyze_archive
    from app.imports.process import ProcessResult
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    from tests.qa_storage_fixtures import MemoryS3
    import io
    import zipfile
    config = ArtifactStoreConfig("test-artifacts", "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    client = MemoryS3()
    store = S3ArtifactStore(config, client)
    monkeypatch.setattr(service, "settings", lambda: {"ready": True, "storage_config_digest": config.fingerprint})
    monkeypatch.setattr(service.storage, "configured", lambda: store)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("package.json", '{"dependencies":{"next":"15.0.0"},"scripts":{"build":"DO NOT EXECUTE"}}')
        z.writestr("dist/index.html", "<h1>Controlled source</h1>")
        z.writestr("LICENSE", "MIT License")
    blob = buffer.getvalue()
    source = dict(source="upload_zip", filename="source.zip", size_bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    calls = []
    def controlled(kind, data, *, archive=None):
        calls.append(kind)
        assert kind == "analyze", "no GitHub/provider calls in this fixture"
        result = analyze_archive(archive, **data)
        return ProcessResult(dict(manifest=result.manifest, report=result.report, provenance=result.provenance), result.archive)
    monkeypatch.setattr(runner.process, "run", controlled)
    yield {**intake, "service": service, "runner": runner, "store": store, "s3": client, "blob": blob, "source": source, "calls": calls}
    # This module shares one migrated disposable DB; do not leave fixture jobs
    # runnable after their in-memory network fixture has gone out of scope.
    from app.db.models import Job
    from sqlalchemy import update
    with intake["factory"]() as session:
        session.execute(update(Job).where(Job.workspace_id == intake["wid"], Job.status.in_(["scheduled", "pending", "retry_wait", "leased"])).values(status="cancelled"))
        session.commit()


def _start(d, key="import-first-intent"):
    return d["service"].start(d["wid"], d["vid"], d["app_id"], d["source"], actor=d["actor"], idempotency_key=key)


def _upload(d, row):
    args = (d["wid"], d["vid"], d["app_id"], row["import_id"])
    claim = d["service"].prepare_upload(*args, actor=d["actor"])
    return d["service"].finish_upload(*args, d["blob"], actor=d["actor"], claim_id=claim["claim_id"])


def _lease(d):
    from app.core.jobs import lease
    with d["factory"]() as session:
        claimed = lease(session, "import-test-worker", kinds=["import.analyze"], n=1)
        session.commit()
    assert len(claimed) == 1
    job = claimed[0]
    return job, {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def test_real_intake_upload_job_receipts_report_and_response_loss_replay(flow):
    from app.db.import_models import ImportRun, SourceSnapshot
    from app.db.supymem_models import ExistingAsset
    from sqlalchemy import select, func
    d = flow
    row = _start(d)
    assert row["state"] == "awaiting_upload" and row["job_id"] is None
    assert _start(d)["import_id"] == row["import_id"]
    d["s3"].lose_put_ack = True
    uploaded = _upload(d, row)
    assert uploaded["state"] == "queued"
    job, payload = _lease(d)
    d["runner"].run_analyze(payload)
    saved = d["service"].read(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])
    assert saved["state"] == "report_complete" and saved["source_snapshot_id"]
    assert saved["report"]["execution_supported"] is False and saved["execution_available"] is False
    assert saved["credit_usage"]["consumed_micro"] == 0 and d["calls"] == ["analyze"]
    puts = len([x for x in d["s3"].calls if x[0] == "put"])
    d["runner"].run_analyze(payload)
    assert _start(d)["source_snapshot_id"] == saved["source_snapshot_id"]
    assert len([x for x in d["s3"].calls if x[0] == "put"]) == puts
    with d["factory"]() as session:
        snapshot = session.get(SourceSnapshot, saved["source_snapshot_id"])
        assert {m["path"] for m in json.loads(snapshot.manifest_json)} == {"package.json", "dist/index.html", "LICENSE"}
        assert session.scalar(select(func.count()).select_from(SourceSnapshot).where(SourceSnapshot.import_run_id == row["import_id"])) == 1
        assert session.scalar(select(func.count()).select_from(ExistingAsset).where(ExistingAsset.source == "verified_import", ExistingAsset.workspace_id == d["wid"])) == 1
        assert session.get(ImportRun, row["import_id"]).reserved_bytes == 0


def test_ambiguous_raw_write_reconciles_without_second_upload(flow):
    from app.db.models import Job
    from app.imports.contracts import ImportFailure
    from app.credits._txn import naive_utc
    d = flow
    row = _start(d)
    d["s3"].lose_head_after_put = True
    with pytest.raises(ImportFailure, match="outcome_unconfirmed"):
        _upload(d, row)
    with d["factory"]() as session:
        jid = d["service"].read(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])["job_id"]
        job = session.get(Job, jid)
        job.run_after = job.scheduled_for = naive_utc()
        session.commit()
    _job, payload = _lease(d)
    d["runner"].run_analyze(payload)
    saved = d["service"].read(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])
    assert saved["state"] == "report_complete"
    rawputs = [value for name, value in d["s3"].calls if name == "put" and "/raw/" in value["Key"]]
    assert len(rawputs) == 1


def test_revoked_actor_cleanup_does_not_publish_source(flow):
    from app.db.models import Workspace
    from app.db.import_models import ImportRun, SourceSnapshot
    from app.core.jobs import FatalError
    from sqlalchemy import select, func
    d = flow
    row = _upload(d, _start(d))
    job, payload = _lease(d)
    with d["factory"]() as session:
        workspace = session.get(Workspace, d["wid"])
        workspace.settings_json = '{"blocked":true}'
        session.commit()
    assert d["runner"].cleanup_scope(job)
    with pytest.raises(FatalError, match="authority_revoked"):
        d["runner"].run_analyze(payload)
    with d["factory"]() as session:
        assert session.get(ImportRun, row["import_id"]).state == "cancelled"
        assert session.scalar(select(func.count()).select_from(SourceSnapshot).where(SourceSnapshot.import_run_id == row["import_id"])) == 0
    assert d["calls"] == []


def test_upload_claim_source_integrity_and_cancel_before_transfer(flow):
    from app.imports.contracts import ImportFailure
    d = flow
    row = _start(d)
    args = (d["wid"], d["vid"], d["app_id"], row["import_id"])
    claim = d["service"].prepare_upload(*args, actor=d["actor"])
    with pytest.raises(ImportFailure, match="in_progress"):
        d["service"].prepare_upload(*args, actor=d["actor"])
    with pytest.raises(ImportFailure, match="bytes_mismatch"):
        d["service"].finish_upload(*args, b"wrong", actor=d["actor"], claim_id=claim["claim_id"])
    saved = d["service"].read(*args, actor=d["actor"])
    assert d["service"].cancel(*args, actor=d["actor"], state_version=saved["state_version"])["state"] == "cancelled"
    with pytest.raises(ImportFailure, match="claim_changed"):
        d["service"].finish_upload(*args, d["blob"], actor=d["actor"], claim_id=claim["claim_id"])
    assert not d["s3"].calls


def test_signed_http_intake_scope_and_legacy_rejection(flow, monkeypatch):
    from fastapi.testclient import TestClient
    from core.assertion import reset_nonce_store, sign_assertion
    from app.infra.apps import create_draft
    d = flow
    secret = "intake-test-assertion-secret-only-" * 2
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "postgres-import-vault-secret-at-least-32-bytes")
    monkeypatch.setenv("ADMIN_REQUIRE_TOTP", "1")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "import-admin@example.test")
    monkeypatch.setenv("LLM_CREDITS_REQUIRE_GATE", "1")
    reset_nonce_store()
    from app.main import create_app
    client = TestClient(create_app())
    base = f'/ventures/{d["vid"]}/apps/{d["app_id"]}/imports'
    def request(method, path, *, workspace=None, extra_headers=None, **kwargs):
        token = sign_assertion(secret, sub=d["actor"], email="", role="user", path=path, method=method)
        return client.request(method, path, headers={"X-COS-Assertion": token, "X-Workspace-ID": workspace or d["wid"], "Idempotency-Key": "http-intake-intent", **(extra_headers or {})}, **kwargs)
    assert client.get(base + "/capabilities").status_code == 401
    assert request("GET", base + "/capabilities", workspace="foreign").status_code == 404
    assert request("POST", base, json={"source": {"source": "fixture", "path": "/tmp/private"}}).status_code == 422
    created = request("POST", base, json={"source": d["source"]})
    assert created.status_code == 202, created.text
    rid = created.json()["import_id"]
    other = create_draft(workspace_id=d["wid"], venture_id=d["vid"], actor=d["actor"], name="Other", slug="other-" + uuid.uuid4().hex)
    path = f'/ventures/{d["vid"]}/apps/{other["id"]}/imports/{rid}'
    assert request("GET", path).status_code == 404
    uploaded = request("PUT", base + "/" + rid + "/archive", content=d["blob"], extra_headers={"Content-Type": "application/zip"})
    assert uploaded.status_code == 202, uploaded.text
    assert uploaded.json()["state"] == "queued"
    assert request("POST", f'/ventures/{d["vid"]}/imports', json={"source": "fixture"}).status_code == 404


def test_concurrent_start_has_one_intent_job_and_quota(flow):
    from tests.integration.test_postgres_auth_security import concurrent
    from app.db.import_models import ImportRun
    from app.imports.contracts import ImportFailure
    from sqlalchemy import select, func
    d = flow
    results = concurrent(lambda: _start(d), count=2)
    assert results[0]["import_id"] == results[1]["import_id"]
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ImportRun).where(ImportRun.workspace_id == d["wid"])) == 2
    with pytest.raises(ImportFailure, match="quota_exceeded"):
        _start(d, "another-request-key")


def test_duplicate_worker_and_cancel_during_parse_do_not_promote(flow, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.core.jobs import FatalError
    from app.db.import_models import ImportRun, SourceSnapshot
    from sqlalchemy import select, func
    d = flow
    row = _upload(d, _start(d))
    _job, payload = _lease(d)
    began, release = threading.Event(), threading.Event()
    original = d["runner"].process.run
    def slow(*args, **kwargs):
        began.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(d["runner"].process, "run", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(d["runner"].run_analyze, payload)
        assert began.wait(5)
        with pytest.raises(FatalError, match="operation_in_progress"):
            d["runner"].run_analyze(payload)
        latest = d["service"].read(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])
        cancelled = d["service"].cancel(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"], state_version=latest["state_version"])
        assert cancelled["state"] == "cancel_requested"
        release.set()
        with pytest.raises(FatalError, match="import_cancelled"):
            first.result(5)
    with d["factory"]() as session:
        assert session.get(ImportRun, row["import_id"]).state == "cancelled"
        assert session.scalar(select(func.count()).select_from(SourceSnapshot).where(SourceSnapshot.import_run_id == row["import_id"])) == 0
    assert not any("/source/" in value["Key"] for name, value in d["s3"].calls if name == "put")


def test_real_worker_dispatch_uses_persisted_scope_and_finishes(flow):
    import asyncio
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.db.models import Job
    d = flow
    row = _upload(d, _start(d))
    job, _payload = _lease(d)
    install_default_handlers()
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    with d["factory"]() as session:
        saved = session.get(Job, job.id)
        assert saved.status == "succeeded", saved.last_error
    assert d["service"].read(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])["state"] == "report_complete"


def test_expired_untouched_intent_does_not_lock_quota_or_mutate_on_read(flow):
    from app.db.import_models import ImportRun
    from app.credits._txn import naive_utc
    d = flow
    row = _start(d)
    with d["factory"]() as session:
        saved = session.get(ImportRun, row["import_id"])
        saved.expires_at = naive_utc() - timedelta(seconds=1)
        session.commit()
    args = (d["wid"], d["vid"], d["app_id"], row["import_id"])
    assert d["service"].read(*args, actor=d["actor"])["state"] == "expired"
    fresh = _start(d, "replacement-after-expiry")
    assert fresh["import_id"] != row["import_id"]
    with d["factory"]() as session:
        assert session.get(ImportRun, row["import_id"]).state == "awaiting_upload"


def test_github_pins_commit_before_retryable_download_failure(flow, monkeypatch):
    from app.core.jobs import RetryableError, fail_job
    from app.imports.contracts import ImportFailure
    from app.imports.process import ProcessResult
    from app.db.models import Job
    from app.db.import_models import ImportRun
    from app.credits._txn import naive_utc
    import io
    import zipfile
    d = flow
    d["source"] = {"source": "github_public", "repo_url": "https://github.com/acme/test"}
    original, operations = d["runner"].process.run, []
    with zipfile.ZipFile(io.BytesIO(d["blob"])) as old:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path in old.namelist():
                archive.writestr("acme-test-abcdef/" + path, old.read(path))
    blob = buffer.getvalue()
    def controlled(kind, data, *, archive=None):
        operations.append((kind, dict(data)))
        if kind == "resolve":
            return ProcessResult({"resolved": {"repository": "acme/test", "commit_sha": "a" * 40,
                "requested_ref": "main", "public": True, "policy_version": "import-intake-v1"}, "requests": 2})
        if kind == "download":
            assert data["commit_sha"] == "a" * 40
            if len([x for x in operations if x[0] == "download"]) == 1:
                raise ImportFailure("import_process_deadline", status=503)
            return ProcessResult({}, blob)
        return original(kind, data, archive=archive)
    monkeypatch.setattr(d["runner"].process, "run", controlled)
    row = _start(d)
    job, payload = _lease(d)
    with pytest.raises(RetryableError):
        d["runner"].run_analyze(payload)
    with d["factory"]() as session:
        fail_job(session, job.id, RetryableError("controlled_transport"), worker_id=job.worker_id, attempt=job.attempts)
        session.flush()
        current = session.get(Job, job.id)
        current.run_after = current.scheduled_for = naive_utc()
        session.commit()
    job, payload = _lease(d)
    d["runner"].run_analyze(payload)
    assert [k for k, _ in operations] == ["resolve", "download", "download", "analyze"]
    with d["factory"]() as session:
        assert session.get(ImportRun, row["import_id"]).source_snapshot_id


def test_actual_worker_honors_bounded_github_rate_delay(flow, monkeypatch):
    import asyncio
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.db.models import Job
    from app.imports.contracts import ImportFailure
    from app.credits._txn import naive_utc
    d = flow
    d["source"] = {"source": "github_public", "repo_url": "https://github.com/acme/test"}
    def limited(*args, **kwargs):
        raise ImportFailure("import_github_rate_limited", status=503, retry_after=900)
    monkeypatch.setattr(d["runner"].process, "run", limited)
    _start(d)
    job, _payload = _lease(d)
    install_default_handlers()
    asyncio.run(_run_one(d["factory"], job, {job.id}))
    with d["factory"]() as session:
        saved = session.get(Job, job.id)
        assert saved.status == "retry_wait" and saved.attempts == 1
        assert 890 <= (saved.run_after - naive_utc()).total_seconds() <= 900
