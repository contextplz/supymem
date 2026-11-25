"""Actual fenced PostgreSQL artifact receipts and SDK-shaped S3 transport only."""
import base64
import hashlib
import json
from uuid import uuid4

import boto3
from botocore.stub import Stubber
import httpx
import pytest
from sqlalchemy import select

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_build_pools import build_pool as build_pool
from tests.integration.test_postgres_build_model_receipts import model_run as model_run, FENCE
from core.artifact_snapshot import freeze_source, manifest_digest


def store_fixture():
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    config = ArtifactStoreConfig("test-artifacts", "managed-builds", "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
    client = boto3.client("s3", region_name=config.region, aws_access_key_id="fixture-access", aws_secret_access_key="fixture-secret")
    return S3ArtifactStore(config, client), Stubber(client)


def captured_run(model_run, tmp_path):
    from app.db.supymem_models import BuildRun
    from app.builder.intent_bridge import install_task_binding
    d, command = model_run, str(uuid4())
    arn = "arn:aws:ecs:us-east-1:123456789012:task/fixture/task"
    with d["factory"]() as session:
        row = session.get(BuildRun, d["run_id"])
        spec = json.loads(row.spec)
        spec.update(managed_command={"id": command, "kind": "start", "body_digest": "x", "state": "completed"},
                    artifact_capture={"state": "pending", "command_id": command})
        row.sandbox_ref, row.spec = arn, json.dumps(spec)
        session.commit()
    install_task_binding(d["run_id"], base_url="http://10.0.1.5:8000", task_arn=arn,
                         task_control_token="task-control-test-secret-at-least32", **FENCE)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("Founder product source\n")
    manifest, blob = freeze_source(source)
    metadata = {"schema_version": 1, "build_id": d["run_id"], "generation": 1, "command_id": command, "format": "zip",
                "archive_sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob), "manifest_digest": manifest_digest(manifest), "manifest": manifest}
    scope = {"workspace_id": d["wid"], "venture_id": d["vid"], "app_id": d["app_id"], "build_id": d["run_id"], "generation": 1}
    return d, command, metadata, blob, scope


def enqueue_store(stub, store, metadata, blob, scope, *, uncertain=False, bad_checksum=False, head_code="404"):
    config = store.config
    base = {"Bucket": config.bucket, "Key": config.key(scope, metadata["manifest_digest"]), "ExpectedBucketOwner": config.expected_owner}
    checksum = base64.b64encode(hashlib.sha256(blob).digest()).decode()
    tags = {**{k: str(v) for k, v in scope.items()}, "manifest_digest": metadata["manifest_digest"], "sha256": metadata["archive_sha256"]}
    stub.add_client_error("head_object", service_error_code=head_code, http_status_code=int(head_code), expected_params={**base, "ChecksumMode": "ENABLED"})
    args = {**base, "Body": blob, "ContentLength": len(blob), "ContentType": "application/zip", "ChecksumSHA256": checksum,
            "Metadata": tags, "IfNoneMatch": "*", "ServerSideEncryption": "aws:kms", "SSEKMSKeyId": config.kms_key_arn}
    if uncertain:
        stub.add_client_error("put_object", service_error_code="RequestTimeout", http_status_code=400, expected_params=args)
    else:
        stub.add_response("put_object", {"VersionId": "fixture-version"}, args)
    stub.add_response("head_object", {"ContentLength": len(blob), "ChecksumSHA256": "wrong" if bad_checksum else checksum,
                                     "Metadata": tags, "ContentType": "application/zip", "ServerSideEncryption": "aws:kms",
                                     "SSEKMSKeyId": config.kms_key_arn, "VersionId": "fixture-version"}, {**base, "ChecksumMode": "ENABLED"})


def network_fixture(monkeypatch, d, command, metadata, blob, *, before_archive=None):
    from app.builder import artifacts
    calls = []
    def handle(request):
        calls.append(request.url.path)
        assert request.url.host == "10.0.1.5" and request.headers["x-supymem-build-id"] == d["run_id"]
        assert request.headers["authorization"] == "Bearer task-control-test-secret-at-least32"
        if request.url.path == "/v1/state":
            body = {"build_id": d["run_id"], "generation": 1, "persistence_available": True, "active_command_id": None,
                    "commands": [{"command_id": command, "sequence": 1, "status": "completed", "driver_status": "finished"}]}
        elif request.url.path.endswith("/manifest"):
            body = metadata
        elif request.url.path.endswith("/archive"):
            assert request.headers["x-supymem-artifact-sha256"] == metadata["archive_sha256"]
            if before_archive:
                before_archive()
            return httpx.Response(200, headers={"content-type": "application/zip", "content-length": str(len(blob))}, stream=httpx.ByteStream(blob))
        else:
            pytest.fail("unreviewed endpoint")
        raw = json.dumps(body).encode()
        return httpx.Response(200, headers={"content-type": "application/json", "content-length": str(len(raw))}, stream=httpx.ByteStream(raw))
    monkeypatch.setattr(artifacts, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle), trust_env=False, follow_redirects=False))
    return calls


@pytest.mark.parametrize("head_code", ["404", "403"])
def test_artifact_capture_checked_s3_lost_ack_commits_once_without_qa(model_run, tmp_path, monkeypatch, head_code):
    from app.builder import artifacts
    from app.db.supymem_models import BuildArtifact, BuildEvent, BuildRun
    d, command, metadata, blob, scope = captured_run(model_run, tmp_path)
    store, stub = store_fixture()
    enqueue_store(stub, store, metadata, blob, scope, uncertain=True, head_code=head_code)
    monkeypatch.setattr(artifacts.S3ArtifactStore, "configured", lambda: store)
    calls = network_fixture(monkeypatch, d, command, metadata, blob)
    with stub:
        result = artifacts.import_completed_artifact(d["run_id"], command, **FENCE)
        repeat = artifacts.import_completed_artifact(d["run_id"], command, **FENCE)
        assert result == repeat and len(calls) == 3
        stub.assert_no_pending_responses()
    with d["factory"]() as session:
        rows = session.scalars(select(BuildArtifact).where(BuildArtifact.build_run_id == d["run_id"])).all()
        assert len(rows) == 1 and rows[0].sha256 == metadata["archive_sha256"]
        row = session.get(BuildRun, d["run_id"])
        spec = json.loads(row.spec)
        assert spec["artifact_capture"]["state"] == "stored" and spec["artifact_verified"]
        assert row.state == "running" and not spec.get("execution_verified") and spec.get("qa_state") != "passed"
        events = session.scalars(select(BuildEvent).where(BuildEvent.build_run_id == row.id, BuildEvent.kind == "artifact")).all()
        assert len(events) == 1 and json.loads(events[0].payload)["qa_state"] == "not_run"
        assert row.event_seq == events[0].seq
        # The normal watcher uses the canonical run counter, not MAX(event).
        from app.builder.execution import _event
        previous = row.event_seq
        _event(session, row, "state", {"state": "running"})
        session.commit()
        following = session.scalar(select(BuildEvent).where(BuildEvent.build_run_id == row.id, BuildEvent.seq == previous + 1))
        assert following.kind == "state" and row.event_seq == previous + 1


def test_artifact_capture_lost_generation_prevents_any_s3_write(model_run, tmp_path, monkeypatch):
    from app.builder import artifacts
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildArtifact, BuildRun
    d, command, metadata, blob, scope = captured_run(model_run, tmp_path)
    store, stub = store_fixture()
    monkeypatch.setattr(artifacts.S3ArtifactStore, "configured", lambda: store)
    def change_generation():
        with d["factory"]() as session:
            row = session.get(BuildRun, d["run_id"])
            spec = json.loads(row.spec)
            spec["generation"] = 2
            row.spec = json.dumps(spec)
            session.commit()
    network_fixture(monkeypatch, d, command, metadata, blob, before_archive=change_generation)
    with stub, pytest.raises(BuildModelError, match="fence"):
        artifacts.import_completed_artifact(d["run_id"], command, **FENCE)
    with d["factory"]() as session:
        assert session.scalar(select(BuildArtifact).where(BuildArtifact.build_run_id == d["run_id"])) is None


def test_bad_s3_checksum_keeps_capture_pending_without_artifact(model_run, tmp_path, monkeypatch):
    from app.builder import artifacts
    from app.builder.model_proxy import BuildModelError
    from app.db.supymem_models import BuildArtifact, BuildRun
    d, command, metadata, blob, scope = captured_run(model_run, tmp_path)
    store, stub = store_fixture()
    enqueue_store(stub, store, metadata, blob, scope, bad_checksum=True)
    monkeypatch.setattr(artifacts.S3ArtifactStore, "configured", lambda: store)
    network_fixture(monkeypatch, d, command, metadata, blob)
    with stub, pytest.raises(BuildModelError, match="verification_failed"):
        artifacts.import_completed_artifact(d["run_id"], command, **FENCE)
    with d["factory"]() as session:
        assert session.scalar(select(BuildArtifact).where(BuildArtifact.build_run_id == d["run_id"])) is None
        row = session.get(BuildRun, d["run_id"])
        assert json.loads(row.spec)["artifact_capture"]["state"] == "uploading"
        assert row.state == "running"
