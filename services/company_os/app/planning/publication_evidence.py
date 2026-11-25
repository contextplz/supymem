"""Venture-scoped public-site evidence for plan read models.

Publication can happen outside plan execution. This records what is observable;
it never changes a plan task's acceptance state.
"""

from sqlalchemy import select

import json

from app.db.models import Job, PlannedTask, SiteVersion, Venture
from app.db.supymem_models import Deployment, ExecutionPlan, FounderApp, PlanMilestone, PlanTask
from app.org.summary import published_site_is_public


def landing_build_result(session, run, spec) -> dict | None:
    if not (spec.get("artifact_profile") == "static_landing_v1"
            or run.trigger == "pagebuilder_source_qa"
            or spec.get("inputs", {}).get("milestone", {}).get("kind") == "landing"):
        return None
    deployment = session.scalar(select(Deployment).join(FounderApp, FounderApp.id == Deployment.app_id).where(
        Deployment.workspace_id == run.workspace_id, Deployment.venture_id == run.venture_id,
        Deployment.app_id == run.app_id, Deployment.target == "supymem_static",
        Deployment.state == "live", FounderApp.workspace_id == run.workspace_id,
        FounderApp.venture_id == run.venture_id, FounderApp.deleted_at.is_(None),
    ).order_by(Deployment.live_at.desc(), Deployment.version.desc()).limit(1))
    live_url = deployment.url if deployment and published_site_is_public(True, deployment.url) else None
    available = bool(live_url or (run.state == "succeeded" and spec.get("artifact_verified")
                                  and spec.get("qa_state") == "passed"))
    job_id = spec.get("source_import", {}).get("identity", {}).get("scope", {}).get("job_id")
    if not available and job_id:
        candidate = session.get(Job, job_id)
        if candidate and (candidate.kind, candidate.workspace_id, candidate.venture_id) == (
                "pagebuilder.build", run.workspace_id, run.venture_id):
            from app.pagebuilder.api import _state
            try:
                doc = json.loads(candidate.payload_json)
                available = _state(candidate, doc)[0] == "ready"
            except (KeyError, TypeError, ValueError, AttributeError):
                available = False
    stopped = run.state in {"failed", "cancelled", "rejected"}
    has_report = spec.get("lifecycle", {}).get("completion", {}).get("verified_report") is True
    from app.tasks.contracts import blocker
    problem = blocker(run.error_code or spec.get("reconciliation_reason") or "pagebuilder_publication_refused")
    return {"available": available, "live_url": live_url,
            "qa_report_available": has_report,
            "qa_note": ("Your saved page is still available. " +
                        ("Review the recorded failed checks or build a new version." if has_report
                         and spec.get("lifecycle", {}).get("completion", {}).get("report_complete") is not False
                         and spec.get("qa_state") not in {"passed", "not_run"} else
                         problem["reason"] + " " + problem["unblock_action"])
                        if available and stopped else
                        "Final checks and publishing are pending; this preview is private."
                        if available and not live_url and spec.get("qa_state") != "passed" else None)}


def public_site_evidence(session, *, workspace_id: str, venture_id: str) -> dict | None:
    deployment = session.scalar(select(Deployment).where(
        Deployment.workspace_id == workspace_id,
        Deployment.venture_id == venture_id,
        Deployment.target == "supymem_static",
        Deployment.state == "live",
    ).order_by(Deployment.live_at.desc(), Deployment.version.desc()).limit(1))
    if deployment and published_site_is_public(True, deployment.url):
        return {"source": "deployment", "id": deployment.id, "url": deployment.url,
                "observed_at": deployment.live_at.isoformat() if deployment.live_at else None}

    # Legacy first-look versions have only workspace scope. They can identify
    # a venture safely only while that workspace contains exactly one venture.
    sole_venture = session.scalars(select(Venture.id).where(
        Venture.workspace_id == workspace_id).limit(2)).all()
    if sole_venture != [venture_id]:
        return None
    version = session.scalar(select(SiteVersion).where(
        SiteVersion.workspace_id == workspace_id,
        SiteVersion.published == 1,
    ).order_by(SiteVersion.version.desc()).limit(1))
    # A non-null newer binding is authoritative. A foreign current row must
    # not be relabelled as this venture's legacy publication or skipped in
    # favor of an older published version.
    if version and version.venture_id in (None, venture_id) and published_site_is_public(True, version.url):
        return {"source": "site_version", "id": version.id, "url": version.url,
                "observed_at": version.created_at.isoformat() if version.created_at else None}
    return None


def landing_publication_for_task(session, task: PlannedTask, *, workspace_id: str) -> dict | None:
    """Attach publication only to a task bound to the same landing milestone.

    This is observation, not task acceptance: a live page does not prove the
    task's copy, conversion, QA, or founder review criteria were satisfied.
    """
    if not task.source.startswith("approved_plan:"):
        return None
    plan_id = task.source.removeprefix("approved_plan:")
    plan = session.get(ExecutionPlan, plan_id)
    if not plan or (plan.workspace_id, plan.venture_id) != (workspace_id, task.venture_id) or plan.superseded_by:
        return None
    try:
        tags = json.loads(task.tags_json or "[]")
    except (TypeError, ValueError):
        return None
    ids = [tag.removeprefix("plan_task:") for tag in tags
           if isinstance(tag, str) and tag.startswith("plan_task:")]
    if len(ids) != 1:
        return None
    plan_task = session.get(PlanTask, ids[0])
    milestone = session.get(PlanMilestone, plan_task.milestone_id) if plan_task else None
    if not plan_task or not milestone or (
        plan_task.workspace_id, plan_task.venture_id, milestone.plan_id,
        milestone.workspace_id, milestone.venture_id, milestone.kind,
    ) != (workspace_id, task.venture_id, plan_id, workspace_id, task.venture_id, "landing"):
        return None
    return public_site_evidence(session, workspace_id=workspace_id, venture_id=task.venture_id)
