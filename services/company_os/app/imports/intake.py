"""Signed company/app intake, durable quota and immutable upload receipt."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
import re
import sys

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, func

from app.builder.access import authorize
from app.builder.model_proxy import BuildModelError
from app.core.jobs import enqueue
from app.credits._txn import naive_utc
from app.db.import_models import ImportRun
from app.db.models import EventLog, Job
from app.db.session import serialize_writes
from app.db.supymem_models import FounderApp, FounderRequest
from app.state import db
from app.tenancy import bind, current_or_none
from core.ids import new_id
from .contracts import ImportFailure, MAX_ARCHIVE_BYTES, POLICY_VERSION, Source, canonical, digest
from . import storage

TERMINAL = {"report_complete", "partial_report", "blocked", "failed_definitive", "cancelled", "expired"}
ACTIVE = {"awaiting_upload", "uploading", "queued", "acquiring", "validating", "analyzing", "needs_reconciliation", "cancel_requested"}
LIMIT_BYTES = 200 * 1024 * 1024


def settings():
    reasons = []
    config = None
    try:
        config = storage.configuration()
    except BuildModelError as exc:
        reasons.append(str(exc))
    if sys.platform != "linux":
        reasons.append("import_parser_platform_unsupported")
    # Resource-limited subprocesses do not isolate a hosted parser. No configured
    # executor has passed that acceptance yet; a flag cannot bypass this fact.
    reasons.append("import_parser_isolation_unavailable")
    try:
        storage.cleanup_ready()
    except BuildModelError as exc:
        reasons.append(str(exc))
    if os.getenv("IMPORT_INTAKE_ENABLED") != "1":
        reasons.append("import_intake_not_accepted")
    if config and os.getenv("IMPORT_INTAKE_STORAGE_ACCEPTANCE") != storage.fingerprint(config):
        reasons.append("import_storage_not_accepted")
    return {"ready": not reasons, "reasons": reasons, "storage_config_digest": config.fingerprint if config else None,
            "policy_version": POLICY_VERSION, "maximum_archive_bytes": MAX_ARCHIVE_BYTES,
            "maximum_files": 2000, "maximum_active": 2, "daily_starts": 20,
            "daily_acquired_bytes": LIMIT_BYTES, "retained_bytes": LIMIT_BYTES,
            "credit_policy": "platform_absorbed_bounded_intake_v1", "ceiling_micro": 0,
            "source_execution_available": False}


def require_ready():
    value = settings()
    if not value["ready"]:
        raise ImportFailure("import_intake_unavailable", status=503)
    return value


def _app(session, wid, vid, aid, actor, *, write=False):
    scope = current_or_none()
    if (not actor or actor in {"system", "anonymous", "local", "founder"}
            or scope and (scope.actor != actor or scope.workspace_id != wid or scope.venture_id not in {"", vid})):
        raise ImportFailure("import_actor_invalid", status=403)
    try:
        venture = authorize(session, workspace_id=wid, venture_id=vid, actor=actor, mutate=write)
    except KeyError:
        raise ImportFailure("import_not_found", status=404) from None
    except PermissionError:
        raise ImportFailure("import_write_forbidden", status=403) from None
    app = session.get(FounderApp, aid)
    if (not app or app.deleted_at or venture.archived_at
            or (app.workspace_id, app.venture_id) != (wid, vid)):
        raise ImportFailure("import_not_found", status=404)
    return app


def _row(session, wid, vid, aid, identifier, actor, *, write=False, lock=False):
    _app(session, wid, vid, aid, actor, write=write)
    row = session.scalar(select(ImportRun).where(ImportRun.id == identifier, ImportRun.workspace_id == wid,
        ImportRun.venture_id == vid, ImportRun.app_id == aid).with_for_update().execution_options(populate_existing=True) if lock else
        select(ImportRun).where(ImportRun.id == identifier, ImportRun.workspace_id == wid,
            ImportRun.venture_id == vid, ImportRun.app_id == aid))
    if row is None:
        raise ImportFailure("import_not_found", status=404)
    return row


def store_scope(row):
    return dict(workspace_id=row.workspace_id, venture_id=row.venture_id, app_id=row.app_id, import_id=row.id)


def event(session, row, topic, *, actor=None, data=None):
    subject = actor or "agt_import"
    session.add(EventLog(id=new_id("evt"), topic="import." + topic, workspace_id=row.workspace_id,
        venture_id=row.venture_id, actor=subject, actor_id=subject, actor_kind="person" if actor else "agent",
        on_behalf_of=row.actor_subject, surface="api" if actor else "internal", target_ref="import:" + row.id,
        outcome="error" if row.error_code else "ok", ts=naive_utc(),
        payload_json=canonical({"import_id": row.id, "app_id": row.app_id, "state": row.state,
            "state_version": row.state_version, "error_code": row.error_code, **(data or {})}).decode()))


def transition(session, row, state, *, error=None, actor=None, data=None):
    row.state, row.error_code, row.updated_at = state, error, naive_utc()
    row.state_version += 1
    if state in TERMINAL:
        row.finished_at = row.updated_at
        row.reserved_bytes = 0
    event(session, row, state, actor=actor, data=data)
    request = session.get(FounderRequest, row.request_id)
    if request:
        request.outcome = "done" if state in {"report_complete", "partial_report"} else "failed" if state in TERMINAL else "pending"
        request.failure_reason = error
        request.resolved_at = row.finished_at


def view(row, job=None):
    doc = json.loads(row.acquisition_json)
    state, error = row.state, row.error_code
    deleted = bool(doc.get("deleted"))
    if state == "awaiting_upload" and row.expires_at <= naive_utc():
        state, error = "expired", "import_upload_expired"
    if state in ACTIVE and job and job.status in {"failed_dead", "cancelled"}:
        uncertain = any(doc.get(key) for key in ("raw_intent", "source_intent"))
        state, error = ("needs_reconciliation", "import_storage_outcome_unconfirmed") if uncertain else ("failed_definitive", "import_job_failed")
    if deleted:
        state, error = "deleted", None
    return {"deleted": deleted, "source_available": bool(row.source_snapshot_id and not doc.get("retired", {}).get("source")),
        "retirement": {"retired_kinds": sorted(doc.get("retired", {})), "verified_erased_kinds": sorted(doc.get("purged", {})),
            "content_free_fences_may_remain": bool(doc.get("purged"))}, "import_id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id, "app_id": row.app_id,
        "actor_subject": row.actor_subject, "state": state, "state_version": row.state_version, "source": json.loads(row.requested_source_json),
        "job_id": row.job_id, "source_snapshot_id": None if deleted else row.source_snapshot_id, "report": None if deleted else json.loads(row.report_json) or None,
        "report_digest": None if deleted else row.report_digest, "error_code": error, "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "upload_path": f"/ventures/{row.venture_id}/apps/{row.app_id}/imports/{row.id}/archive"
            if row.source_kind == "upload_zip" and state in {"awaiting_upload", "uploading", "needs_reconciliation"} else None,
        "bytes": {"acquired": row.acquired_bytes, "retained": row.retained_bytes},
        "credit_usage": {"consumed_micro": 0, "policy": "platform_absorbed_bounded_intake_v1"},
        "execution_available": False, "execution_blocker": "import_managed_source_ingress_unavailable"}


def capabilities(wid, vid, aid, *, actor):
    with db()() as session:
        _app(session, wid, vid, aid, actor)
    return settings()


def _admit(session, wid, expected):
    now = naive_utc()
    rows = session.execute(select(ImportRun, Job).outerjoin(Job, Job.id == ImportRun.job_id).where(
        ImportRun.workspace_id == wid, ImportRun.state.in_(ACTIVE))).all()
    # Derived terminal state only: never overwrite a concurrently completing
    # worker from an admission/status snapshot. No new side effect can resume a
    # definitely untouched dead job or expired upload without fresh admission.
    active = [(row, job) for row, job in rows if not json.loads(row.acquisition_json).get("retired") and view(row, job)["state"] not in TERMINAL]
    recent = session.scalars(select(ImportRun).where(ImportRun.workspace_id == wid,
        ImportRun.created_at > now - timedelta(days=1))).all()
    if len(active) >= 2 or len(recent) >= 20:
        raise ImportFailure("import_quota_exceeded", status=429)
    effective_ids = {row.id for row, _ in active}
    raw_daily = sum(row.acquired_bytes or (json.loads(row.acquisition_json).get("raw_limit", 0)
        if row.id in effective_ids else 0) for row in recent)
    held = session.scalar(select(func.coalesce(func.sum(ImportRun.retained_bytes + ImportRun.reserved_bytes), 0)).where(ImportRun.workspace_id == wid))
    held -= sum(row.reserved_bytes for row, _ in rows if row.id not in effective_ids
        and not any(json.loads(row.acquisition_json).get(kind + "_intent") for kind in ("raw", "source")))
    if raw_daily + expected > LIMIT_BYTES or held + expected + MAX_ARCHIVE_BYTES > LIMIT_BYTES:
        raise ImportFailure("import_byte_quota_exceeded", status=429)


def _queue(session, row, *, delayed=False):
    if row.job_id:
        job = session.get(Job, row.job_id)
        if job and job.status in {"scheduled", "retry_wait", "pending"} and not delayed:
            job.run_after = job.scheduled_for = naive_utc()
        return row.job_id
    with bind(workspace_id=row.workspace_id, venture_id=row.venture_id, actor=row.actor_subject):
        jid = enqueue(session, "import.analyze", {"import_id": row.id, "app_id": row.app_id, "schema_version": 1},
            workspace_id=row.workspace_id, venture_id=row.venture_id, dedupe_key="import:" + row.id,
            run_after=naive_utc() + timedelta(seconds=300) if delayed else None, max_attempts=3)
        session.flush()  # Insert the scoped job before assigning the FK.
        row.job_id = jid
    return row.job_id


def start(wid, vid, aid, requested, *, actor, idempotency_key):
    try:
        source = TypeAdapter(Source).validate_python(requested).model_dump(mode="json")
    except ValidationError:
        raise ImportFailure("import_input_invalid") from None
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[!-~]{8,160}", idempotency_key):
        raise ImportFailure("import_idempotency_key_invalid")
    intent, fingerprint = hashlib.sha256(idempotency_key.encode()).hexdigest(), digest(source)
    with db()() as session:
        serialize_writes(session, key="import-admission:" + wid)
        _app(session, wid, vid, aid, actor, write=True)
        prior = session.scalar(select(ImportRun).where(ImportRun.workspace_id == wid, ImportRun.venture_id == vid,
            ImportRun.app_id == aid, ImportRun.actor_subject == actor, ImportRun.idempotency_hash == intent))
        if prior:
            if prior.input_digest != fingerprint:
                raise ImportFailure("import_idempotency_conflict", status=409)
            return view(prior, session.get(Job, prior.job_id) if prior.job_id else None)
        policy = require_ready()
        expected = source.get("size_bytes", MAX_ARCHIVE_BYTES)
        _admit(session, wid, expected)
        now, identifier, request_id = naive_utc(), new_id("imp"), new_id("frq")
        session.add(FounderRequest(id=request_id, workspace_id=wid, venture_id=vid, source="import",
            text=source.get("repo_url", source.get("filename", "Imported project")), intent="import", intent_category="import",
            classification="unknown", outcome="pending", related_id=identifier, created_at=now))
        session.flush()
        row = ImportRun(id=identifier, workspace_id=wid, venture_id=vid, app_id=aid, actor_subject=actor, request_id=request_id,
            source_kind=source["source"], idempotency_hash=intent, input_digest=fingerprint, policy_version=POLICY_VERSION,
            state="awaiting_upload" if source["source"] == "upload_zip" else "queued", state_version=0,
            requested_source_json=canonical(source).decode(), acquisition_json=canonical({"raw_limit": expected,
                "storage_config_digest": policy["storage_config_digest"]}).decode(),
            report_json="{}", reserved_bytes=expected + MAX_ARCHIVE_BYTES, acquired_bytes=0, retained_bytes=0,
            created_at=now, updated_at=now, expires_at=now + timedelta(minutes=15))
        session.add(row)
        session.flush()
        if source["source"] == "github_public":
            _queue(session, row)
        event(session, row, "requested", actor=actor)
        session.commit()
        return view(row)


def read(wid, vid, aid, identifier, *, actor):
    with db()() as session:
        row = _row(session, wid, vid, aid, identifier, actor)
        return view(row, session.get(Job, row.job_id) if row.job_id else None)


def recent(wid, vid, aid, *, actor, before=None):
    with db()() as session:
        _app(session, wid, vid, aid, actor)
        query = select(ImportRun).where(ImportRun.workspace_id == wid, ImportRun.venture_id == vid, ImportRun.app_id == aid)
        if before:
            previous = _row(session, wid, vid, aid, before, actor)
            query = query.where((ImportRun.created_at < previous.created_at) |
                                ((ImportRun.created_at == previous.created_at) & (ImportRun.id < previous.id)))
        rows = session.scalars(query.order_by(ImportRun.created_at.desc(), ImportRun.id.desc()).limit(21)).all()
        return {"items": [view(row, session.get(Job, row.job_id) if row.job_id else None) for row in rows[:20]],
                "next_cursor": rows[19].id if len(rows) > 20 else None}


def cancel(wid, vid, aid, identifier, *, actor, state_version):
    with db()() as session:
        initial = _row(session, wid, vid, aid, identifier, actor, write=True)
        if initial.job_id:
            session.get(Job, initial.job_id, with_for_update=True)
        row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
        if row.state in TERMINAL:
            return view(row)
        if row.state_version != state_version:
            raise ImportFailure("import_state_changed", status=409)
        doc = json.loads(row.acquisition_json)
        if not doc.get("raw_intent") and not doc.get("source_intent"):
            transition(session, row, "cancelled", actor=actor)
            if row.job_id:
                job = session.get(Job, row.job_id)
                if job.status in {"pending", "scheduled", "retry_wait"}:
                    job.status, job.finished_at = "cancelled", naive_utc()
        else:
            transition(session, row, "cancel_requested", actor=actor)
            _queue(session, row)
        session.commit()
        return view(row)


def prepare_upload(wid, vid, aid, identifier, *, actor):
    """Authenticate and claim exact expected bytes before consuming request body."""
    with db()() as session:
        initial = _row(session, wid, vid, aid, identifier, actor, write=True)
        if initial.job_id:
            session.get(Job, initial.job_id, with_for_update=True)
        row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
        source, doc = json.loads(row.requested_source_json), json.loads(row.acquisition_json)
        require_not_retired(doc)
        if row.source_kind != "upload_zip":
            raise ImportFailure("import_upload_not_expected", status=409)
        if doc.get("raw_receipt"):
            return {"complete": True, "view": view(row)}
        require_ready()
        if (row.state not in {"awaiting_upload", "uploading", "needs_reconciliation"}
                or row.expires_at <= naive_utc() and not doc.get("raw_intent")):
            raise ImportFailure("import_upload_expired", status=409)
        claim = doc.get("upload_claim")
        if claim and claim["until"] > naive_utc().isoformat():
            raise ImportFailure("import_upload_in_progress", status=409)
        claim = {"id": new_id("upl"), "until": (naive_utc() + timedelta(seconds=240)).isoformat()}
        doc["upload_claim"] = claim
        row.acquisition_json = canonical(doc).decode()
        transition(session, row, "uploading", actor=actor)
        session.commit()
        return {"complete": False, "claim_id": claim["id"], "size_bytes": source["size_bytes"], "sha256": source["sha256"]}


def configured_store(doc):
    store = storage.configured()
    if doc.get("storage_config_digest") != store.config.fingerprint:
        raise ImportFailure("import_storage_configuration_changed", status=409)
    return store


def _upload_row(session, wid, vid, aid, identifier, actor, claim_id):
    initial = _row(session, wid, vid, aid, identifier, actor, write=True)
    if initial.job_id:
        session.get(Job, initial.job_id, with_for_update=True)
    row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
    doc = json.loads(row.acquisition_json)
    require_not_retired(doc)
    if doc.get("upload_claim", {}).get("id") != claim_id or row.state != "uploading":
        raise ImportFailure("import_upload_claim_changed", status=409)
    return row, doc


def abort_upload(wid, vid, aid, identifier, *, actor, claim_id, error="import_upload_interrupted"):
    with db()() as session:
        try:
            row, doc = _upload_row(session, wid, vid, aid, identifier, actor, claim_id)
        except ImportFailure:
            return
        doc.pop("upload_claim", None)
        row.acquisition_json = canonical(doc).decode()
        transition(session, row, "needs_reconciliation" if doc.get("raw_intent") else "awaiting_upload", error=error, actor=actor)
        session.commit()


def finish_upload(wid, vid, aid, identifier, blob, *, actor, claim_id):
    with db()() as session:
        row, doc = _upload_row(session, wid, vid, aid, identifier, actor, claim_id)
        source = json.loads(row.requested_source_json)
        if len(blob) != source["size_bytes"] or hashlib.sha256(blob).hexdigest() != source["sha256"]:
            raise ImportFailure("import_upload_bytes_mismatch")
        require_ready()
        store = configured_store(doc)
        scope = store_scope(row)
        intent = {"sha256": source["sha256"], "size_bytes": source["size_bytes"], "object_digest": source["sha256"]}
        if doc.get("raw_intent", intent) != intent:
            raise ImportFailure("import_upload_intent_changed", status=409)
        doc["raw_intent"] = intent
        from .cleanup import schedule_raw
        schedule_raw(session, row)
        row.acquisition_json = canonical(doc).decode()
        event(session, row, "upload_write_intent", actor=actor)
        _queue(session, row, delayed=True)
        session.commit()
    # Intent exists before bytes leave the process. A lost response is resolved
    # by the same immutable key; never pretend the write definitely failed.
    try:
        receipt = store.persist_import(blob, scope, kind="raw", object_digest=intent["object_digest"])
        store.read_import(receipt, scope, kind="raw", object_digest=intent["object_digest"])
    except BuildModelError:
        abort_upload(wid, vid, aid, identifier, actor=actor, claim_id=claim_id, error="import_storage_outcome_unconfirmed")
        raise ImportFailure("import_storage_outcome_unconfirmed", status=503) from None
    with db()() as session:
        row, doc = _upload_row(session, wid, vid, aid, identifier, actor, claim_id)
        doc["raw_receipt"] = receipt
        doc.pop("upload_claim", None)
        row.acquisition_json = canonical(doc).decode()
        row.acquired_bytes = row.retained_bytes = len(blob)
        row.reserved_bytes = MAX_ARCHIVE_BYTES
        transition(session, row, "queued", actor=actor)
        _queue(session, row)
        session.commit()
        return view(row)


def retry(wid, vid, aid, identifier, *, actor, state_version):
    """One explicit recovery generation; no automatic unbounded job creation."""
    with db()() as session:
        initial = _row(session, wid, vid, aid, identifier, actor, write=True)
        job = session.get(Job, initial.job_id, with_for_update=True) if initial.job_id else None
        row = _row(session, wid, vid, aid, identifier, actor, write=True, lock=True)
        doc = json.loads(row.acquisition_json)
        require_not_retired(doc)
        if row.state_version != state_version:
            raise ImportFailure("import_state_changed", status=409)
        if (not job or job.status not in {"failed_dead", "cancelled"} or row.state in TERMINAL
                or not any(doc.get(k) for k in ("raw_intent", "source_intent"))):
            raise ImportFailure("import_retry_not_available", status=409)
        require_ready()
        if doc.get("recovery_count", 0) >= 1:
            raise ImportFailure("import_recovery_exhausted", status=409)
        doc["recovery_count"] = 1
        row.acquisition_json = canonical(doc).decode()
        # Reuse the same persisted job and immutable source intent, with three
        # further leases at most. Original attempts remain audited in events.
        event(session, row, "recovery_requested", actor=actor, data={"prior_attempts": job.attempts})
        job.max_attempts += 3
        job.status, job.run_after, job.scheduled_for, job.finished_at = "retry_wait", naive_utc(), naive_utc(), None
        job.locked_at = job.locked_by = None
        transition(session, row, "cancel_requested" if row.state == "cancel_requested" else "queued", actor=actor)
        session.commit()
        return view(row, job)


def require_not_retired(doc):
    if doc.get("deleted") or doc.get("retired"):
        raise ImportFailure("import_source_retired", status=409)
