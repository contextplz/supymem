"""Source-bound revisions of published static landing pages.

The request is durable and private. It enters the existing immutable build,
independent QA, founder acceptance, and explicit publication lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.builder.access import authorize
from app.credits._txn import naive_utc
from app.core.local_mode import is_local
from app.db.supymem_models import BuildArtifact, Deployment, FounderApp, FounderRequest
from app.hosting.service import HostingError, _accepted_source
from app.state import db


def _baseline(session, app_id: str, actor: str):
    app = session.get(FounderApp, app_id)
    if app is None or app.deleted_at is not None:
        raise HostingError("hosting_app_not_found", status=404)
    try:
        authorize(session, workspace_id=app.workspace_id, venture_id=app.venture_id,
                  actor=actor, mutate=True)
    except KeyError as exc:
        raise HostingError("hosting_app_not_found", status=404) from exc
    except PermissionError as exc:
        raise HostingError(str(exc), status=403) from exc
    live = session.scalar(select(Deployment).where(
        Deployment.app_id == app.id, Deployment.workspace_id == app.workspace_id,
        Deployment.venture_id == app.venture_id, Deployment.state == "live",
    ).order_by(Deployment.version.desc()).limit(1))
    if live is None or live.target != "supymem_static" or not live.build_artifact_id:
        raise HostingError("hosting_live_static_site_required")
    artifact = session.get(BuildArtifact, live.build_artifact_id)
    if artifact is None or (artifact.app_id, artifact.workspace_id) != (app.id, app.workspace_id):
        raise HostingError("hosting_live_source_unverified")
    run, accepted, _, capture, _ = _accepted_source(session, app, artifact.build_run_id)
    # Older accepted static builds did not record artifact_profile. The live
    # supymem_static deployment plus _accepted_source's exact artifact, QA,
    # and public_manifest checks establish their static provenance. Never
    # reinterpret an explicit nonstatic profile as a landing page.
    profile = json.loads(run.spec or "{}").get("artifact_profile")
    if (accepted.id != artifact.id or capture.get("sha256") != artifact.sha256
            or profile not in (None, "static_landing_v1")):
        raise HostingError("hosting_live_source_unverified")
    return app, live, run, artifact


def _public(row: FounderRequest):
    data = json.loads(row.text)
    return {"id": row.id, "state": row.outcome, "instruction": data["instruction"],
            "deployment_id": data["deployment_id"], "build_id": data["build_id"],
            "source_artifact_id": data["source_artifact_id"],
            "source_sha256": data["source_sha256"],
            "created_at": row.created_at.isoformat() + "Z"}


def _public_analytics_script(app_id: str, *, session=None, read_only=False) -> str:
    from app.apps.keys import ensure_ingest_key
    from app.product_analytics.beacon import script_tag
    if not read_only:
        key = ensure_ingest_key(app_id)
        return script_tag(key=key["prefix"] if is_local() else key["secret"])
    from app.db.supymem_models import AppApiKey
    app = session.get(FounderApp, app_id)
    key = session.scalar(select(AppApiKey).where(
        AppApiKey.app_id == app_id, AppApiKey.workspace_id == app.workspace_id,
        AppApiKey.prefix.startswith("pk_app_"), AppApiKey.state == "active")) if app else None
    if key is None:
        raise HostingError("build_review_check_unavailable", status=503)
    return script_tag(key=key.prefix)


def request_revision(app_id: str, *, actor: str, instruction: str,
                     deployment_id: str, source_sha256: str):
    instruction = instruction.strip()
    if not 8 <= len(instruction) <= 2000 or any(ord(c) < 32 and c not in "\n\t" for c in instruction):
        raise HostingError("hosting_revision_instruction_invalid", status=400)
    if is_local():
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(instruction):
            raise HostingError("device_chat_secret_use_form", status=400)
    with db()() as session:
        app, live, run, artifact = _baseline(session, app_id, actor)
        if (deployment_id, source_sha256) != (live.id, artifact.sha256):
            raise HostingError("hosting_revision_baseline_changed")
        data = {"instruction": instruction, "deployment_id": live.id,
                "build_id": run.id, "source_artifact_id": artifact.id,
                "source_sha256": artifact.sha256, "actor": actor}
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        request_id = "fr_" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        prior = session.get(FounderRequest, request_id)
        if prior:
            if (prior.workspace_id, prior.venture_id, prior.related_id, prior.text) != (
                app.workspace_id, app.venture_id, app.id, canonical):
                raise HostingError("hosting_revision_request_conflict")
            return _public(prior)
        row = FounderRequest(id=request_id, workspace_id=app.workspace_id,
                             venture_id=app.venture_id, source="website_revision",
                             text=canonical, intent="revise_published_landing_page",
                             intent_category="website", classification="revision_request",
                             outcome="pending", related_id=app.id, created_at=naive_utc())
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Two identical clicks may race across API workers. The deterministic
            # identity makes this retry safe without scheduling duplicate work.
            session.rollback()
            prior = session.get(FounderRequest, request_id)
            if prior is None or (prior.workspace_id, prior.venture_id, prior.related_id, prior.text) != (
                    app.workspace_id, app.venture_id, app.id, canonical):
                raise HostingError("hosting_revision_request_conflict")
            return _public(prior)
        return _public(row)


def revision_status(app_id: str, *, actor: str):
    with db()() as session:
        app, live, run, artifact = _baseline(session, app_id, actor)
        rows = session.scalars(select(FounderRequest).where(
            FounderRequest.workspace_id == app.workspace_id,
            FounderRequest.venture_id == app.venture_id,
            FounderRequest.related_id == app.id,
            FounderRequest.source == "website_revision",
        ).order_by(FounderRequest.created_at.desc()).limit(20)).all()
        return {"baseline": {"deployment_id": live.id, "build_id": run.id,
                             "source_artifact_id": artifact.id,
                             "source_sha256": artifact.sha256},
                "requests": [_public(row) for row in rows]}


def build_inputs(session, app: FounderApp, request_id: str, *, actor: str, read_only=False):
    """Recheck a revision against the live immutable source at every admission."""
    row = session.get(FounderRequest, request_id)
    if row is not None and row.source == "private_build_correction":
        from app.builder.private_corrections import build_inputs as private_inputs
        return private_inputs(session, app, request_id, actor=actor, read_only=read_only)
    if (row is None or row.source != "website_revision" or row.outcome != "pending"
            or (row.workspace_id, row.venture_id, row.related_id) != (
                app.workspace_id, app.venture_id, app.id)):
        raise HostingError("hosting_revision_request_not_found", status=404)
    live_app, live, origin, artifact = _baseline(session, app.id, actor)
    if live_app.id != app.id:
        raise HostingError("hosting_revision_request_not_found", status=404)
    request = json.loads(row.text)
    if (request.get("deployment_id"), request.get("build_id"),
            request.get("source_artifact_id"), request.get("source_sha256")) != (
                live.id, origin.id, artifact.id, artifact.sha256):
        raise HostingError("hosting_revision_baseline_changed")
    original = json.loads(origin.spec or "{}").get("inputs")
    original_capture = json.loads(origin.spec or "{}").get("artifact_capture", {})
    if (not isinstance(original, dict) or original.get("milestone", {}).get("kind") != "landing"
            or original.get("task", {}).get("capability_id") != "build.run"
            or not original.get("task", {}).get("landing_expectations", {}).get("contract")
            or not original_capture.get("manifest_digest")):
        raise HostingError("hosting_revision_source_invalid")
    inputs = deepcopy(original)
    inputs["source"] = {"kind": "artifact", "id": artifact.id,
                        "sha256": artifact.sha256, "size_bytes": artifact.size_bytes}
    inputs["revision"] = {"request_id": row.id, "deployment_id": live.id,
                          "source_artifact_id": artifact.id, "source_sha256": artifact.sha256,
                          "source_manifest_digest": original_capture["manifest_digest"]}
    context = inputs.get("product_context") or {}
    context["revision_instruction"] = request["instruction"]
    context["revision_request_id"] = row.id
    context["analytics_beacon_script"] = _public_analytics_script(app.id, session=session, read_only=read_only)
    inputs["product_context"] = context
    if len(json.dumps(context).encode()) > 20_000:
        raise HostingError("hosting_revision_instruction_invalid", status=400)
    return inputs


def assert_publication_baseline(session, app: FounderApp, run, artifact):
    """A revised artifact may replace only the deployment its founder reviewed."""
    spec = json.loads(run.spec or "{}")
    if spec.get("lifecycle_version") == 3:
        from app.build.subscription_static_baseline import assert_current
        try:
            assert_current(session, app, spec.get("inputs", {}).get("publication_baseline"),
                           accepted_artifact_id=artifact.id)
        except ValueError as error:
            raise HostingError(str(error)) from error
        return
    revision = json.loads(run.spec or "{}").get("inputs", {}).get("revision")
    if not revision:
        return
    if revision.get("kind") == "private_build_correction":
        from app.builder.private_corrections import assert_publication
        assert_publication(session, app, run, artifact)
        return
    row = session.get(FounderRequest, revision.get("request_id"))
    if (row is None or row.source != "website_revision" or row.outcome != "approved"
            or (row.workspace_id, row.venture_id, row.related_id) != (
                app.workspace_id, app.venture_id, app.id)):
        raise HostingError("hosting_revision_not_approved")
    request = json.loads(row.text)
    if (revision.get("deployment_id"), revision.get("source_artifact_id"),
            revision.get("source_sha256")) != (
                request.get("deployment_id"), request.get("source_artifact_id"),
                request.get("source_sha256")):
        raise HostingError("hosting_revision_source_changed")
    origin, source, _, capture, _ = _accepted_source(session, app, request["build_id"])
    if (origin.id != request["build_id"] or source.id != revision["source_artifact_id"]
            or capture.get("manifest_digest") != revision.get("source_manifest_digest")):
        raise HostingError("hosting_revision_source_changed")
    live = session.scalar(select(Deployment).where(
        Deployment.app_id == app.id, Deployment.workspace_id == app.workspace_id,
        Deployment.venture_id == app.venture_id, Deployment.state == "live",
    ).order_by(Deployment.version.desc()).limit(1))
    if live is None or (live.id != revision["deployment_id"] and live.build_artifact_id != artifact.id):
        raise HostingError("hosting_revision_baseline_changed")
