"""Synthetic prior usage settlement and real scoped report receipts; no paid calls."""
from datetime import timedelta
import json

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle


@pytest.fixture()
def completed_report(lifecycle, request):
    from app.db.models import GtmContact, GtmTouch, Job, VentureDocument
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import TaskRun, TaskStep
    from app.tasks.contracts import Contract
    from app.tasks.locks import lock_for_job, lock_run, lock_step
    from app.tasks.reports import write_report
    from app.tenancy import bind

    d = lifecycle
    for model in (GtmContact, GtmTouch, VentureDocument, VentureWaitlistEntry):
        model.__table__.create(d["factory"].kw["bind"], checkfirst=True)
    with d["factory"]() as session, bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        task = d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle", owner_person_id="usr_lifecycle",
            contract=Contract(title="Waitlist summary", goal="Summarize my waitlist", budget={"cost_micro": 10, "tokens": 100},
                steps=[{"title": "Summarize signups", "skill": "tasks.waitlist_summary"}]))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        ids = task.id, run.id, step.id, run.control_job_id
        session.add(VentureWaitlistEntry(id="signup_completion", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            email="synthetic@example.test", consent_version="synthetic", consent_text="Synthetic test consent",
            is_test=False, created_at=d["clock"][0] - timedelta(days=1)))
        session.commit()
    with d["factory"]() as session:
        task, run, _, job = lock_for_job(session, ids[3])
        result = write_report(session, task_id=task.id, run_id=run.id, workspace_id=task.workspace_id,
            venture_id=task.venture_id, now=d["clock"][0] + timedelta(seconds=1))
        _, _, step = lock_step(session, ids[2])
        step.state, step.evidence_json = "completed", json.dumps(result["evidence"])
        job.status = "succeeded"
        session.commit()
    # S02 owns wallet proof. This fixture exercises the task settlement seam
    # with a clearly synthetic estimate followed by confirmed actual overage.
    with d["usage"].run_context(ids[1]):
        invocation = d["usage"].before_call(1, max_tokens=0)
    actual = getattr(request, "param", {"cost": 11, "tokens": 0})
    d["usage"].after_call(invocation, actual_micro=actual["cost"], tokens_used=actual["tokens"])
    with d["factory"]() as session:
        task, run = lock_run(session, ids[1])
        assert task.state == run.state == "input_required"
        assert json.loads(task.blocker_json)["class"] == "budget_exhausted" and run.cost_micro == actual["cost"]
        original = (task.budget_json, session.get(TaskStep, ids[2]).inputs_json,
            session.get(TaskStep, ids[2]).evidence_json, run.evaluation_json, run.tokens_used, run.attempts, run.tool_calls)
        job_count = session.scalar(select(func.count()).select_from(Job))
    return {**d, "ids": ids, "original": original, "job_count": job_count}


def check(d):
    from app.tasks import budgets
    from app.tasks.locks import lock_run
    with d["factory"]() as session:
        task, run = lock_run(session, d["ids"][1])
        return budgets.eligibility(session, task, run)


def continue_result(d, key="complete-known-overage"):
    from app.db.task_models import Task
    from app.tenancy import bind
    from core.clock import as_utc
    with d["factory"]() as session:
        stamp = as_utc(session.get(Task, d["ids"][0]).updated_at).isoformat()
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        return d["api"].resume("ven_lifecycle", d["ids"][0],
            d["api"].ResumeTaskBody(continue_budget=True, expected_updated_at=stamp), idempotency_key=key)


def test_verified_report_overage_continues_to_database_only_completion(completed_report, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import planner, verification
    d = completed_report
    async def no_provider(*args, **kwargs):
        pytest.fail("Database-only completion must not plan or call a rubric judge")
    monkeypatch.setattr(planner, "preview", no_provider)
    monkeypatch.setattr(verification, "verify", no_provider)
    assert check(d) is None
    view = continue_result(d)
    assert view["state"] == "completed" and not view["can_continue"]
    assert continue_result(d)["state"] == "completed"  # Lost response recovers the same grant.
    with d["factory"]() as session:
        task, run, step = session.get(Task, d["ids"][0]), session.get(TaskRun, d["ids"][1]), session.get(TaskStep, d["ids"][2])
        assert run.state == step.state == "completed" and run.cost_micro == 11
        assert (task.budget_json, step.inputs_json, step.evidence_json, run.evaluation_json,
            run.tokens_used, run.attempts, run.tool_calls) == d["original"]
        allowance = json.loads(run.allowance_json)
        assert allowance["count"] == 1 and allowance["approvals"][0]["target"] == "complete"
        assert run.control_job_id == d["ids"][3]
        assert session.scalar(select(func.count()).select_from(Job)) == d["job_count"]


@pytest.mark.parametrize("mutation", ["body", "scope", "source", "evidence", "step_check", "whole_check", "device", "either"])
def test_completion_allowance_rechecks_authoritative_sources_and_counts(completed_report, mutation):
    from app.db.models import VentureDocument
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import Task, TaskStep
    d = completed_report
    with d["factory"]() as session:
        task, step = session.get(Task, d["ids"][0]), session.get(TaskStep, d["ids"][2])
        report = session.get(VentureDocument, "task-report:" + d["ids"][1])
        if mutation == "body":
            report.body_md += "\nUnverified edit."
        elif mutation == "scope":
            report.venture_id = "foreign"
        elif mutation == "source":
            session.get(VentureWaitlistEntry, "signup_completion").is_test = True
        elif mutation == "evidence":
            step.evidence_json = json.dumps([{"ref": "venture_document:invented", "source_count": 1000}])
        elif mutation in {"step_check", "whole_check"}:
            count = json.dumps({"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 1}})
            if mutation == "step_check":
                step.done_test_json = count
            else:
                task.done_test_json = count
        else:
            step.runs_on = mutation
        session.commit()
    assert check(d) is not None
    with pytest.raises(HTTPException) as error:
        continue_result(d)
    assert error.value.status_code == 409


@pytest.mark.parametrize("unknown", ["reserved_cost", "reserved_tokens", "zero_estimate", "calling", "uncertain"])
def test_complete_result_never_bypasses_unknown_usage(completed_report, unknown):
    from app.db.task_models import Task, TaskRun
    d = completed_report
    with d["factory"]() as session:
        task, run = session.get(Task, d["ids"][0]), session.get(TaskRun, d["ids"][1])
        if unknown == "reserved_cost":
            run.reserved_cost_micro = 1
        elif unknown == "reserved_tokens":
            run.reserved_tokens = 1
        elif unknown == "zero_estimate":
            from app.tasks.locks import lock_run
            task, run = lock_run(session, run.id)
            d["service"].event(session, task, "call_uncertain", "Synthetic unresolved call.", run_id=run.id,
                data={"invocation": "synthetic_unresolved"})
        else:
            run.evaluation_json = json.dumps({"state": unknown})
        session.commit()
    assert check(d) is not None


@pytest.mark.parametrize("completed_report", [{"cost": 100_010, "tokens": 25_100}], indirect=True)
def test_known_actual_equal_to_new_cap_needs_no_additional_attempt(completed_report):
    d = completed_report
    assert check(d) is None and continue_result(d)["state"] == "completed"
