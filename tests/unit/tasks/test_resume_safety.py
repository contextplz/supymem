"""Synthetic owner recovery proofs and actual API/inline paths; no paid calls."""
import asyncio
import json
import sys

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.unit.tasks.test_drafts import draft_db as draft_db
from tests.unit.tasks.test_task_lifecycle import lifecycle as lifecycle
from tests.unit.tasks.test_verification import judge_db as judge_db, verify


def paused(d):
    from app.db.task_models import Task
    with d["factory"]() as session:
        session.get(Task, "task_draft").state = "paused"
        session.commit()


def proof(d, *, actor="usr_lifecycle"):
    from app.db.task_models import Task, TaskRun
    from app.tasks.budgets import resume_eligibility
    from app.tenancy import bind
    with d["factory"]() as session, bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor=actor):
        return resume_eligibility(session, session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"))


def snapshot(d):
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        return (task.state, task.blocker_json, task.updated_at, run.state, run.allowance_json, run.evaluation_json,
            run.cost_micro, run.tokens_used, run.reserved_cost_micro, run.reserved_tokens, run.control_job_id,
            step.state, step.inputs_json, step.evidence_json,
            session.scalar(select(func.count()).select_from(TaskEvent)),
            session.scalar(select(func.count()).select_from(TaskRun)), session.scalar(select(func.count()).select_from(Job)))


def api_resume(d):
    from app.tenancy import bind
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        return d["api"].resume("ven_lifecycle", "task_draft", d["api"].ResumeTaskBody(), idempotency_key=None)


def test_scheduled_plan_paused_before_application_recovers_a_future_fire_on_resume(lifecycle, monkeypatch):
    from datetime import datetime
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    from app.tasks import intake
    from app.tasks.contracts import Contract, next_fire, trigger_from_words
    from app.tenancy import bind
    from core.clock import as_utc
    from tests.task_billing_fixtures import install_recovery, synthetic_receipt

    d = lifecycle
    recovery = install_recovery(monkeypatch)
    goal = "Every Friday, summarize waitlist sign-ups and replies"
    kind, trigger = trigger_from_words(goal, "Asia/Kolkata", d["clock"][0])
    contract = Contract(title="Weekly summary", goal=goal, kind=kind, trigger=trigger,
        steps=[{"title": "Summarize the saved counts", "skill": "tasks.waitlist_summary"}])
    with d["factory"]() as session:
        task, should_plan = intake.begin(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", create_key="scheduled-paused-plan", accepted_zone="Asia/Kolkata",
            body_input={"goal": goal, "timezone": "Asia/Kolkata", "start": True, "contract": None})
        assert should_plan
        task_id, request_id = task.id, json.loads(task.scope_json)[intake.KEY]["request_id"]
        session.commit()
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        assert d["api"].pause("ven_lifecycle", task_id)["state"] == "paused"
        receipt = synthetic_receipt({"workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle",
            "purpose": "task.plan", "request_id": request_id})
        with d["factory"]() as session:
            intake.respond(session, task_id=task_id, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                owner_person_id="usr_lifecycle", request_id=request_id, contract=contract, usage=receipt)
            session.commit()
        recovery.confirm(receipt)
        confirmed_billing = intake.reconcile(task_id=task_id, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle")
        with d["factory"]() as session:
            task = intake.recover(session, task_id=task_id, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                owner_person_id="usr_lifecycle", confirmed_billing=confirmed_billing)
            assert task.state == "paused" and task.next_fire_at is None
            saved_trigger = json.loads(task.trigger_json)
            session.commit()
        result = d["api"].resume("ven_lifecycle", task_id)
        assert result["state"] == "submitted"
        assert as_utc(datetime.fromisoformat(result["next_fire_at"])) == as_utc(next_fire(saved_trigger, d["clock"][0]))
        assert result["deadline_is_estimate"] and result["deadline_at"]
        assert d["api"].resume("ven_lifecycle", task_id)["next_fire_at"] == result["next_fire_at"]
    with d["factory"]() as session:
        assert not session.scalar(select(func.count()).select_from(TaskRun))
        assert not session.scalar(select(func.count()).select_from(Job))
        assert session.get(Task, task_id).state == "submitted"


def test_invalid_paused_schedule_refuses_resume_without_changing_state_or_creating_work(lifecycle):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    from app.tenancy import bind
    from tests.unit.tasks.test_task_lifecycle import _contract, _create

    d = lifecycle
    task_id = _create(d, _contract(kind="event", trigger={"event_type": "waitlist.signup", "filter": {}}), start=False)
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        task.kind, task.state, task.next_fire_at, task.trigger_json = "scheduled", "paused", None, '{}'
        session.commit()
        before = task.updated_at
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"), pytest.raises(HTTPException) as error:
        d["api"].resume("ven_lifecycle", task_id)
    assert error.value.status_code == 409
    with d["factory"]() as session:
        task = session.get(Task, task_id)
        assert task.state == "paused" and task.next_fire_at is None and task.updated_at == before
        assert not session.scalar(select(func.count()).select_from(TaskRun))
        assert not session.scalar(select(func.count()).select_from(Job))


def test_paused_never_called_draft_proof_is_pure(draft_db):
    d = draft_db
    paused(d)
    before = snapshot(d)
    assert proof(d) is None
    assert snapshot(d) == before and not d["calls"]


@pytest.mark.parametrize("state", ["calling", "uncertain"])
def test_paused_ambiguous_draft_cannot_resume_or_mutate(draft_db, state):
    from app.db.task_models import TaskStep
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").evidence_json = json.dumps({"state": state})
        session.commit()
    before = snapshot(d)
    assert "uncertain action" in proof(d)
    with pytest.raises(HTTPException) as error:
        api_resume(d)
    assert error.value.status_code == 409 and snapshot(d) == before and not d["calls"]


@pytest.mark.parametrize("field", ["reserved_cost_micro", "reserved_tokens"])
def test_paused_usage_reservation_requires_confirmation(draft_db, field):
    from app.db.task_models import TaskRun
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        setattr(session.get(TaskRun, "run_draft"), field, 1)
        session.commit()
    before = snapshot(d)
    assert proof(d) is not None and snapshot(d) == before


def test_zero_estimate_unsettled_invocation_still_blocks_resume(draft_db):
    from app.db.task_models import Task, TaskEvent
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        d["service"].event(session, session.get(Task, "task_draft"), "call_reserved", "Synthetic zero estimate.",
            run_id="run_draft", step_id="step_draft", data={"invocation": "synthetic_invocation", "cost_micro": 0, "tokens": 0})
        session.commit()
    before = snapshot(d)
    assert proof(d) is not None and snapshot(d) == before
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == 1


@pytest.mark.parametrize("row,state", [("task", "canceled"), ("task", "failed"), ("run", "canceled"), ("run", "failed")])
def test_terminal_task_or_run_cannot_resume(draft_db, row, state):
    from app.db.task_models import Task, TaskRun
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        target = session.get(Task, "task_draft") if row == "task" else session.get(TaskRun, "run_draft")
        target.state = state
        session.commit()
    before = snapshot(d)
    assert proof(d) is not None and snapshot(d) == before and not d["calls"]


def test_foreign_founder_cannot_prove_inline_resume(draft_db):
    d = draft_db
    paused(d)
    before = snapshot(d)
    assert "founder manages" in proof(d, actor="usr_other")
    assert snapshot(d) == before


@pytest.mark.parametrize("status", ["running", "leased"])
def test_live_control_must_finish_before_resume(draft_db, status):
    from app.db.models import Job
    from app.db.task_models import TaskRun
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        session.add(Job(id="job_resume_control", kind="tasks.execute", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json=json.dumps({"task_run_id": "run_draft"}), status=status, created_at=d["clock"][0]))
        session.get(TaskRun, "run_draft").control_job_id = "job_resume_control"
        session.commit()
    before = snapshot(d)
    assert "still finishing" in proof(d) and snapshot(d) == before


def test_legacy_control_calling_journal_requires_owner_reconciliation(draft_db):
    from app.db.models import Job
    from app.db.task_models import TaskRun
    d = draft_db
    paused(d)
    with d["factory"]() as session:
        session.add(Job(id="job_resume_control", kind="tasks.execute", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json=json.dumps({"task_run_id": "run_draft", "_runtime": {"operations": {"call": {"state": "calling"}}}}),
            status="failed_dead", created_at=d["clock"][0]))
        session.get(TaskRun, "run_draft").control_job_id = "job_resume_control"
        session.commit()
    before = snapshot(d)
    assert "uncertain action" in proof(d) and snapshot(d) == before


def test_failed_calling_control_cannot_create_a_fresh_run(draft_db):
    from app.db.task_models import Task, TaskRun
    d = draft_db
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.kind, task.state, run.state = "one_time", "failed", "failed"
        run.evaluation_json = json.dumps({"state": "calling", "request_id": "synthetic_paid_before_crash"})
        session.commit()
    before = snapshot(d)
    with pytest.raises(HTTPException) as error:
        api_resume(d)
    assert error.value.status_code == 409 and snapshot(d) == before and not d["calls"]


def test_linked_account_block_uses_monolithic_owner_resume(draft_db):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    with d["factory"]() as session:
        task, run, step = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft"), session.get(TaskStep, "step_draft")
        task.state = run.state = step.state = "auth_required"
        task.blocker_json = json.dumps({"class": "needs_auth"})
        session.add(Job(id="job_resume_workflow", kind="research.deep", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            payload_json="{}", status="failed_dead", created_at=d["clock"][0]))
        step.skill, step.job_id = "research.deep", "job_resume_workflow"
        session.commit()
    before = snapshot(d)
    assert "saved progress screen" in proof(d) and snapshot(d) == before


def test_proven_wallet_refusal_has_safe_owned_resume(draft_db, monkeypatch):
    from app.credits.errors import InsufficientCredits
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    async def not_called(*args, **kwargs):
        raise InsufficientCredits("synthetic", 0, 20)
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", not_called)
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    with d["factory"]() as session:
        d["service"].block(session, session.get(Task, "task_draft"), result["blocker"],
            run=session.get(TaskRun, "run_draft"), step=session.get(TaskStep, "step_draft"))
        session.commit()
    before = snapshot(d)
    assert proof(d) is None and snapshot(d) == before and not d["calls"]
    view = api_resume(d)
    assert view["state"] == "working" and not d["calls"]


def test_account_label_alone_cannot_authorize_new_inline_call(draft_db):
    from app.db.task_models import Task, TaskRun
    d = draft_db
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        task.state = run.state = "auth_required"
        task.blocker_json = json.dumps({"class": "needs_auth"})
        session.commit()
    before = snapshot(d)
    assert proof(d) is not None and snapshot(d) == before and not d["calls"]


def test_completed_draft_source_change_is_rechecked_before_resume(draft_db):
    from app.db.models import GtmTouch
    from app.db.task_models import TaskStep
    d = draft_db
    result = asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    with d["factory"]() as session:
        step = session.get(TaskStep, "step_draft")
        step.state, step.evidence_json = "completed", json.dumps(result["evidence"])
        session.commit()
    paused(d)
    assert proof(d) is None
    with d["factory"]() as session:
        session.get(GtmTouch, "reply_draft").body_text = "Changed after the draft was saved."
        session.commit()
    before = snapshot(d)
    assert proof(d) is not None and snapshot(d) == before and len(d["calls"]) == 1


def test_actual_paused_late_draft_recovers_cached_answer_without_new_call(draft_db, monkeypatch):
    from app.db.task_models import Task, TaskStep
    d = draft_db
    original = sys.modules["app.credits.metered"].llm
    async def paused_response(*args, **kwargs):
        response = await original(*args, **kwargs)
        paused(d)  # Actual provider response arrives after the founder pause.
        return response
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", paused_response)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    payload = {"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        assert session.get(Task, "task_draft").state == "paused"
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "responded"
    assert len(d["calls"]) == 1 and proof(d) is None
    assert api_resume(d)["state"] == "working"
    asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        assert session.get(TaskStep, "step_draft").state == "completed"
    assert len(d["calls"]) == 1


def test_actual_pause_before_draft_gate_records_no_call_then_runs_once(draft_db, monkeypatch):
    from app.db.task_models import Task, TaskEvent, TaskStep
    d, bindings = draft_db, []
    original = sys.modules["app.credits.metered"].llm
    async def pause_before_gate(*args, **kwargs):
        with d["factory"]() as session:
            journal = json.loads(session.get(TaskStep, "step_draft").evidence_json)
            assert journal["state"] == "calling"
            bindings.append(journal["binding"])
        paused(d)
        return await original(*args, **kwargs)  # Actual before_call refuses before synthetic transport.
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", pause_before_gate)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    payload = {"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}
    asyncio.run(d["execution"].execute(payload))
    with d["factory"]() as session:
        journal = json.loads(session.get(TaskStep, "step_draft").evidence_json)
        assert journal["state"] == "not_called_pause" and journal["known_no_call"] is True
        assert journal["binding"] == bindings[0] and session.get(Task, "task_draft").state == "paused"
        assert not session.scalar(select(TaskEvent.id).where(TaskEvent.kind == "call_reserved"))
    assert not d["calls"] and proof(d) is None
    assert api_resume(d)["state"] == "working"
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", original)
    asyncio.run(d["execution"].execute(payload))
    asyncio.run(d["execution"].execute(payload))
    assert len(d["calls"]) == 1 and d["calls"][0][1]["request_id"] == bindings[0]["request_id"]


def test_pause_text_after_transport_preserves_funded_response_without_claiming_no_call(draft_db, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.task_models import TaskStep
    d = draft_db
    original = sys.modules["app.credits.metered"].llm
    async def ambiguous_after_transport(*args, **kwargs):
        await original(*args, **kwargs)
        paused(d)
        raise FatalError("task_paused")
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", ambiguous_after_transport)
    asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    with d["factory"]() as session:
        journal = json.loads(session.get(TaskStep, "step_draft").evidence_json)
        assert journal["state"] == "responded" and "known_no_call" not in journal and journal["billing"]
    assert len(d["calls"]) == 1 and proof(d) is None  # The confirmed invoice authorizes cached recovery only.
    assert api_resume(d)["state"] == "working"
    assert asyncio.run(d["drafts"].draft_reply(**d["keys"]))["verified"]
    assert len(d["calls"]) == 1


@pytest.mark.parametrize("state,kind", [("auth_required", "needs_auth"), ("approval_required", "needs_approval"),
    ("input_required", "needs_input"), ("canceled", None)])
def test_pretransport_waiting_refusal_preserves_the_existing_stop(draft_db, monkeypatch, state, kind):
    from app.db.task_models import Task, TaskRun, TaskStep
    d = draft_db
    original = sys.modules["app.credits.metered"].llm
    reason = json.dumps({"class": kind, "reason": "Synthetic original stop.", "unblock_action": "Review this stop."}) if kind else None
    async def stop_before_gate(*args, **kwargs):
        with d["factory"]() as session:
            task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
            task.state = run.state = state
            task.blocker_json = reason
            session.commit()
        return await original(*args, **kwargs)
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", stop_before_gate)
    with d["factory"]() as session:
        session.get(TaskStep, "step_draft").state = "submitted"
        session.commit()
    asyncio.run(d["execution"].execute({"task_run_id": "run_draft", "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        assert (task.state, run.state, task.blocker_json) == (state, state, reason)
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["state"] == "not_called_pause"
    assert not d["calls"] and proof(d) is not None


def test_no_call_pause_cannot_release_prior_unknown_usage_or_grant_budget(draft_db, monkeypatch):
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import budgets
    d = draft_db
    original = sys.modules["app.credits.metered"].llm
    async def pause_before_gate(*args, **kwargs):
        with d["factory"]() as session:
            run = session.get(TaskRun, "run_draft")
            run.reserved_cost_micro = 20
            session.get(Task, "task_draft").state = "paused"
            session.commit()
        return await original(*args, **kwargs)
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", pause_before_gate)
    asyncio.run(d["drafts"].draft_reply(**d["keys"]))
    assert proof(d) is not None and not d["calls"]
    with d["factory"]() as session:
        task, run = session.get(Task, "task_draft"), session.get(TaskRun, "run_draft")
        assert run.reserved_cost_micro == 20
        assert json.loads(session.get(TaskStep, "step_draft").evidence_json)["known_no_call"] is True
        run.reserved_cost_micro = 0  # A separate disposable fixture checks allowance eligibility.
        task.state = run.state = "input_required"
        task.blocker_json = json.dumps({"class": "budget_exhausted"})
        assert budgets.eligibility(session, task, run) is not None


def judge_proof(d):
    from app.db.supymem_models import ExecutionPlan, ResearchDossier
    from app.db.task_models import Task, TaskRun
    from app.tasks.budgets import resume_eligibility
    for model in (ExecutionPlan, ResearchDossier):
        model.__table__.create(d.engine, checkfirst=True)
    with Session(d.engine) as session, d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
        return resume_eligibility(session, session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify"))


def test_paused_judge_recovers_valid_checkpoint_with_no_second_call(judge_db):
    from app.db.models import Job
    from app.db.task_models import Task
    d = judge_db
    def pause_during_call():
        with Session(d.engine) as session:
            session.get(Task, "tsk_verify").state = "paused"
            session.commit()
    d.during_call = pause_during_call
    assert verify(d)["state"] == "stopped"
    with Session(d.engine) as session:
        session.get(Job, "job_judge").status = "succeeded"
        session.commit()
    assert judge_proof(d) is None and len(d.calls) == 1
    with Session(d.engine) as session:
        session.get(Task, "tsk_verify").state = "working"
        session.commit()
    assert verify(d)["state"] == "recovered" and len(d.calls) == 1


def test_changed_cached_judge_artifact_cannot_resume(judge_db):
    from app.db.models import Job, VentureDocument
    from app.db.task_models import Task, TaskRun
    d = judge_db
    assert verify(d)["passed"]
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        task.state, run.state, run.finished_at = "paused", "working", None
        session.get(Job, "job_judge").status = "succeeded"
        session.get(VentureDocument, "task-note:planned_verify").body_md += "\nChanged after review."
        session.commit()
    assert judge_proof(d) is not None and len(d.calls) == 1


def test_verifier_wallet_refusal_requires_exact_artifact_binding(judge_db, monkeypatch):
    from app.credits.errors import AccountSuspended
    from app.db.models import Job, VentureDocument
    d = judge_db
    async def not_called(*args, **kwargs):
        raise AccountSuspended("synthetic")
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", not_called)
    assert verify(d)["state"] == "blocked" and not d.calls
    with Session(d.engine) as session:
        session.get(Job, "job_judge").status = "succeeded"
        session.commit()
    assert judge_proof(d) is None
    with Session(d.engine) as session:
        session.get(VentureDocument, "task-note:planned_verify").body_md += "\nChanged before account recovery."
        session.commit()
    assert judge_proof(d) is not None and not d.calls


def test_actual_pause_before_judge_gate_records_no_call_then_runs_once(judge_db, monkeypatch):
    from app.api import handoffs_api
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun
    d, bindings = judge_db, []
    original = sys.modules["app.credits.metered"].llm
    async def pause_before_gate(*args, **kwargs):
        with Session(d.engine) as session:
            task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
            journal = json.loads(run.evaluation_json)
            assert journal["state"] == "calling"
            bindings.append((journal["artifact_digest"], journal["contract_digest"], journal["request_id"]))
            task.state = "paused"
            session.commit()
        return await original(*args, **kwargs)
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", pause_before_gate)
    assert verify(d)["state"] == "stopped" and not d.calls
    with Session(d.engine) as session:
        run = session.get(TaskRun, "trn_verify")
        journal = json.loads(run.evaluation_json)
        assert journal["state"] == "not_called_pause" and journal["known_no_call"] is True
        assert (journal["artifact_digest"], journal["contract_digest"], journal["request_id"]) == bindings[0]
        assert run.cost_micro == run.reserved_cost_micro == run.reserved_tokens == 0
        run.control_job_id = "job_judge"
        session.get(Job, "job_judge").status = "succeeded"
        session.commit()
    assert judge_proof(d) is None
    with d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
        assert handoffs_api.resume("ven_verify", "tsk_verify", handoffs_api.ResumeTaskBody(), idempotency_key=None)["state"] == "working"
    with Session(d.engine) as session:
        control = session.get(TaskRun, "trn_verify").control_job_id
    monkeypatch.setattr(sys.modules["app.credits.metered"], "llm", original)
    assert verify(d, _job_id=control)["passed"] is True
    assert verify(d, _job_id=control)["passed"] is True
    assert len(d.calls) == 1 and d.requests[-1]["request_id"] == bindings[0][2]


@pytest.mark.parametrize("marker", [False, None])
def test_pause_marker_without_known_no_call_proof_cannot_resume(judge_db, marker):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        snapshot = d.module.artifact_snapshot(session, task, run)
        task.state = "paused"
        run.evaluation_json = json.dumps({"state": "not_called_pause", "known_no_call": marker,
            "artifact_digest": snapshot["artifact_digest"], "contract_digest": snapshot["contract_digest"]})
        session.commit()
    assert judge_proof(d) is not None and not d.calls


@pytest.mark.parametrize("evidence", [[{"ref": "venture_document:synthetic", "state": "verified"}],
    {"order_ref": "device_order:synthetic", "effect_outcome": "completed"}])
def test_whole_completion_never_bypasses_a_step_count_check(draft_db, evidence):
    from app.tasks.locks import lock_run, lock_step
    d = draft_db
    with d["factory"]() as session:
        task, run = lock_run(session, "run_draft")
        _, _, step = lock_step(session, "step_draft")
        task.kind = "one_time"
        step.state, step.evidence_json = "completed", json.dumps(evidence)
        step.done_test_json = json.dumps({"type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 1}})
        d["service"].complete_run(session, task, run)
        session.commit()
        assert task.state != "completed" and run.state != "completed"
        assert json.loads(task.blocker_json)["class"] == "impossible"
    assert not d["calls"]
