"""Explicit retries resume the same settled QA source, never regenerate or republish unknown effects."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.integration.test_postgres_pagebuilder_admission import complete_no_plan_qa, qa_cloud, worker_step
from tests.integration.test_postgres_static_publication import _hosting_environment
from tests.unit.pagebuilder_admission.test_boundary import modules as modules
from tests.unit.pagebuilder_admission.test_no_plan_inputs import no_plan_candidate as no_plan_candidate, setup as setup


@pytest.fixture(autouse=True)
def hosting(monkeypatch):
    _hosting_environment(monkeypatch)


@pytest.fixture()
def failed_probe(no_plan_candidate, monkeypatch):
    import httpx
    from app.conversion import waitlist
    from app.pagebuilder_admission import publication

    d = no_plan_candidate
    requested = publication.request(d["vid"], d["caller"], d["selection"])
    d = qa_cloud(d, monkeypatch, requested["run_id"])
    closed = complete_no_plan_qa(d)
    client, control = httpx.Client, {"status": 503, "requests": 0}

    def reply(request):
        assert request.method == "POST" and str(request.url) == waitlist.public_url(d["vid"])
        control["requests"] += 1
        result = waitlist.capture(d["vid"], json.loads(request.content)) if control["status"] == 201 else {}
        return httpx.Response(control["status"], json=result)

    monkeypatch.setattr(waitlist.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(reply), **kwargs))
    job = worker_step(d, publication.KIND)
    assert job.status == "failed_dead" and job.last_error == "pagebuilder_publication_refused"
    return {**d, "publication_job_id": job.id, "attempt": job.attempts, "control": control,
            "qa_cost": closed["spent"], "source_artifact_id": closed["spec"]["artifact_capture"]["artifact_id"]}


def expire_probe(d):
    from app.db.supymem_models import VentureWaitlist
    from core.clock import utcnow

    with d["factory"]() as session:
        session.get(VentureWaitlist, d["vid"]).probe_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()


def test_retry_requeues_same_job_once_and_old_response_replays_never_consume_a_later_attempt(failed_probe):
    from app.db.models import Job
    from app.db.supymem_models import BuildRun
    from app.pagebuilder_admission import publication

    d = failed_probe
    before = publication.status(d["vid"], d["caller"], d["build_run_id"])
    assert before["retryable"] and before["job_attempt"] == d["attempt"]
    with d["factory"]() as session:
        job = session.get(Job, d["publication_job_id"])
        frozen = (job.payload_json, job.dedupe_key, job.attempts)
        run = session.get(BuildRun, d["build_run_id"])
        unchanged = (run.spec, run.credits_used, run.artifact_id)
    assert publication.status(d["vid"], d["caller"], d["build_run_id"]) == before
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        assert (run.spec, run.credits_used, run.artifact_id) == unchanged
    first = publication.retry(d["vid"], d["caller"], d["build_run_id"], expected_attempt=d["attempt"])
    assert first["state"] == "publishing" and not first["retryable"]
    assert publication.retry(d["vid"], d["caller"], d["build_run_id"], expected_attempt=d["attempt"]) == first
    with d["factory"]() as session:
        job = session.get(Job, d["publication_job_id"])
        assert (job.payload_json, job.dedupe_key, job.attempts) == frozen
        assert job.status == "scheduled" and job.max_attempts == d["attempt"] + 1
    expire_probe(d)
    later = worker_step(d, publication.KIND)  # The still-failed public hop consumes only this explicit retry.
    assert later.id == d["publication_job_id"] and later.attempts == d["attempt"] + 1 and later.status == "failed_dead"
    replay = publication.retry(d["vid"], d["caller"], d["build_run_id"], expected_attempt=d["attempt"])
    assert replay["state"] == "failed" and replay["retryable"] and replay["job_attempt"] == later.attempts
    with d["factory"]() as session:
        assert session.get(Job, later.id).status == "failed_dead"
        run = session.get(BuildRun, d["build_run_id"])
        assert run.credits_used == d["qa_cost"] and run.artifact_id == d["source_artifact_id"]


@pytest.mark.parametrize("fault", ["source", "unsettled", "rejected", "deployment", "actor", "payload", "unknown_reason", "stale_attempt"])
def test_retry_refuses_changed_or_unsettled_source_and_unknown_publication_effects(failed_probe, fault):
    from app.builder import execution
    from app.builder.model_proxy import BuildModelError
    from app.db.models import CreditReservation, Job
    from app.db.supymem_models import BuildRun, Deployment
    from app.pagebuilder_admission import publication
    from core.clock import utcnow

    d, caller, attempt = failed_probe, failed_probe["caller"], failed_probe["attempt"]
    with d["factory"]() as session:
        run = session.get(BuildRun, d["build_run_id"])
        spec = json.loads(run.spec)
        if fault == "source":
            job = session.get(Job, d["job_id"])
            doc = json.loads(job.payload_json)
            doc["result"]["bundles"][0]["spec_markdown"] += " changed"
            job.payload_json = json.dumps(doc)
        elif fault == "unsettled":
            session.scalar(select(CreditReservation).where(CreditReservation.parent_id == run.reservation_id)).status = "open"
        elif fault == "rejected":
            spec["lifecycle"]["review"].update(decision="reject", status="rejected")
        elif fault == "deployment":
            session.add(Deployment(id="unknown_deployment", app_id=run.app_id, workspace_id=run.workspace_id,
                venture_id=run.venture_id, target="supymem_static", build_artifact_id=run.artifact_id,
                state="pushing", version=1, started_at=utcnow()))
        elif fault == "actor":
            caller = SimpleNamespace(sub="foreign", role="user", legacy=False)
        elif fault == "payload":
            job = session.get(Job, d["publication_job_id"])
            payload = json.loads(job.payload_json)
            payload["approval_digest"] = "changed"
            job.payload_json = json.dumps(payload)
        elif fault == "unknown_reason":
            spec["publication_request"]["failure_reason"] = "private_provider_detail"
        else:
            attempt -= 1
        run.spec = json.dumps(spec)
        session.commit()
    with pytest.raises((BuildModelError, execution.BuildError, HTTPException, KeyError, ValueError)):
        publication.retry(d["vid"], caller, d["build_run_id"], expected_attempt=attempt)
    with d["factory"]() as session:
        job = session.get(Job, d["publication_job_id"])
        assert job.status == "failed_dead" and job.attempts == d["attempt"]
        assert session.get(BuildRun, d["build_run_id"]).credits_used == d["qa_cost"]


@pytest.mark.parametrize("kind,code,status,expected", [
    ("waitlist", "waitlist_probe_recent", 429, "waitlist_probe_recent"),
    ("waitlist", "waitlist_public_submission_unconfirmed", 503, "waitlist_public_submission_unconfirmed"),
    ("hosting", "hosting_provider_unavailable", 503, "hosting_provider_unavailable"),
    ("waitlist", "waitlist_probe_recent", 409, "publication_refused"),
    ("waitlist", "waitlist_storage_receipt_missing", 503, "publication_refused"),
    ("other", "waitlist_probe_recent", 429, "publication_refused"),
])
def test_only_typed_allowlisted_failure_codes_are_retained(modules, kind, code, status, expected):
    from app.pagebuilder_admission import publication

    error = (publication.waitlist.WaitlistError(code, status) if kind == "waitlist"
             else publication.HostingError(code, status=status) if kind == "hosting" else ValueError(code))
    assert publication._failure_reason(error) == expected


@pytest.mark.hosted_only
def test_retry_wire_requires_observed_integer_and_explicit_human_click(modules, monkeypatch):
    from app.pagebuilder_admission import publication, router
    from core.assertion import sign_assertion

    secret = "retry-route-fixture-assertion-more-than-thirty-two"
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", secret)
    calls = []
    monkeypatch.setattr(publication, "retry", lambda *_args, **kwargs: calls.append(kwargs) or {"state": "publishing"})
    app = FastAPI()
    app.include_router(router.router)
    path = "/ventures/venture_one/pagebuilder-admission/publish/run_one/retry"
    headers = {"X-COS-Assertion": sign_assertion(secret, sub="founder_one", email="", role="user", path=path, method="POST")}
    with TestClient(app) as client:
        assert client.post(path, json={"expected_attempt": 1, "confirmed": True}).status_code == 401
        for body in ({"expected_attempt": True, "confirmed": True}, {"expected_attempt": -1, "confirmed": True},
                     {"expected_attempt": 1, "confirmed": 1}, {"expected_attempt": 1},
                     {"expected_attempt": 1, "confirmed": True, "execution_verified": True}):
            assert client.post(path, headers=headers, json=body).status_code == 422
        body = {"expected_attempt": 1, "confirmed": True}
        assert client.post(path, headers={**headers, "X-SupyMem-Source": "voice"}, json=body).status_code == 403
        assert client.post(path, headers=headers, json=body).status_code == 202
    assert calls == [{"expected_attempt": 1}]
