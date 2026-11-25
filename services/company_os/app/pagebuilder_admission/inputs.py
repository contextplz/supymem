"""Current paid candidate, reviewed landing contract and immutable source identity."""
from __future__ import annotations

import json
import os
import re

from sqlalchemy import select

from app.builder import execution
from app.builder.model_proxy import BuildModelError, digest
from app.build import subscription_static_baseline as baseline
from app.db.models import Venture, VentureDocument
from app.db.supymem_models import BuildRun, ExecutionPlan, IdeaRevision, IdeaSession, PlanTask
from app.pagebuilder import api, native_reference as reference
from core.static_qa_contracts import Contract
from . import ORIGIN, owns
from .projection import POLICY as PROJECTION_POLICY

NO_PLAN_CONTRACT = "pagebuilder_confirmed_revision_v1"


def no_plan(run, spec):
    """Recognize only the bound server contract; current source proof is still required."""
    try:
        values, identity = spec["inputs"], spec["source_import"]["identity"]
        scope = identity["scope"]
        return (owns(run, spec) and run.plan_task_id is None
            and values["contract_source"] == NO_PLAN_CONTRACT
            and values["plan_id"] is None and values["plan_task_id"] is None
            and scope["plan_id"] is None and scope["plan_task_id"] is None
            and (scope["workspace_id"], scope["venture_id"], scope["app_id"], scope["actor_id"], scope["provider"])
                == (run.workspace_id, run.venture_id, run.app_id, spec["actor"], "pagebuilder")
            and scope["revision_id"] == values["product_context"]["revision_id"]
            and identity["inputs_digest"] == digest(values))
    except (KeyError, TypeError, AttributeError):
        return False


def _server_inputs(session, app, doc, selected):
    """Reuse the controller's reviewed copy/actions/artwork, never generated HTML as authority."""
    from app.pagebuilder.models import PageInput, PagePlan, RasterImage
    from app.pagebuilder.qa import qa_contract
    from app.product_analytics.beacon import public_events_url

    revision_id, live = api.snapshot(session, app.venture_id, app.workspace_id)
    frozen = PageInput.model_validate(doc["_runtime"]["input"])
    if revision_id != doc["revision_id"] or PageInput.model_validate(live) != frozen:
        raise BuildModelError("pagebuilder_admission_inputs_changed")
    try:
        bundle = doc["result"]["bundles"][selected.variant]
        contract = qa_contract(frozen, PagePlan.model_validate(bundle["plan"]),
            [RasterImage.model_validate(image) for image in bundle.get("imagery", [])])
        manifest = bundle["manifest"]
        if Contract.model_validate(manifest["qa_contract"]) != contract:
            raise ValueError("contract_changed")
        beacon = manifest["analytics_beacon"]
        if (beacon.get("controller"), beacon.get("app_id"), beacon.get("endpoint")) != (
                "platform", app.id, public_events_url()) or not re.fullmatch(r"[a-f0-9]{64}", beacon.get("script_sha256", "")):
            raise ValueError("beacon_changed")
        capture = manifest["artifact_capture"]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise BuildModelError("pagebuilder_admission_contract_changed") from exc
    return {"contract_source": NO_PLAN_CONTRACT, "plan_id": None, "plan_task_id": None, "plan_version": None,
        "dependency_digest": digest({"revision_id": revision_id, "input": frozen.model_dump(mode="json")}),
        "product_context": {"revision_id": revision_id, "idea": frozen.idea, "product_name": frozen.name},
        "pagebuilder_input": frozen.model_dump(mode="json"), "analytics_beacon": beacon,
        "task": {"landing_expectations": {"contract": contract.model_dump(mode="json")}},
        "milestone": {"kind": "landing", "key": "server_page", "title": "Server-built landing page"},
        "source": {"kind": "pagebuilder_candidate", "job_id": selected.job_id, "variant": selected.variant,
            "sha256": capture["sha256"], "manifest_digest": capture["manifest_digest"], "size_bytes": capture["size_bytes"]}}


def authority(venture_id, caller):
    if os.getenv("SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED", "").lower() not in {"1", "true"}:
        raise KeyError("pagebuilder_admission_not_found")
    if getattr(caller, "role", None) != "user" or getattr(caller, "legacy", True) or not getattr(caller, "sub", None):
        raise PermissionError("pagebuilder_admission_founder_required")
    return api.authority(venture_id, caller, write=True)


def current(session, venture_id, caller, selection, *, active_run_id=None):
    wid, actor = authority(venture_id, caller)
    selected = reference.CandidateSelection.model_validate({k: selection[k] for k in ("job_id", "variant")})
    row, doc = reference._job(session, selected.job_id)
    if (row.workspace_id, row.venture_id, doc.get("_actor_subject")) != (wid, venture_id, actor):
        raise KeyError("pagebuilder_admission_not_found")
    app = execution._app(session, doc["_runtime"]["analytics_app_id"], actor, mutate=True)
    plan_id, task_id = selection["plan_id"], selection["plan_task_id"]
    if (plan_id is None) != (task_id is None):
        raise BuildModelError("pagebuilder_admission_inputs_changed")
    task = session.get(PlanTask, task_id) if task_id is not None else None
    plan = session.get(ExecutionPlan, plan_id) if plan_id is not None else None
    idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == wid,
        IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned").order_by(IdeaSession.created_at.desc()).limit(1))
    revision = session.get(IdeaRevision, doc["revision_id"])
    venture = session.get(Venture, venture_id)
    if (app.workspace_id != wid or app.venture_id != venture_id or venture is None or venture.archived_at or venture.stage == "purged"
            or idea is None or idea.state != "confirmed" or idea.current_revision_id != doc["revision_id"]
            or idea.confirmed_revision_id != doc["revision_id"] or revision is None
            or (revision.workspace_id, revision.venture_id, revision.session_id) != (wid, venture_id, idea.id)):
        raise BuildModelError("pagebuilder_admission_inputs_changed")
    frozen = doc["_runtime"]["input"]
    if (frozen.get("name"), frozen.get("idea"), frozen.get("clarified_answers")) != (
            venture.name, revision.summary or venture.raw_idea, json.loads(revision.structured)):
        raise BuildModelError("pagebuilder_admission_inputs_changed")
    if plan_id is None:
        values = _server_inputs(session, app, doc, selected)
    else:
        if (task is None or (task.workspace_id, task.venture_id) != (wid, venture_id)
                or plan is None or (plan.workspace_id, plan.venture_id, plan.revision_id) != (wid, venture_id, doc["revision_id"])):
            raise BuildModelError("pagebuilder_admission_inputs_changed")
        values = execution._inputs(session, app, plan.id, task.id, None, active_run_id=active_run_id, source_import=True)
    if values["milestone"]["kind"] != "landing":
        raise BuildModelError("pagebuilder_admission_landing_required")
    Contract.model_validate(values["task"]["landing_expectations"]["contract"])
    if active_run_id:
        run = session.get(BuildRun, active_run_id)
        spec = json.loads(run.spec) if run else {}
        if not run or not owns(run, spec) or (run.app_id, run.workspace_id, run.venture_id, run.plan_task_id) != (app.id, wid, venture_id, task_id):
            raise BuildModelError("pagebuilder_admission_scope_changed")
        publication = spec["inputs"]["publication_baseline"]
        baseline.assert_current(session, app, publication, accepted_artifact_id=run.artifact_id)
    else:
        publication = baseline.reviewed(session, wid, venture_id)
        if publication["app_id"] != app.id:
            raise BuildModelError("pagebuilder_admission_app_ambiguous")
    values["publication_baseline"] = publication
    binding, _archive = reference._encode(row, doc, selected.variant)
    saved = session.get(VentureDocument, reference._reference_id(row.id, selected.variant))
    stored = json.loads(saved.provenance_json) if saved else {}
    if (not saved or saved.kind != reference.KIND or saved.venture_id != venture_id or stored.get("state") != "stored"
            or stored.get("binding") != binding or not isinstance(stored.get("receipt"), dict)
            or not stored["receipt"].get("version_id") or stored["receipt"]["version_id"] == "null"):
        raise BuildModelError("pagebuilder_admission_source_unavailable")
    identity = {"schema_version": 1, "origin": ORIGIN,
        "projection_policy": PROJECTION_POLICY, "hostname": publication["hostname"],
        "scope": {"workspace_id": wid, "venture_id": venture_id, "app_id": app.id, "actor_id": actor,
            "caller_subject": caller.sub, "provider": "pagebuilder", "job_id": row.id, "variant": selected.variant,
            "revision_id": doc["revision_id"], "plan_id": plan_id, "plan_task_id": task_id},
        "candidate": binding, "receipt_digest": digest(doc["_runtime"]["operations"]),
        "inputs_digest": digest(values),
        "source": {"sha256": binding["source_sha256"], "manifest_digest": binding["source_manifest_digest"],
            "size_bytes": binding["source_size_bytes"], "storage_receipt": stored["receipt"]},
        "contract_digest": digest(values["task"]["landing_expectations"])}
    return {"identity": identity, "fingerprint": digest(identity), "inputs": values}, app


# Extra QA attempts per exact source when QA itself could not run; page-content verdicts stay final.
QA_INFRA_RETRIES = 3
# Infrastructure reasons: the QA task, its launch, storage or a deadline failed, not the page.
INFRA_REASONS = {"build_budget_expired", "subscription_static_qa_deadline", "subscription_static_import_deadline",
    "pagebuilder_admission_storage_failed", "subscription_static_source_import_failed", "build_runtime_deadline",
    "build_compute_usage_unverified", "build_infrastructure_usage_unverified", "build_task_control_unavailable"}


def qa_attempt(spec):
    attempt = spec.get("qa_attempt", 0)
    return attempt if type(attempt) is int and attempt >= 0 else 0


def qa_retry(run, spec):
    """The next QA attempt for this exact source, or None when the latest verdict is final."""
    reason = getattr(run, "error_code", None) or ""
    completion = spec.get("lifecycle", {}).get("completion", {})
    infra = (reason in INFRA_REASONS or reason.startswith("qa_")) and reason not in {
        "qa_checks_failed", "qa_receipt_scope_invalid", "qa_task_binding_changed", "qa_task_response_integrity",
        "qa_approved_inputs_changed", "qa_admission_revoked"}
    if (owns(run, spec) and run.state == "failed" and run.closed_at and infra
            and (completion.get("verified_report") is False or completion.get("report_complete") is False)
            and qa_attempt(spec) < QA_INFRA_RETRIES):
        return qa_attempt(spec) + 1
    return None


def prior(session, venture_id, caller, selected):
    """The latest owned committed intent for this source; a read recovery, never another QA attempt."""
    wid, actor = authority(venture_id, caller)
    row, doc = reference._job(session, selected["job_id"])
    if (row.workspace_id, row.venture_id, doc.get("_actor_subject")) != (wid, venture_id, actor):
        raise KeyError("pagebuilder_admission_not_found")
    binding, _archive = reference._encode(row, doc, selected["variant"])
    found = []
    for run in session.scalars(select(BuildRun).where(BuildRun.workspace_id == wid, BuildRun.venture_id == venture_id, BuildRun.trigger == "pagebuilder_source_qa")):
        spec = json.loads(run.spec)
        identity = spec["source_import"]["identity"]
        if owns(run, spec) and selection(identity) == selected and identity["scope"]["actor_id"] == actor and identity["candidate"] == binding:
            found.append((qa_attempt(spec), run))
    if not found:
        return None
    run = max(found, key=lambda item: item[0])[1]
    execution._app(session, run.app_id, actor, mutate=True)
    return run


def selection(identity):
    return {k: identity["scope"][k] for k in ("job_id", "variant", "plan_id", "plan_task_id")}


def recheck(session, ctx, spec, *, check_rate=True):
    from types import SimpleNamespace
    from app.build import subscription_static_policy
    if not owns(ctx.run, spec):
        raise BuildModelError("pagebuilder_admission_origin_invalid")
    if check_rate:
        subscription_static_policy.validate(spec["runtime"])
    identity = spec["source_import"]["identity"]
    caller = SimpleNamespace(sub=identity["scope"]["caller_subject"], role="user", legacy=False)
    found, _app = current(session, ctx.run.venture_id, caller, selection(identity), active_run_id=ctx.run.id)
    if found != {"identity": identity, "fingerprint": ctx.run.request_digest, "inputs": spec["inputs"]}:
        raise BuildModelError("pagebuilder_admission_inputs_changed")
