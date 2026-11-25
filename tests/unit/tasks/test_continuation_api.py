"""Actual resume API with persisted synthetic scope; no providers or paid calls."""
from datetime import timedelta
import json

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from tests.unit.tasks.test_drafts import draft_db as draft_db
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_budget_continuation import blocked_draft


def stamp(d):
    from app.db.task_models import Task
    from core.clock import as_utc
    with d["factory"]() as session:
        return as_utc(session.get(Task, "task_draft").updated_at).isoformat()


def resume(d, *, key="api-continue", expected=None, actor="usr_lifecycle", body=None):
    from app.tenancy import bind
    body = body or d["api"].ResumeTaskBody(continue_budget=True, expected_updated_at=expected or stamp(d))
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor=actor):
        return d["api"].resume("ven_lifecycle", "task_draft", body, idempotency_key=key)


def saved(d):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        return {"state": task.state, "blocker": task.blocker_json, "base": task.budget_json,
            "allowance": run.allowance_json, "cost": run.cost_micro, "tokens": run.tokens_used,
            "reserved": run.reserved_cost_micro, "reserved_tokens": run.reserved_tokens,
            "step_state": step.state, "inputs": step.inputs_json, "evidence": step.evidence_json,
            "control": run.control_job_id, "jobs": [(job.id, job.kind, job.dedupe_key, job.effect_key,
                job.workspace_id, job.venture_id, job.payload_json) for job in session.scalars(select(Job)).all()]}


def test_continue_grants_once_and_enqueues_owned_epoch_control_job(draft_db):
    d = draft_db
    blocked_draft(d)
    original = saved(d)
    result = resume(d)
    after = saved(d)
    assert result["state"] == "working" and after["step_state"] == "submitted"
    assert after["base"] == original["base"] and (after["cost"], after["tokens"], after["reserved"], after["reserved_tokens"]) == (0, 0, 0, 0)
    allowance = json.loads(after["allowance"])
    assert allowance["count"] == 1 and len(after["jobs"]) == 1
    job = after["jobs"][0]
    assert job[1:4] == ("tasks.execute", "task-continue:run_draft:1", "task-continue:run_draft:1")
    assert job[4:6] == ("ws_lifecycle", "ven_lifecycle")
    assert json.loads(job[6])["_actor_subject"] == "usr_lifecycle" and json.loads(job[6])["task_run_id"] == "run_draft"
    assert after["control"] == job[0] and not d["calls"]


def test_lost_response_same_key_recovers_before_stale_cas_without_second_grant(draft_db):
    d = draft_db
    blocked_draft(d)
    original_stamp = stamp(d)
    first = resume(d, expected=original_stamp)
    after = saved(d)
    d["clock"][0] += timedelta(minutes=1)
    second = resume(d, expected="2020-01-01T00:00:00Z")
    assert first["id"] == second["id"] and second["state"] == "working"
    assert saved(d) == after


def test_stale_cas_changes_neither_allowance_checkpoint_nor_jobs(draft_db):
    d = draft_db
    blocked_draft(d)
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d, expected="2020-01-01T00:00:00Z")
    assert error.value.status_code == 409 and saved(d) == original and not d["calls"]


def test_foreign_founder_cannot_continue_owned_task(draft_db):
    d = draft_db
    blocked_draft(d)
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d, actor="usr_other")
    assert error.value.status_code == 403 and saved(d) == original


def test_ordinary_resume_cannot_clear_budget_blocker(draft_db):
    d = draft_db
    blocked_draft(d)
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d, body=d["api"].ResumeTaskBody())
    assert error.value.status_code == 409 and saved(d) == original


@pytest.mark.parametrize("state", ["calling", "uncertain"])
def test_ambiguous_draft_cannot_get_allowance_or_new_job(draft_db, state):
    from app.db.task_models import TaskStep
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").evidence_json = json.dumps({"format": d["drafts"].FORMAT, "state": state})
        session.commit()
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d)
    assert error.value.status_code == 409 and saved(d) == original and not d["calls"]


def test_changed_source_is_refused_before_hidden_expense_or_checkpoint_reset(draft_db):
    from app.db.models import GtmTouch
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        _, _, step, binding, _ = d["drafts"]._source(session, **d["keys"])
        d["drafts"]._journal(step, "not_called_budget", binding)
        session.get(GtmTouch, "reply_draft").body_text = "The reply changed after this attempt."
        session.commit()
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d)
    assert error.value.status_code == 409 and saved(d) == original and not d["calls"]


def test_live_control_job_must_finish_before_continuation(draft_db):
    from app.db.models import Job
    from app.db.task_models import TaskRun
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        session.add(Job(id="job_active_control", kind="tasks.execute", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json=json.dumps({"task_run_id": "run_draft", "_actor_subject": "usr_lifecycle"}),
            status="leased", locked_by="synthetic_worker", locked_at=d["clock"][0], created_at=d["clock"][0]))
        session.get(TaskRun, "run_draft").control_job_id = "job_active_control"
        session.commit()
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d)
    assert error.value.status_code == 409 and saved(d) == original


def test_public_view_keeps_allowance_and_usage_internal(draft_db):
    d = draft_db
    blocked_draft(d)
    view = resume(d)
    hidden = {"allowance_json", "budget_json", "cost_micro", "tokens_used", "reserved_cost_micro", "reserved_tokens", "wallet_account_id", "approvals"}
    assert not hidden & set(view)
    serialized = json.dumps(view)
    assert all(name not in serialized for name in hidden)
    assert "api-continue" not in serialized and "100000" not in serialized and "25000" not in serialized


def test_resume_body_accepts_no_user_supplied_amounts(draft_db):
    with pytest.raises(ValidationError):
        draft_db["api"].ResumeTaskBody.model_validate({"continue_budget": True, "expected_updated_at": stamp(draft_db),
            "cost_micro": 999_999_999, "tokens": 999_999_999, "slices": 20})


def test_lost_response_after_completion_recovers_owned_grant_without_replay(draft_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    blocked_draft(d)
    resume(d, key="completed-grant")
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.state = run.state = "completed"
        session.get(TaskStep, "step_draft").state = "completed"
        session.get(Job, run.control_job_id).status = "succeeded"
        session.commit()
    original = saved(d)
    result = resume(d, key="completed-grant", expected="2020-01-01T00:00:00Z")
    assert result["state"] == "completed" and saved(d) == original
    with pytest.raises(HTTPException) as foreign_key:
        resume(d, key="unrelated-new-key", expected="2020-01-01T00:00:00Z")
    assert foreign_key.value.status_code == 409 and saved(d) == original


def test_persisted_grant_key_does_not_bypass_foreign_owner(draft_db):
    d = draft_db
    blocked_draft(d)
    resume(d, key="owner-only-key")
    original = saved(d)
    with pytest.raises(HTTPException) as error:
        resume(d, key="owner-only-key", expected="2020-01-01T00:00:00Z", actor="usr_other")
    assert error.value.status_code == 403 and saved(d) == original
