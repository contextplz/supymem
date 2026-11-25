"""The current candidate and estimate use the saved task and synthetic receipts."""
import json

import pytest
from fastapi import HTTPException

from tests.unit.pagebuilder.test_api import setup as setup  # noqa: PLC0414
from tests.unit.pagebuilder.test_native_reference import candidate as candidate, plan  # noqa: PLC0414


def test_profile_before_generation_uses_saved_landing_contract_and_existing_quote(setup):
    from app.api.pagebuilder_api import profile
    from app.pagebuilder import api
    d, caller = setup, plan(setup)
    current = profile(d["vid"], caller)
    assert current["task_id"] == "plt_reference" and current["plan_id"] == "pln_reference"
    assert current["artifact_profile"] == "static_landing_v1" and current["revision_id"] == d["rid"]
    assert current["current"] is None and current["quote"] == api._estimate(d["vid"], caller, variants=1)
    assert current["quote"]["renderer_verification"] == "worker_preflight_before_charging"
    with pytest.raises(HTTPException) as hidden:
        profile(d["vid"], "different-founder")
    assert hidden.value.status_code == 404


@pytest.mark.parametrize("idea", ["Invoice reconciliation for freight teams", "Recipe sharing for families"])
def test_local_profile_without_a_coding_tool_keeps_your_saved_brief_accessible(setup, monkeypatch, idea):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api, tool

    with setup["factory"]() as session:
        session.get(Venture, setup["vid"]).raw_idea = idea
        session.get(IdeaRevision, setup["rid"]).summary = idea
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(tool, "connected_provider", lambda actor: None)
    monkeypatch.setattr(api, "_estimate", lambda *args, **kwargs: pytest.fail("A disconnected tool must not need a build quote."))
    current = api.profile(setup["vid"], setup["actor"])
    assert current["revision_id"] == setup["rid"]
    assert current["current"] is None and current["quote"] is None


def test_saved_publication_failure_survives_current_admission_recheck_failure(setup, monkeypatch):
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    from app.pagebuilder import api
    from app.pagebuilder_admission import inputs, publication
    d = setup
    job_id = api.build(d["vid"], d["actor"])["job_id"]
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    with d["factory"]() as session:
        row = session.get(Job, job_id)
        doc = json.loads(row.payload_json)
        doc["_runtime"]["state"] = "ready"
        doc["result"] = {"bundles": [{"direction": {}}]}
        row.status, row.payload_json = "succeeded", api.dump(doc)
        session.commit()
    saved = {"run_id": "saved-run", "state": "failed", "error_reason": "Our final checks could not complete."}
    monkeypatch.setattr(publication, "current", lambda *_args: saved)
    def changed(*_args, **_kwargs):
        raise ValueError("pagebuilder_admission_inputs_changed")
    monkeypatch.setattr(inputs, "current", changed)
    result = profile(d["vid"], d["actor"])["current"]
    assert result["publication"] == saved
    assert not result["server_publish_ready"]


def test_profile_exposes_only_durable_candidate_of_current_approved_landing(candidate):
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    d, caller = candidate, plan(candidate)
    current = profile(d["vid"], caller)
    assert current["quote"] is None and current["current"]["job_id"] == d["job_id"]
    assert current["current"]["queue_state"] == "succeeded" and current["current"]["native_ready"]
    assert [item["variant"] for item in current["current"]["candidates"]] == [0]
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).status = "retry_wait"
        session.commit()
    pending = profile(d["vid"], caller)["current"]
    assert pending["state"] == "ready" and pending["queue_state"] == "retry_wait"
    assert pending["native_ready"] is False and pending["candidates"] == []


def test_no_plan_profile_exposes_server_publication_without_native_eligibility(candidate, monkeypatch):
    from types import SimpleNamespace
    from app.api.pagebuilder_api import profile
    from app.db.models import Job
    from app.db.supymem_models import ExecutionPlan, PlanTask
    from app.pagebuilder.models import PageInput, PagePlan
    from app.pagebuilder.qa import qa_contract
    from tests.integration.test_postgres_static_publication import _hosting_environment
    from sqlalchemy import select

    d = candidate
    _hosting_environment(monkeypatch)
    monkeypatch.setenv("SUPYMEM_PAGEBUILDER_ADMISSION_ENABLED", "1")
    caller = SimpleNamespace(sub=d["actor"], role="user", legacy=False)
    with d["factory"]() as session:
        assert not session.scalar(select(ExecutionPlan.id)) and not session.scalar(select(PlanTask.id))
        row = session.get(Job, d["job_id"])
        doc = json.loads(row.payload_json)
        bundle = doc["result"]["bundles"][0]
        bundle["manifest"]["qa_contract"] = qa_contract(PageInput.model_validate(doc["_runtime"]["input"]),
            PagePlan.model_validate(bundle["plan"])).model_dump(mode="json")
        row.payload_json = json.dumps(doc)
        session.commit()
    saved = profile(d["vid"], caller)
    assert saved["plan_id"] is None and saved["task_id"] is None
    assert saved["current"]["native_ready"] is False and saved["current"]["candidates"] == []
    assert saved["current"]["server_publish_ready"] is True
    assert [item["variant"] for item in saved["current"]["server_candidates"]] == [0]
    with d["factory"]() as session:
        session.get(Job, d["job_id"]).status = "retry_wait"
        session.commit()
    pending = profile(d["vid"], caller)["current"]
    assert pending["server_publish_ready"] is False and pending["server_candidates"] == []


def test_publish_selection_requires_exact_confirmation_and_optional_plan_pair(setup):
    from app.pagebuilder_admission.router import PublishSelection
    from pydantic import ValidationError

    selection = {"job_id": "job_source", "variant": 0, "confirmed": True}
    assert PublishSelection.model_validate(selection).plan_id is None
    for changed in ({"plan_id": "plan_one"}, {"plan_task_id": "task_one"},
                    {"confirmed": False}, {"confirmed": 1}, {"confirmed": "true"}, {"execution_verified": True}):
        with pytest.raises(ValidationError):
            PublishSelection.model_validate(selection | changed)


def test_other_authorized_member_sees_original_creator_handoff_without_new_generation(candidate):
    from app.api.pagebuilder_api import profile
    from app.authz.workspace import ensure_subject_person
    from app.credits.accounts import get_account
    from app.db.models import Job, WorkspaceMember
    from app.pagebuilder import api, native_reference
    from core.clock import utcnow
    from sqlalchemy import select
    d, caller = candidate, plan(candidate)
    member = "pagebuilder-other-maintainer"
    ensure_subject_person(member)
    with d["factory"]() as session:
        session.add(WorkspaceMember(id="pagebuilder-other-membership", workspace_id=d["wid"],
            person_id=member, role="maintainer", can_approve=1, created_at=utcnow()))
        session.commit()
    owner = profile(d["vid"], caller)
    assert owner["current"]["original_creator_required"] is False and owner["current"]["native_ready"]
    other = profile(d["vid"], member)
    assert other["current"]["job_id"] == d["job_id"] and other["current"]["original_creator_required"] is True
    assert other["current"]["native_ready"] is False and other["current"]["candidates"] == [] and other["quote"] is None
    with pytest.raises(KeyError):
        native_reference.resolve_candidate(d["vid"], member, {"job_id": d["job_id"], "variant": 0})
    spent = get_account(d["vid"]).spent_micro
    assert api.start(d["vid"], member)["job_id"] == d["job_id"]
    with d["factory"]() as session:
        assert len(session.scalars(select(Job).where(Job.kind == api.KIND, Job.venture_id == d["vid"])).all()) == 1
    assert get_account(d["vid"]).spent_micro == spent


def test_source_retry_endpoint_requires_exact_attempt_and_authenticated_scope(setup, monkeypatch):
    from app.main import create_app
    from app.pagebuilder import api, native_reference
    from fastapi.testclient import TestClient
    d, calls = setup, []
    def requeue(job_id, **kwargs):
        calls.append((job_id, kwargs))
        if kwargs["expected_attempt"] == 4:
            raise ValueError("pagebuilder_inputs_changed")
        return {"job_id": job_id, "state": "ready", "queue_state": "pending", "queue_attempt": 3}
    monkeypatch.setattr(native_reference, "retry_source", requeue, raising=False)
    monkeypatch.setattr(api, "publish_wake", lambda: None)
    path = f"/ventures/{d['vid']}/pagebuilder/jobs/job-test/retry-source"
    headers = {"X-Subject": d["actor"]}
    with TestClient(create_app()) as client:
        response = client.post(path, json={"expected_attempt": 3}, headers=headers)
        assert response.status_code == 202 and response.json()["queue_state"] == "pending", (response.text, calls)
        assert calls == [("job-test", {"person_id": d["actor"], "workspace_id": d["wid"],
            "venture_id": d["vid"], "expected_attempt": 3})]
        for body in ({}, {"expected_attempt": True}, {"expected_attempt": -1},
                {"expected_attempt": "3"}, {"expected_attempt": 3, "variants": 2}):
            assert client.post(path, json=body, headers=headers).status_code == 422
        assert len(calls) == 1
        assert client.post(path, json={"expected_attempt": 3}, headers={"X-Subject": "different-founder"}).status_code == 404
        assert len(calls) == 1
        stale = client.post(path, json={"expected_attempt": 4}, headers=headers)
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "pagebuilder_source_retry_unavailable"
