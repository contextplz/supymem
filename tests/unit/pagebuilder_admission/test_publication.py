"""Original Publish approval and transaction fences survive cancellation and read recovery."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.unit.pagebuilder_admission.test_boundary import modules as modules
from tests.unit.pagebuilder_admission.test_no_plan_inputs import no_plan_candidate as no_plan_candidate
from tests.unit.pagebuilder_admission.test_no_plan_inputs import setup as setup


@pytest.fixture()
def approved(modules):
    from app.pagebuilder_admission import publication
    from core.clock import utcnow

    run = SimpleNamespace(id="build_one", request_digest="a" * 64, trigger="pagebuilder_source_qa",
        workspace_id="workspace_one", venture_id="venture_one", app_id="app_one", artifact_id="artifact_one",
        state="succeeded", closed_at=utcnow())
    spec = {"lifecycle_version": 3, "source_origin": "pagebuilder_candidate_v1", "actor": "founder_one",
        "runtime": {"qa_compute_ceiling_micro": 100}, "qa_state": "passed", "execution_verified": False,
        "lifecycle": {"review": {"status": "pending"}},
        "source_import": {"identity": {"hostname": "reviewed.supymem.com",
            "scope": {"caller_subject": "founder_one"}, "source": {"sha256": "b" * 64}}}}
    spec["publication_request"] = {"schema_version": 1, "binding": publication._binding(run, spec)}
    run.spec = json.dumps(spec)
    return publication, run, spec


@pytest.mark.parametrize("fault", ["schema_bool", "schema_string", "actor", "source", "hostname", "ceiling", "native_origin"])
def test_original_publish_binding_cannot_be_reinterpreted(approved, fault):
    from app.builder.model_proxy import BuildModelError

    publication, run, spec = approved
    assert publication._approved(run, spec) is spec["publication_request"]
    changed = deepcopy(spec)
    if fault.startswith("schema"):
        changed["publication_request"]["schema_version"] = True if fault == "schema_bool" else "1"
    elif fault == "actor":
        changed["actor"] = "another_founder"
    elif fault == "source":
        changed["source_import"]["identity"]["source"]["sha256"] = "c" * 64
    elif fault == "hostname":
        changed["source_import"]["identity"]["hostname"] = "changed.supymem.com"
    elif fault == "ceiling":
        changed["runtime"]["qa_compute_ceiling_micro"] += 1
    else:
        run.trigger = "coding_source_import"
    with pytest.raises(BuildModelError, match="pagebuilder_publication_approval_changed"):
        publication._approved(run, changed)


@pytest.mark.parametrize("receipt", ["no_live_time", "wrong_url", "failed", "complete"])
def test_status_exposes_only_a_scoped_completed_live_receipt(approved, receipt):
    from core.clock import utcnow

    publication, run, spec = approved
    deployment = SimpleNamespace(state="failed" if receipt == "failed" else "live",
        url="https://foreign.invalid/" if receipt == "wrong_url" else "https://reviewed.supymem.com/",
        live_at=None if receipt == "no_live_time" else utcnow())
    session = SimpleNamespace(scalar=lambda _query: deployment, get=lambda *_args: None)
    result = publication._view(session, run, spec)
    assert (result["state"] == "live") is (receipt == "complete")
    assert result["live_url"] == (deployment.url if receipt == "complete" else None)
    assert result["error_code"] == ("pagebuilder_publication_refused" if receipt == "failed" else None)


def test_status_and_current_reads_never_capture_or_queue_approval(approved, monkeypatch):
    publication, run, spec = approved
    spec["publication_request"]["error_code"] = "private-provider-detail"
    run.spec = json.dumps(spec)
    before = run.spec
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("A status read attempted a publication effect")

    monkeypatch.setattr(publication, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(publication, "_owned", lambda *_args, **_kwargs: (run, json.loads(run.spec)))
    monkeypatch.setattr(publication.inputs, "prior", lambda *_args: run)
    monkeypatch.setattr(publication, "enqueue", forbidden)
    monkeypatch.setattr(publication, "queue_if_requested", forbidden)
    monkeypatch.setattr(publication.execution, "review", forbidden)
    monkeypatch.setattr(publication, "queue_accepted_build", forbidden)
    monkeypatch.setattr(publication.waitlist, "probe", forbidden)
    caller = SimpleNamespace(sub="founder_one", role="user", legacy=False)
    first = publication.status(run.venture_id, caller, run.id)
    assert publication.current(run.venture_id, caller, {}) == first
    assert first["state"] == "failed" and first["error_code"] == "pagebuilder_publication_refused"
    assert "private-provider-detail" not in str(first) and run.spec == before


@pytest.mark.parametrize("verified", [False, True])
def test_startup_failure_offers_publish_again_only_without_a_verified_page_verdict(approved, verified):
    publication, run, spec = approved
    run.state, run.error_code = "failed", "qa_task_stopped_before_report"
    spec["qa_attempt"] = 0
    spec["lifecycle"]["completion"] = {"verified_report": verified}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["state"] == "failed"
    assert view["retryable"] is (not verified)
    assert view["next_action"] == ("build_new_version" if verified else "publish_again")


@pytest.mark.parametrize("code", ["qa_task_stopped_before_report", "qa_runtime_deadline", "build_compute_usage_unverified",
    "build_budget_expired", "subscription_static_qa_deadline", "subscription_static_source_import_failed"])
def test_no_report_failures_are_not_content_verdicts(approved, code):
    publication, run, spec = approved
    run.state, run.error_code = "failed", code
    spec["qa_state"] = "failed"
    spec["lifecycle"]["completion"] = {"verified_report": False}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["failure_code"] == code and not view["qa_report_available"]
    assert view["next_action"] == "publish_again"
    assert "no page-content failure was confirmed" in view["error_reason"]


def test_exhausted_infra_attempts_never_claim_page_failed_checks(approved):
    publication, run, spec = approved
    run.state, run.error_code = "failed", "qa_runtime_deadline"
    spec["qa_attempt"] = publication.inputs.QA_INFRA_RETRIES
    spec["lifecycle"]["completion"] = {"verified_report": False}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["next_action"] == "build_new_version"
    assert "no page-content failure was confirmed" in view["error_reason"]
    assert "ran out of time" in view["error_reason"]


def test_verified_failed_report_offers_actual_findings_not_infra_retry(approved):
    publication, run, spec = approved
    run.state, run.error_code = "failed", "qa_checks_failed"
    spec["qa_state"] = "failed"
    spec["lifecycle"]["completion"] = {"verified_report": True}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["qa_report_available"] and view["next_action"] == "build_new_version"
    assert "recorded failed checks" in view["error_reason"]


def test_verified_incomplete_runtime_report_is_not_a_content_failure(approved):
    publication, run, spec = approved
    run.state, run.error_code = "failed", "qa_runtime_failed"
    spec["qa_state"] = "failed"
    spec["lifecycle"]["completion"] = {"verified_report": True, "report_complete": False}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["qa_report_available"] and view["next_action"] == "publish_again"
    assert "no page-content failure was confirmed" in view["error_reason"]
    assert "runner stopped" in view["error_reason"]


def test_publication_reason_retains_known_failure_code_without_exposing_it(approved):
    publication, run, spec = approved
    spec["publication_request"].update(error_code="pagebuilder_publication_refused",
        failure_code="pagebuilder_publication_scope_mismatch")
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert view["failure_code"] == "pagebuilder_publication_scope_mismatch"
    assert "approved request" in view["error_reason"]
    assert "pagebuilder_publication_scope_mismatch" not in view["error_reason"]


def test_no_report_policy_failure_keeps_its_safe_reason(approved):
    publication, run, spec = approved
    run.state, run.error_code = "failed", "qa_approved_inputs_changed"
    spec["lifecycle"]["completion"] = {"verified_report": False}
    session = SimpleNamespace(scalar=lambda _query: None, get=lambda *_args: None)
    view = publication._view(session, run, spec)
    assert "approved request" in view["error_reason"]
    assert not view["retryable"] and not view["qa_report_available"]


@pytest.fixture()
def leased_publication(no_plan_candidate):
    from app.core.jobs import enqueue, lease
    from app.db.supymem_models import BuildRun
    from app.pagebuilder_admission import publication

    d = no_plan_candidate
    requested = publication.request(d["vid"], d["caller"], d["selection"])
    with d["factory"]() as session:
        run = session.get(BuildRun, requested["run_id"])
        spec = json.loads(run.spec)
        binding = spec["publication_request"]["binding"]
        job_id = enqueue(session, publication.KIND, {"build_run_id": run.id,
            "approval_digest": publication.digest(binding), "_actor_subject": binding["caller_subject"]},
            workspace_id=d["wid"], venture_id=d["vid"], dedupe_key="unit-publication-fence:" + run.id)
        spec["publication_request"]["job_id"] = job_id
        run.spec = json.dumps(spec)
        session.commit()
    with d["factory"]() as session:
        job = lease(session, "publication-test-worker", kinds=[publication.KIND], n=1)[0]
        session.commit()
    payload = {**job.payload, "_job_id": job.id, "_worker_id": job.worker_id, "_job_attempt": job.attempts}
    return {**d, "run_id": requested["run_id"], "publication_job_id": job_id, "payload": payload}


@pytest.mark.parametrize("fault", ["worker", "attempt", "saved_actor", "saved_approval", "workspace", "cancelled"])
def test_worker_requires_the_exact_saved_job_payload_and_current_lease(leased_publication, fault):
    from app.core.jobs import FatalError
    from app.db.models import Job
    from app.pagebuilder_admission import publication

    d, payload = leased_publication, dict(leased_publication["payload"])
    with d["factory"]() as session:
        assert publication._worker_owned(session, payload)[0].id == d["run_id"]
    if fault in {"worker", "attempt", "workspace"}:
        field = {"worker": "_worker_id", "attempt": "_job_attempt", "workspace": "workspace_id"}[fault]
        payload[field] = "foreign" if fault != "attempt" else payload[field] + 1
    else:
        with d["factory"]() as session:
            job = session.get(Job, d["publication_job_id"])
            if fault == "cancelled":
                job.status = "cancelled"
            else:
                stored = json.loads(job.payload_json)
                stored["_actor_subject" if fault == "saved_actor" else "approval_digest"] = "changed"
                job.payload_json = json.dumps(stored)
            session.commit()
    code = "lease_lost" if fault in {"worker", "attempt", "cancelled"} else "scope_mismatch"
    with d["factory"]() as session, pytest.raises(FatalError, match="pagebuilder_publication_" + code):
        publication._worker_owned(session, payload)


@pytest.mark.parametrize("effect", ["review", "deployment"])
@pytest.mark.parametrize("change", ["cancel", "reclaim"])
def test_cancel_or_reclaim_after_precheck_refuses_the_effect_transaction(leased_publication, monkeypatch, effect, change):
    from app.builder import execution
    from app.core.jobs import FatalError
    from app.db.models import Job
    from app.db.supymem_models import BuildRun, Deployment
    from app.hosting import service as hosting
    from app.pagebuilder_admission import publication

    d, payload = leased_publication, leased_publication["payload"]
    with d["factory"]() as session:
        run, spec, _approval = publication._worker_owned(session, payload)
        before = run.spec
        assert spec["lifecycle"]["review"]["status"] == "not_ready"
    with d["factory"]() as session:
        job = session.get(Job, d["publication_job_id"])
        if change == "cancel":
            job.status = "cancelled"
        else:
            job.attempts += 1
            job.locked_by = "replacement-worker"
        session.commit()
    fence = lambda session: publication._lease(session, payload)
    with pytest.raises(FatalError, match="pagebuilder_publication_lease_lost"):
        if effect == "review":
            execution.review(d["run_id"], actor=d["actor"], source_artifact_id="unaccepted-artifact",
                report_digest="a" * 64, decision="accept", effect_fence=fence)
        else:
            monkeypatch.setattr(hosting.HostingConfig, "from_environment", lambda: SimpleNamespace())
            hosting.queue_accepted_build(d["app_id"], d["run_id"], actor=d["actor"], effect_fence=fence)
    with d["factory"]() as session:
        assert session.get(BuildRun, d["run_id"]).spec == before
        assert not session.scalar(select(Deployment.id))
