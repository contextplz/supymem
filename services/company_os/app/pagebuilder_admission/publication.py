"""One preview Publish approval, continued only after settled independent QA."""
from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace

from sqlalchemy import select

from app.builder import execution
from app.builder.model_proxy import BuildModelError, digest
from app.core.jobs import FatalError, enqueue
from app.conversion import waitlist
from app.credits._txn import naive_utc
from app.db.models import Job
from app.db.supymem_models import BuildRun, Deployment
from app.hosting.service import HostingError, queue_accepted_build
from app.state import db
from . import admission, inputs, owns

KIND = "pagebuilder.publish"
RETRYABLE_FAILURES = {"waitlist_probe_recent": 429, "waitlist_public_submission_unconfirmed": 503,
                      "hosting_provider_unavailable": 503}


def _failure_reason(error):
    code = getattr(error, "code", None)
    return (code if isinstance(error, (HostingError, waitlist.WaitlistError))
        and code in RETRYABLE_FAILURES and getattr(error, "status", None) == RETRYABLE_FAILURES[code]
        else "publication_refused")


def _retryable(run, spec, approval, job, deployment):
    return bool(job and job.status == "failed_dead" and approval.get("failure_reason") in RETRYABLE_FAILURES
        and not deployment and not approval.get("deployment_id") and run.state == "succeeded" and run.closed_at
        and spec.get("qa_state") == "passed" and spec.get("execution_verified") is False
        and spec.get("source_import_verified") is True
        and spec.get("lifecycle", {}).get("review", {}).get("decision") != "reject")


def _binding(run, spec):
    identity = spec["source_import"]["identity"]
    return {"run_id": run.id, "source_fingerprint": run.request_digest,
            "identity_digest": digest(identity), "actor": spec["actor"],
            "caller_subject": identity["scope"]["caller_subject"],
            "hostname": identity["hostname"],
            "qa_ceiling_micro": spec["runtime"]["qa_compute_ceiling_micro"]}


def _approved(run, spec):
    approval = spec.get("publication_request")
    if (not owns(run, spec) or not isinstance(approval, dict) or type(approval.get("schema_version")) is not int
            or approval["schema_version"] != 1 or approval.get("binding") != _binding(run, spec)):
        raise BuildModelError("pagebuilder_publication_approval_changed")
    return approval


def _owned(session, venture_id, caller, run_id, *, lock=False):
    wid, actor = inputs.authority(venture_id, caller)
    query = select(BuildRun).where(BuildRun.id == run_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    run = session.scalar(query)
    spec = json.loads(run.spec) if run else {}
    if not run or not owns(run, spec) or (run.workspace_id, run.venture_id, spec.get("actor")) != (wid, venture_id, actor):
        raise KeyError("pagebuilder_publication_not_found")
    execution._app(session, run.app_id, actor, mutate=True)
    return run, spec


def _view(session, run, spec):
    approval = _approved(run, spec)
    deployment = session.scalar(select(Deployment).where(
        Deployment.app_id == run.app_id, Deployment.workspace_id == run.workspace_id,
        Deployment.venture_id == run.venture_id, Deployment.build_artifact_id == run.artifact_id,
        Deployment.target == "supymem_static").order_by(Deployment.version.desc()).limit(1)) if run.artifact_id else None
    live = bool(deployment and deployment.state == "live" and deployment.live_at is not None
                and deployment.url == "https://" + approval["binding"]["hostname"] + "/")
    job = session.get(Job, approval["job_id"]) if approval.get("job_id") else None
    failed = (run.state in {"failed", "cancelled"} or approval.get("error_code")
              or spec.get("lifecycle", {}).get("review", {}).get("decision") == "reject"
              or job and job.status in {"failed_dead", "failed", "poison", "cancelled"}
              or deployment and deployment.state in {"failed", "rolled_back"})
    resumable = _retryable(run, spec, approval, job, deployment)
    # QA that never ran for infrastructure reasons gets a fresh attempt on the next Publish click.
    again = bool(failed and not live and not resumable and inputs.qa_retry(run, spec))
    verified_report = spec.get("lifecycle", {}).get("completion", {}).get("verified_report") is True
    report_complete = spec.get("lifecycle", {}).get("completion", {}).get("report_complete")
    failure_code = (getattr(run, "error_code", None) or spec.get("reconciliation_reason") or approval.get("failure_code")
        or approval.get("failure_reason") or (job.last_error if job else None) or "pagebuilder_publication_refused")
    if not re.fullmatch(r"(?:pagebuilder|qa|build|subscription_static|waitlist|hosting)_[a-z_]{1,80}", failure_code):
        failure_code = "pagebuilder_publication_refused"
    from app.tasks.contracts import blocker
    problem = blocker(failure_code)
    reason = ("The page did not pass its final checks and stays private. Review the recorded failed checks, then build a new version."
        if verified_report and report_complete is not False and spec.get("qa_state") not in {"passed", "not_run"} else
        "Our final checks couldn't complete. Your saved preview stays private; no page-content failure was confirmed. " + problem["reason"]
        if (not verified_report or report_complete is False) and run.state == "failed" else problem["reason"] + " " + problem["unblock_action"])
    return {"run_id": run.id, "state": "live" if live else "failed" if failed else "publishing" if run.closed_at else "checking",
            "live_url": deployment.url if live else None,
            "error_code": "pagebuilder_publication_refused" if failed and not live else None,
            "failure_code": failure_code if failed and not live else None,
            "error_reason": reason if failed and not live else None,
            "qa_report_available": verified_report,
            "retryable": resumable or again,
            "next_action": None if live or not failed else "retry_publication" if resumable
                else "publish_again" if again else "build_new_version",
            "job_attempt": job.attempts if job else None}


def status(venture_id, caller, run_id):
    with db()() as session:
        run, spec = _owned(session, venture_id, caller, run_id)
        return _view(session, run, spec)


def current(venture_id, caller, selection):
    """Read recovery of this exact source; GET never approves or queues work."""
    with db()() as session:
        run = inputs.prior(session, venture_id, caller, selection)
        if run is None:
            return None
        spec = json.loads(run.spec)
        return _view(session, run, spec) if spec.get("publication_request") else None


def request(venture_id, caller, selection):
    quote = admission.estimate(venture_id, caller, selection)
    saved = quote.get("existing_run") or admission.admit(venture_id, caller, selection,
        quote_id=quote["quote_id"], quote_digest=quote["quote_digest"],
        approve_qa_ceiling_micro=quote["qa_ceiling_micro"])
    with db()() as session:
        admission._lock(session, venture_id, caller, selection)
        run, spec = _owned(session, venture_id, caller, saved["id"], lock=True)
        inputs.current(session, venture_id, caller, selection, active_run_id=run.id)
        if spec.get("publication_request"):
            _approved(run, spec)
        else:
            spec["publication_request"] = {"schema_version": 1, "binding": _binding(run, spec),
                "requested_at": naive_utc().isoformat() + "Z"}
            run.spec = json.dumps(spec, sort_keys=True)
        queue_if_requested(session, run, spec)
        session.commit()
        return _view(session, run, json.loads(run.spec))


def retry(venture_id, caller, run_id, *, expected_attempt):
    """One explicit retry of a known safe continuation; source, QA and paid receipts stay immutable."""
    if type(expected_attempt) is not int or expected_attempt < 0:
        raise BuildModelError("pagebuilder_publication_retry_unavailable")
    with db()() as session:
        admission._lock(session, venture_id, caller, None)
        run, spec = _owned(session, venture_id, caller, run_id, lock=True)
        approval = _approved(run, spec)
        job = session.scalar(select(Job).where(Job.id == approval.get("job_id"))
            .with_for_update().execution_options(populate_existing=True))
        binding = approval["binding"]
        saved = json.loads(job.payload_json) if job else {}
        if (not job or (job.kind, job.workspace_id, job.venture_id) != (KIND, run.workspace_id, run.venture_id)
                or any(saved.get(field) != value for field, value in {
                    "build_run_id": run.id, "approval_digest": digest(binding), "_actor_subject": binding["caller_subject"],
                    "workspace_id": run.workspace_id, "venture_id": run.venture_id}.items())):
            raise BuildModelError("pagebuilder_publication_retry_unavailable")
        # A lost response or stale duplicate never spends another attempt, even after this job runs again.
        consumed = approval.get("retried_attempt")
        if type(consumed) is int and expected_attempt <= consumed:
            return _view(session, run, spec)
        deployment = session.scalar(select(Deployment).where(Deployment.app_id == run.app_id,
            Deployment.workspace_id == run.workspace_id, Deployment.venture_id == run.venture_id,
            Deployment.build_artifact_id == run.artifact_id, Deployment.target == "supymem_static").limit(1))
        if job.attempts != expected_attempt or not _retryable(run, spec, approval, job, deployment):
            raise BuildModelError("pagebuilder_publication_retry_unavailable")
        inputs.recheck(session, SimpleNamespace(run=run), spec, check_rate=False)
        reviewed = spec["lifecycle"]["review"]
        if reviewed.get("decision") == "accept":
            from app.hosting.service import _accepted_source
            _accepted_source(session, execution._app(session, run.app_id, spec["actor"], mutate=True), run.id)
        else:
            from app.builder.review_readiness import require
            require(session, run, spec, actor=spec["actor"], source_artifact_id=run.artifact_id,
                    report_digest=reviewed.get("report_digest"))
        job.max_attempts = job.attempts + 1
        job.status = "scheduled"
        job.run_after = job.scheduled_for = naive_utc()
        job.started_at = job.finished_at = job.locked_at = job.locked_by = job.last_error = None
        approval["retried_attempt"] = expected_attempt
        approval.pop("error_code", None)
        approval.pop("failure_reason", None)
        approval.pop("failure_code", None)
        run.spec = json.dumps(spec, sort_keys=True)
        session.commit()
        return _view(session, run, spec)


def queue_if_requested(session, run, spec):
    """Called inside settled QA closure; it only persists a scoped continuation."""
    if not spec.get("publication_request"):
        return
    approval = _approved(run, spec)
    if approval.get("job_id") or run.state != "succeeded" or not run.closed_at:
        return
    if spec.get("qa_state") != "passed" or spec.get("execution_verified") is not False:
        raise BuildModelError("pagebuilder_publication_qa_unverified")
    binding = approval["binding"]
    approval["job_id"] = enqueue(session, KIND, {"build_run_id": run.id,
        "approval_digest": digest(binding), "_actor_subject": binding["caller_subject"]},
        workspace_id=run.workspace_id, venture_id=run.venture_id,
        dedupe_key=KIND + ":" + run.id, max_attempts=3)
    run.spec = json.dumps(spec, sort_keys=True)


def _lease(session, payload):
    job = session.scalar(select(Job).where(Job.id == payload.get("_job_id"))
                         .with_for_update().execution_options(populate_existing=True))
    if (not job or job.kind != KIND or job.status != "leased"
            or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt")):
        raise FatalError("pagebuilder_publication_lease_lost")
    saved = json.loads(job.payload_json)
    if (job.workspace_id, job.venture_id) != (payload.get("workspace_id"), payload.get("venture_id")):
        raise FatalError("pagebuilder_publication_scope_mismatch")
    if any(saved.get(field) != payload.get(field) for field in
           ("build_run_id", "approval_digest", "_actor_subject", "workspace_id", "venture_id")):
        raise FatalError("pagebuilder_publication_scope_mismatch")
    return job


def _worker_owned(session, payload, *, check_authority=True):
    run = session.scalar(select(BuildRun).where(BuildRun.id == payload.get("build_run_id"))
                         .with_for_update().execution_options(populate_existing=True))
    job = _lease(session, payload)
    spec = json.loads(run.spec) if run else {}
    if not run or (job.workspace_id, job.venture_id) != (run.workspace_id, run.venture_id):
        raise FatalError("pagebuilder_publication_scope_mismatch")
    approval = _approved(run, spec)
    binding = approval["binding"]
    if (approval.get("job_id") != job.id or payload.get("approval_digest") != digest(binding)
            or payload.get("_actor_subject") != binding["caller_subject"]):
        raise FatalError("pagebuilder_publication_scope_mismatch")
    caller = SimpleNamespace(sub=binding["caller_subject"], role="user", legacy=False)
    if check_authority:
        _owned(session, run.venture_id, caller, run.id)
    return run, spec, approval


def run(payload):
    """Apply the original source-bound founder approval through existing gates."""
    try:
        with db()() as session:
            build, spec, approval = _worker_owned(session, payload)
            reviewed = spec["lifecycle"]["review"]
            run_id, app_id, actor = build.id, build.app_id, approval["binding"]["actor"]
            artifact_id, report_digest = build.artifact_id, reviewed["report_digest"]
            try:
                waitlist.verified_receipt(session, build.venture_id, build.workspace_id)
                needs_probe = False
            except waitlist.WaitlistError as exc:
                if exc.code != "waitlist_verification_required":
                    raise
                needs_probe = True
            venture_id, workspace_id = build.venture_id, build.workspace_id
        if needs_probe:
            waitlist.probe(venture_id, workspace_id)
        execution.review(run_id, actor=actor, source_artifact_id=artifact_id,
                         report_digest=report_digest, decision="accept",
                         effect_fence=lambda session: _lease(session, payload))
        deployment = queue_accepted_build(app_id, run_id, actor=actor,
                                         effect_fence=lambda session: _lease(session, payload))
        with db()() as session:
            build, spec, approval = _worker_owned(session, payload)
            approval["deployment_id"] = deployment["id"]
            build.spec = json.dumps(spec, sort_keys=True)
            session.commit()
    except (KeyError, PermissionError, BuildModelError, execution.BuildError, HostingError, waitlist.WaitlistError) as exc:
        raw_code = getattr(exc, "code", None) or str(exc).partition(":")[0]
        if not re.fullmatch(r"[a-z][a-z_]{1,100}", raw_code):
            raw_code = "pagebuilder_publication_refused"
        logging.getLogger(__name__).warning("pagebuilder_publication_failed", extra={
            "job_id": payload.get("_job_id"), "error_code": raw_code})
        with db()() as session:
            build, spec, approval = _worker_owned(session, payload, check_authority=False)
            approval["error_code"] = "pagebuilder_publication_refused"
            approval["failure_reason"] = _failure_reason(exc)
            approval["failure_code"] = raw_code
            build.spec = json.dumps(spec, sort_keys=True)
            session.commit()
        raise FatalError("pagebuilder_publication_refused") from exc
