"""Reviewed repairs of an accepted private static artifact; never edit its history."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from app.builder.execution import BuildError, _app
from app.builder.model_proxy import digest
from app.credits._txn import naive_utc
from app.db.models import Venture
from app.db.supymem_models import BuildRun, Deployment, ExecutionPlan, FounderRequest, PlanTask
from app.state import db

KIND = "private_build_correction"


def baseline(session, build_id, *, actor, read_only=False):
    from app.hosting.service import HostingError, _accepted_source
    from app.planning.compiler import _current
    run = session.get(BuildRun, build_id)
    if not run:
        raise KeyError("build")
    app = _app(session, run.app_id, actor, mutate=True)
    # Serialize all requests against the reviewed venture's mutation floor.
    if not read_only:
        session.get(Venture, app.venture_id, with_for_update=True)
    from app.ventures.lifecycle import is_paused
    if is_paused(app.venture_id):
        raise BuildError("private_correction_venture_paused")
    if session.scalar(select(Deployment.id).where(Deployment.app_id == app.id,
            Deployment.workspace_id == app.workspace_id, Deployment.venture_id == app.venture_id,
            or_(Deployment.state.in_(["live", "pending", "packaging", "pushing", "creating_infra", "health_checking"]),
                and_(Deployment.state == "failed", Deployment.error == "hosting_public_state_uncertain"))).limit(1)):
        raise BuildError("private_correction_use_published_revision")
    try:
        run, artifact, _, capture, _ = _accepted_source(session, app, build_id)
    except HostingError as error:
        raise BuildError(error.code, status=error.status) from error
    spec = json.loads(run.spec)
    original = spec.get("inputs", {})
    if (spec.get("artifact_profile") != "static_landing_v1"
            or original.get("milestone", {}).get("kind") != "landing"
            or not original.get("task", {}).get("landing_expectations", {}).get("contract")):
        raise BuildError("private_correction_static_source_required")
    plan = session.get(ExecutionPlan, original.get("plan_id"))
    task = session.get(PlanTask, original.get("plan_task_id"))
    if (not plan or not task or (plan.workspace_id, plan.venture_id, task.workspace_id, task.venture_id)
            != (app.workspace_id, app.venture_id, app.workspace_id, app.venture_id)
            or task.state != "done" or plan.state not in {"approved", "executing"}
            or not plan.approved_at or not plan.approved_by):
        raise BuildError("private_correction_plan_changed")
    _, stale = _current(plan, json.loads(plan.structured), session,
                        include_builder_runtime=spec.get("lifecycle_version") != 3)
    if stale or session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == app.venture_id,
            ExecutionPlan.version > plan.version, ExecutionPlan.state.in_(["approved", "founder_review"])).limit(1)):
        raise BuildError("private_correction_plan_changed")
    candidates = session.scalars(select(BuildRun).where(BuildRun.app_id == app.id,
        BuildRun.workspace_id == app.workspace_id, BuildRun.venture_id == app.venture_id,
        BuildRun.runtime_version == 1).order_by(BuildRun.created_at.desc(), BuildRun.id.desc()).limit(100)).all()
    latest = next((r for r in candidates if json.loads(r.spec or "{}").get("lifecycle", {})
                   .get("review", {}).get("status") == "accepted"), None)
    if not latest or latest.id != run.id:
        raise BuildError("private_correction_baseline_changed")
    return app, run, artifact, capture, plan


def _binding(run, artifact, capture, plan):
    spec = json.loads(run.spec)
    return {"build_id": run.id, "source_artifact_id": artifact.id, "source_sha256": artifact.sha256,
            "source_manifest_digest": capture["manifest_digest"], "report_digest": spec["lifecycle"]["review"]["report_digest"],
            "plan_id": plan.id, "plan_digest": digest(json.loads(plan.structured)),
            "plan_task_id": spec["inputs"]["plan_task_id"]}


def availability(build_id, *, actor):
    """Check the real current baseline without saving feedback or starting work."""
    with db()() as session:
        run = session.get(BuildRun, build_id)
        if run is None:
            raise KeyError("build")
        _app(session, run.app_id, actor)  # access precedes even a refusal reason
        try:
            _, run, artifact, _, _ = baseline(session, build_id, actor=actor)
        except BuildError as error:
            return {"build_id": build_id, "available": False, "reason": error.code}
        return {"build_id": run.id, "available": True, "reason": "",
                "source_artifact_id": artifact.id,
                "report_digest": json.loads(run.spec)["lifecycle"]["review"]["report_digest"]}


def public(row):
    value = json.loads(row.text)
    return {"id": row.id, "state": row.outcome, "instruction": value["instruction"],
            **{key: value[key] for key in ("build_id", "source_artifact_id", "report_digest")},
            "next_gate": "review_correction_estimate", "created_at": row.created_at.isoformat() + "Z"}


def request(build_id, *, actor, source_artifact_id, report_digest, instruction):
    instruction = instruction.strip()
    if not 8 <= len(instruction) <= 2000 or any(ord(c) < 32 and c not in "\n\t" for c in instruction):
        raise BuildError("private_correction_instruction_invalid", status=400)
    with db()() as session:
        app, run, artifact, capture, plan = baseline(session, build_id, actor=actor)
        value = _binding(run, artifact, capture, plan)
        if (source_artifact_id, report_digest) != (artifact.id, value["report_digest"]):
            raise BuildError("private_correction_baseline_changed")
        value.update(instruction=instruction, actor=actor)
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        key = "fr_" + hashlib.sha256(text.encode()).hexdigest()[:32]
        prior = session.get(FounderRequest, key)
        if prior:
            if (prior.source, prior.workspace_id, prior.venture_id, prior.related_id, prior.text) != (
                    KIND, app.workspace_id, app.venture_id, app.id, text):
                raise BuildError("private_correction_request_conflict")
            return public(prior)
        row = FounderRequest(id=key, workspace_id=app.workspace_id, venture_id=app.venture_id,
            source=KIND, text=text, intent="correct_private_static_build", classification="revision_request",
            outcome="pending", related_id=app.id, created_at=naive_utc())
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            prior = session.get(FounderRequest, key)
            if not prior or prior.text != text or prior.source != KIND:
                raise BuildError("private_correction_request_conflict")
            return public(prior)
        return public(row)


def build_inputs(session, app, request_id, *, actor, read_only=False):
    row = session.get(FounderRequest, request_id, with_for_update=not read_only)
    if (not row or row.source != KIND or row.outcome != "pending"
            or (row.workspace_id, row.venture_id, row.related_id) != (app.workspace_id, app.venture_id, app.id)):
        raise KeyError("private_correction")
    value = json.loads(row.text)
    if value.get("actor") != actor:
        raise KeyError("private_correction")
    owned, run, artifact, capture, plan = baseline(session, value["build_id"], actor=actor, read_only=read_only)
    if owned.id != app.id or any(value.get(key) != expected for key, expected in _binding(run, artifact, capture, plan).items()):
        raise BuildError("private_correction_baseline_changed")
    inputs = deepcopy(json.loads(run.spec)["inputs"])
    inputs["source"] = {"kind": "artifact", "id": artifact.id, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes}
    inputs["revision"] = {"kind": KIND, "request_id": row.id, "build_id": run.id,
        "source_artifact_id": artifact.id, "source_sha256": artifact.sha256,
        "source_manifest_digest": capture["manifest_digest"]}
    from app.hosting.revisions import _public_analytics_script
    inputs["product_context"].update(revision_instruction=value["instruction"], revision_request_id=row.id,
                                    analytics_beacon_script=_public_analytics_script(app.id, session=session, read_only=read_only))
    if len(json.dumps(inputs["product_context"]).encode()) > 20000:
        raise BuildError("private_correction_instruction_invalid", status=400)
    return inputs


def assert_publication(session, app, run, artifact):
    from app.hosting.service import HostingError, _accepted_source
    revision = json.loads(run.spec)["inputs"]["revision"]
    row = session.get(FounderRequest, revision["request_id"])
    if not row or row.source != KIND or row.outcome != "approved" or (row.workspace_id, row.venture_id, row.related_id) != (app.workspace_id, app.venture_id, app.id):
        raise HostingError("private_correction_not_approved")
    value = json.loads(row.text)
    plan = session.get(ExecutionPlan, value["plan_id"])
    if (not plan or (plan.workspace_id, plan.venture_id) != (app.workspace_id, app.venture_id)
            or digest(json.loads(plan.structured)) != value["plan_digest"]
            or session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == app.venture_id,
                ExecutionPlan.version > plan.version, ExecutionPlan.state.in_(["approved", "founder_review"])).limit(1))):
        raise HostingError("private_correction_plan_changed")
    later = session.scalars(select(BuildRun).where(BuildRun.app_id == app.id,
        BuildRun.workspace_id == app.workspace_id, BuildRun.venture_id == app.venture_id,
        BuildRun.runtime_version == 1).order_by(BuildRun.created_at.desc(), BuildRun.id.desc()).limit(100)).all()
    latest = next((r for r in later if json.loads(r.spec or "{}").get("lifecycle", {})
        .get("review", {}).get("status") == "accepted"), None)
    if not latest or latest.id != run.id:
        raise HostingError("private_correction_baseline_changed")
    if any(revision.get(key) != value.get(key) for key in ("build_id", "source_artifact_id", "source_sha256", "source_manifest_digest")):
        raise HostingError("private_correction_baseline_changed")
    _, source, _, capture, _ = _accepted_source(session, app, value["build_id"], historical=True)
    if (source.id, source.sha256, capture.get("manifest_digest")) != (value["source_artifact_id"], value["source_sha256"], value["source_manifest_digest"]):
        raise HostingError("private_correction_baseline_changed")
    # Private admission cannot replace a subsequently published predecessor.
    live = session.scalar(select(Deployment).where(Deployment.app_id == app.id,
        Deployment.workspace_id == app.workspace_id, Deployment.venture_id == app.venture_id,
        Deployment.state == "live").limit(1))
    if live is not None and live.build_artifact_id != artifact.id:
        raise HostingError("private_correction_baseline_changed")


def estimate(build_id, *, actor, request_id):
    from app.builder.execution import _quote
    with db()() as session:
        app, _, _, _, _ = baseline(session, build_id, actor=actor)
        inputs = build_inputs(session, app, request_id, actor=actor)
        if inputs["revision"]["build_id"] != build_id:
            raise BuildError("private_correction_baseline_changed")
        return _quote(session, app, actor, inputs, source_artifact_id=inputs["source"]["id"],
                      revision_request_id=request_id)
