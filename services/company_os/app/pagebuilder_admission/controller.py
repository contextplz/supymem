"""Import verified server bytes, then reuse the independent QA-only lifecycle."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError, digest
from app.build import subscription_static_controller as shared
from app.credits import pools
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildArtifact
from app.pagebuilder import native_reference as reference
from app.state import db
from core.artifact_snapshot import canonical
from . import ORIGIN, TRIGGER, owns
from . import inputs
from .projection import project


def original_source(store, identity):
    binding = reference.CandidateBinding.model_validate(identity["candidate"]).model_dump()
    archive = store.read_import(identity["source"]["storage_receipt"], reference._storage_scope(binding),
        kind="raw", object_digest=binding["reference_sha256"])
    if len(archive) != binding["reference_size_bytes"] or hashlib.sha256(archive).hexdigest() != binding["reference_sha256"]:
        raise BuildModelError("pagebuilder_admission_storage_changed")
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        entries = opened.infolist()
        if len(entries) != 4 or {i.filename for i in entries} != {"source.zip", "manifest.json", "SPEC.md", "REFERENCE.json"}:
            raise BuildModelError("pagebuilder_admission_storage_changed")
        if any(i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1 or i.is_dir() for i in entries):
            raise BuildModelError("pagebuilder_admission_storage_changed")
        if opened.read("REFERENCE.json") != canonical({k: v for k, v in binding.items() if not k.startswith("reference_")}):
            raise BuildModelError("pagebuilder_admission_storage_changed")
        source = opened.read("source.zip")
        if hashlib.sha256(opened.read("SPEC.md")).hexdigest() != binding["spec_sha256"]:
            raise BuildModelError("pagebuilder_admission_storage_changed")
        # The public exporter independently verifies the entire inner source/manifest.
        if (len(source), hashlib.sha256(source).hexdigest()) != (binding["source_size_bytes"], binding["source_sha256"]):
            raise BuildModelError("pagebuilder_admission_storage_changed")
        return source


def import_source(payload):
    from app.builder.orchestration import _begin, _save, _commit, _followup, _revoke_if_needed
    from app.builder import lifecycle
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        if not owns(ctx.run, spec):
            raise BuildModelError("pagebuilder_admission_origin_invalid")
        if spec["source_import"]["state"] == "stored":
            return True
        try:
            inputs.recheck(session, ctx, spec)
        except Exception:
            pools.mark_reconciliation(session, ctx, "pagebuilder_admission_inputs_changed")
            spec = json.loads(ctx.run.spec)
        if spec.get("credentials_revoked"):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            shared.finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        identity = spec["source_import"]["identity"]
        spec["source_import"]["state"] = "verifying_storage"
        _save(ctx, spec)
        _commit(session, ctx, payload)
    try:
        store = S3ArtifactStore.configured()
        original = original_source(store, identity)
        public, projection = project(original, identity, spec["inputs"]["publication_baseline"])
        scope = {k: identity["scope"][k] for k in ("workspace_id", "venture_id", "app_id")}
        scope.update(build_id=payload["build_run_id"], generation=spec["generation"])
        stored = store.persist(public, projection, scope)
        if store.source(stored, scope) != public:
            raise BuildModelError("pagebuilder_admission_storage_changed")
    except Exception as error:
        with db()() as session:
            ctx, spec = _begin(session, payload)
            uncertain = str(error) in {"artifact_store_outcome_uncertain", "artifact_store_read_failed", "import_store_read_failed", "import_store_outcome_uncertain"}
            if uncertain and not shared.deadline_reason(spec):
                spec["phase"] = "pagebuilder_storage_reconciliation"
                _followup(session, ctx, spec)
            else:
                pools.mark_reconciliation(session, ctx, "pagebuilder_admission_storage_failed")
                spec = json.loads(ctx.run.spec)
                lifecycle.release_unlaunched_qa(session, ctx, spec)
                shared.finish(session, ctx, spec)
            _save(ctx, spec)
            _commit(session, ctx, payload)
        return False
    with db()() as session:
        ctx, spec = _begin(session, payload)
        spec = _revoke_if_needed(session, ctx, spec)
        try:
            inputs.recheck(session, ctx, spec)
        except Exception:
            pools.mark_reconciliation(session, ctx, "pagebuilder_admission_inputs_changed")
            spec = json.loads(ctx.run.spec)
        if spec.get("credentials_revoked"):
            lifecycle.release_unlaunched_qa(session, ctx, spec)
            shared.finish(session, ctx, spec)
            _commit(session, ctx, payload)
            return False
        aid = "bpa_" + digest({"run": ctx.run.id, "projection": projection})[:48]
        artifact = session.get(BuildArtifact, aid)
        if artifact is None:
            artifact = BuildArtifact(id=aid, build_run_id=ctx.run.id, app_id=ctx.run.app_id,
                workspace_id=ctx.run.workspace_id, kind="source_zip", s3_key=stored["key"],
                sha256=projection["archive_sha256"], size_bytes=len(public), created_at=naive_utc())
            session.add(artifact)
        elif (artifact.build_run_id, artifact.sha256, artifact.s3_key, artifact.size_bytes) != (
                ctx.run.id, projection["archive_sha256"], stored["key"], len(public)):
            raise BuildModelError("pagebuilder_admission_artifact_conflict")
        ctx.run.artifact_id = aid
        spec["artifact_capture"] = {**stored, "state": "stored", "artifact_id": aid, "manifest": projection["manifest"]}
        spec.update(artifact_verified=True, source_import_verified=True, phase="qa_ready")
        spec["source_import"].update(state="stored", projection=projection, artifact_receipt_digest=digest(spec["artifact_capture"]))
        spec["lifecycle"]["phase"] = "qa_ready"
        _save(ctx, spec)
        _commit(session, ctx, payload)
        return True


def verified(session, run, spec):
    try:
        imported = spec["source_import"]
        identity, scope = imported["identity"], imported["identity"]["scope"]
        binding = reference.CandidateBinding.model_validate(identity["candidate"]).model_dump()
        if (not owns(run, spec) or identity["origin"] != ORIGIN or identity["schema_version"] != 1
                or identity["projection_policy"] != "pagebuilder_public_metadata_v1"
                or imported["fingerprint"] != run.request_digest or digest(identity) != run.request_digest
                or imported["inputs"] != spec["inputs"] or identity["inputs_digest"] != digest(spec["inputs"])
                or (scope["workspace_id"], scope["venture_id"], scope["app_id"], scope["plan_task_id"], scope["actor_id"], scope["provider"])
                    != (run.workspace_id, run.venture_id, run.app_id, run.plan_task_id, spec["actor"], "pagebuilder")
                or any(scope[key] != binding[key] for key in ("workspace_id", "venture_id", "app_id", "job_id", "variant", "revision_id"))
                or (identity["source"]["sha256"], identity["source"]["manifest_digest"], identity["source"]["size_bytes"])
                    != (binding["source_sha256"], binding["source_manifest_digest"], binding["source_size_bytes"])):
            return False
        return shared._verified_source_qa(session, run, spec, current_scope=False, expected_trigger=TRIGGER)
    except (KeyError, TypeError, ValueError):
        return False


def review_code(session, run, spec):
    if not verified(session, run, spec):
        return "build_review_source_proof_invalid"
    try:
        from types import SimpleNamespace
        inputs.recheck(session, SimpleNamespace(run=run), spec, check_rate=False)
    except Exception:
        return "pagebuilder_admission_inputs_changed"
    return None
