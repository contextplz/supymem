"""Synthetic rubric/provider receipts. No network or paid provider calls run."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tests.company_os_harness import load_company_os_app
from tests.task_billing_fixtures import checkpoint, install_recovery, synthetic_receipt

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
REF = "venture_document:task-note:planned_verify"


@pytest.fixture()
def judge_db(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.credits import _txn
    from app.db.models import Base, Job, Person, PlannedTask, Venture, VentureDocument, Workspace
    from app.db.supymem_models import Notice
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.state import set_factory
    from app.tasks import usage, verification
    from app.tasks.contracts import Budget
    from app.tenancy import bind

    monkeypatch.setattr(_txn, "utcnow", lambda: NOW)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, Job, PlannedTask, VentureDocument, Task, TaskRun, TaskStep, TaskEvent, Notice)])
    set_factory(sessionmaker(engine, expire_on_commit=False))
    with Session(engine) as session:
        moment = NOW.replace(tzinfo=None)
        session.add(Person(id="usr_verify", display_name="Synthetic founder", created_at=moment))
        session.add(Workspace(id="ws_verify", owner_id="usr_verify", kind="venture", name="Synthetic",
            entry_mode="idea", created_at=moment, updated_at=moment))
        session.add(Venture(id="ven_verify", workspace_id="ws_verify", name="Synthetic", raw_idea="Synthetic task",
            stage="draft", created_at=moment, updated_at=moment))
        session.add(Task(id="tsk_verify", workspace_id="ws_verify", venture_id="ven_verify", owner_person_id="usr_verify",
            title="Draft a customer interview guide", goal="Draft a customer interview guide", kind="one_time",
            done_test_json=json.dumps({"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["Clear questions", "Useful next actions"]}}),
            budget_json=Budget().model_dump_json(), tolerance=0.8, state="working",
            deadline_at=moment + timedelta(minutes=20), created_at=moment, updated_at=moment))
        session.add(TaskRun(id="trn_verify", task_id="tsk_verify", run_key="initial", state="working",
            started_at=moment, deadline_at=moment + timedelta(minutes=20)))
        inputs = {"task_id": "planned_verify", "_actor_subject": "usr_verify"}
        session.add(Job(id="job_artifact", workspace_id="ws_verify", venture_id="ven_verify", kind="task.run",
            payload_json=json.dumps(inputs), status="succeeded", created_at=moment))
        session.add(PlannedTask(id="planned_verify", workspace_id="ws_verify", venture_id="ven_verify",
            title="Draft a customer interview guide", why_now="Synthetic", status="done", finished_at=moment,
            created_at=moment, updated_at=moment))
        session.add(VentureDocument(id="task-note:planned_verify", venture_id="ven_verify", kind="task_note",
            title="Draft a customer interview guide", body_md="# Interview guide\n\nStatus: completed\n\nAsk about the last real incident. Ask what it cost. End with one agreed next action.\n",
            provenance_json=json.dumps({"task_id": "planned_verify", "source": "task.run"}), created_at=moment, updated_at=moment))
        session.add(TaskStep(id="tst_verify", run_id="trn_verify", position=0, title="Draft a customer interview guide",
            skill="task.run", inputs_json=json.dumps(inputs), evidence_json=json.dumps([{"ref": REF, "state": "completed"}]),
            job_id="job_artifact", state="completed", finished_at=moment, deadline_at=moment + timedelta(minutes=20)))
        session.add(Job(id="job_judge", kind="tasks.verify", workspace_id="ws_verify", venture_id="ven_verify",
            payload_json=json.dumps({"task_run_id": "trn_verify", "_actor_subject": "usr_verify"}), status="running", created_at=moment))
        session.commit()
    d = SimpleNamespace(module=verification, engine=engine, bind=bind, calls=[], requests=[],
        transport_error=None, during_call=None, after_checkpoint=None, meter_exit_error=False, receipt=None,
        billing=install_recovery(monkeypatch), billing_source="platform", blocking=False, usage={"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        response={"score": 0.85, "criterion_scores": [
            {"criterion_index": 0, "score": 0.9, "evidence_refs": [REF], "reason": "The questions ask about a concrete past incident."},
            {"criterion_index": 1, "score": 0.8, "evidence_refs": [REF], "reason": "The guide ends with an agreed next action."}],
            "evidence_refs": [REF], "reason": "The guide meets both review criteria."})

    async def synthetic_llm(messages, **kwargs):
        transport_options = {key: value for key, value in kwargs.items() if key != "before_meter_exit"}
        d.requests.append(transport_options)
        estimate, actual = (0, 0) if d.billing_source == "free" else (500, 32)
        invocation = usage.before_call(estimate, 1600)
        d.calls.append((messages, transport_options))
        if d.during_call:
            d.during_call()
        if d.blocking:
            d.started.set()
            await d.release.wait()
        if d.transport_error:
            usage.abort_call(invocation, definitive=False)
            raise d.transport_error
        content = d.response if isinstance(d.response, str) else json.dumps(d.response)
        result = SimpleNamespace(usage=d.usage, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])
        if d.usage is None:
            usage.abort_call(invocation, definitive=False)
        else:
            d.receipt = synthetic_receipt(kwargs, invocation, estimate_micro=estimate, actual_micro=actual,
                prompt=100, completion=200, source=d.billing_source)
            if d.billing_source == "byok":
                d.receipt["actual_micro"] = 0
            checkpoint(result, kwargs, d.receipt)
            if d.after_checkpoint:
                d.after_checkpoint()
            if d.meter_exit_error:
                from app.credits.errors import CreditsUnavailable
                raise CreditsUnavailable("Synthetic metering exit did not confirm settlement")
            d.billing.confirm(d.receipt)
            usage.after_call(invocation, actual, 300)
        return result

    # S02 owns the real wrapper. Only this synthetic test transport is injected.
    transport = ModuleType("app.credits.metered")
    transport.llm = synthetic_llm
    monkeypatch.setitem(sys.modules, "app.credits.metered", transport)
    yield d
    engine.dispose()


def payload(**overrides):
    return {"task_run_id": "trn_verify", "_job_id": "job_judge", "workspace_id": "ws_verify", "venture_id": "ven_verify", **overrides}


def verify(d, **overrides):
    with d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
        return asyncio.run(d.module.verify(payload(**overrides)))


def saved(d):
    from app.db.task_models import Task, TaskRun, TaskEvent
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        return SimpleNamespace(task_state=task.state, run_state=run.state, blocker=json.loads(task.blocker_json or "{}"),
            evaluation=json.loads(run.evaluation_json or "{}"), tokens=run.tokens_used, cost=run.cost_micro,
            reserved=run.reserved_cost_micro, events=[row.data_json for row in session.scalars(select(TaskEvent))])


def test_judge_is_metered_bounded_and_finishes_only_after_valid_evidence(judge_db):
    d = judge_db
    result = verify(d, evidence=[{"ref": "invented", "body": "Caller-injected material must never reach the judge."}])
    assert result == {"state": "complete", "passed": True, "score": pytest.approx(0.85)}
    assert saved(d).task_state == saved(d).run_state == "completed"
    messages, options = d.calls[0]
    assert options["purpose"] == "task.verify" and options["tier"] == "pro" and options["max_tokens"] == 1500
    assert options["request_id"] == "task-verify:trn_verify:" + saved(d).evaluation["artifact_digest"]
    assert sum(len(message["content"]) for message in messages) <= 12000
    assert "Caller-injected" not in json.dumps(messages)
    assert saved(d).cost == 32 and saved(d).tokens == 300 and saved(d).reserved == 0
    assert saved(d).evaluation["valid"] is True and saved(d).evaluation["usage"]["total_tokens"] == 300
    assert verify(d)["passed"] and len(d.calls) == 1


def test_score_below_threshold_keeps_partial_result_and_typed_blocker(judge_db):
    d = judge_db
    d.response["score"] = 0.5
    for item in d.response["criterion_scores"]:
        item["score"] = 0.5
    assert verify(d)["passed"] is False
    assert saved(d).task_state == saved(d).run_state == "failed"
    assert saved(d).blocker["class"] == "impossible"
    assert saved(d).evaluation["evidence_refs"] == [REF]
    assert verify(d)["passed"] is False and len(d.calls) == 1


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "invented_ref", "score_mismatch", "confidence", "invalid_score", "bad_json", "secret_reason"])
def test_invalid_judgment_is_charged_once_and_never_success(judge_db, mutation):
    d = judge_db
    if mutation == "missing":
        d.response["criterion_scores"].pop()
    elif mutation == "duplicate":
        d.response["criterion_scores"][1]["criterion_index"] = 0
    elif mutation == "invented_ref":
        d.response["criterion_scores"][0]["evidence_refs"] = ["venture_document:unowned"]
    elif mutation == "score_mismatch":
        d.response["score"] = 1.0
    elif mutation == "confidence":
        d.response["confidence"] = 1.0
    elif mutation == "invalid_score":
        d.response["score"] = True
    elif mutation == "secret_reason":
        d.response["reason"] = "sk" + "-" + "synthetic-credential-1234567890"
    else:
        d.response = "not JSON"
    assert verify(d)["state"] == "blocked"
    assert saved(d).evaluation["valid"] is False and saved(d).evaluation["state"] == "responded"
    assert saved(d).blocker["class"] == "impossible" and saved(d).cost == 32
    assert not verify(d)["passed"] and len(d.calls) == 1
    if mutation == "secret_reason":
        assert d.response["reason"] not in json.dumps(saved(d).evaluation)


@pytest.mark.parametrize("mode", ["transport", "missing_usage", "calling_checkpoint"])
def test_ambiguous_call_or_journal_requires_reconciliation_without_replay(judge_db, mode):
    from app.db.task_models import TaskRun
    d = judge_db
    if mode == "transport":
        d.transport_error = TimeoutError("Synthetic provider timeout")
    elif mode == "missing_usage":
        d.usage = None
    else:
        with Session(d.engine) as session:
            session.get(TaskRun, "trn_verify").evaluation_json = json.dumps({"state": "calling", "request_id": "original-call"})
            session.commit()
    assert verify(d)["state"] == "uncertain"
    assert saved(d).evaluation["state"] == "uncertain"
    assert saved(d).blocker["reconciliation_required"] and saved(d).task_state == "input_required"
    count = len(d.calls)
    assert verify(d)["state"] == "uncertain" and len(d.calls) == count
    if mode != "calling_checkpoint":
        assert saved(d).reserved == 500


def test_complete_checkpoint_recovers_without_another_call(judge_db):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    verify(d)
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        checkpoint = json.loads(run.evaluation_json)
        checkpoint["state"] = "responded"
        run.evaluation_json = json.dumps(checkpoint)
        task.state = run.state = "working"
        session.commit()
    assert verify(d)["state"] == "recovered"
    assert saved(d).task_state == "completed" and len(d.calls) == 1


@pytest.mark.parametrize("mutation", ["artifact", "criteria"])
def test_artifact_or_contract_change_during_call_cannot_complete(judge_db, mutation):
    from app.db.models import VentureDocument
    from app.db.task_models import Task
    d = judge_db
    def change():
        with Session(d.engine) as session:
            if mutation == "artifact":
                session.get(VentureDocument, "task-note:planned_verify").body_md += "A later changed conclusion."
            else:
                task = session.get(Task, "tsk_verify")
                check = json.loads(task.done_test_json)
                check["spec"]["criteria"] = ["A different criterion"]
                task.done_test_json = json.dumps(check)
            session.commit()
    d.during_call = change
    assert verify(d)["state"] == "blocked"
    assert saved(d).task_state == "failed" and saved(d).evaluation["state"] == "responded"


def test_unverified_step_reference_blocks_before_model_call(judge_db):
    from app.db.task_models import TaskStep
    d = judge_db
    with Session(d.engine) as session:
        session.get(TaskStep, "tst_verify").evidence_json = json.dumps([{"ref": "venture_document:invented"}])
        session.commit()
    assert verify(d)["state"] == "blocked"
    assert not d.calls and saved(d).task_state == "failed"


def test_rubric_cannot_override_a_failed_deterministic_step_check(judge_db):
    from app.db.task_models import TaskStep
    d = judge_db
    with Session(d.engine) as session:
        session.get(TaskStep, "tst_verify").done_test_json = json.dumps({"type": "deterministic",
            "spec": {"predicate": "at_least", "metric": "draft_count", "count": 2}})
        session.commit()
    assert verify(d)["state"] == "blocked"
    assert not d.calls and saved(d).task_state == "failed"


def test_saved_secret_is_never_sent_or_copied_to_verification_events(judge_db):
    from app.db.models import VentureDocument
    d = judge_db
    fake_secret = "sk" + "-" + "synthetic-credential-1234567890"
    with Session(d.engine) as session:
        session.get(VentureDocument, "task-note:planned_verify").body_md += fake_secret
        session.commit()
    assert verify(d)["state"] == "blocked"
    assert not d.calls and saved(d).blocker["class"] == "needs_input"
    assert fake_secret not in json.dumps(saved(d).events)


@pytest.mark.parametrize("field", ["workspace_id", "venture_id", "_job_id"])
def test_payload_scope_and_owned_verifier_job_are_required(judge_db, field):
    with pytest.raises(PermissionError, match="scope_mismatch"):
        verify(judge_db, **{field: "unowned"})
    assert not judge_db.calls


def test_task_budget_stops_before_transport(judge_db):
    from app.db.task_models import Task
    from app.tasks.contracts import Budget
    d = judge_db
    with Session(d.engine) as session:
        session.get(Task, "tsk_verify").budget_json = Budget(cost_micro=0).model_dump_json()
        session.commit()
    assert verify(d)["state"] == "blocked"
    assert not d.calls and saved(d).blocker["class"] == "budget_exhausted"


def test_canceled_during_call_keeps_confirmed_usage_without_resurrection(judge_db):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    def cancel():
        with Session(d.engine) as session:
            session.get(Task, "tsk_verify").state = session.get(TaskRun, "trn_verify").state = "canceled"
            session.commit()
    d.during_call = cancel
    assert verify(d)["state"] == "stopped"
    assert saved(d).task_state == "canceled" and saved(d).cost == 32
    assert saved(d).evaluation["state"] == "responded" and len(d.calls) == 1


def test_concurrent_attempt_observes_journal_and_cannot_duplicate_paid_call(judge_db):
    d = judge_db
    async def exercise():
        d.blocking, d.started, d.release = True, asyncio.Event(), asyncio.Event()
        with d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
            first = asyncio.create_task(d.module.verify(payload()))
            await d.started.wait()
            second = await d.module.verify(payload())
            assert second["state"] == "uncertain"
            d.release.set()
            assert (await first)["passed"]
    asyncio.run(exercise())
    assert len(d.calls) == 1 and saved(d).task_state == "completed"


def test_large_artifact_prompt_is_bounded_and_labeled_truncated(judge_db):
    from app.db.models import VentureDocument
    d = judge_db
    with Session(d.engine) as session:
        session.get(VentureDocument, "task-note:planned_verify").body_md += "A concrete observation.\n" * 4000
        session.commit()
    verify(d)
    messages = d.calls[0][0]
    assert sum(len(message["content"]) for message in messages) <= 12000
    assert json.loads(messages[-1]["content"])["truncated"] is True


def test_normalized_rubric_and_private_invoice_are_saved_before_meter_exit(judge_db):
    d, observed = judge_db, []
    def before_exit():
        row = saved(d)
        assert row.task_state == row.run_state == "working" and row.reserved == 500 and row.cost == 0
        assert row.evaluation["state"] == "responded" and row.evaluation["valid"] is True
        assert row.evaluation["billing"]["format"] == "task_billing_v1"
        assert row.evaluation["evaluation_sha256"] and not d.billing.proofs
        observed.append(True)
    d.after_checkpoint = before_exit
    assert verify(d)["passed"] and observed == [True]
    assert "before_meter_exit" not in d.calls[0][1]


def test_meter_exit_failure_keeps_paid_judgment_until_exact_invoice_recovery(judge_db):
    d = judge_db
    d.meter_exit_error = True
    assert verify(d)["state"] == "billing_pending"
    assert saved(d).evaluation["state"] == "responded" and saved(d).evaluation["valid"] is True
    assert saved(d).task_state != "completed" and saved(d).reserved == 500
    assert verify(d)["state"] == "billing_pending" and len(d.calls) == 1
    d.billing.confirm(d.receipt)
    assert verify(d)["state"] == "recovered"
    assert saved(d).task_state == "completed" and (saved(d).reserved, saved(d).cost, saved(d).tokens) == (0, 32, 300)
    assert len(d.calls) == 1


@pytest.mark.parametrize("mutation", ["missing_billing", "billing_hash", "judgment"])
def test_cached_rubric_rejects_missing_or_tampered_paid_checkpoint_without_redial(judge_db, mutation):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    assert verify(d)["passed"]
    with Session(d.engine) as session:
        run = session.get(TaskRun, "trn_verify")
        data = json.loads(run.evaluation_json)
        data["state"] = "responded"
        if mutation == "missing_billing":
            data.pop("billing")
        elif mutation == "billing_hash":
            data["billing"]["sha256"] = "changed"
        else:
            data["score"] = 1
        run.evaluation_json = json.dumps(data)
        session.get(Task, "tsk_verify").state = run.state = "working"
        session.commit()
    assert verify(d)["state"] == "billing_pending"
    assert saved(d).task_state != "completed" and len(d.calls) == 1


@pytest.mark.parametrize("source", ["byok", "free"])
def test_byok_and_free_zero_charge_are_valid_only_with_exact_task_invocation(judge_db, source):
    d = judge_db
    d.billing_source = source
    assert verify(d)["passed"] and verify(d)["passed"]
    assert saved(d).evaluation["billing"]["usage"]["source"] == source
    assert saved(d).cost == (32 if source == "byok" else 0) and saved(d).tokens == 300
    assert len(d.calls) == 1 and d.billing.calls


@pytest.mark.parametrize("state", ["input_required", "auth_required", "approval_required", "paused", "canceled", "failed", "rejected"])
@pytest.mark.parametrize("failure", ["invalid", "uncertain", "budget", "account"])
def test_verifier_error_save_preserves_every_unrelated_stop(judge_db, state, failure):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    with d.bind(workspace_id="ws_verify", venture_id="ven_verify", actor="usr_verify"):
        snapshot, done = d.module._prepare(payload())
        assert done is None
        original = json.dumps({"class": "needs_approval", "reason": "Keep the exact saved review open.",
            "unblock_action": "Review its original authority."})
        with Session(d.engine) as session:
            task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
            task.state = run.state = state
            task.blocker_json = original
            session.commit()
        flags = {"uncertain": failure == "uncertain", "budget": failure == "budget", "account": failure == "account"}
        d.module._save(payload(), snapshot, {"valid": False}, **flags)
    with Session(d.engine) as session:
        assert session.get(Task, "tsk_verify").state == session.get(TaskRun, "trn_verify").state == state
        assert session.get(Task, "tsk_verify").blocker_json == original
    assert d.calls == []


@pytest.mark.parametrize("state", ["input_required", "auth_required", "approval_required"])
def test_unknown_call_replay_preserves_existing_waiting_requirement(judge_db, state):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    original = json.dumps({"class": "needs_approval", "reason": "Keep this requirement.", "unblock_action": "Review it."})
    with Session(d.engine) as session:
        task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
        task.state = run.state = state
        task.blocker_json = original
        run.evaluation_json = json.dumps({"state": "calling", "request_id": "earlier unresolved call"})
        session.commit()
    assert verify(d)["state"] == "uncertain"
    with Session(d.engine) as session:
        assert session.get(Task, "tsk_verify").state == session.get(TaskRun, "trn_verify").state == state
        assert session.get(Task, "tsk_verify").blocker_json == original
    assert d.calls == []


@pytest.mark.parametrize("state", ["input_required", "auth_required", "approval_required"])
def test_paid_invalid_judgment_does_not_replace_a_late_review_or_input(judge_db, state):
    from app.db.task_models import Task, TaskRun
    d = judge_db
    original = json.dumps({"class": "needs_approval", "reason": "This late review stays open.", "unblock_action": "Review it."})
    def stop():
        with Session(d.engine) as session:
            task, run = session.get(Task, "tsk_verify"), session.get(TaskRun, "trn_verify")
            task.state = run.state = state
            task.blocker_json = original
            session.commit()
    d.during_call = stop
    d.response["evidence_refs"] = ["invented source"]
    verify(d)
    with Session(d.engine) as session:
        assert session.get(Task, "tsk_verify").state == session.get(TaskRun, "trn_verify").state == state
        assert session.get(Task, "tsk_verify").blocker_json == original
    assert len(d.calls) == 1 and saved(d).cost == 32


@pytest.mark.parametrize("mutation", ["artifact", "goal", "criteria"])
def test_completed_replay_rechecks_current_goal_and_artifact_without_new_judgment(judge_db, mutation):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun
    d = judge_db
    assert verify(d)["passed"]
    with Session(d.engine) as session:
        task = session.get(Task, "tsk_verify")
        if mutation == "artifact":
            session.get(VentureDocument, "task-note:planned_verify").body_md += "A changed saved conclusion."
        elif mutation == "goal":
            task.goal = "A new goal after the earlier run finished"
        else:
            task.done_test_json = json.dumps({"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["A different review criterion"]}})
        session.commit()
    assert verify(d)["passed"] is False and len(d.calls) == 1
    with Session(d.engine) as session:
        assert session.get(Task, "tsk_verify").state == session.get(TaskRun, "trn_verify").state == "completed"
