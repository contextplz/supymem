"""Read-only, exact-order source QA and publication facts for chat."""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select


def snapshot(session, source):
    from app.builder import qa_receipts
    from app.build.subscription_static_controller import verified_historical_source_qa, verified_source_qa
    from app.db.supymem_models import BuildRun, Deployment, FounderApp, PlanTask, QaRun

    result = {"qa_status": "not_run", "source_accepted": False, "source_published": False}
    runs = session.scalars(select(BuildRun).where(
        BuildRun.workspace_id == source.workspace_id, BuildRun.venture_id == source.venture_id,
        BuildRun.app_id == source.app_id, BuildRun.plan_task_id == source.plan_task_id,
        BuildRun.trigger == "coding_source_import", BuildRun.runtime_version == 1,
    ).order_by(BuildRun.created_at.desc(), BuildRun.id.desc())).yield_per(50)
    for run in runs:
        try:
            spec = json.loads(run.spec)
            identity = spec["source_import"]["identity"]
            scope = identity["scope"]
            if (spec.get("lifecycle_version") != 3 or any(scope.get(key) != getattr(source, key) for key in
                    ("workspace_id", "venture_id", "app_id", "actor_id", "device_id", "order_id", "execution_id"))
                    or scope.get("provider") != source.provider
                    or identity["task_snapshot"].get("task_id") != source.plan_task_id
                    or identity["source"] != {"sha256": source.sha256, "manifest_digest": source.manifest_sha256,
                        "size_bytes": source.size_bytes, "storage_receipt": json.loads(source.receipt_json)}):
                continue
            result.update(build_run_id=run.id, build_state=run.state,
                          review_href=f"/v/{source.venture_id}/builds?app={source.app_id}&run={run.id}")
            task = session.get(PlanTask, source.plan_task_id)
            if task and (task.workspace_id, task.venture_id) == (source.workspace_id, source.venture_id):
                result["plan_task_state"] = task.state
            life = spec["lifecycle"]
            qa = session.get(QaRun, life.get("qa_id")) if life.get("qa_id") else None
            if not qa:
                result["qa_status"] = "unverified" if run.closed_at else "pending"
                return result
            view = qa_receipts.serialize(qa)
            artifact, capture, _scope = qa_receipts._source(session, SimpleNamespace(run=run), spec)
            from core.static_qa_contracts import Contract
            contract = Contract.model_validate(spec["inputs"]["task"]["landing_expectations"]["contract"])
            bound = bool(view["verified_report"] and qa.finished_at and run.closed_at
                and (qa.workspace_id, qa.venture_id, qa.app_id, qa.build_run_id, qa.generation,
                     qa.source_artifact_id, qa.source_sha256, qa.source_manifest_digest, qa.contract_digest)
                == (source.workspace_id, source.venture_id, source.app_id, run.id, spec["generation"],
                    artifact.id, artifact.sha256, capture["manifest_digest"], contract.digest)
                and all(type(value) is int and value >= 0 for value in (qa.passed, qa.failed, qa.unsupported)))
            passed = bound and verified_historical_source_qa(session, run, spec)
            failed = bound and view["outcome"] == "failed" and run.state == "failed"
            result["qa_status"] = "passed" if passed else "failed" if failed else "unverified" if run.closed_at else "pending"
            if not (passed or failed):
                return result
            result.update(qa_id=qa.id, qa_passed=qa.passed, qa_failed=qa.failed,
                          qa_unsupported=qa.unsupported, qa_report_digest=qa.report_digest)
            if not passed:
                return result
            result["current_scope"] = bool(verified_source_qa(session, run, spec))
            app = session.get(FounderApp, source.app_id)
            if not app or app.deleted_at:
                return result
            from app.hosting.service import _accepted_source, HostingError
            try:
                _accepted_source(session, app, run.id, historical=True)
            except (HostingError, KeyError, TypeError, ValueError, AttributeError):
                return result
            result["source_accepted"] = True
            deployment = session.scalar(select(Deployment).where(
                Deployment.workspace_id == source.workspace_id, Deployment.venture_id == source.venture_id,
                Deployment.app_id == source.app_id, Deployment.build_artifact_id == artifact.id,
                Deployment.target == "supymem_static", Deployment.state == "live",
            ).order_by(Deployment.version.desc()).limit(1))
            if deployment:
                from app.build.subscription_static_baseline import _identity
                try:
                    _identity(session, app, deployment)
                except (KeyError, TypeError, ValueError, AttributeError):
                    # Publication uncertainty cannot erase independently valid QA.
                    pass
                else:
                    result.update(source_published=True, publication_url=deployment.url,
                                  deployment_id=deployment.id)
            return result
        except (KeyError, TypeError, ValueError, AttributeError):
            # A malformed candidate never borrows another run's success.
            if result.get("build_run_id") == run.id:
                result["qa_status"] = "unverified"
                return result
    return result


def describe(coding):
    """Project only closed, bounded facts, never a whole-product guarantee."""
    qa = coding.get("source_qa") or {}
    status = qa.get("qa_status")
    if status in {"passed", "failed"}:
        text = (f"Independent static runtime-QA {status} for this exact saved source: "
                f"{qa['qa_passed']} passed, {qa['qa_failed']} failed, {qa['qa_unsupported']} unsupported "
                f"(report SHA-256: {qa['qa_report_digest']}). ")
        if status == "passed" and qa.get("current_scope") is not True:
            text += "This is historical QA proof; the current plan/source approval scope needs rechecking. "
    elif status == "pending":
        text = "Independent static runtime-QA for this exact source is pending; no pass is recorded. "
    elif status == "unverified":
        text = "The saved QA attempt has no verified source-bound result; do not treat it as a pass. "
    else:
        text = "Independent QA and hosting not attested for this source in the saved summary. "
    text += "Source accepted. " if qa.get("source_accepted") is True else "Source acceptance is not verified. "
    if qa.get("source_published") is True:
        text += f"This exact source has a verified live publication at {qa['publication_url']}. "
    else:
        text += "Publication of this exact source is not verified; another live site is not its publication. "
    if qa.get("plan_task_state"):
        text += f"Plan task state: {qa['plan_task_state']}. "
    return text
