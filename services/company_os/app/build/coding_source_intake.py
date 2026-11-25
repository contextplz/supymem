"""Durable source custody for a stopped, reviewed local coding order.

This accepts transport bytes only. It never marks a plan task complete, runs
untrusted source, creates a BuildRun, or authorizes hosting.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.artifact_policy import (
    DEFAULT_LIMITS,
    ArtifactLimits,
    ArtifactRejected,
    extract_zip,
    inspect_blob,
)
from core.artifact_snapshot import manifest_digest
from sqlalchemy import select

from app.build import device_orders
from app.builder.access import authorize
from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from app.db.models import VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import BuildRun, CodingSourceReceipt, FounderApp
from app.state import db


class SourceIntakeError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope(session, venture_id: str, order_id: str, caller) -> tuple[dict, str]:
    wid, actor, device = device_orders._scope(venture_id, caller)
    authorize(session, workspace_id=wid, venture_id=venture_id, actor=actor)
    row = session.get(VentureDocument, order_id)
    if row is None or row.kind != device_orders.KIND or row.venture_id != venture_id:
        raise SourceIntakeError("coding_source_order_not_found", 404)
    order = json.loads(row.provenance_json or "{}")
    if ((order.get("workspace_id"), order.get("actor_id"), order.get("device_id")) != (wid, actor, device)
            or order.get("id") != order_id):
        raise SourceIntakeError("coding_source_order_not_found", 404)
    verification = (order.get("manifest") or {}).get("coding_verification") or {}
    stop = order.get("native_stop") or {}
    if (order.get("state") != "stopped" or not order.get("claimed_at") or not order.get("closed_at")
            or not order.get("settlement_ledger_id") or stop.get("processes_stopped") is not True
            or stop.get("execution_id") != order.get("execution_id")
            or verification.get("operation") != "workspace_edit_v1"
            or verification.get("provider") not in {"codex", "claude"}):
        raise SourceIntakeError("coding_source_order_not_settled")
    apps = session.scalars(select(FounderApp).where(
        FounderApp.workspace_id == wid, FounderApp.venture_id == venture_id,
        FounderApp.deleted_at.is_(None))).all()
    # A venture may contain multiple products. A reviewed repair order carries
    # the failed build it supersedes; that durable run identifies the product.
    task = order.get("task_snapshot") or {}
    if task.get("kind") == "personal_build":
        bound_app = session.get(FounderApp, task.get("app_id"))
        if (bound_app is None or bound_app.deleted_at is not None
                or (bound_app.workspace_id, bound_app.venture_id) != (wid, venture_id)):
            raise SourceIntakeError("coding_source_app_ambiguous")
        return order, bound_app.id
    if task.get("artifact_profile") == "static_landing_v1" and task.get("publication_baseline") is not None:
        bound_app = session.get(FounderApp, task.get("app_id"))
        if (bound_app is None or bound_app.deleted_at is not None
                or (bound_app.workspace_id, bound_app.venture_id) != (wid, venture_id)
                or task["publication_baseline"].get("app_id") != bound_app.id):
            raise SourceIntakeError("coding_source_app_ambiguous")
        return order, bound_app.id
    prior_id = task.get("closed_failed_build_run_id")
    if isinstance(prior_id, str) and prior_id:
        prior = session.get(BuildRun, prior_id)
        app_ids = {app.id for app in apps}
        if (prior is not None and prior.app_id in app_ids
                and prior.workspace_id == wid and prior.venture_id == venture_id
                and prior.plan_task_id == task.get("task_id")
                and prior.closed_at is not None and prior.state == "failed"):
            return order, prior.app_id
        raise SourceIntakeError("coding_source_app_ambiguous")
    if len(apps) == 1:
        return order, apps[0].id
    raise SourceIntakeError("coding_source_app_ambiguous")


def _view(row: CodingSourceReceipt) -> dict:
    return {"order_id": row.order_id, "workspace_id": row.workspace_id,
            "venture_id": row.venture_id, "app_id": row.app_id,
            "plan_task_id": row.plan_task_id, "execution_id": row.execution_id,
            "provider": row.provider, "state": row.state, "sha256": row.sha256,
            "manifest_sha256": row.manifest_sha256, "size_bytes": row.size_bytes,
            "storage_receipt": json.loads(row.receipt_json) if row.state == "stored" and row.receipt_json else None,
            "qa_verified": False, "hosting_authorized": False}


def prepare(venture_id: str, order_id: str, caller, *, execution_id: str,
            sha256: str, manifest_sha256: str, size_bytes: int) -> dict:
    if (not re.fullmatch(r"dex_[A-Za-z0-9]+", execution_id or "")
            or not re.fullmatch(r"[a-f0-9]{64}", sha256 or "")
            or not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256 or "")
            or type(size_bytes) is not int or not 0 < size_bytes <= DEFAULT_LIMITS.max_archive_bytes):
        raise SourceIntakeError("coding_source_input_invalid", 400)
    with db()() as session:
        serialize_writes(session, key="coding-source:" + order_id)
        order, app_id = _scope(session, venture_id, order_id, caller)
        if execution_id != order["execution_id"]:
            raise SourceIntakeError("coding_source_execution_mismatch")
        existing = session.get(CodingSourceReceipt, order_id)
        if existing is not None:
            if (existing.workspace_id, existing.venture_id, existing.app_id,
                    existing.actor_id, existing.device_id, existing.plan_task_id,
                    existing.execution_id, existing.provider, existing.sha256, existing.manifest_sha256,
                    existing.size_bytes) != (
                    order["workspace_id"], venture_id, app_id, order["actor_id"],
                    order["device_id"], order["task_snapshot"]["task_id"],
                    execution_id, order["manifest"]["coding_verification"]["provider"], sha256, manifest_sha256, size_bytes):
                raise SourceIntakeError("coding_source_intent_conflict")
            return _view(existing)
        now = _now()
        record = CodingSourceReceipt(order_id=order_id, workspace_id=order["workspace_id"],
            venture_id=venture_id, app_id=app_id, actor_id=order["actor_id"],
            device_id=order["device_id"], plan_task_id=order["task_snapshot"]["task_id"],
            execution_id=execution_id, provider=order["manifest"]["coding_verification"]["provider"], state="awaiting_upload",
            sha256=sha256, manifest_sha256=manifest_sha256, size_bytes=size_bytes,
            created_at=now, updated_at=now)
        session.add(record)
        session.commit()
        return _view(record)


def _archive_manifest(blob: bytes) -> str:
    # This is bounded parsing, not source execution or runtime QA.
    inspect_blob(blob, limits=ArtifactLimits(max_file_bytes=DEFAULT_LIMITS.max_archive_bytes))
    with tempfile.TemporaryDirectory(prefix="supymem-coding-source-") as directory:
        manifest = extract_zip(blob, Path(directory).resolve() / "source")
    return manifest_digest(sorted(manifest, key=lambda item: item["path"]))


def upload(venture_id: str, order_id: str, caller, blob: bytes) -> dict:
    if not isinstance(blob, bytes) or not 0 < len(blob) <= DEFAULT_LIMITS.max_archive_bytes:
        raise SourceIntakeError("coding_source_archive_bounds", 413)
    with db()() as session:
        order, app_id = _scope(session, venture_id, order_id, caller)
        row = session.get(CodingSourceReceipt, order_id)
        if (row is None or row.app_id != app_id or row.workspace_id != order["workspace_id"]
                or row.device_id != order["device_id"] or row.actor_id != order["actor_id"]):
            raise SourceIntakeError("coding_source_intent_missing")
        expected = (row.sha256, row.manifest_sha256, row.size_bytes, row.execution_id)
        stored_view = _view(row) if row.state == "stored" else None
    sha, manifest, size, execution_id = expected
    if len(blob) != size or hashlib.sha256(blob).hexdigest() != sha or execution_id != order["execution_id"]:
        raise SourceIntakeError("coding_source_archive_mismatch")
    if stored_view is not None:
        return stored_view
    try:
        if _archive_manifest(blob) != manifest:
            raise SourceIntakeError("coding_source_manifest_mismatch")
    except ArtifactRejected as error:
        raise SourceIntakeError("coding_source_archive_rejected", 400) from error
    scope = {"workspace_id": order["workspace_id"], "venture_id": venture_id,
             "app_id": app_id, "order_id": order_id}
    try:
        store = S3ArtifactStore.configured()
        receipt = store.persist_coding_source(blob, scope, sha256=sha, manifest_digest=manifest)
        if store.read_coding_source(receipt, scope, sha256=sha, manifest_digest=manifest) != blob:
            raise BuildModelError("coding_store_readback_mismatch")
    except BuildModelError as error:
        raise SourceIntakeError("coding_source_storage_unconfirmed", 503) from error
    with db()() as session:
        serialize_writes(session, key="coding-source:" + order_id)
        current_order, current_app_id = _scope(session, venture_id, order_id, caller)
        row = session.get(CodingSourceReceipt, order_id, with_for_update=True)
        if (row is None or current_app_id != app_id or current_order["execution_id"] != execution_id
                or (row.sha256, row.manifest_sha256, row.size_bytes) != (sha, manifest, size)):
            raise SourceIntakeError("coding_source_intent_changed")
        if row.state == "stored":
            if json.loads(row.receipt_json or "{}") != receipt:
                raise SourceIntakeError("coding_source_storage_conflict")
            return _view(row)
        row.state, row.receipt_json, row.stored_at, row.updated_at = (
            "stored", json.dumps(receipt, sort_keys=True, separators=(",", ":")), _now(), _now())
        session.commit()
        return _view(row)


def download(venture_id: str, order_id: str, caller) -> bytes:
    saved = read(venture_id, order_id, caller)
    if saved["state"] != "stored" or not saved["storage_receipt"]:
        raise SourceIntakeError("coding_source_not_found", 404)
    scope = {"workspace_id": saved["workspace_id"], "venture_id": venture_id,
             "app_id": saved["app_id"], "order_id": order_id}
    try:
        return S3ArtifactStore.configured().read_coding_source(saved["storage_receipt"], scope,
            sha256=saved["sha256"], manifest_digest=saved["manifest_sha256"])
    except BuildModelError as error:
        raise SourceIntakeError("coding_source_storage_unconfirmed", 503) from error


def read(venture_id: str, order_id: str, caller) -> dict:
    with db()() as session:
        order, app_id = _scope(session, venture_id, order_id, caller)
        row = session.get(CodingSourceReceipt, order_id)
        if (row is None or row.app_id != app_id or row.workspace_id != order["workspace_id"]
                or row.actor_id != order["actor_id"] or row.device_id != order["device_id"]):
            raise SourceIntakeError("coding_source_not_found", 404)
        return _view(row)
