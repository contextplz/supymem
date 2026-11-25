"""Read-only, source-version-bound runtime contract preflight.

This never executes captured code or authorizes hosting. It records why an
independent runtime QA run cannot start, or that runtime QA is still required.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import zipfile

from core.artifact_policy import ArtifactRejected
from core.artifact_snapshot import canonical

from app.build import coding_source_intake as intake
from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from app.db.session import serialize_writes
from app.db.supymem_models import CodingSourcePreflight, CodingSourceReceipt
from app.state import db


def inspect_contract(blob: bytes, *, source_sha256: str, manifest_sha256: str, order_id: str,
                     artifact_profile: str = "founder_product_v1") -> dict:
    """Inspect a previously validated ZIP; never build, import or run its files."""
    findings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        # The supported hosted runtime loads product.routes into the Python
        # scaffold. A Node review server can have a start script yet return 503
        # on every authenticated API; it is not a hostable product artifact.
        product_files = {"product/__init__.py", "product/routes.py",
                         "product/static/index.html"}
        if artifact_profile == "static_landing_v1":
            runtime = "static_landing_v1"
            if "index.html" not in names:
                findings.append("static_landing_index_missing")
            else:
                index = archive.read("index.html")
                if not index or len(index) > 1024 * 1024:
                    findings.append("static_landing_index_invalid")
            # Source parsing is not browser behavior verification. Server code
            # must not be confused with a supported static-only artifact.
            if product_files.intersection(names) - {"index.html"}:
                findings.append("static_landing_server_code_present")
            else:
                from pathlib import PurePosixPath
                from core.static_qa_contracts import STATIC_MEDIA
                from app.build.subscription_static_export import private_path
                if any(not private_path(name) and PurePosixPath(name).suffix.lower() not in STATIC_MEDIA
                       for name in names):
                    findings.append("static_landing_non_public_file")
        elif product_files <= names:
            runtime = "python_product_v1"
            try:
                routes = archive.read("product/routes.py")
                index = archive.read("product/static/index.html")
                if len(routes) > 256 * 1024 or not index or len(index) > 1024 * 1024:
                    findings.append("product_artifact_invalid")
                else:
                    ast.parse(routes.decode("utf-8"), filename="product/routes.py")
            except (SyntaxError, UnicodeError, ValueError):
                findings.append("product_routes_syntax_invalid")
        else:
            runtime = "unsupported_node_review_shell"
            findings.append("hosted_product_artifact_missing")
        # Preserve the older Node diagnostics as a clue for repairing a review
        # shell, but do not require Node at all when a Python product is present.
        if runtime == "unsupported_node_review_shell":
            findings.extend(_node_review_findings(archive, names))
    return {
        "schema_version": 2,
        "order_id": order_id,
        "source_sha256": source_sha256,
        "source_manifest_sha256": manifest_sha256,
        "runtime": runtime,
        "status": "blocked_runtime_contract" if findings else "requires_runtime_qa",
        "findings": sorted(set(findings)),
        "runtime_qa_verified": False,
        "hosting_authorized": False,
    }


def _node_review_findings(archive: zipfile.ZipFile, names: set[str]) -> list[str]:
    findings: list[str] = []
    needs_dependency_lock = False
    if "package.json" not in names:
        findings.append("root_package_json_missing")
    else:
        try:
            raw = archive.read("package.json")
            if len(raw) > 1024 * 1024:
                raise ValueError("package_too_large")
            package = json.loads(raw)
            if not isinstance(package, dict):
                raise TypeError("package_not_object")
            scripts = package.get("scripts")
            if not isinstance(scripts, dict) or not isinstance(scripts.get("start"), str) or not scripts["start"].strip():
                findings.append("start_script_missing")
            for field in ("dependencies", "devDependencies", "optionalDependencies"):
                declared = package.get(field, {})
                if not isinstance(declared, dict):
                    raise TypeError("package_dependencies_invalid")
                needs_dependency_lock |= bool(declared)
        except (TypeError, ValueError, UnicodeError):
            findings.append("root_package_json_invalid")
    if needs_dependency_lock and not names.intersection({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"}):
        findings.append("dependency_lock_missing")
    return findings


def _view(row: CodingSourcePreflight) -> dict:
    return {**json.loads(row.report_json), "report_sha256": row.report_sha256,
            "created_at": row.created_at.isoformat()}


def read(venture_id: str, order_id: str, caller) -> dict:
    with db()() as session:
        order, app_id = intake._scope(session, venture_id, order_id, caller)
        row = session.get(CodingSourcePreflight, order_id)
        if row is None or (row.workspace_id, row.venture_id, row.app_id) != (order["workspace_id"], venture_id, app_id):
            raise intake.SourceIntakeError("coding_source_preflight_not_found", 404)
        return _view(row)


def run(venture_id: str, order_id: str, caller) -> dict:
    with db()() as session:
        order, app_id = intake._scope(session, venture_id, order_id, caller)
        source = session.get(CodingSourceReceipt, order_id)
        if source is None or source.state != "stored" or source.app_id != app_id or source.workspace_id != order["workspace_id"]:
            raise intake.SourceIntakeError("coding_source_not_stored")
        existing = session.get(CodingSourcePreflight, order_id)
        if existing is not None:
            if (existing.source_sha256, existing.source_manifest_sha256) != (source.sha256, source.manifest_sha256):
                raise intake.SourceIntakeError("coding_source_preflight_conflict")
            return _view(existing)
        storage_receipt = json.loads(source.receipt_json or "{}")
        scope = {"workspace_id": source.workspace_id, "venture_id": source.venture_id,
                 "app_id": source.app_id, "order_id": source.order_id}
        sha, manifest, size = source.sha256, source.manifest_sha256, source.size_bytes
    try:
        blob = S3ArtifactStore.configured().read_coding_source(
            storage_receipt, scope, sha256=sha, manifest_digest=manifest)
    except BuildModelError as error:
        raise intake.SourceIntakeError("coding_source_readback_unconfirmed", 503) from error
    if len(blob) != size or hashlib.sha256(blob).hexdigest() != sha:
        raise intake.SourceIntakeError("coding_source_readback_mismatch", 503)
    # Re-validate the archive manifest rather than trusting storage metadata.
    try:
        if intake._archive_manifest(blob) != manifest:
            raise intake.SourceIntakeError("coding_source_readback_mismatch", 503)
    except ArtifactRejected as error:
        raise intake.SourceIntakeError("coding_source_readback_mismatch", 503) from error
    report = inspect_contract(blob, source_sha256=sha, manifest_sha256=manifest, order_id=order_id,
                              artifact_profile=(order.get("task_snapshot") or {}).get(
                                  "artifact_profile", "founder_product_v1"))
    report_bytes = canonical(report)
    with db()() as session:
        serialize_writes(session, key="coding-source-preflight:" + order_id)
        order, app_id = intake._scope(session, venture_id, order_id, caller)
        source = session.get(CodingSourceReceipt, order_id)
        if source is None or source.state != "stored" or (source.sha256, source.manifest_sha256, source.app_id) != (sha, manifest, app_id):
            raise intake.SourceIntakeError("coding_source_preflight_conflict")
        existing = session.get(CodingSourcePreflight, order_id)
        if existing is not None:
            if existing.report_sha256 != hashlib.sha256(report_bytes).hexdigest():
                raise intake.SourceIntakeError("coding_source_preflight_conflict")
            return _view(existing)
        row = CodingSourcePreflight(order_id=order_id, workspace_id=source.workspace_id,
            venture_id=venture_id, app_id=app_id, source_sha256=sha,
            source_manifest_sha256=manifest, status=report["status"],
            report_json=report_bytes.decode(), report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            created_at=intake._now())
        session.add(row)
        session.commit()
        return _view(row)
