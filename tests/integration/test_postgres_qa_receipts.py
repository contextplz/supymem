"""Migrated PostgreSQL QA boundaries; only S3/task-report transport is a fixture."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from core.artifact_snapshot import canonical, freeze_source, manifest_digest
from tests.qa_storage_fixtures import CONTRACT, POLICY, MemoryS3, binding, report_fixture
from tests.integration.test_postgres_auth_security import ROOT, concurrent, migration_head, pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_build_pools import build_pool as build_pool, snapshot
from tests.integration.test_postgres_build_model_receipts import model_run as model_run, FENCE


@pytest.fixture()
def qa_source(model_run, tmp_path, monkeypatch):
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    from app.db.supymem_models import BuildArtifact, BuildRun
    from app.credits._txn import naive_utc
    d = model_run
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "index.html").write_text("<!doctype html><title>Acorn</title><h1>Acorn</h1><p>Reading lists</p>")
    manifest, blob = freeze_source(directory)
    meta = {"manifest_digest": manifest_digest(manifest), "archive_sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob)}
    scope = {"workspace_id": d["wid"], "venture_id": d["vid"], "app_id": d["app_id"], "build_id": d["run_id"], "generation": 1}
    config = ArtifactStoreConfig("test-artifacts", "managed-builds", "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    client = MemoryS3()
    store = S3ArtifactStore(config, client)
    receipt = store.persist(blob, meta, scope)
    artifact_id = "bar_" + uuid4().hex
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        session.add(BuildArtifact(id=artifact_id, build_run_id=run.id, app_id=d["app_id"], workspace_id=d["wid"],
                                  kind="source_zip", s3_key=receipt["key"], sha256=receipt["sha256"], size_bytes=len(blob), created_at=naive_utc()))
        spec = json.loads(run.spec)
        spec.update(artifact_verified=True, artifact_capture={**receipt, "artifact_id": artifact_id, "manifest": manifest, "state": "stored"})
        run.artifact_id, run.spec = artifact_id, json.dumps(spec)
        session.commit()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: store)
    return {**d, "store": store, "s3": client, "source_receipt": receipt, "blob": blob, "scope": scope, "artifact_id": artifact_id}


def prepared(d):
    from app.builder.qa_receipts import prepare, bind_task
    value = prepare(d["run_id"], contract=CONTRACT, policy=POLICY, **FENCE)
    bound = bind_task(value["id"], observation=binding(), **FENCE)
    report, evidence = report_fixture(value, d["run_id"])
    return value, bound, report, evidence


def capture(d, values):
    from app.builder.qa_receipts import capture
    value, bound, report, evidence = values
    return capture(value["id"], report_blob=report, evidence=evidence, task_binding_digest=bound["task_binding_digest"], **FENCE)


def test_real_pg_source_exact_version_duplicate_capture_and_no_fake_completion(qa_source):
    from app.builder.qa_receipts import prepare, read
    from app.db.supymem_models import BuildRun, QaRun, BuildEvent
    d = qa_source
    before = snapshot(d)
    values = prepared(d)
    value, _bound, report, evidence = values
    repeat = prepare(d["run_id"], contract=CONTRACT, policy=POLICY, **FENCE)
    assert repeat == value
    assert len([c for c in d["s3"].calls if c[0] == "get"]) == 1
    d["s3"].lose_put_ack = True
    one = capture(d, values)
    assert one == capture(d, values)
    assert one["state"] == "report_ready" and one["verified_report"] is True and one["verified_execution"] is False
    assert one["passed"] == len(json.loads(report)["cases"]) and one["failed"] == one["unsupported"] == 0
    assert len([c for c in d["s3"].calls if c[0] == "put"]) == 4  # source + report + two PNGs
    assert snapshot(d) == before  # No price, charge, refund, pool close or settlement is invented here.
    assert read(value["id"], run_id=d["run_id"], actor=d["actor"], object_id="report.json")[0] == report
    assert read(value["id"], run_id=d["run_id"], actor=d["actor"], object_id="desktop-0.png")[0] == evidence["desktop-0.png"]
    with d["factory"]() as session:
        run = session.get(BuildRun, d["run_id"])
        assert run.state == "running" and run.closed_at is None and not json.loads(run.spec).get("qa_verified")
        assert session.scalar(select(QaRun).where(QaRun.id == value["id"])).report_digest == hashlib.sha256(report).hexdigest()
        assert len(session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == run.id, BuildEvent.kind == "qa_report_stored")).all()) == 1


def test_source_retrieval_pins_version_and_rejects_tampered_body_and_foreign_scope(qa_source):
    from app.builder.qa_receipts import prepare
    from app.builder.model_proxy import BuildModelError
    d = qa_source
    receipt = d["source_receipt"]
    key = (receipt["bucket"], receipt["key"])
    d["s3"].latest[key] = "malicious-latest"
    assert d["store"].source(receipt, d["scope"]) == d["blob"]
    with pytest.raises(BuildModelError, match="receipt_invalid"):
        d["store"].source(receipt, {**d["scope"], "workspace_id": "foreign"})
    obj = d["s3"].objects[(*key, receipt["version_id"])]
    obj["body"] = b"x" * len(d["blob"])
    with pytest.raises(BuildModelError, match="body_mismatch"):
        prepare(d["run_id"], contract=CONTRACT, policy=POLICY, **FENCE)


def test_unknown_s3_ack_stays_uploading_and_retry_recovers_without_double_write(qa_source):
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import QaRun
    d = qa_source
    values = prepared(d)
    before = snapshot(d)
    d["s3"].lose_head_after_put = True
    with pytest.raises(BuildModelError, match="outcome_uncertain"):
        capture(d, values)
    with d["factory"]() as session:
        row = session.get(QaRun, values[0]["id"])
        assert json.loads(row.receipt_json)["phase"] == "uploading" and row.report_digest is None
    assert snapshot(d) == before
    assert capture(d, values)["verified_report"]
    assert len([c for c in d["s3"].calls if c[0] == "put"]) == 4
    assert snapshot(d) == before


@pytest.mark.parametrize("tamper", ["source", "missing_case", "duplicate_case", "evidence", "fake_counts", "self_attestation"])
def test_tampered_qa_protocol_cannot_write_or_claim_success(qa_source, tamper):
    from app.builder.model_proxy import BuildModelError
    d = qa_source
    value, bound, report, evidence = prepared(d)
    doc = json.loads(report)
    if tamper == "source":
        doc["source_sha256"] = "0" * 64
    elif tamper == "missing_case":
        doc["cases"] = [c for c in doc["cases"] if c["id"] != "http:0"]
        doc["coverage"]["observed_cases"] -= 1
    elif tamper == "duplicate_case":
        doc["cases"].append(doc["cases"][0])
    elif tamper == "fake_counts":
        doc["coverage"]["observed_cases"] = 9999
    elif tamper == "self_attestation":
        doc["trusted_task"] = binding()
    else:
        evidence["desktop-0.png"] = b"forged image"
    puts = len([c for c in d["s3"].calls if c[0] == "put"])
    with pytest.raises(BuildModelError):
        capture(d, (value, bound, canonical(doc), evidence))
    assert len([c for c in d["s3"].calls if c[0] == "put"]) == puts


def test_concurrent_report_capture_has_one_receipt_and_event(qa_source):
    from app.db.supymem_models import QaRun, BuildEvent
    d = qa_source
    values = prepared(d)
    results = concurrent(lambda: capture(d, values))
    assert len({json.dumps(v, sort_keys=True) for v in results}) == 1
    with d["factory"]() as session:
        assert len(session.scalars(select(QaRun).where(QaRun.id == values[0]["id"])).all()) == 1
        assert len(session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == d["run_id"], BuildEvent.kind == "qa_report_stored")).all()) == 1


def test_binding_wrong_image_and_lost_controller_cannot_capture(qa_source):
    from app.builder.qa_receipts import prepare, bind_task
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildRun
    d = qa_source
    value = prepare(d["run_id"], contract=CONTRACT, policy=POLICY, **FENCE)
    with pytest.raises(BuildModelError, match="observation_invalid"):
        bind_task(value["id"], observation={**binding(), "image_digest": "sha256:" + "b" * 64}, **FENCE)
    values = prepared(d)
    def rotate():
        with d["factory"]() as session:
            run = session.get(BuildRun, d["run_id"])
            spec = json.loads(run.spec)
            spec["controller_fence"]["attempt"] = 2
            run.spec = json.dumps(spec)
            session.commit()
    d["s3"].after_put = rotate
    with pytest.raises(BuildModelError, match="fence_lost"):
        capture(d, values)
    assert len([c for c in d["s3"].calls if c[0] == "put"]) == 2  # source + one already-in-flight immutable PNG only


def test_signed_api_qa_is_scoped_and_legacy_stays_not_run(qa_source, monkeypatch):
    secret = "qa-signed-api-secret-at-least32characters"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    monkeypatch.setenv("COMPANY_OS_VAULT_SECRET", "qa-test-vault-secret-more-than32characters")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "qa-admin@example.test")
    from app.main import create_app
    from app.db.supymem_models import QaRun, BuildRun
    from app.authz.workspace import ensure_subject_person
    from app.credits._txn import naive_utc
    from core.assertion import sign_assertion
    d = qa_source
    values = prepared(d)
    capture(d, values)
    ensure_subject_person("usr_qa_foreign")
    with d["factory"]() as session:
        session.add(QaRun(id="qa_legacy_" + uuid4().hex, app_id=d["app_id"], build_run_id=d["run_id"], workspace_id=d["wid"],
                          venture_id=d["vid"], state="passed", passed=9999, failed=0, created_at=naive_utc()))
        run = session.get(BuildRun, d["run_id"])
        run.spec = json.dumps({**json.loads(run.spec), "qa_verified": True})  # Old global boolean must not authorize legacy results.
        session.commit()
    def headers(path, actor=d["actor"]):
        return {"X-COS-Assertion": sign_assertion(secret, sub=actor, email="", role="user", path=path, method="GET")}
    base = f"/builds/{d['run_id']}/qa/{values[0]['id']}"
    client = TestClient(create_app())  # No lifespan/bootstrap/scheduler side effects.
    try:
        assert client.get(base).status_code == 401
        assert client.get(base, headers=headers(base, "usr_qa_foreign")).status_code in {403, 404}
        report = client.get(base + "/report", headers=headers(base + "/report"))
        assert report.status_code == 200 and report.content == values[2]
        assert report.headers["content-disposition"] == "attachment" and report.headers["cache-control"] == "no-store"
        path = base + "/evidence/desktop-0.png"
        assert client.get(path, headers=headers(path)).content == values[3]["desktop-0.png"]
        unknown = base + "/evidence/request.json"
        assert client.get(unknown, headers=headers(unknown)).status_code == 404
        path = f"/apps/{d['app_id']}/qa"
        items = client.get(path, headers=headers(path)).json()["items"]
        legacy = next(row for row in items if row["id"].startswith("qa_legacy"))
        assert legacy["state"] == "not_run" and legacy["passed"] is None and legacy["verified_report"] is False
    finally:
        client.close()


def test_migrated_constraints_reject_managed_cross_scope_and_missing_source(qa_source):
    from alembic import command
    from alembic.config import Config
    from app.db.supymem_models import QaRun
    d = qa_source
    values = prepared(d)
    with d["factory"]() as session, pytest.raises(IntegrityError):
        row = session.get(QaRun, values[0]["id"])
        row.workspace_id = "foreign"
        session.commit()
    with d["factory"]() as session, pytest.raises(IntegrityError):
        row = session.get(QaRun, values[0]["id"])
        row.source_artifact_id = None
        session.commit()
    columns = {column["name"] for column in inspect(d["engine"]).get_columns("qa_runs")}
    assert {"runtime_version", "receipt_json", "managed_build_id", "source_artifact_id"} <= columns
    cfg = Config(str(ROOT / "services/company_os/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "services/company_os/app/db/migrations"))
    with pytest.raises(RuntimeError, match="managed_qa_history_requires_forward_migration"):
        command.downgrade(cfg, "s007contentgen")
    from sqlalchemy import text
    with d["engine"].connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == migration_head()
    with d["factory"]() as session:
        assert session.get(QaRun, values[0]["id"]).input_digest == values[0]["input_digest"]
