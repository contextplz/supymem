"""Synthetic fixed allowances and never-called inline retries; no paid requests."""
import asyncio
from datetime import timedelta
import json

import pytest
from sqlalchemy.orm import Session

from tests.unit.tasks.test_drafts import draft_db as draft_db
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_verification import judge_db as judge_db, verify, saved as saved_judge


def blocked_draft(d, *, tokens=100_000):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import Budget
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.state = run.state = "input_required"
        task.budget_json = Budget(tokens=tokens).model_dump_json()
        task.blocker_json = json.dumps({"class": "budget_exhausted", "reason": "The task reached its agreed limit.",
            "unblock_action": "Review the partial result and choose whether to continue."})
        session.get(TaskStep, "step_draft").state = "input_required"
        session.commit()


def grant_draft(d, key="continue-action", *, reset=False):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets
    from app.tenancy import bind
    with d["factory"]() as session, bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        epoch = budgets.grant(session, task, run, key, now=d["clock"][0])
        if reset:
            task.state = run.state = "working"
            task.blocker_json = None
            session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
        return epoch


def test_fixed_slice_preserves_usage_future_budget_and_recovers_duplicate_key(draft_db):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        original = task.budget_json
        run.cost_micro, run.tokens_used, run.attempts, run.tool_calls = 50, 20, 1, 1
        session.commit()
    d["clock"][0] += timedelta(days=2)
    assert grant_draft(d, reset=True) == 1
    assert grant_draft(d) == 1
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        assert task.budget_json == original
        assert (run.cost_micro, run.tokens_used, run.attempts, run.tool_calls) == (50, 20, 1, 1)
        limits = budgets.effective_limits(task, run)
        assert (limits.tokens, limits.cost_micro, limits.attempts, limits.tool_calls) == (125_000, 600_000, 15, 38)
        assert limits.wall_until == (d["clock"][0] + timedelta(minutes=15)).replace(tzinfo=None)
        assert "continue-action" not in run.allowance_json
        assert budgets.epoch_for_key(run, "continue-action") == 1
        assert budgets.epoch_for_key(run, "another-action") is None


def test_only_two_source_fixed_slices_are_allowed(draft_db):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = draft_db
    blocked_draft(d)
    assert grant_draft(d, "first") == 1
    assert grant_draft(d, "second") == 2
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        assert budgets.effective_limits(task, run).cost_micro == 700_000
        assert "used its continuation" in budgets.eligibility(session, task, run)
    with pytest.raises(ValueError, match="used its continuation"):
        grant_draft(d, "third")


@pytest.mark.parametrize("raw", ["[]", "true", '{"count":true,"approvals":[]}', '{"count":3,"approvals":[]}',
    '{"count":1,"wall_until":"2099-01-01T00:00:00","approvals":[{"epoch":1,"key":"bad","at":"2026-10-08T12:00:00"}]}'])
def test_malformed_allowance_fails_closed(draft_db, raw):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        run.allowance_json = raw
        with pytest.raises(ValueError, match="needs review"):
            budgets.effective_limits(task, run)
        assert budgets.eligibility(session, task, run) is not None


@pytest.mark.parametrize("state", ["calling", "uncertain", "failed"])
def test_unknown_or_unproven_failed_draft_is_never_reset(draft_db, state):
    from app.db.task_models import TaskStep
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").evidence_json = json.dumps({"format": d["drafts"].FORMAT, "state": state})
        session.commit()
    with pytest.raises(ValueError):
        grant_draft(d)
    with d["factory"]() as session:
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == state
    assert not d["calls"]


@pytest.mark.parametrize("field", ["reserved_cost_micro", "reserved_tokens"])
def test_outstanding_usage_cannot_be_rebudgeted(draft_db, field):
    from app.db.task_models import TaskRun
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        setattr(session.get(TaskRun, "run_draft"), field, 1)
        session.commit()
    with pytest.raises(ValueError, match="usage confirmed"):
        grant_draft(d)


def test_never_called_inline_draft_continues_with_fresh_binding(draft_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        task = session.get(Task, "task_draft")
        from app.tasks.contracts import Budget
        task.budget_json = Budget(tokens=100).model_dump_json()
        _, _, _, old_binding, _ = d["drafts"]._source(session, **d["keys"])
        session.commit()
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    assert result["blocker"]["class"] == "budget_exhausted" and not d["calls"]
    blocked_draft(d, tokens=100)
    assert grant_draft(d, reset=True) == 1
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    assert result["verified"] and len(d["calls"]) == 1
    assert d["calls"][0][1]["request_id"] != old_binding["request_id"]
    with d["factory"]() as session:
        run, step = session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        assert run.attempts == 1 and run.cost_micro == 7
        assert json.loads(step.inputs_json)["_budget_attempt"] == 1


def test_pretransport_budget_marker_is_preserved_before_fresh_draft_attempt(draft_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks.contracts import Budget
    d = draft_db
    with d["factory"]() as session:
        session.get(Task, "task_draft").budget_json = Budget(cost_micro=0).model_dump_json()
        session.commit()
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    assert result["blocker"]["class"] == "budget_exhausted" and not d["calls"]
    with d["factory"]() as session:
        step = session.get(TaskStep, "step_draft")
        assert json.loads(step.evidence_json)["state"] == "not_called_budget"
        step.state = "input_required"
        session.commit()
    assert grant_draft(d, reset=True) == 1
    assert asyncio.run(d["drafts"].draft_reply(**d["keys"]))["verified"]
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        assert json.loads(task.budget_json)["cost_micro"] == 0 and run.attempts == 2
        assert json.loads(step.evidence_json)["previous_attempts"][0]["state"] == "not_called_budget"


@pytest.mark.parametrize("error_type", ["insufficient", "suspended", "uncertain_usage"])
def test_wallet_problem_is_not_a_budget_allowance_promise(draft_db, monkeypatch, error_type):
    import sys
    from app.credits.errors import AccountSuspended, CreditsUnavailable, InsufficientCredits
    from app.db.task_models import TaskStep
    d = draft_db
    exception = InsufficientCredits("synthetic", 0, 20) if error_type == "insufficient" else (
        AccountSuspended("synthetic") if error_type == "suspended" else CreditsUnavailable("credit usage needs reconciliation"))
    async def refused(*args, **kwargs):
        raise exception
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", refused)
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    with d["factory"]() as session:
        journal = json.loads(session.get(TaskStep, "step_draft").evidence_json)
    if error_type == "uncertain_usage":
        assert result["blocker"]["class"] == "needs_input" and journal["state"] == "uncertain"
    else:
        assert result["blocker"]["class"] == "needs_auth" and journal["state"] == "not_called_account"
        assert "Settings" in result["blocker"]["unblock_action"]


def test_draft_responded_checkpoint_recovers_without_new_binding_or_model(draft_db):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    assert asyncio.run(d["drafts"].draft_reply(**d["keys"]))["verified"]
    with d["factory"]() as session:
        row = session.get(VentureDocument, "task-draft:run_draft")
        data = json.loads(row.provenance_json)
        session.delete(row)  # This disposable fixture simulates pre-artifact checkpoint recovery.
        step = session.get(TaskStep, "step_draft")
        d["drafts"]._journal(step, "responded", data["binding"], response=data["draft"], billing=data["billing"])
        original_inputs, original_journal = step.inputs_json, step.evidence_json
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.state = run.state = "input_required"
        task.blocker_json = json.dumps({"class": "budget_exhausted"})
        step.state = "input_required"
        session.commit()
    assert grant_draft(d, reset=True) == 1
    with d["factory"]() as session:
        step = session.get(TaskStep, "step_draft")
        assert (step.inputs_json, step.evidence_json) == (original_inputs, original_journal)
    assert asyncio.run(d["drafts"].draft_reply(**d["keys"]))["verified"] and len(d["calls"]) == 1


def test_only_owner_scope_can_grant_continuation(draft_db):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    from app.tenancy import bind
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session, bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_other"):
        with pytest.raises(PermissionError, match="founder manages"):
            budgets.grant(session, session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), "key")


def test_admitted_monolithic_workflow_requires_its_owner_resume(draft_db):
    from app.db.models import Job
    from app.db.task_models import TaskStep
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        session.add(Job(id="job_prior_workflow", kind="research.deep", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json="{}", status="failed_dead", created_at=d["clock"][0]))
        step = session.get(TaskStep, "step_draft")
        step.skill, step.job_id = "research.deep", "job_prior_workflow"
        session.commit()
    with pytest.raises(ValueError, match="saved progress screen"):
        grant_draft(d)


def test_completed_steps_and_bindings_are_untouched_by_later_verifier_allowance(judge_db):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets
    from app.tasks.contracts import Budget
    d = judge_db
    with Session(d.engine) as session:
        task = session.get(Task, "tsk_verify")
        task.budget_json = Budget(cost_micro=0).model_dump_json()
        original = session.get(TaskStep, "tst_verify").inputs_json, session.get(TaskStep, "tst_verify").evidence_json
        session.commit()
    assert verify(d)["state"] == "blocked" and not d.calls
    assert saved_judge(d).evaluation["state"] == "not_called_budget"
    with Session(d.engine) as session, d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        assert budgets.grant(session, task, run, "continue-judge") == 1
        task.state = run.state = "working"
        task.blocker_json = None
        session.commit()
    assert verify(d)["passed"] and len(d.calls) == 1
    assert d.calls[0][1]["request_id"].endswith(":continue:1")
    assert saved_judge(d).evaluation["previous_attempts"][0]["state"] == "not_called_budget"
    with Session(d.engine) as session:
        step = session.get(TaskStep, "tst_verify")
        assert (step.inputs_json, step.evidence_json) == original and step.state == "completed"


def test_maximum_base_caps_cannot_be_inflated(draft_db):
    from app.db.task_models import Task, TaskRun
    from app.tasks import budgets
    from app.tasks.contracts import Budget
    d = draft_db
    blocked_draft(d)
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.budget_json = Budget(tokens=2_000_000, cost_micro=50_000_000, attempts=100, tool_calls=500, wall_seconds=86400).model_dump_json()
        run.attempts = 100
        assert "maximum supported" in budgets.eligibility(session, task, run)
