"""Leased, bounded import acquisition and static reporting. Never executes source."""
from __future__ import annotations

import hashlib
import json

from app.core.jobs import FatalError, RetryableError
from app.db.import_models import ImportRun, SourceSnapshot
from app.db.models import Job
from app.db.supymem_models import ExistingAsset
from app.state import db
from app.credits._txn import naive_utc
from app.builder.model_proxy import BuildModelError
from core.ids import new_id
from core.artifact_snapshot import manifest_digest, validate_manifest
from . import process
from .contracts import ImportFailure, MAX_ARCHIVE_BYTES, MAX_REPORT_BYTES, canonical, digest
from .intake import (_app, configured_store, event, require_ready, store_scope, transition, TERMINAL, require_not_retired)


class ImportRetry(RetryableError):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.retry_after = min(900, retry_after) if type(retry_after) is int and retry_after > 0 else None


def cleanup_scope(job):
    """Only a persisted import job may reconcile its exact objects after revocation."""
    with db()() as session:
        row = session.get(ImportRun, job.payload.get("import_id"))
        return bool(row and job.payload.get("schema_version") == 1 and
            (row.job_id, row.workspace_id, row.venture_id, row.app_id, row.actor_subject) ==
            (job.id, job.workspace_id, job.venture_id, job.payload.get("app_id"), job.payload.get("_actor_subject")))


def locked(session, payload, *, owner=None):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if (not job or job.kind != "import.analyze" or job.status != "leased"
            or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt")):
        raise FatalError("import_lease_lost")
    saved = json.loads(job.payload_json)
    row = session.get(ImportRun, saved.get("import_id"), with_for_update=True)
    if (not row or saved.get("schema_version") != 1 or
            (row.job_id, row.workspace_id, row.venture_id, row.app_id, row.actor_subject) !=
            (job.id, job.workspace_id, job.venture_id, saved.get("app_id"), saved.get("_actor_subject")) or
            any(payload.get(k) != saved.get(k) for k in ("import_id", "app_id", "workspace_id", "venture_id", "_actor_subject"))):
        raise FatalError("import_scope_mismatch")
    doc = json.loads(row.acquisition_json)
    if owner and doc.get("operation_owner") != owner:
        raise FatalError("import_operation_changed")
    return row, doc, job


def authority(session, row):
    _app(session, row.workspace_id, row.venture_id, row.app_id, row.actor_subject, write=True)
    require_ready()


def mutate(payload, owner, fn, *, authorize=True):
    with db()() as session:
        row, doc, job = locked(session, payload, owner=owner)
        if authorize:
            require_not_retired(doc)
            authority(session, row)
            if row.state == "cancel_requested" or row.state in TERMINAL:
                raise ImportFailure("import_cancelled", status=409)
        result = fn(session, row, doc, job)
        row.acquisition_json = canonical(doc).decode()
        session.commit()
        return result


def run_analyze(payload):
    owner = f'{payload.get("_job_id")}:{payload.get("_worker_id")}:{payload.get("_job_attempt")}'
    with db()() as session:
        row, doc, job = locked(session, payload)
        if row.state in TERMINAL or doc.get("retired"):
            return
        if doc.get("operation_owner") == owner:
            raise FatalError("import_operation_in_progress")
        doc["operation_owner"] = owner
        row.acquisition_json = canonical(doc).decode()
        session.commit()
    try:
        _execute(payload, owner)
    except ImportFailure as exc:
        def failed(session, row, doc, job, exc=exc):
            doc.pop("operation_owner", None)
            uncertain = any(doc.get(k + "_intent") and not doc.get(k + "_receipt") for k in ("raw", "source"))
            if uncertain:
                transition(session, row, "needs_reconciliation", error=exc.code)
            elif exc.status == 503:
                transition(session, row, "queued", error=exc.code)
            else:
                transition(session, row, "cancelled" if exc.code in {"import_cancelled", "import_authority_revoked"} else "blocked", error=exc.code)
        mutate(payload, owner, failed, authorize=False)
        if exc.status == 503:
            raise ImportRetry(exc.code, exc.retry_after) from None
        raise FatalError(exc.code) from None
    except BuildModelError:
        def uncertain(session, row, doc, job):
            doc.pop("operation_owner", None)
            transition(session, row, "needs_reconciliation", error="import_storage_outcome_unconfirmed")
        mutate(payload, owner, uncertain, authorize=False)
        raise RetryableError("import_storage_outcome_unconfirmed") from None


def _execute(payload, owner):
    def begin(session, row, doc, job):
        return store_scope(row), json.loads(row.requested_source_json), dict(doc), row.state
    scope, source, doc, state = mutate(payload, owner, begin, authorize=False)
    store = configured_store(doc)

    # Reconcile a prior uncertain write before any new network acquisition. This
    # narrow path can run after founder revocation, but cannot publish or execute.
    for kind in ("raw", "source"):
        intent = doc.get(kind + "_intent")
        if intent and not doc.get(kind + "_receipt"):
            receipt = store.probe_import(scope, kind=kind, **intent)
            if receipt:
                def recovered(session, row, current, job, receipt=receipt, kind=kind):
                    current[kind + "_receipt"] = receipt
                    _account(row, current)
                    event(session, row, "object_reconciled", data={"kind": kind})
                mutate(payload, owner, recovered, authorize=False)
                doc[kind + "_receipt"] = receipt
    try:
        mutate(payload, owner, lambda *args: None)
    except ImportFailure as exc:
        if exc.status in {403, 404}:
            raise ImportFailure("import_authority_revoked") from None
        raise

    if source["source"] == "github_public" and not doc.get("resolved"):
        mutate(payload, owner, lambda session, row, *_: transition(session, row, "acquiring"))
        result = process.run("resolve", source)
        def resolved(session, row, current, job):
            current["resolved"] = result.data["resolved"]
            current["resolution_requests"] = result.data["requests"]
        mutate(payload, owner, resolved)
        doc["resolved"] = result.data["resolved"]

    raw = None
    if doc.get("raw_receipt"):
        raw = store.read_import(doc["raw_receipt"], scope, kind="raw", object_digest=doc["raw_intent"]["object_digest"])
    elif source["source"] == "upload_zip":
        # A known absent object is safe to retry with exactly the approved ZIP.
        raise ImportFailure("import_upload_required", status=409)
    else:
        mutate(payload, owner, lambda session, row, *_: transition(session, row, "acquiring"))
        result = process.run("download", doc["resolved"])
        raw = result.archive
        intent = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "object_digest": hashlib.sha256(raw).hexdigest()}
        if doc.get("raw_intent", intent) != intent:
            raise ImportFailure("import_pinned_archive_changed", status=409)
        def raw_intent(session, row, current, job):
            current["raw_intent"] = intent
            from .cleanup import schedule_raw
            schedule_raw(session, row)
            event(session, row, "raw_write_intent")
        mutate(payload, owner, raw_intent)
        receipt = store.persist_import(raw, scope, kind="raw", object_digest=intent["object_digest"])
        store.read_import(receipt, scope, kind="raw", object_digest=intent["object_digest"])
        def raw_saved(session, row, current, job):
            current["raw_receipt"] = receipt
            _account(row, current)
            event(session, row, "raw_stored")
        mutate(payload, owner, raw_saved, authorize=False)
        doc.update(raw_intent=intent, raw_receipt=receipt)

    mutate(payload, owner, lambda session, row, *_: transition(session, row, "analyzing"))
    result = process.run("analyze", {"github_wrapper": source["source"] == "github_public",
        "selected_root": source.get("subdirectory") if source["source"] == "github_public" else source.get("root")}, archive=raw)
    manifest, report, provenance = (result.data[k] for k in ("manifest", "report", "provenance"))
    validate_manifest(manifest)
    if len(canonical(report)) > MAX_REPORT_BYTES:
        raise ImportFailure("import_report_limit")
    sha = hashlib.sha256(result.archive).hexdigest()
    intent = {"sha256": sha, "size_bytes": len(result.archive), "object_digest": manifest_digest(manifest)}
    if doc.get("source_intent", intent) != intent:
        raise ImportFailure("import_analysis_changed", status=409)
    def source_intent(session, row, current, job):
        current["source_intent"] = intent
        event(session, row, "source_write_intent")
    mutate(payload, owner, source_intent)
    receipt = store.persist_import(result.archive, scope, kind="source", object_digest=intent["object_digest"])
    store.read_import(receipt, scope, kind="source", object_digest=intent["object_digest"])
    def source_saved(session, row, current, job):
        current["source_receipt"] = receipt
        _account(row, current)
        event(session, row, "source_stored")
    mutate(payload, owner, source_saved, authorize=False)
    def complete(session, row, current, job):
        sid = new_id("src")
        provenance.update(source_kind=row.source_kind, resolved=current.get("resolved"), storage_verified=True)
        session.add(SourceSnapshot(id=sid, workspace_id=row.workspace_id, venture_id=row.venture_id,
            app_id=row.app_id, import_run_id=row.id, normalized_archive_sha256=sha, size_bytes=len(result.archive),
            manifest_digest=intent["object_digest"], manifest_json=canonical(manifest).decode(), storage_receipt_json=canonical(receipt).decode(),
            source_provenance_json=canonical(provenance).decode(), analysis_policy_version=row.policy_version, created_at=naive_utc()))
        session.flush()
        row.source_snapshot_id, row.report_json, row.report_digest = sid, canonical(report).decode(), digest(report)
        session.add(ExistingAsset(id=new_id("asset"), workspace_id=row.workspace_id, venture_id=row.venture_id,
            kind="imported_source", name=source.get("repo_url", source.get("filename")),
            detail=canonical({"app_id": row.app_id, "import_id": row.id, "source_snapshot_id": sid, "manifest_digest": intent["object_digest"]}).decode(),
            exists=True, accessible=True, compatible=None, usable=None, usable_tasks='["static_analysis"]',
            source="verified_import", created_at=naive_utc(), verified_at=naive_utc()))
        current.pop("operation_owner", None)
        transition(session, row, "partial_report" if report.get("coverage_gaps") else "report_complete")
    mutate(payload, owner, complete)


def _account(row, doc):
    raw, source = doc.get("raw_receipt", {}), doc.get("source_receipt", {})
    # Erasure receipts are durable. A late storage ACK may add history, never
    # restore source availability or quota after verified retirement.
    row.acquired_bytes = max(row.acquired_bytes, raw.get("size_bytes", 0))
    purged = doc.get("purged", {})
    row.retained_bytes = sum(receipt.get("size_bytes", 0) for kind, receipt in (("raw", raw), ("source", source)) if kind not in purged)
    row.reserved_bytes = 0
    for kind, receipt, bound in (("raw", raw, doc.get("raw_limit", 0)), ("source", source, MAX_ARCHIVE_BYTES)):
        if receipt or kind in purged:
            continue
        if doc.get(kind + "_intent"):
            row.reserved_bytes += doc[kind + "_intent"]["size_bytes"]
        elif kind not in doc.get("retired", {}) and row.state not in TERMINAL:
            row.reserved_bytes += bound
