"""A public static source review is not its separately approved publication.

Only the typed managed-publication/conversion contract creates this extra gate;
private projects and revision predecessors acquire no publication obligation.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.builder.model_proxy import digest
from app.core.fsm import advance
from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
from core.static_qa_contracts import Contract


def publication_required(session, run, spec):
    if (run.trigger not in {"coding_source_import", "pagebuilder_source_qa"} or spec.get("lifecycle_version") != 3
            or spec.get("artifact_profile") != "static_landing_v1"
            or spec.get("inputs", {}).get("revision")):
        return False
    inputs = spec.get("inputs", {})
    if inputs.get("milestone", {}).get("kind") != "landing":
        return False
    plan_id = inputs.get("plan_id")
    plan = session.get(ExecutionPlan, plan_id) if plan_id else None
    if not plan or (plan.workspace_id, plan.venture_id) != (run.workspace_id, run.venture_id):
        return False
    try:
        context = json.loads(plan.structured)["_runtime"]["context"]
        publication, conversion = context["managed_publication"], context["managed_conversion"]
        contract = Contract.model_validate(inputs["task"]["landing_expectations"]["contract"])
        from app.conversion.waitlist import public_url
        return (publication.get("kind") == "reviewed_static_publication"
                and publication.get("review_path") == f"/v/{run.venture_id}/site"
                and conversion.get("kind") == "venture_waitlist"
                and conversion.get("url") == public_url(run.venture_id)
                and any(action.kind == "external_link" and action.page == "index.html"
                        and action.target == conversion["url"] for action in contract.actions))
    except (KeyError, TypeError, ValueError):
        return False


def pending(run, task, spec):
    review = spec["lifecycle"]["review"]
    return {"schema_version": 1, "kind": "reviewed_static_publication", "state": "pending_publication",
            "workspace_id": run.workspace_id, "venture_id": run.venture_id, "app_id": run.app_id,
            "build_run_id": run.id, "plan_id": spec["inputs"]["plan_id"],
            "plan_task_id": task.id, "milestone_id": task.milestone_id,
            "source_artifact_id": run.artifact_id, "report_digest": review["report_digest"],
            "inputs_digest": digest(spec["inputs"]),
            "requirements": ["verified_public_source", "verified_public_waitlist_submission"]}


def complete_at_publication(session, run, spec, deployment, conversion):
    """Called in the verified hosting commit, after exact public source readback.

    Missing markers are historical/private flows, never a reason to rewrite old
    plan rows. A present marker must match every owned source/plan receipt.
    """
    saved = spec.get("lifecycle", {}).get("task_completion")
    if saved is None:
        return
    from app.hosting.service import HostingError
    task = session.get(PlanTask, run.plan_task_id, with_for_update=True)
    milestone = session.get(PlanMilestone, task.milestone_id, with_for_update=True) if task else None
    expected = pending(run, task, spec) if task else None
    if (saved != expected or not publication_required(session, run, spec)
            or not task or task.state != "waiting_for_founder" or task.build_run_id != run.id
            or (task.workspace_id, task.venture_id) != (run.workspace_id, run.venture_id)
            or not milestone or (milestone.plan_id, milestone.workspace_id, milestone.venture_id, milestone.kind)
            != (saved["plan_id"], run.workspace_id, run.venture_id, "landing")
            or deployment.state != "live" or not deployment.live_at
            or (deployment.app_id, deployment.workspace_id, deployment.venture_id, deployment.build_artifact_id,
                deployment.target) != (run.app_id, run.workspace_id, run.venture_id, run.artifact_id, "supymem_static")
            or not conversion or (conversion.get("build_id"), conversion.get("artifact_id"))
            != (run.id, run.artifact_id)
            or not conversion.get("entry_id") or not conversion.get("verified_at")
            or conversion.get("contract_digest") != spec["lifecycle"]["review"].get("contract_digest")):
        raise HostingError("hosting_task_completion_receipt_changed")
    advance(session, task, "done", actor="hosting", reason="Accepted source and exact public publication verified")
    task.completed_at = deployment.live_at
    session.flush()
    if milestone.state == "review" and not session.scalar(select(PlanTask.id).where(
            PlanTask.milestone_id == milestone.id, PlanTask.state != "done").limit(1)):
        advance(session, milestone, "done", actor="hosting", reason="Accepted static landing and public conversion verified")
        milestone.completed_at = deployment.live_at
    spec["lifecycle"]["task_completion"] = {**saved, "state": "completed", "deployment_id": deployment.id,
        "public_url": deployment.url, "verified_at": deployment.live_at.isoformat() + "Z",
        "conversion_receipt": conversion}
    spec["phase"] = spec["lifecycle"]["phase"] = "publication_verified"
    run.spec = json.dumps(spec, sort_keys=True)
