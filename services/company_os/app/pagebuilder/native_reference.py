"""Private immutable candidate references, never execution or QA receipts."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
from typing import Literal
import zipfile

from core.artifact_policy import DEFAULT_LIMITS
from core.artifact_snapshot import canonical, import_source, manifest_digest, validate_manifest
from core.clock import utcnow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from app.core.jobs import RetryableError
from app.db.models import CreditReservation, Job, Venture, VentureDocument, Workspace
from app.db.session import serialize_writes
from app.db.supymem_models import DesktopDevice, ExecutionPlan, FounderApp, IdeaRevision, IdeaSession
from app.pagebuilder import api, runtime
from app.core.local_mode import is_local
from app.state import db

KIND = "pagebuilder_native_reference_v1"
INVALID = "pagebuilder_candidate_invalid"
CHANGED = "pagebuilder_candidate_changed"
UNAVAILABLE = "pagebuilder_reference_unavailable"
RUNNER_UNAVAILABLE = "pagebuilder_runner_upgrade_required"
MIN_RUNNER_VERSION = (0, 1, 84)


def artifact_store():
    if is_local():
        from app.pagebuilder.local_artifacts import LocalArtifactStore
        return LocalArtifactStore.configured()
    return S3ArtifactStore.configured()


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    variant: int = Field(ge=0, le=2)


class CandidateBinding(CandidateSelection):
    schema_version: Literal[1]
    revision_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    venture_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    app_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_size_bytes: int = Field(gt=0, le=DEFAULT_LIMITS.max_archive_bytes)
    spec_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_size_bytes: int = Field(gt=0, le=DEFAULT_LIMITS.max_archive_bytes)


def _reference_id(job_id, variant):
    return "pbr_" + api.digest([job_id, variant])


def _job(session, job_id, *, terminal=True):
    row = session.get(Job, job_id)
    if not row or row.kind != api.KIND:
        raise KeyError("pagebuilder_candidate_not_found")
    if row.status not in ({"succeeded"} if terminal else {"leased", "succeeded", "failed_dead"}):
        raise ValueError(INVALID)
    return row, _completed_candidate(session, row)


def _replaced_output(operations, key):
    """Only the pipeline's bounded successor can replace a rejected output."""
    variant, _, stage = key.partition(":")
    successors = {"brief_direction_plan": ("plan_repair_1",),
        "plan_repair_1": ("plan_repair_2",), "plan_repair_2": ("plan_repair_3",), "plan_repair_3": ("plan_repair_4",),
        "plan_repair_4": ("plan_repair_5",),
        "capability_review_1": ("capability_review_2",), "capability_review_2": ("capability_review_3",),
        "capability_review_3": ("capability_review_4",), "capability_review_4": ("capability_review_5",),
        "capability_review_5": ("capability_review_6",),
        "code": ("repair_1", "repair_2"), "repair_1": ("repair_2",)}.get(stage, ())
    for successor in successors:
        saved = operations.get(variant + ":" + successor, {})
        content = saved.get("response", {}).get("content")
        if (saved.get("state") == "completed" and not saved.get("error_code")
                and saved.get("usage", {}).get("settled") is True
                and isinstance(content, str) and content.strip()):
            return True
    return False


def _completed_candidate(session, row):
    from app.pagebuilder.local import CODING_TOOL_REVIEW
    from app.pagebuilder.pipeline import KNOWN_OUTPUT_FAILURES
    doc = json.loads(row.payload_json)
    operations = doc.get("_runtime", {}).get("operations", {})
    # The current image predicate decodes both envelopes. Enforce the native
    # source budget before entering it, including saved/corrupted job documents.
    result = doc.get("result") or {}
    bundles = result.get("bundles", []) if isinstance(result, dict) else None
    if not isinstance(bundles, list):
        raise ValueError(INVALID)
    for bundle in bundles:
        if not isinstance(bundle, dict):
            raise ValueError(INVALID)
        source, files = bundle.get("source_zip"), bundle.get("files")
        if (not isinstance(source, str) or len(source) > 4 * ((DEFAULT_LIMITS.max_archive_bytes + 2) // 3)
                or not isinstance(files, dict) or any(not isinstance(value, str)
                    or len(value) > 4 * ((DEFAULT_LIMITS.max_file_bytes + 2) // 3) for value in files.values())):
            raise ValueError(INVALID)
    quote = doc.get("_runtime", {}).get("quote") or {}
    tool_review = is_local() and quote.get("provider") == CODING_TOOL_REVIEW
    if (doc.get("_runtime", {}).get("state") != "ready"
            or not api._candidate_current(doc) or not (operations or tool_review)):
        raise ValueError(INVALID)
    used_images = {f"variant_{variant}:{image.get('operation')}" for variant, bundle in enumerate(doc["result"]["bundles"])
                   for image in bundle.get("imagery", [])}
    fallback_keys = {f"variant_{variant}:" + bundle["manifest"]["repair_fallback"]["stage"]
        for variant, bundle in enumerate(doc["result"]["bundles"]) if bundle["manifest"].get("repair_fallback")}
    fallback_variants = {f"variant_{variant}" for variant, bundle in enumerate(doc["result"]["bundles"])
        if bundle["qa"]["capability_grounding"].get("policy") == "deterministic_early_access_v1"}
    for key, operation in operations.items():
        usage = operation.get("usage", {})
        error = operation.get("error_code")
        if key in fallback_keys and operation.get("state") in {"uncertain", "failed", "retryable"}:
            if ((usage.get("provider_identity"), usage.get("provider"), usage.get("model"), usage.get("account_id"),
                    usage.get("workspace_id"), usage.get("venture_id"), usage.get("source")) != (
                    quote.get("provider_identity"), quote.get("provider"), quote.get("model"), quote.get("account_id"),
                    row.workspace_id, row.venture_id, "byok" if quote.get("payer") == "founder" else "platform")):
                raise ValueError(INVALID)
            if quote.get("payer") != "founder":
                hold = session.get(CreditReservation, usage.get("reservation_id")) if usage.get("reservation_id") else None
                if (hold is None or (hold.workspace_id, hold.account_id, hold.purpose, hold.request_id) != (
                        row.workspace_id, quote.get("account_id"), "pagebuilder." + key.partition(":")[2], usage.get("request_id"))
                        or not (hold.request_id or "").startswith(row.id + ":" + key + ":")):
                    raise ValueError(INVALID)
            continue
        if key.partition(":")[2].startswith("image_"):
            # Artwork fell back to native SVG unless its exact settled bytes are in the bundle (_candidate_current).
            image = quote.get("image") or {}
            if key in used_images and (operation.get("state") != "completed" or error):
                raise ValueError(INVALID)
            if operation.get("state") != "completed":
                continue
            if (usage.get("settled") is not True or (usage.get("provider_identity"), usage.get("provider"),
                    usage.get("model"), usage.get("account_id"), usage.get("source")) != (
                    image.get("provider_identity"), image.get("provider"), image.get("model"), quote.get("account_id"), "byok" if is_local() else "platform")):
                raise ValueError(INVALID)
        elif (operation.get("state") != "completed"
                or error and (error not in KNOWN_OUTPUT_FAILURES or not (
                    _replaced_output(operations, key) or key.partition(":")[0] in fallback_variants))
                or usage.get("settled") is not True
                or (usage.get("provider_identity"), usage.get("provider"), usage.get("model"), usage.get("account_id")) != (
                    quote.get("provider_identity"), quote.get("provider"), quote.get("model"), quote.get("account_id"))
                or usage.get("source") != ("byok" if quote.get("payer") == "founder" else "platform")):
            raise ValueError(INVALID)
        rid = usage.get("reservation_id")
        if quote.get("payer") != "founder" and not rid:
            raise ValueError(INVALID)
        if rid:
            hold = session.get(CreditReservation, rid)
            if (hold is None or hold.status != "settled" or hold.is_pool
                    or (hold.workspace_id, hold.account_id, hold.settled_micro) != (
                        row.workspace_id, usage.get("account_id"), usage.get("credits_micro"))
                    or hold.purpose != "pagebuilder." + key.partition(":")[2]
                    or not (hold.request_id or "").startswith(row.id + ":" + key + ":")):
                raise ValueError(INVALID)
    for variant, bundle in enumerate(doc["result"]["bundles"]):
        if f"variant_{variant}" in fallback_variants:
            continue
        tool_verified = False
        if is_local() and bundle["manifest"].get("built_with_coding_tool") is True:
            from app.build.device_orders import KIND as ORDER_KIND
            from app.pagebuilder.models import PageInput
            from app.pagebuilder.pipeline import tool_page_plan
            from app.pagebuilder.tool import verified_order
            saved = session.get(VentureDocument, (doc.get("tool_build") or {}).get("order_id"))
            if saved and saved.kind == ORDER_KIND and saved.venture_id == row.venture_id:
                order = json.loads(saved.provenance_json)
                tool_verified = (verified_order(order, row.workspace_id, doc.get("_actor_subject"), row.id, order.get("execution_id"))
                    and order["task_snapshot"].get("page_input") == PageInput.model_validate(doc["_runtime"]["input"]).model_dump(mode="json")
                    and tool_page_plan(PageInput.model_validate(doc["_runtime"]["input"]), order["task_snapshot"]["page_plan"],
                        {name: base64.b64decode(body, validate=True) for name, body in bundle["files"].items()}).model_dump(mode="json") == bundle["plan"]
                    and order["iterations"][-1].get("file_manifest_sha256") == manifest_digest(bundle["manifest"]["manifest"]))
        final_review = bundle["qa"]["capability_grounding"]["reviews"][-1]["stage"]
        if tool_review:
            if not tool_verified or final_review != CODING_TOOL_REVIEW:
                raise ValueError(INVALID)
        elif ((f"variant_{variant}:code" not in operations
                or is_local() and bundle["manifest"].get("built_with_coding_tool") is True) and not tool_verified
                or operations[f"variant_{variant}:{final_review}"].get("error_code")):
            raise ValueError(INVALID)
    return doc


def _encode(row, doc, variant):
    try:
        bundle = doc["result"]["bundles"][variant]
        capture = bundle["manifest"]["artifact_capture"]
        manifest = validate_manifest(capture["manifest"])
        if not isinstance(bundle["source_zip"], str) or len(bundle["source_zip"]) > 4 * ((DEFAULT_LIMITS.max_archive_bytes + 2) // 3):
            raise ValueError(INVALID)
        source = base64.b64decode(bundle["source_zip"], validate=True)
        spec = bundle["spec_markdown"].encode("utf-8")
        app_id = doc["_runtime"]["analytics_app_id"]
        if ((not is_local() and bundle["manifest"]["analytics_beacon"]["app_id"] != app_id)
                or capture.get("state") != "candidate" or capture.get("kind") != "source_zip"
                or manifest_digest(manifest) != capture["manifest_digest"]
                or not 0 < len(spec) <= 65536 or len(canonical(manifest)) > 1024 * 1024):
            raise ValueError(INVALID)
        with tempfile.TemporaryDirectory(prefix="pagebuilder-reference-") as directory:
            import_source(source, Path(directory).resolve(strict=True) / "source", manifest=manifest,
                sha256=capture["sha256"], size_bytes=capture["size_bytes"])
        identity = {"schema_version": 1, "job_id": row.id, "variant": variant,
            "revision_id": doc["revision_id"], "workspace_id": row.workspace_id,
            "venture_id": row.venture_id, "app_id": app_id,
            "source_sha256": capture["sha256"], "source_manifest_digest": capture["manifest_digest"],
            "source_size_bytes": len(source), "spec_sha256": hashlib.sha256(spec).hexdigest()}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, blob in sorted({"source.zip": source, "manifest.json": canonical(manifest),
                    "SPEC.md": spec, "REFERENCE.json": canonical(identity)}.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, 0o100600 << 16
                archive.writestr(info, blob)
        blob = output.getvalue()
        binding = CandidateBinding.model_validate({**identity,
            "reference_sha256": hashlib.sha256(blob).hexdigest(), "reference_size_bytes": len(blob)})
        return binding.model_dump(), blob
    except (KeyError, IndexError, TypeError, ValueError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ValueError(INVALID) from exc


def _storage_scope(binding):
    return {key: binding[key] for key in ("workspace_id", "venture_id", "app_id")} | {
        "import_id": _reference_id(binding["job_id"], binding["variant"])}


def persist_job(job_id, payload=None, *, store=None):
    """Retry only immutable storage after generation is saved; never call a model."""
    with db()() as session:
        if payload is not None:
            runtime.fence(session, payload)
        try:
            row, doc = _job(session, job_id, terminal=False)
        except ValueError:
            logging.getLogger(__name__).warning("pagebuilder_source_verification_failed", extra={
                "job_id": job_id, "error_code": INVALID})
            raise
        references = [_encode(row, doc, variant) for variant in range(doc["variant_count"])]
    try:
        store = store or artifact_store()
    except BuildModelError as exc:
        raise RetryableError(UNAVAILABLE) from exc
    for binding, blob in references:
        rid = _reference_id(job_id, binding["variant"])
        with db()() as session:
            serialize_writes(session, key="pagebuilder-reference:" + rid)
            if payload is not None:
                runtime.fence(session, payload)
            _job(session, job_id, terminal=False)
            saved = session.get(VentureDocument, rid)
            if saved:
                intent = json.loads(saved.provenance_json)
                if saved.kind != KIND or saved.venture_id != binding["venture_id"] or intent.get("binding") != binding:
                    raise ValueError(CHANGED)
            else:
                intent = {"binding": binding, "state": "persisting"}
                session.add(VentureDocument(id=rid, venture_id=binding["venture_id"], kind=KIND,
                    title="Private generated reference for native coding review", provenance_json=api.dump(intent),
                    created_at=utcnow(), updated_at=utcnow()))
            session.commit()  # durable intent precedes S3; a lost ACK cannot regenerate source
        try:
            scope, sha = _storage_scope(binding), binding["reference_sha256"]
            receipt = intent.get("receipt") or store.persist_import(blob, scope, kind="raw", object_digest=sha)
            if store.read_import(receipt, scope, kind="raw", object_digest=sha) != blob:
                raise BuildModelError("pagebuilder_reference_readback_mismatch")
        except BuildModelError as exc:
            raise RetryableError(UNAVAILABLE) from exc
        with db()() as session:
            serialize_writes(session, key="pagebuilder-reference:" + rid)
            if payload is not None:
                runtime.fence(session, payload)
            saved = session.get(VentureDocument, rid, with_for_update=True)
            current = json.loads(saved.provenance_json)
            if current.get("binding") != binding or current.get("receipt", receipt) != receipt:
                raise ValueError(CHANGED)
            saved.provenance_json = api.dump({**current, "state": "stored", "receipt": receipt})
            saved.updated_at = utcnow()
            session.commit()
    with db()() as session:
        if payload is not None:
            runtime.fence(session, payload)
        row, doc = _job(session, job_id, terminal=False)
        doc["result"]["ready_delivered"] = True
        row.payload_json = api.dump(doc)
        session.commit()
    return [binding for binding, _ in references]


def retry_source(job_id, *, person_id, venture_id, workspace_id, expected_attempt):
    """One explicit retry of saved source storage; never restart generation."""
    if type(expected_attempt) is not int or expected_attempt < 0:
        raise ValueError("pagebuilder_source_retry_unavailable")
    if api.authority(venture_id, person_id, write=True) != (workspace_id, person_id):
        raise KeyError("pagebuilder_candidate_not_found")
    with db()() as session:
        serialize_writes(session, key="pagebuilder-source-retry:" + job_id)
        row = session.get(Job, job_id, with_for_update=True)
        if not row or (row.kind, row.workspace_id, row.venture_id) != (api.KIND, workspace_id, venture_id):
            raise KeyError("pagebuilder_candidate_not_found")
        doc = json.loads(row.payload_json)
        if doc.get("_actor_subject") != person_id:
            raise KeyError("pagebuilder_candidate_not_found")
        idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == workspace_id,
            IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1))
        if (idea is None or idea.state != "confirmed"
                or idea.current_revision_id != doc.get("revision_id")
                or idea.confirmed_revision_id != doc.get("revision_id")):
            raise ValueError(CHANGED)
        revision_id, inputs = api.snapshot(session, venture_id, workspace_id)
        if revision_id != doc.get("revision_id") or api.digest(inputs) != api.digest(doc["_runtime"]["input"]):
            raise ValueError(CHANGED)
        _completed_candidate(session, row)
        for variant in range(doc["variant_count"]):
            binding, _blob = _encode(row, doc, variant)
            saved = session.get(VentureDocument, _reference_id(job_id, variant))
            if saved and (saved.kind != KIND or saved.venture_id != venture_id
                    or json.loads(saved.provenance_json).get("binding") != binding):
                raise ValueError(CHANGED)
        if row.status == "failed_dead" and row.attempts == expected_attempt:
            row.max_attempts = row.attempts + 1
            row.status = "pending"
            row.run_after = row.scheduled_for = utcnow()
            row.started_at = row.finished_at = row.locked_at = row.locked_by = row.last_error = None
            session.commit()
        elif row.status not in {"pending", "scheduled", "retry_wait", "leased", "succeeded", "failed_dead"}:
            raise ValueError("pagebuilder_source_retry_unavailable")
        return {"job_id": row.id, "state": "ready", "queue_state": row.status}


def bind_candidate(session, wid, vid, actor, snapshot, selection):
    """Select only an owned settled candidate under the current approved contract."""
    selection = CandidateSelection.model_validate(selection)
    row, doc = _job(session, selection.job_id)
    if (row.workspace_id, row.venture_id, doc.get("_actor_subject")) != (wid, vid, actor):
        raise KeyError("pagebuilder_candidate_not_found")
    venture, workspace = session.get(Venture, vid), session.get(Workspace, wid)
    idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == wid,
        IdeaSession.venture_id == vid, IdeaSession.state != "abandoned")
        .order_by(IdeaSession.created_at.desc()).limit(1))
    revision = session.get(IdeaRevision, doc.get("revision_id"))
    app_id = doc["_runtime"].get("analytics_app_id")
    app = session.get(FounderApp, app_id) if app_id else None
    plan = session.get(ExecutionPlan, snapshot.get("plan_id"))
    if (venture is None or venture.workspace_id != wid or venture.archived_at or venture.stage == "purged"
            or workspace is None or workspace.archived_at or idea is None or idea.state != "confirmed"
            or idea.current_revision_id != doc["revision_id"] or idea.confirmed_revision_id != doc["revision_id"]
            or revision is None or (revision.workspace_id, revision.venture_id, revision.session_id) != (wid, vid, idea.id)
            or plan is None or plan.revision_id != doc["revision_id"]
            or (plan.workspace_id, plan.venture_id) != (wid, vid)
            or snapshot.get("artifact_profile") != "static_landing_v1"
            or snapshot.get("app_id") != app_id or app is None or app.deleted_at
            or (app.workspace_id, app.venture_id) != (wid, vid)):
        raise ValueError(CHANGED)
    if session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.workspace_id == wid,
            ExecutionPlan.venture_id == vid, ExecutionPlan.version > plan.version,
            ExecutionPlan.state.in_(["approved", "executing", "founder_review"])).limit(1)):
        raise ValueError(CHANGED)
    from app.build.device_orders import _coding_offline_profile
    _coding_offline_profile(snapshot)
    inputs = doc["_runtime"]["input"]
    if (inputs.get("name"), inputs.get("idea"), inputs.get("clarified_answers")) != (
            venture.name, revision.summary or venture.raw_idea, json.loads(revision.structured)):
        raise ValueError(CHANGED)
    from app.build.subscription_static_baseline import assert_current
    assert_current(session, app, snapshot.get("publication_baseline"))
    binding, _blob = _encode(row, doc, selection.variant)
    saved = session.get(VentureDocument, _reference_id(row.id, selection.variant))
    intent = json.loads(saved.provenance_json) if saved else {}
    if (not saved or saved.kind != KIND or saved.venture_id != vid or intent.get("state") != "stored"
            or intent.get("binding") != binding or not intent.get("receipt")):
        raise ValueError(UNAVAILABLE)
    return binding


def resolve_candidate(venture_id, caller, selection):
    """Founder profile eligibility shares the exact native prepare boundary."""
    wid, actor = api.authority(venture_id, caller, write=True)
    from app.build import device_orders
    with db()() as session:
        snapshot = device_orders._coding_task_snapshot(session, wid, venture_id,
            "Build the approved landing page with Codex")
        device_orders._coding_offline_profile(snapshot)
        return bind_candidate(session, wid, venture_id, actor, snapshot, selection)


def assert_binding(session, wid, vid, actor, snapshot, binding):
    expected = CandidateBinding.model_validate(binding).model_dump()
    if actor is None:
        _row, doc = _job(session, expected["job_id"])
        actor = doc["_actor_subject"]
    if bind_candidate(session, wid, vid, actor, snapshot,
            {key: expected[key] for key in ("job_id", "variant")}) != expected:
        raise ValueError(CHANGED)


def runner_supported(session, actor, device_id):
    """Only the exact active signed device's native heartbeat sets this version."""
    import re
    device = session.get(DesktopDevice, device_id)
    return bool(device and device.person_id == actor and device.revoked_at is None
        and re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", device.app_version or "")
        and tuple(map(int, device.app_version.split("."))) >= MIN_RUNNER_VERSION)


def assert_runner(session, actor, device_id):
    if not runner_supported(session, actor, device_id):
        raise ValueError(RUNNER_UNAVAILABLE)


def download(venture_id, caller, order_id, *, store=None):
    """Claimed device-only bytes; recheck custody after the bounded private read."""
    from app.build import device_invocations
    def current():
        with db()() as session:
            _row, body, account = device_invocations._locked(session, venture_id, caller, order_id)
            device_invocations._check_executing(session, venture_id, body, account, utcnow())
            assert_runner(session, body["actor_id"], body["device_id"])
            binding = (body["manifest"].get("coding_verification") or {}).get("pagebuilder_candidate")
            if not binding:
                raise ValueError(INVALID)
            saved = session.get(VentureDocument, _reference_id(binding["job_id"], binding["variant"]))
            return binding, json.loads(saved.provenance_json)["receipt"]
    binding, receipt = current()
    try:
        blob = (store or artifact_store()).read_import(receipt, _storage_scope(binding),
            kind="raw", object_digest=binding["reference_sha256"])
        if len(blob) != binding["reference_size_bytes"] or hashlib.sha256(blob).hexdigest() != binding["reference_sha256"]:
            raise BuildModelError("pagebuilder_reference_readback_mismatch")
    except BuildModelError as exc:
        raise ValueError(UNAVAILABLE) from exc
    if current() != (binding, receipt):
        raise ValueError(CHANGED)
    return blob
