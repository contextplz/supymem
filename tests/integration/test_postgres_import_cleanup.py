"""Migrated PG retirement/jobs plus real version-fence algorithm; no cloud calls."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_import_intake import intake as intake, flow as flow, _start, _upload, _lease
from tests.unit.test_import_cleanup_store import VersionedS3


@pytest.fixture()
def retiring(flow, monkeypatch):
    from app.imports import cleanup
    from app.imports.cleanup_store import ImportObjectCleaner
    data = dict(flow)
    data["store"].client = data["s3"] = VersionedS3()
    monkeypatch.setattr(cleanup.storage, "cleanup_ready", lambda: data["store"].config)
    phases = []
    def child(operation, target, *, fence=None, plan=None):
        phases.append(operation)
        assert target["storage_config_digest"] == data["store"].config.fingerprint
        cleaner = ImportObjectCleaner(data["store"], target["scope"], kind=target["kind"], intent=target["intent"])
        return cleaner.fence() if operation == "fence" else cleaner.plan(fence) if operation == "plan" else cleaner.erase(fence, plan)
    monkeypatch.setattr(cleanup.cleanup_process, "run", child)
    return {**data, "cleanup": cleanup, "phases": phases}


def _due(d, identifier, kind):
    from app.db.import_cleanup_models import ImportObjectCleanup
    from app.db.models import Job
    from app.credits._txn import naive_utc
    with d["factory"]() as session:
        item = session.scalar(select(ImportObjectCleanup).where(ImportObjectCleanup.import_run_id == identifier,
            ImportObjectCleanup.object_kind == kind))
        item.due_at = naive_utc() - timedelta(seconds=1)
        job = session.get(Job, item.job_id)
        job.run_after = job.scheduled_for = item.due_at
        session.commit()
        return item.id


def _cleanup_lease(d):
    from app.core.jobs import lease
    with d["factory"]() as session:
        jobs = lease(session, "cleanup-test", kinds=["import.cleanup"], n=1)
        session.commit()
    assert len(jobs) == 1
    job = jobs[0]
    assert d["cleanup"].cleanup_scope(job)
    return job, {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}


def _read(d, identifier):
    return d["service"].read(d["wid"], d["vid"], d["app_id"], identifier, actor=d["actor"])


def _complete_import(d):
    row = _upload(d, _start(d))
    _, payload = _lease(d)
    d["runner"].run_analyze(payload)
    return _read(d, row["import_id"])


def _remove(d, row):
    return d["cleanup"].remove(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"], state_version=row["state_version"])


def test_raw_retention_keeps_report_then_founder_deletion_erases_source_exactly_once(retiring):
    from app.db.import_cleanup_models import ImportObjectCleanup
    from app.db.import_models import ImportRun, SourceSnapshot
    from app.db.supymem_models import ExistingAsset
    d = retiring
    row = _complete_import(d)
    identifier = row["import_id"]
    _due(d, identifier, "raw")
    _, payload = _cleanup_lease(d)
    d["cleanup"].run_cleanup(payload)
    d["cleanup"].run_cleanup(payload)  # Duplicate after durable completion is harmless.
    saved = _read(d, identifier)
    assert saved["source_available"] and saved["report"]
    assert saved["bytes"]["acquired"] == len(d["blob"])
    assert saved["bytes"]["retained"] < row["bytes"]["retained"]
    assert saved["retirement"]["verified_erased_kinds"] == ["raw"]
    deleted = _remove(d, saved)
    assert deleted["state"] == "deleted" and not deleted["report"] and not deleted["source_available"]
    _remove(d, saved)  # Original version after accepted deletion is idempotent.
    _due(d, identifier, "source")
    _, payload = _cleanup_lease(d)
    d["cleanup"].run_cleanup(payload)
    with d["factory"]() as session:
        imported = session.get(ImportRun, identifier)
        assert imported.retained_bytes == imported.reserved_bytes == 0
        assert session.get(SourceSnapshot, imported.source_snapshot_id) is not None
        items = session.scalars(select(ImportObjectCleanup).where(ImportObjectCleanup.import_run_id == identifier)).all()
        assert len(items) == 2 and all(x.state == "complete" for x in items)
        assert sum(x.released_bytes for x in items) == row["bytes"]["retained"]
        asset = session.scalar(select(ExistingAsset).where(ExistingAsset.workspace_id == d["wid"], ExistingAsset.kind == "imported_source"))
        assert not asset.exists and not asset.accessible and not asset.usable
        # Late successful I/O receipts preserve history but cannot re-charge quota.
        doc = json.loads(imported.acquisition_json)
        d["runner"]._account(imported, doc)
        assert imported.retained_bytes == imported.reserved_bytes == 0
        assert imported.acquired_bytes == len(d["blob"])
        session.rollback()
    assert len(d["s3"].data) == 2 and all(x["Body"] == b"" for x in d["s3"].data.values())


def test_lost_delete_response_retains_quota_and_replays_saved_plan(retiring):
    from app.core.jobs import RetryableError
    from app.db.import_cleanup_models import ImportObjectCleanup
    from app.db.import_models import ImportRun
    from app.db.models import Job
    d = retiring
    row = _complete_import(d)
    cid = _due(d, row["import_id"], "raw")
    _, payload = _cleanup_lease(d)
    d["s3"].lost_delete = True
    with pytest.raises(RetryableError, match="delete_unconfirmed"):
        d["cleanup"].run_cleanup(payload)
    with d["factory"]() as session:
        item = session.get(ImportObjectCleanup, cid)
        plan = item.plan_json
        assert item.state == "needs_attention" and item.released_bytes == 0
        assert session.get(ImportRun, row["import_id"]).retained_bytes == row["bytes"]["retained"]
        job = session.get(Job, item.job_id)
        job.status = "failed_dead"
        session.commit()
        version = item.state_version
    d["cleanup"].recover(d["wid"], d["vid"], d["app_id"], row["import_id"], cid, actor=d["actor"], state_version=version)
    _, payload = _cleanup_lease(d)
    d["cleanup"].run_cleanup(payload)
    with d["factory"]() as session:
        item = session.get(ImportObjectCleanup, cid)
        assert item.state == "complete" and item.plan_json == plan and item.recovery_count == 1
    assert d["phases"] == ["fence", "plan", "erase", "erase"]


def test_retirement_before_raw_write_has_no_s3_call_and_prevents_stale_upload(retiring):
    from app.imports.contracts import ImportFailure
    from app.db.import_models import ImportRun
    d = retiring
    row = _start(d)
    claim = d["service"].prepare_upload(d["wid"], d["vid"], d["app_id"], row["import_id"], actor=d["actor"])
    _remove(d, _read(d, row["import_id"]))
    with pytest.raises(ImportFailure, match="source_retired"):
        d["service"].finish_upload(d["wid"], d["vid"], d["app_id"], row["import_id"], d["blob"], actor=d["actor"], claim_id=claim["claim_id"])
    for kind in ("raw", "source"):
        _due(d, row["import_id"], kind)
        _, payload = _cleanup_lease(d)
        d["cleanup"].run_cleanup(payload)
    assert d["phases"] == [] and not d["s3"].calls
    with d["factory"]() as session:
        saved = session.get(ImportRun, row["import_id"])
        assert saved.retained_bytes == saved.reserved_bytes == 0


def test_expired_incomplete_import_can_never_promote_source_and_scope_is_immutable(retiring):
    from app.db.import_cleanup_models import ImportObjectCleanup
    from app.db.import_models import ImportRun
    from app.core.jobs import FatalError
    d = retiring
    row = _upload(d, _start(d))
    _, analyze = _lease(d)
    cid = _due(d, row["import_id"], "raw")
    _, payload = _cleanup_lease(d)
    with pytest.raises(FatalError, match="scope_mismatch"):
        d["cleanup"].run_cleanup({**payload, "app_id": "foreign"})
    d["cleanup"].run_cleanup(payload)
    d["runner"].run_analyze(analyze)
    with d["factory"]() as session:
        assert not session.get(ImportRun, row["import_id"]).source_snapshot_id
        assert json.loads(session.get(ImportRun, row["import_id"]).acquisition_json)["retired"]["source"]
        for column in ("workspace_id", "target_json", "plan_json", "receipt_json"):
            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(text(f"UPDATE import_object_cleanups SET {column} = :v WHERE id=:id"), {"id": cid, "v": "changed"})
                session.commit()
            session.rollback()
        assert session.get(ImportObjectCleanup, cid).state == "complete"
    assert not d["calls"]


def test_parent_deletion_reconciles_exact_objects_without_actor_or_model(retiring):
    from app.db.supymem_models import FounderApp
    from app.credits._txn import naive_utc
    from app.db.import_models import ImportRun
    d = retiring
    row = _complete_import(d)
    with d["factory"]() as session:
        session.get(FounderApp, d["app_id"]).deleted_at = naive_utc()
        session.commit()
    result = d["cleanup"].sweep()
    assert result["parents_retired"] >= 1
    # The intake fixture also creates an untouched import in this same app.
    # Parent deletion must clean both imports (four object jobs), not assume
    # that the first two globally leased jobs belong to the uploaded source.
    identifiers = {d["import_id"], row["import_id"]}
    for identifier in identifiers:
        for kind in ("raw", "source"):
            _due(d, identifier, kind)
    observed = set()
    for _ in range(4):
        _, payload = _cleanup_lease(d)
        assert payload["import_id"] in identifiers and payload["app_id"] == d["app_id"]
        observed.add(payload["cleanup_id"])
        d["cleanup"].run_cleanup(payload)
    assert len(observed) == 4
    with d["factory"]() as session:
        for identifier in identifiers:
            saved = session.get(ImportRun, identifier)
            assert json.loads(saved.acquisition_json)["deleted"]["reason"] == "parent_retired"
            assert saved.retained_bytes == saved.reserved_bytes == 0
