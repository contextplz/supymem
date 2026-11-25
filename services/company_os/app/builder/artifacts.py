"""Fenced task export -> independent safe source import -> durable S3 receipt.

Only transport integrity is attested here. QA/deployment states remain unchanged.
A task must remain available until this returns a committed artifact receipt.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from uuid import UUID

import httpx
from core.application_source import import_product
from core.artifact_policy import ArtifactRejected, DEFAULT_LIMITS, read_file
from core.artifact_snapshot import import_source, manifest_digest, validate_manifest

from .artifact_store import S3ArtifactStore
from .intent_bridge import _endpoint
from .model_proxy import BuildModelError, _authority, _fence, _locked, digest
from app.connectors.secrets import decrypt_at_rest
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildArtifact
from app.state import db


def _context(session, run_id, command_id, generation, controller_id, attempt):
    ctx = _locked(session, run_id)
    spec = _authority(session, ctx)
    _fence(ctx, spec, generation, controller_id, attempt)
    command, binding = spec.get("managed_command", {}), spec.get("managed_task", {})
    if command.get("id") != command_id or command.get("state") != "completed":
        raise BuildModelError("artifact_command_not_complete")
    if binding.get("generation") != generation or not binding.get("task_arn") or binding["task_arn"] != ctx.run.sandbox_ref:
        raise BuildModelError("artifact_task_binding_unavailable")
    scope = {"workspace_id": ctx.run.workspace_id, "venture_id": ctx.run.venture_id,
             "app_id": ctx.run.app_id, "build_id": run_id, "generation": generation}
    token = decrypt_at_rest(binding["control_token_ref"])
    secrets = [token]
    for kind in ("model_credential", "intent_credential"):
        value = spec.get(kind, {})
        if value.get("generation") == generation and value.get("token_ref"):
            secrets.append(decrypt_at_rest(value["token_ref"]))
    view = {"scope": scope, "binding": binding,
            "command": {key: command.get(key) for key in ("id", "kind", "body_digest", "state")}}
    return ctx, spec, _endpoint(binding.get("base_url")), token, tuple(secrets), scope, digest(view)


def _client():
    return httpx.Client(timeout=httpx.Timeout(10, connect=3), follow_redirects=False, trust_env=False)


def _visual_diagnostics(value) -> dict | None:
    """Accept only the builder's bounded, source-free visual failure receipt."""
    if not isinstance(value, dict) or set(value) != {
        "page_readable", "candidate_count", "qualified_distinct_count", "candidates",
    }:
        return None
    if (type(value["page_readable"]) is not bool
            or type(value["candidate_count"]) is not int
            or not 0 <= value["candidate_count"] <= 100
            or type(value["qualified_distinct_count"]) is not int
            or not 0 <= value["qualified_distinct_count"] <= min(value["candidate_count"], 1)
            or not isinstance(value["candidates"], list)
            or len(value["candidates"]) != min(value["candidate_count"], 8)
            or (not value["page_readable"] and value["candidate_count"] != 0)):
        return None
    indices: set[int] = set()
    for row in value["candidates"]:
        if (not isinstance(row, dict) or set(row) != {
            "element_index", "tag", "semantic", "name_ok", "distinct_name",
            "width_ok", "height_ok", "responsive",
        } or row["tag"] not in {"img", "svg"}
                or type(row["element_index"]) is not int
                or not 1 <= row["element_index"] <= 1_000_000
                or any(type(row[key]) is not bool for key in (
                    "semantic", "name_ok", "distinct_name", "width_ok", "height_ok", "responsive"
                )) or row["element_index"] in indices):
            return None
        indices.add(row["element_index"])
    return value


def _read(client, url, headers, *, limit, content_type, deadline, allowed_errors=()):
    with client.stream("GET", url, headers=headers) as response:
        if time.monotonic() > deadline:
            raise BuildModelError("artifact_transfer_limit")
        response_type = response.headers.get("content-type", "").split(";", 1)[0]
        if response.status_code == 422 and response_type == "application/json" and allowed_errors:
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > 4096):
                raise BuildModelError("artifact_task_read_failed")
            body = bytearray()
            for chunk in response.iter_raw():
                body.extend(chunk)
                if len(body) > 4096 or time.monotonic() > deadline:
                    raise BuildModelError("artifact_task_read_failed")
            try:
                detail = json.loads(body).get("detail")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, str) and detail in allowed_errors:
                raise BuildModelError(detail)
            if (isinstance(detail, dict) and set(detail) == {"code", "diagnostics"}
                    and detail.get("code") == "artifact_product_visual_contract_invalid"
                    and detail["code"] in allowed_errors):
                diagnostics = _visual_diagnostics(detail["diagnostics"])
                if diagnostics is not None:
                    raise BuildModelError(detail["code"], diagnostics=diagnostics)
            raise BuildModelError("artifact_task_read_failed")
        if (response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity"
                or response_type != content_type):
            raise BuildModelError("artifact_task_read_failed")
        length = response.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > limit):
            raise BuildModelError("artifact_transfer_limit")
        chunks, size = [], 0
        # Inspect each transport chunk. Buffering 64 KiB before yielding lets
        # a slow peer postpone the deadline check while keeping reads alive.
        for chunk in response.iter_raw():
            size += len(chunk)
            if size > limit or time.monotonic() > deadline:
                raise BuildModelError("artifact_transfer_limit")
            chunks.append(chunk)
        if time.monotonic() > deadline:
            raise BuildModelError("artifact_transfer_limit")
        if length is not None and int(length) != size:
            raise BuildModelError("artifact_transfer_size_mismatch")
        return b"".join(chunks)


def _download(endpoint, token, run_id, command_id, generation):
    headers = {"Authorization": "Bearer " + token, "X-Supymem-Build-ID": run_id,
               "X-Supymem-Generation": str(generation), "Accept-Encoding": "identity"}
    deadline = time.monotonic() + 120
    try:
        with _client() as client:
            state = json.loads(_read(client, endpoint + "/v1/state", headers, limit=262144, content_type="application/json", deadline=deadline))
            commands = state.get("commands")
            if (state.get("build_id") != run_id or type(state.get("generation")) is not int or state["generation"] != generation
                    or state.get("active_command_id") is not None or state.get("persistence_available") is not True
                    or not isinstance(commands, list) or not 1 <= len(commands) <= 100):
                raise BuildModelError("artifact_task_state_invalid")
            current = next((c for c in commands if c.get("command_id") == command_id), {})
            if (current.get("status") != "completed" or current.get("driver_status") != "finished"
                    or type(current.get("sequence")) is not int or current["sequence"] < 1
                    or current["sequence"] != max(c.get("sequence", 0) for c in commands)):
                raise BuildModelError("artifact_task_command_not_complete")
            base = endpoint + f"/v1/artifacts/{command_id}"
            metadata = json.loads(_read(client, base + "/manifest", headers, limit=1048576, content_type="application/json", deadline=deadline,
                                        allowed_errors={"artifact_unapproved_contact_detail", "artifact_product_javascript_invalid",
                                                        "artifact_product_javascript_check_unavailable",
                                                        "artifact_product_visual_contract_invalid",
                                                        "artifact_product_route_contract_invalid",
                                                        "artifact_product_record_contract_invalid"}))
            if (set(metadata) != {"schema_version", "build_id", "generation", "command_id", "format", "archive_sha256", "size_bytes", "manifest_digest", "manifest"}
                    or type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1 or metadata["format"] != "zip" or metadata["build_id"] != run_id
                    or type(metadata["generation"]) is not int or metadata["generation"] != generation or metadata["command_id"] != command_id
                    or type(metadata["size_bytes"]) is not int or not 1 <= metadata["size_bytes"] <= DEFAULT_LIMITS.max_archive_bytes
                    or not re.fullmatch(r"[a-f0-9]{64}", metadata["archive_sha256"])):
                raise BuildModelError("artifact_task_manifest_invalid")
            validate_manifest(metadata["manifest"])
            if manifest_digest(metadata["manifest"]) != metadata["manifest_digest"]:
                raise BuildModelError("artifact_task_manifest_invalid")
            headers["X-Supymem-Artifact-SHA256"] = metadata["archive_sha256"]
            blob = _read(client, base + "/archive", headers, limit=metadata["size_bytes"], content_type="application/zip", deadline=deadline)
            return metadata, blob
    except BuildModelError:
        raise
    except (httpx.HTTPError, ArtifactRejected, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BuildModelError("artifact_task_read_failed") from exc


def _validate_revision_artifact(spec, metadata, *, html=None):
    revision = spec.get("inputs", {}).get("revision")
    if not revision:
        return
    if metadata["manifest_digest"] == revision["source_manifest_digest"]:
        raise BuildModelError("artifact_revision_unchanged")
    if html is not None:
        beacon = spec["inputs"]["product_context"]["analytics_beacon_script"].strip()
        if (not beacon or html.count(beacon) != 1
                or html.count('data-supymem-analytics="1"') != 1):
            raise BuildModelError("artifact_revision_analytics_missing")


def import_completed_artifact(run_id, command_id, generation, controller_id, attempt):
    try:
        if str(UUID(command_id)) != command_id:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise BuildModelError("artifact_command_invalid") from exc
    fence = (run_id, command_id, generation, controller_id, attempt)
    with db()() as session:
        ctx, spec, endpoint, token, secrets, scope, context_digest = _context(session, *fence)
        prior = spec.get("artifact_capture", {})
        if prior.get("state") == "stored" and prior.get("command_id") == command_id:
            row = session.get(BuildArtifact, prior.get("artifact_id"))
            if not row or row.build_run_id != run_id or row.workspace_id != scope["workspace_id"]:
                raise BuildModelError("artifact_receipt_invalid")
            return {"artifact_id": row.id, "manifest_digest": prior["manifest_digest"]}
    store = S3ArtifactStore.configured()
    metadata, blob = _download(endpoint, token, run_id, command_id, generation)
    revision = spec.get("inputs", {}).get("revision")
    _validate_revision_artifact(spec, metadata)
    # A fresh owned destination is created only after the complete remote ZIP
    # passes path/type/secret/size checks. Never execute imported code in Core.
    with TemporaryDirectory(prefix="supymem-artifact-verify-") as temporary:
        try:
            importer = import_product if spec.get("artifact_profile") == "founder_product_v1" else import_source
            source_root = Path(temporary).resolve() / "source"
            importer(blob, source_root, manifest=metadata["manifest"],
                     sha256=metadata["archive_sha256"], size_bytes=metadata["size_bytes"], exact_secrets=secrets)
            if revision:
                html = read_file(source_root, "index.html").decode("utf-8")
                _validate_revision_artifact(spec, metadata, html=html)
        except ArtifactRejected as exc:
            raise BuildModelError(str(exc)) from exc
    capture = {"state": "uploading", "command_id": command_id, "manifest_digest": metadata["manifest_digest"],
               "sha256": metadata["archive_sha256"], "size_bytes": metadata["size_bytes"],
               "bucket": store.config.bucket, "key": store.config.key(scope, metadata["manifest_digest"]),
               "storage_config_digest": store.config.fingerprint}
    with db()() as session:
        ctx, spec, *rest = _context(session, *fence)
        if rest[-1] != context_digest:
            raise BuildModelError("artifact_task_context_changed")
        prior = spec.get("artifact_capture", {})
        if prior.get("state") in {"uploading", "stored"} and any(prior.get(key) != value for key, value in capture.items() if key != "state"):
            raise BuildModelError("artifact_capture_conflict")
        if prior.get("state") == "stored":
            return {"artifact_id": prior["artifact_id"], "manifest_digest": prior["manifest_digest"]}
        spec["artifact_capture"] = capture
        ctx.run.spec = json.dumps(spec, sort_keys=True)
        session.commit()  # durable upload intent before any S3 mutation
    receipt = store.persist(blob, metadata, scope)
    with db()() as session:
        ctx, spec, *rest = _context(session, *fence)
        if rest[-1] != context_digest:
            raise BuildModelError("artifact_task_context_changed")
        artifact_id = "bar_" + hashlib.sha256(f"{run_id}:{generation}:{metadata['manifest_digest']}".encode()).hexdigest()[:32]
        row = session.get(BuildArtifact, artifact_id)
        if row is None:
            row = BuildArtifact(id=artifact_id, build_run_id=run_id, app_id=scope["app_id"], workspace_id=scope["workspace_id"],
                                kind="source_zip", s3_key=receipt["key"], sha256=receipt["sha256"], size_bytes=receipt["size_bytes"], created_at=naive_utc())
            session.add(row)
            from .execution import _event
            _event(session, ctx.run, "artifact", {"artifact_id": artifact_id, "sha256": receipt["sha256"],
                   "size_bytes": receipt["size_bytes"], "manifest_digest": receipt["manifest_digest"], "qa_state": "not_run"})
        elif (row.build_run_id, row.workspace_id, row.sha256, row.size_bytes, row.s3_key) != (run_id, scope["workspace_id"], receipt["sha256"], receipt["size_bytes"], receipt["key"]):
            raise BuildModelError("artifact_receipt_conflict")
        spec["artifact_capture"] = {**capture, **receipt, "state": "stored", "artifact_id": artifact_id, "manifest": metadata["manifest"]}
        spec["artifact_verified"] = True
        ctx.run.artifact_id, ctx.run.spec = artifact_id, json.dumps(spec, sort_keys=True)
        session.commit()
        return {"artifact_id": artifact_id, "manifest_digest": metadata["manifest_digest"]}
