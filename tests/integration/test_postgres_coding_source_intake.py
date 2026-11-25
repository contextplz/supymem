"""A stopped Mac coding order gains source custody, never task or hosting success."""
import hashlib
import json
import uuid

import pytest

from tests.integration.test_postgres_auth_security import (
    pg_database as pg_database,  # noqa: PLC0414
)
from tests.integration.test_postgres_journey_jobs import (
    journey_db as journey_db,  # noqa: PLC0414
)


def test_stopped_order_source_is_scoped_immutable_and_read_back(journey_db, monkeypatch, tmp_path):
    from app.admin.authn import CallerContext
    from app.build import coding_source_intake as intake
    from app.build import coding_source_preflight as preflight
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    from app.db.models import VentureDocument
    from app.db.supymem_models import BuildRun, CodingSourceReceipt, FounderApp
    from core.artifact_snapshot import freeze_source, manifest_digest
    from core.clock import utcnow
    from core.ids import new_id

    from tests.qa_storage_fixtures import MemoryS3

    d = journey_db
    config = ArtifactStoreConfig("test-artifacts", "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
        "us-east-1", "123456789012")
    memory = MemoryS3()
    monkeypatch.setattr(S3ArtifactStore, "configured", lambda: S3ArtifactStore(config, memory))
    source = tmp_path / "app-source"
    source.mkdir()
    (source / "package.json").write_text('{"scripts":{"build":"echo test"}}')
    manifest, archive = freeze_source(source)
    sha = hashlib.sha256(archive).hexdigest()
    digest = manifest_digest(manifest)
    order_id, task_id, execution_id, app_id = new_id("dwo"), new_id("tsk"), new_id("dex"), new_id("app")
    prior_id = new_id("bld")
    caller = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test", device_id="dev_one")
    body = {"id": order_id, "workspace_id": d["wid"], "venture_id": d["vid"], "actor_id": d["actor"],
            "device_id": "dev_one", "state": "stopped", "claimed_at": "2026-09-24T00:00:00Z",
            "closed_at": "2026-09-24T00:01:00Z", "settlement_ledger_id": "led_test",
            "execution_id": execution_id, "native_stop": {"processes_stopped": True,
              "execution_id": execution_id}, "task_snapshot": {"task_id": task_id,
              "closed_failed_build_run_id": prior_id},
            "manifest": {"coding_verification": {"operation": "workspace_edit_v1", "provider": "codex"}}}
    now = utcnow()
    with d["factory"]() as session:
        session.add(FounderApp(id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            slug="coding-source-" + uuid.uuid4().hex, name="Test app", kind="web",
            created_at=now, updated_at=now))
        session.add(FounderApp(id=new_id("app"), workspace_id=d["wid"], venture_id=d["vid"],
            slug="other-product-" + uuid.uuid4().hex, name="Other app", kind="web",
            created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id=prior_id, app_id=app_id, workspace_id=d["wid"], venture_id=d["vid"],
            plan_task_id=task_id, trigger="test", agent="codex", state="failed",
            spec="{}", closed_at=now, created_at=now))
        session.add(VentureDocument(id=order_id, venture_id=d["vid"], kind="device_work_order_v1",
            title="Reviewed coding", provenance_json=json.dumps(body), created_at=now, updated_at=now))
        session.commit()

    intent = {"execution_id": execution_id, "sha256": sha, "manifest_sha256": digest, "size_bytes": len(archive)}
    prepared = intake.prepare(d["vid"], order_id, caller, **intent)
    assert prepared["state"] == "awaiting_upload" and prepared["app_id"] == app_id
    assert intake.prepare(d["vid"], order_id, caller, **intent)["state"] == "awaiting_upload"
    with pytest.raises(intake.SourceIntakeError, match="coding_source_intent_conflict"):
        intake.prepare(d["vid"], order_id, caller, **{**intent, "sha256": "b" * 64})
    with pytest.raises(intake.SourceIntakeError, match="coding_source_archive_mismatch"):
        intake.upload(d["vid"], order_id, caller, archive + b"changed")
    wrong_device = CallerContext(d["actor"], "", "user", "/test", "POST", 9999999999, "test",
                                 device_id="dev_other")
    with pytest.raises(intake.SourceIntakeError, match="coding_source_order_not_found"):
        intake.read(d["vid"], order_id, wrong_device)
    saved = intake.upload(d["vid"], order_id, caller, archive)
    assert saved["state"] == "stored" and saved["sha256"] == sha
    assert saved["qa_verified"] is False and saved["hosting_authorized"] is False
    assert intake.upload(d["vid"], order_id, caller, archive) == saved
    assert intake.read(d["vid"], order_id, caller) == saved
    checked = preflight.run(d["vid"], order_id, caller)
    assert checked["source_sha256"] == sha
    assert checked["status"] == "blocked_runtime_contract"
    assert "start_script_missing" in checked["findings"]
    assert "hosted_product_artifact_missing" in checked["findings"]
    assert checked["runtime_qa_verified"] is False and checked["hosting_authorized"] is False
    assert preflight.run(d["vid"], order_id, caller) == checked
    assert preflight.read(d["vid"], order_id, caller) == checked
    with pytest.raises(intake.SourceIntakeError, match="coding_source_order_not_found"):
        preflight.read(d["vid"], order_id, wrong_device)
    from app.analytics.snapshot import snapshot
    operator = snapshot()
    assert operator["coding_source_custody"]["orders_by_state"]["stored"] >= 1
    assert operator["coding_source_preflight"]["reports_by_status"]["blocked_runtime_contract"] >= 1
    assert snapshot(venture_id=d["vid"])["coding_source_custody"]["ventures_with_stored_source"] == 1
    with d["factory"]() as session:
        assert session.get(CodingSourceReceipt, order_id).state == "stored"
