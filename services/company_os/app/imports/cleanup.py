"""Durable import retirement. Only verified erasure releases byte reservations.

The worker receives an exact persisted object, never a caller-supplied S3 key.
Lock order is cleanup Job -> ImportRun -> Cleanup; scheduling under ImportRun
creates a new job only and never takes an existing job lock.
"""
from __future__ import annotations

from datetime import timedelta
import json
import logging

from sqlalchemy import select, or_

from app.core.jobs import enqueue, FatalError, RetryableError
from app.credits._txn import naive_utc
from app.db.import_cleanup_models import ImportObjectCleanup
from app.db.import_models import ImportRun
from app.db.models import Job, Venture
from app.db.supymem_models import ExistingAsset, FounderApp
from app.state import db
from app.tenancy import bind
from core.ids import new_id
from . import cleanup_process, storage
from .contracts import ImportFailure, canonical
from .intake import _row, event, store_scope, view


def schedule(session, row, *, kind, reason, actor, due=None):
    """Caller holds ImportRun. The unique ledger survives retries and revocation."""
    item = session.scalar(select(ImportObjectCleanup).where(ImportObjectCleanup.import_run_id == row.id,
        ImportObjectCleanup.object_kind == kind))
    if item:
        return item
    now = naive_utc()
    item = ImportObjectCleanup(id=new_id("icl"), workspace_id=row.workspace_id, venture_id=row.venture_id,
        app_id=row.app_id, import_run_id=row.id, object_kind=kind, reason=reason, initiated_by=actor,
        state="scheduled", state_version=0, recovery_count=0, due_at=due or now,
        target_json="{}", fence_json="{}", plan_json="{}", receipt_json="{}", released_bytes=0,
        created_at=now, updated_at=now)
    session.add(item)
    session.flush()
    with bind(workspace_id=row.workspace_id, venture_id=row.venture_id, actor=actor):
        job_id = enqueue(session, "import.cleanup", {"schema_version": 1, "cleanup_id": item.id,
            "import_id": row.id, "app_id": row.app_id}, workspace_id=row.workspace_id, venture_id=row.venture_id,
            dedupe_key="import-cleanup:" + item.id, run_after=item.due_at, max_attempts=3)
        session.flush()  # Insert the Job before its compound FK is assigned.
        item.job_id = job_id
    return item


def schedule_raw(session, row):
    return schedule(session, row, kind="raw", reason="raw_retention", actor=row.actor_subject,
                    due=naive_utc() + timedelta(hours=6))


def _retire(session, row, doc, kind, *, actor):
    retired = doc.setdefault("retired", {})
    if kind not in retired:
        retired[kind] = {"at": naive_utc().isoformat(), "actor": actor}
        row.state_version += 1
        row.updated_at = naive_utc()
        event(session, row, "source_retired", data={"object_kind": kind})
    if kind == "source":
        assets = session.scalars(select(ExistingAsset).where(ExistingAsset.workspace_id == row.workspace_id,
            ExistingAsset.venture_id == row.venture_id, ExistingAsset.kind == "imported_source")).all()
        for asset in assets:
            detail = json.loads(asset.detail or "{}")
            if detail.get("import_id") == row.id and detail.get("app_id") == row.app_id:
                asset.exists = asset.accessible = asset.usable = False
    row.acquisition_json = canonical(doc).decode()


def remove(wid, vid, aid, identifier, *, actor, state_version):
    with db()() as session:
        row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
        doc = json.loads(row.acquisition_json)
        if not doc.get("deleted"):
            if row.state_version != state_version:
                raise ImportFailure("import_state_changed", status=409)
            doc["deleted"] = {"at": naive_utc().isoformat(), "actor": actor}
            for kind in ("raw", "source"):
                _retire(session, row, doc, kind, actor=actor)
                schedule(session, row, kind=kind, reason="founder_delete", actor=actor)
            event(session, row, "deletion_requested", actor=actor)
        session.commit()
        return view(row)


def summary(session, row):
    items = session.scalars(select(ImportObjectCleanup).where(ImportObjectCleanup.import_run_id == row.id)).all()
    return [{"cleanup_id": x.id, "object_kind": x.object_kind, "state": x.state, "state_version": x.state_version,
        "due_at": x.due_at.isoformat(), "completed_at": x.completed_at.isoformat() if x.completed_at else None,
        "released_bytes": x.released_bytes, "error_code": x.error_code,
        "content_free_fence_retained": bool(json.loads(x.receipt_json).get("content_free_fence_retained"))} for x in items]


def cleanup_scope(job):
    with db()() as session:
        item = session.get(ImportObjectCleanup, job.payload.get("cleanup_id"))
        return bool(item and job.kind == "import.cleanup" and job.payload.get("schema_version") == 1
            and (item.job_id, item.workspace_id, item.venture_id, item.app_id, item.import_run_id, item.initiated_by) ==
            (job.id, job.workspace_id, job.venture_id, job.payload.get("app_id"), job.payload.get("import_id"), job.payload.get("_actor_subject")))


def _locked(session, payload, *, owner=None):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if (not job or job.kind != "import.cleanup" or job.status != "leased"
            or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt")):
        raise FatalError("import_cleanup_lease_lost")
    saved = json.loads(job.payload_json)
    row = session.get(ImportRun, saved.get("import_id"), with_for_update=True)
    item = session.get(ImportObjectCleanup, saved.get("cleanup_id"), with_for_update=True)
    if (not row or not item or saved.get("schema_version") != 1
            or (item.job_id, item.workspace_id, item.venture_id, item.app_id, item.import_run_id, item.initiated_by) !=
            (job.id, job.workspace_id, job.venture_id, saved.get("app_id"), row.id, saved.get("_actor_subject"))
            or (row.workspace_id, row.venture_id, row.app_id) != (item.workspace_id, item.venture_id, item.app_id)
            or any(payload.get(k) != saved.get(k) for k in ("cleanup_id", "import_id", "app_id", "workspace_id", "venture_id", "_actor_subject"))):
        raise FatalError("import_cleanup_scope_mismatch")
    if owner and item.operation_owner != owner:
        raise FatalError("import_cleanup_operation_changed")
    return row, item, job


def _mutate(payload, owner, fn):
    with db()() as session:
        row, item, job = _locked(session, payload, owner=owner)
        result = fn(session, row, item, job)
        item.updated_at = naive_utc()
        session.commit()
        return result


def run_cleanup(payload):
    owner = f'{payload.get("_job_id")}:{payload.get("_worker_id")}:{payload.get("_job_attempt")}'
    with db()() as session:
        row, item, job = _locked(session, payload)
        if item.state == "complete":
            return
        if item.operation_owner == owner:
            raise FatalError("import_cleanup_operation_in_progress")
        if item.due_at > naive_utc():
            raise RetryableError("import_cleanup_not_due")
        item.operation_owner = owner
        doc = json.loads(row.acquisition_json)
        _retire(session, row, doc, item.object_kind, actor=item.initiated_by)
        # Expired raw bytes may never be promoted later by a sleeping analyzer.
        if item.object_kind == "raw" and not row.source_snapshot_id:
            _retire(session, row, doc, "source", actor=item.initiated_by)
            schedule(session, row, kind="source", reason="incomplete_retention", actor=item.initiated_by)
        item.retired_at = item.retired_at or naive_utc()
        target = {"scope": store_scope(row), "kind": item.object_kind, "intent": doc.get(item.object_kind + "_intent"),
                  "storage_config_digest": doc.get("storage_config_digest")}
        if item.target_json != "{}" and json.loads(item.target_json) != target:
            raise FatalError("import_cleanup_target_changed")
        item.target_json = canonical(target).decode()
        item.state, item.error_code = "fencing", None
        item.state_version += 1
        session.commit()
    try:
        if target["intent"]:
            storage.cleanup_ready()  # Before starting a process or touching AWS.
            fence = _mutate(payload, owner, lambda s, r, i, j: json.loads(i.fence_json))
            if not fence:
                fence = cleanup_process.run("fence", target)
                def save_fence(s, r, i, j):
                    i.fence_json, i.state = canonical(fence).decode(), "deleting_versions"
                    i.state_version += 1
                _mutate(payload, owner, save_fence)
            plan = _mutate(payload, owner, lambda s, r, i, j: json.loads(i.plan_json))
            if not plan:
                plan = cleanup_process.run("plan", target, fence=fence)
                def save_plan(s, r, i, j):
                    i.plan_json, i.state = canonical(plan).decode(), "verifying"
                    i.state_version += 1
                _mutate(payload, owner, save_plan)
            _mutate(payload, owner, lambda *args: None)
            receipt = cleanup_process.run("erase", target, fence=fence, plan=plan)
            if (receipt.get("source_data_versions_absent") is not True
                    or receipt.get("content_free_fence_retained") is not True
                    or receipt.get("release_bound_bytes") != target["intent"]["size_bytes"]
                    or receipt.get("fence") != fence or receipt.get("plan_digest") != plan["plan_digest"]):
                raise ImportFailure("import_cleanup_receipt_invalid", status=503)
        else:
            # Every write requires an immutable intent committed under ImportRun.
            # Retirement now prohibits such an intent, so there was no possible key.
            receipt = {"no_write_intent": True, "release_bound_bytes": 0, "source_data_versions_absent": True,
                       "content_free_fence_retained": False}
        def complete(session, row, item, job):
            from .runner import _account
            doc = json.loads(row.acquisition_json)
            before = row.retained_bytes + row.reserved_bytes
            doc.setdefault("purged", {})[item.object_kind] = {"cleanup_id": item.id, "verified": True}
            _account(row, doc)
            item.released_bytes = max(0, before - row.retained_bytes - row.reserved_bytes)
            row.acquisition_json = canonical(doc).decode()
            item.receipt_json, item.state, item.completed_at = canonical(receipt).decode(), "complete", naive_utc()
            item.state_version += 1
            item.operation_owner, item.error_code = None, None
            row.state_version += 1
            event(session, row, "object_erased", data={"object_kind": item.object_kind, "cleanup_id": item.id,
                "released_bytes": item.released_bytes, "content_free_fence_retained": receipt["content_free_fence_retained"]})
        _mutate(payload, owner, complete)
    except Exception as exc:
        if isinstance(exc, FatalError):
            raise
        code = str(exc)
        if not code.startswith("import_cleanup_") or len(code) > 100:
            code = "import_cleanup_outcome_unconfirmed"
        def failed(s, r, i, j):
            i.error_code, i.state, i.operation_owner = code, "needs_attention", None
            i.state_version += 1
            event(s, r, "cleanup_unconfirmed", data={"cleanup_id": i.id, "error_code": code})
        _mutate(payload, owner, failed)
        raise RetryableError(code) from None


def recover(wid, vid, aid, identifier, cleanup_id, *, actor, state_version):
    with db()() as session:
        # Read scope without locks, then preserve worker lock order.
        _row(session, wid, vid, aid, identifier, actor, write=True)
        item = session.get(ImportObjectCleanup, cleanup_id)
        if not item or item.import_run_id != identifier:
            raise ImportFailure("import_not_found", status=404)
        job = session.get(Job, item.job_id, with_for_update=True)
        row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
        item = session.get(ImportObjectCleanup, cleanup_id, with_for_update=True, populate_existing=True)
        if item.state == "complete":
            return summary(session, row)
        if item.state_version != state_version:
            raise ImportFailure("import_state_changed", status=409)
        if not job or job.status not in {"failed_dead", "cancelled"} or item.recovery_count:
            raise ImportFailure("import_cleanup_recovery_unavailable", status=409)
        item.recovery_count, item.state, item.error_code = 1, "scheduled", None
        item.state_version += 1
        item.operation_owner = None
        job.max_attempts += 3
        job.status, job.finished_at = "retry_wait", None
        job.run_after = job.scheduled_for = naive_utc()
        job.locked_at = job.locked_by = None
        event(session, row, "cleanup_recovery_requested", actor=actor, data={"cleanup_id": item.id})
        session.commit()
        return summary(session, row)


def sweep(*, limit=100):
    """Bounded DB maintenance; no cloud calls or broad bucket scans."""
    now = naive_utc()
    with db()() as session:
        rows = session.scalars(select(ImportRun).join(FounderApp, FounderApp.id == ImportRun.app_id)
            .join(Venture, Venture.id == ImportRun.venture_id).where(or_(FounderApp.deleted_at.is_not(None), Venture.archived_at.is_not(None)))
            .where(~ImportRun.id.in_(select(ImportObjectCleanup.import_run_id).where(ImportObjectCleanup.object_kind == "source")))
            .order_by(ImportRun.created_at, ImportRun.id).limit(limit).with_for_update(of=ImportRun, skip_locked=True)).all()
        for row in rows:
            doc = json.loads(row.acquisition_json)
            doc.setdefault("deleted", {"at": now.isoformat(), "actor": "system", "reason": "parent_retired"})
            for kind in ("raw", "source"):
                _retire(session, row, doc, kind, actor="system")
                schedule(session, row, kind=kind, reason="parent_retired", actor=row.actor_subject)
        overdue = session.scalars(select(ImportObjectCleanup).where(ImportObjectCleanup.state != "complete",
            ImportObjectCleanup.due_at < now - timedelta(hours=18), ImportObjectCleanup.error_code.is_(None))
            .order_by(ImportObjectCleanup.due_at).limit(limit).with_for_update(skip_locked=True)).all()
        # No ImportRun locks here (avoids reversing the worker's lock order).
        for item in overdue:
            logging.getLogger(__name__).error("import_retention_slo_exceeded", extra={"cleanup_id": item.id,
                "workspace_id": item.workspace_id, "import_id": item.import_run_id, "object_kind": item.object_kind})
            item.error_code = "import_cleanup_retention_slo_exceeded"
            item.updated_at = now
        session.commit()
        return {"parents_retired": len(rows), "overdue": len(overdue)}


def read_summary(workspace_id, venture_id, app_id, import_id, *, actor):
    with db()() as session:
        row = _row(session, workspace_id, venture_id, app_id, import_id, actor)
        return summary(session, row)
