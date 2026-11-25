"""Synthetic task lifecycle checks on SQLite; no live model or effects execute."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app import state
    from app.api import handoffs_api
    from app.chat import conversations
    from app.core import jobs
    from app.db.models import Base, Job, Person, Venture, Workspace
    from app.db.supymem_models import ExecutionPlan, Notice, ResearchDossier
    from app.db.task_models import MaintenanceSchedule, Task, TaskEvent, TaskRun, TaskStep
    from app.tasks import execution, service, usage
    from app.tenancy import Scope
    from app.worker import main as worker
    from core.clock import set_clock

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, Job, Notice, ResearchDossier, ExecutionPlan,
        Task, TaskRun, TaskStep, TaskEvent, MaintenanceSchedule)])
    factory = sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 10, 8, 12, tzinfo=timezone.utc)]
    set_clock(lambda: clock[0])
    monkeypatch.setattr(state, "db", lambda: factory)
    monkeypatch.setattr(execution, "db", lambda: factory)
    monkeypatch.setattr(usage, "db", lambda: factory)
    monkeypatch.setattr(handoffs_api, "db", lambda: factory)
    monkeypatch.setattr(conversations, "capture_job_conversation", lambda *a: None)
    monkeypatch.setattr(conversations, "bind_job_conversation", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(conversations, "job_receipt_conversation", lambda *a, **kw: nullcontext(False))
    monkeypatch.setattr(worker, "_job_scope", lambda job: Scope(job.workspace_id or "none",
        job.venture_id or "", job.payload.get("_actor_subject") or "system"))
    monkeypatch.setattr(worker, "_funnel_outcome", lambda *a: None)
    monkeypatch.setattr(worker, "_stage_funnel", lambda *a: None)
    monkeypatch.setattr(worker, "_record_failure", lambda *a: None)
    monkeypatch.setattr(jobs, "_watchdog_failure", lambda *a: None)
    monkeypatch.setattr(jobs, "publish_wake", lambda: None)
    with factory() as session:
        now = clock[0].replace(tzinfo=None)
        session.add(Person(id="usr_lifecycle", display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_lifecycle", owner_id="usr_lifecycle", kind="company", entry_mode="idea",
            name="Synthetic workspace", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_lifecycle", workspace_id="ws_lifecycle", name="Synthetic idea",
            raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.commit()
    yield {"factory": factory, "clock": clock, "jobs": jobs, "service": service, "worker": worker,
           "api": handoffs_api, "execution": execution, "usage": usage}
    set_clock(None)
    engine.dispose()


def _contract(**changes):
    from app.tasks.contracts import Contract
    return Contract.model_validate({"title": "Synthetic research", "goal": "Research my idea",
        "steps": [{"title": "Research", "skill": "research.deep"}], **changes})


@pytest.mark.parametrize("domain", ["industrial freight teams", "community music groups"])
@pytest.mark.parametrize("missing", ["venture", "task", "cancel"])
def test_local_missing_task_keeps_private_404_and_offers_venture_recovery(lifecycle, monkeypatch, domain, missing):
    from app.tenancy import bind

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    api = lifecycle["api"]
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        with pytest.raises(HTTPException) as error:
            if missing == "cancel":
                api.cancel_task("missing_" + domain.split()[0])
            else:
                with lifecycle["factory"]() as session:
                    if missing == "venture":
                        api._owner(session, "missing_" + domain.split()[0])
                    else:
                        api._task(session, "ven_lifecycle", "missing_" + domain.split()[0])
    assert error.value.status_code == 404
    assert error.value.detail == "Open your venture home, refresh Tasks, then choose an available saved task."


def _create(d, contract=None, *, start=True, create_key=None):
    with d["factory"]() as session:
        task = d["service"].create(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            owner_person_id="usr_lifecycle", contract=contract or _contract(), start=start, create_key=create_key)
        session.commit()
        return task.id


@pytest.mark.parametrize("failure", ["past_deadline", "after_enqueue"])
def test_past_daily_deadline_isolated_and_safety_commits_before_task_firing(lifecycle, monkeypatch, failure):
    from app.db.models import Venture, Workspace
    from app.db.task_models import MaintenanceSchedule, Task, TaskRun
    d = lifecycle
    d["clock"][0] = d["clock"][0].replace(hour=8)
    trigger = {"rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0", "tz": "UTC"}
    bad_id = _create(d, _contract(kind="scheduled", trigger=trigger,
                                goal="Research my idea by today at 9am" if failure == "past_deadline" else "Research my idea"), start=False)
    start_run = d["service"].start_run

    def start(session, task, key):
        run = start_run(session, task, key)
        if task.id == bad_id and failure == "after_enqueue":
            raise RuntimeError("synthetic post-enqueue failure")
        return run

    monkeypatch.setattr(d["service"], "start_run", start)
    with d["factory"]() as session:
        now = d["clock"][0].replace(tzinfo=None)
        session.add(Workspace(id="ws_other", owner_id="usr_lifecycle", kind="company", entry_mode="idea",
                              name="Other workspace", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_other", workspace_id="ws_other", name="Other venture",
                            raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.flush()
        good = d["service"].create(session, workspace_id="ws_other", venture_id="ven_other",
                                   owner_person_id="usr_lifecycle", contract=_contract(kind="scheduled", trigger=trigger), start=False)
        good_id = good.id
        session.commit()
    d["clock"][0] += timedelta(hours=2)
    maintenance = []

    def sweep():
        with d["factory"]() as session:
            assert session.get(MaintenanceSchedule, "maintenance").next_fire_at > d["clock"][0].replace(tzinfo=None)
        maintenance.append(True)

    monkeypatch.setattr(d["worker"], "_run_maintenance", sweep)
    monkeypatch.setattr(d["worker"], "publish_wake", lambda: None)
    result = d["worker"]._run_scheduler()
    assert maintenance == [True] and len(result["tasks"]) == 1
    with d["factory"]() as session:
        bad, good = session.get(Task, bad_id), session.get(Task, good_id)
        assert bad.state == "failed" and bad.next_fire_at is None and bad.blocker_json
        assert good.state == "working" and good.next_fire_at > d["clock"][0].replace(tzinfo=None)
        assert session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == bad_id)) == 0
        assert session.scalar(select(func.count()).select_from(TaskRun).where(TaskRun.task_id == good_id)) == 1
    assert d["worker"]._run_scheduler()["tasks"] == []
    d["clock"][0] += timedelta(minutes=1)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic task reconciliation failure")

    monkeypatch.setattr(d["service"], "fire_due", fail)
    with pytest.raises(RuntimeError, match="synthetic task reconciliation failure"):
        d["worker"]._run_scheduler()
    assert maintenance == [True, True]


def _admit(d, *, dedupe="synthetic-research", context=None, kind="research.deep"):
    with d["factory"]() as session, (d["service"].step_context(*context) if context else nullcontext()):
        jid = d["jobs"].enqueue(session, kind, {"session_id": "idea_synthetic", "revision_id": "rev_synthetic",
            "_actor_subject": "usr_lifecycle"}, workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            dedupe_key=dedupe, effect_key="effect:" + dedupe)
        session.commit()
        return jid


def test_terminal_publication_job_uses_owned_receipt_reason_not_generic_worker_code(lifecycle, monkeypatch):
    from app.tasks import skills
    from app.tasks.contracts import blocker
    d = lifecycle
    jid = _admit(d, kind="pagebuilder.publish")
    expected = blocker("pagebuilder_publication_scope_mismatch")
    checked = []
    def verify(_session, kind, payload, **scope):
        checked.append((kind, payload["_job_id"], scope["venture_id"]))
        return {"verified": False, "blocker": expected}
    monkeypatch.setattr(skills, "verify_result", verify)
    with d["factory"]() as session:
        d["service"].after_job(session, jid, error="pagebuilder_publication_refused")
        session.commit()
    task, _, _ = _rows(d)
    assert checked == [("pagebuilder.publish", jid, "ven_lifecycle")]
    assert json.loads(task.blocker_json) == expected


def test_deleting_legacy_job_keeps_task_history_and_detaches_job_links(lifecycle):
    from sqlalchemy import text
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    d = lifecycle
    with d["factory"]() as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
    jid = _admit(d)
    with d["factory"]() as session:
        step = session.scalar(select(TaskStep).where(TaskStep.job_id == jid))
        run = session.get(TaskRun, step.run_id)
        task = session.get(Task, run.task_id)
        run.control_job_id = jid
        d["service"].event(session, task, "queued", "Saved original admission", run_id=run.id,
                            step_id=step.id, data={"job_id": jid})
        session.commit()
        session.delete(session.get(Job, jid))
        session.commit()
        session.expire_all()
        assert step.job_id is None and run.control_job_id is None
        assert run.run_key == jid and session.get(Task, task.id) is not None
        saved = session.scalar(select(TaskEvent).where(TaskEvent.kind == "queued"))
        assert json.loads(saved.data_json) == {"job_id": jid}


def _rows(d, task_id=None):
    from app.db.task_models import Task, TaskRun, TaskStep
    with d["factory"]() as session:
        task = session.get(Task, task_id) if task_id else session.scalar(select(Task))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id).order_by(TaskStep.position)).all() if run else []
        return task, run, steps


def _claim(d, kind="research.deep"):
    with d["factory"]() as session:
        claimed = d["jobs"].lease(session, "synthetic-worker", kinds=[kind], now=d["clock"][0])
        session.commit()
        assert len(claimed) == 1
        return claimed[0]


def _verified(monkeypatch):
    from app.tasks import skills
    monkeypatch.setattr(skills, "verify_result", lambda *a, **kw: {"verified": True, "pending": False,
        "evidence": [{"ref": "synthetic:fixture_receipt", "state": "stored"}], "blocker": None})


def test_admission_projects_once_and_preserves_original_effect_custody(lifecycle):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d = lifecycle
    first, replay = _admit(d), _admit(d)
    assert first == replay
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 1
        assert session.scalar(select(func.count()).select_from(TaskStep)) == 1
        step = session.scalar(select(TaskStep))
        job = session.get(Job, first)
        assert step.job_id == first and json.loads(step.inputs_json) == json.loads(job.payload_json)
        assert job.effect_key == "effect:synthetic-research" and job.token_budget > 0


@pytest.mark.parametrize("outcome", ["succeeded", "provider_retryable:RetryableError", "research_outcome_uncertain"])
@pytest.mark.parametrize("kind", ["research.deep", "pagebuilder.publish"])
def test_projected_workflow_replay_keeps_original_job_outcome(lifecycle, monkeypatch, outcome, kind):
    from app.db.models import Job
    from app.db.task_models import Task
    d, calls = lifecycle, []
    jid = _admit(d, kind=kind)
    _verified(monkeypatch)

    async def dispatch(kind, payload):
        assert d["usage"].context() is None
        calls.append(payload["_job_attempt"])
        if outcome == "research_outcome_uncertain":
            raise d["jobs"].FatalError(outcome)
        if outcome != "succeeded":
            raise d["jobs"].RetryableError("unknown usage")

    monkeypatch.setattr(d["worker"], "dispatch", dispatch)
    for attempt in range(2):
        claimed = _claim(d, kind)
        asyncio.run(d["worker"]._run_one(d["factory"], claimed, {jid}))
        with d["factory"]() as session:
            saved = session.get(Job, jid)
            assert saved.status == ("succeeded" if outcome == "succeeded" else
                "failed_dead" if outcome == "research_outcome_uncertain" else "retry_wait")
            assert saved.last_error == (None if outcome == "succeeded" else outcome)
            assert not saved.cancel_requested
            if attempt == 0:
                task = session.scalar(select(Task))
                task.state = "completed" if outcome == "succeeded" else "input_required"
                saved.status, saved.run_after, saved.scheduled_for = "retry_wait", None, None
                session.commit()
    assert calls == [1, 2]


def test_create_request_replay_has_one_run_and_outbox_job(lifecycle):
    from app.db.models import Job
    from app.db.task_models import TaskRun
    d = lifecycle
    task_id = _create(d, create_key="synthetic-create")
    assert _create(d, create_key="synthetic-create") == task_id
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(TaskRun)) == 1
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "tasks.execute")) == 1


def test_request_key_cannot_silently_adopt_changed_contract(lifecycle):
    _create(lifecycle, create_key="synthetic-create")
    with pytest.raises(ValueError, match="idempotency_conflict"):
        _create(lifecycle, _contract(budget={"tokens": 1000}), create_key="synthetic-create")


def test_recovered_admission_links_or_explains_current_step_without_duplicate_job(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskStep
    from app.research import service as research
    d = lifecycle
    jid = _admit(d)
    task_id = _create(d)
    _, run, steps = _rows(d, task_id)
    # Replace only the founder-domain admission seam. The task admission
    # checkpoint, recovery handling, and real jobs outbox remain exercised.
    monkeypatch.setattr(research, "start", lambda *a, **kw: {"job_id": _admit(d)})
    asyncio.run(d["execution"].execute({"task_run_id": run.id,
        "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    with d["factory"]() as session:
        linked = session.get(TaskStep, steps[0].id)
        task = session.get(Task, task_id)
        assert linked.job_id == jid or (task.state == "input_required"
            and json.loads(task.blocker_json)["class"] == "needs_input"), "Recovered admission was left stuck without an explanation"
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "research.deep")) == 1


def test_cancel_stops_queued_steps_and_keeps_inflight_lease_until_settlement(lifecycle):
    from app.db.models import Job
    from app.db.task_models import Task, TaskRun, TaskStep
    d = lifecycle
    task_id = _create(d, _contract(steps=[{"title": "First", "skill": "research.deep"},
        {"title": "Second", "skill": "research.deep"}]))
    _, run, steps = _rows(d, task_id)
    first = _admit(d, dedupe="first", context=(run.id, steps[0].id))
    queued = _admit(d, dedupe="second", context=(run.id, steps[1].id))
    claimed = _claim(d)
    assert claimed.id == first
    with d["factory"]() as session:
        d["service"].cancel(session, session.get(Task, task_id))
        session.commit()
    with d["factory"]() as session:
        from app.tasks.locks import lock_for_job
        lock_for_job(session, first)
        inflight, future = session.get(Job, first), session.get(Job, queued)
        assert inflight.status == "leased" and inflight.cancel_requested
        assert future.status == "cancelled" and future.cancel_requested
        # A provider result may still be recorded while the original lease is
        # held. Only then does complete_job close the cancelled attempt.
        inflight.payload_json = json.dumps({**json.loads(inflight.payload_json),
            "synthetic_settlement": {"state": "settled", "cost_micro": 10}})
        d["jobs"].complete_job(session, first, worker_id=claimed.worker_id, attempt=claimed.attempts)
        session.commit()
    with d["factory"]() as session:
        assert session.get(Job, first).status == "cancelled"
        assert json.loads(session.get(Job, first).payload_json)["synthetic_settlement"]["state"] == "settled"
        assert session.get(TaskRun, run.id).state == "canceled"
        assert all(step.state == "canceled" for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)))
        assert d["jobs"].lease(session, "after-cancel", kinds=["research.deep"]) == []


def test_pause_preserves_queued_job_for_resume(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.tenancy import bind
    d, calls = lifecycle, []
    jid = _admit(d)
    task, _, _ = _rows(d)
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        assert d["api"].pause("ven_lifecycle", task.id)["state"] == "paused"
    async def dispatch(kind, payload):
        calls.append((kind, payload))
    monkeypatch.setattr(d["worker"], "dispatch", dispatch)
    claimed = _claim(d)
    asyncio.run(d["worker"]._run_one(d["factory"], claimed, {claimed.id}))
    assert not calls
    with d["factory"]() as session:
        job = session.get(Job, jid)
        assert not job.cancel_requested and job.status not in {"cancelled", "failed_dead"}
    _verified(monkeypatch)
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        assert d["api"].resume("ven_lifecycle", task.id)["state"] == "working"
    d["clock"][0] += timedelta(minutes=1)
    claimed = _claim(d)
    asyncio.run(d["worker"]._run_one(d["factory"], claimed, {claimed.id}))
    assert len(calls) == 1 and _rows(d)[0].state == "completed"


def test_resume_reconciles_receipt_saved_while_paused(lifecycle, monkeypatch):
    from app.tenancy import bind
    d = lifecycle
    jid = _admit(d)
    task, _, _ = _rows(d)
    claimed = _claim(d)
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts)
        session.commit()
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        d["api"].pause("ven_lifecycle", task.id)
    _verified(monkeypatch)
    with d["factory"]() as session:
        d["jobs"].complete_job(session, jid, worker_id=claimed.worker_id, attempt=claimed.attempts)
        session.commit()
    if _rows(d)[0].state != "completed":
        with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_lifecycle"):
            d["api"].resume("ven_lifecycle", task.id)
    with d["factory"]() as session:
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
    assert _rows(d)[0].state == "completed"


def test_real_report_cannot_complete_an_unproven_step_count(lifecycle):
    from app.db.models import GtmContact, GtmTouch, VentureDocument
    from app.db.supymem_models import VentureWaitlistEntry
    d = lifecycle
    for model in (GtmContact, GtmTouch, VentureDocument, VentureWaitlistEntry):
        model.__table__.create(d["factory"].kw["bind"], checkfirst=True)
    tid = _create(d, _contract(goal="Summarize waitlist sign-ups and replies", steps=[{
        "title": "Summarize", "skill": "tasks.waitlist_summary", "done_test": {
            "type": "deterministic", "spec": {"predicate": "at_least", "metric": "source_count", "count": 1}}}]))
    _, run, _ = _rows(d, tid)
    asyncio.run(d["execution"].execute({"task_run_id": run.id, "workspace_id": "ws_lifecycle", "venture_id": "ven_lifecycle"}))
    task, saved_run, steps = _rows(d, tid)
    assert task.state == saved_run.state == steps[0].state == "failed"
    assert json.loads(task.blocker_json)["class"] == "impossible"
    with d["factory"]() as session:
        assert session.get(VentureDocument, "task-report:" + run.id) is not None


def test_task_owner_scope_is_enforced_for_reads_and_mutations(lifecycle):
    from app.tenancy import bind
    d = lifecycle
    task_id = _create(d)
    with bind(workspace_id="ws_lifecycle", venture_id="ven_lifecycle", actor="usr_other"):
        with pytest.raises(HTTPException) as refused:
            d["api"].cancel("ven_lifecycle", task_id)
        assert refused.value.status_code == 403
    with bind(workspace_id="other_workspace", venture_id="ven_lifecycle", actor="usr_lifecycle"):
        with pytest.raises(HTTPException) as missing:
            d["api"].read_task("ven_lifecycle", task_id)
        assert missing.value.status_code == 404


def test_repeated_verified_callback_emits_one_completion_and_notice(lifecycle, monkeypatch):
    from app.db.supymem_models import Notice
    from app.db.task_models import TaskEvent
    d = lifecycle
    jid = _admit(d)
    _verified(monkeypatch)
    with d["factory"]() as session:
        for _ in range(2):
            d["service"].after_job(session, jid)
            session.flush()
        session.commit()
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.kind == "step_finished")) == 1
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.kind == "completed")) == 1
        assert session.scalar(select(func.count()).select_from(Notice).where(Notice.title == "Task finished")) == 1


def test_unknown_done_test_cannot_complete_from_generic_workflow_receipt(lifecycle, monkeypatch):
    d = lifecycle
    try:
        task_id = _create(d, _contract(done_test={"type": "deterministic",
            "spec": {"type": "source_count", "minimum": 20}}))
    except ValueError:
        return  # Rejecting an unsupported check before work is also safe.
    _, run, steps = _rows(d, task_id)
    jid = _admit(d, context=(run.id, steps[0].id))
    from app.tasks import skills
    monkeypatch.setattr(skills, "verify_result", lambda *a, **kw: {"verified": True, "pending": False,
        "evidence": [{"ref": "synthetic:one_source", "source_count": 1}], "blocker": None})
    with d["factory"]() as session:
        d["service"].after_job(session, jid)
        session.commit()
    assert _rows(d, task_id)[0].state != "completed", "The founder's done-test was never checked"


@pytest.mark.parametrize("budget_field,used_field", [("tokens", "tokens_used"), ("cost_micro", "cost_micro"),
    ("tool_calls", "tool_calls"), ("attempts", "attempts")])
def test_guard_rejects_next_work_at_hard_budget_ceiling(lifecycle, budget_field, used_field):
    from app.db.task_models import Task, TaskRun
    d = lifecycle
    task_id = _create(d, _contract(budget={budget_field: 1}))
    _, run, _ = _rows(d, task_id)
    with d["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run.id)
        setattr(run, used_field, 1)
        deltas = ({"tokens": 1} if budget_field == "tokens" else {"cost_micro": 1}
            if budget_field == "cost_micro" else {"tool_calls": 1} if budget_field == "tool_calls" else {"attempts": 1})
        assert not d["service"].guard(session, task, run, **deltas)


def test_before_job_does_not_overshoot_tool_call_cap(lifecycle):
    from app.db.task_models import TaskRun
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["factory"]() as session:
        from app.tasks.locks import lock_for_job
        lock_for_job(session, jid)
        from app.db.task_models import Task
        saved_task, saved_run = session.get(Task, task.id), session.get(TaskRun, run.id)
        budget = json.loads(saved_task.budget_json)
        budget["tool_calls"] = 1
        saved_task.budget_json = json.dumps(budget)
        saved_run.tool_calls = 1
        assert not d["service"].before_job(session, jid, 1)
        assert saved_run.tool_calls == 1


def test_wall_budget_blocks_work_but_estimate_slip_is_only_a_notice(lifecycle):
    from app.db.task_models import Task, TaskEvent, TaskRun
    d = lifecycle
    task_id = _create(d, _contract(estimate_seconds=60, budget={"wall_seconds": 180}))
    _, run, _ = _rows(d, task_id)
    with d["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run.id)
        run.state = "working"
        session.commit()
    d["clock"][0] += timedelta(seconds=61)
    with d["factory"]() as session:
        assert d["service"].guard(session, session.get(Task, task_id), session.get(TaskRun, run.id))
        d["service"].fire_due(session, now=d["clock"][0])
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
    with d["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.kind == "eta_slip")) == 1
        assert session.get(Task, task_id).state == "working"
    d["clock"][0] += timedelta(seconds=120)
    with d["factory"]() as session:
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
        task = session.get(Task, task_id)
        assert task.state == "input_required" and json.loads(task.blocker_json)["class"] == "budget_exhausted"


def test_event_delivery_is_deduped_filtered_and_tenant_scoped(lifecycle):
    from app.db.task_models import TaskRun
    d = lifecycle
    task_id = _create(d, _contract(kind="event", trigger={"event_type": "waitlist.signup", "filter": {"source": "website"}}))
    with d["factory"]() as session:
        emit = d["service"].emit_domain_event
        assert emit(session, workspace_id="other", venture_id="ven_lifecycle", event_type="waitlist.signup",
                    event_id="event_synthetic", data={"source": "website"}) == []
        assert emit(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle", event_type="waitlist.signup",
                    event_id="event_synthetic", data={"source": "other"}) == []
        for _ in range(2):
            emit(session, workspace_id="ws_lifecycle", venture_id="ven_lifecycle", event_type="waitlist.signup",
                 event_id="event_synthetic", data={"source": "website"})
        session.commit()
        runs = session.scalars(select(TaskRun).where(TaskRun.task_id == task_id)).all()
        assert len(runs) == 1 and runs[0].run_key == "event:waitlist.signup:event_synthetic"


def test_unknown_safe_handler_exception_retries_once(lifecycle, monkeypatch):
    from app.db.models import Job
    d, calls = lifecycle, []
    with d["factory"]() as session:
        jid = d["jobs"].enqueue(session, "ops.signal_sweep", {})
        session.commit()
    async def broken(kind, payload):
        calls.append(payload["_job_attempt"])
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(d["worker"], "dispatch", broken)
    for expected in ("retry_wait", "failed_dead"):
        claimed = _claim(d, "ops.signal_sweep")
        asyncio.run(d["worker"]._run_one(d["factory"], claimed, {claimed.id}))
        with d["factory"]() as session:
            assert session.get(Job, jid).status == expected
        d["clock"][0] += timedelta(minutes=1)
    assert calls == [1, 2]


def test_general_worker_preserves_known_pagebuilder_source_failure(lifecycle, monkeypatch):
    from app.db.models import Job
    d = lifecycle
    with d["factory"]() as session:
        job_id = d["jobs"].enqueue(session, "pagebuilder.build", {"revision_id": "revision_fixture"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
        session.commit()
    async def invalid(_kind, _payload):
        raise ValueError("pagebuilder_candidate_invalid: private provider detail")
    monkeypatch.setattr(d["worker"], "dispatch", invalid)
    job = _claim(d, "pagebuilder.build")
    asyncio.run(d["worker"]._run_one(d["factory"], job, {job.id}))
    with d["factory"]() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "failed_dead" and saved.last_error == "pagebuilder_candidate_invalid"
    assert _rows(d)[0].state == "input_required"


def test_same_live_attempt_duplicate_preserves_task_budget_and_owner(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import Task, TaskEvent, TaskRun
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    claimed = _claim(d)
    with d["factory"]() as session:
        from app.tasks.locks import lock_for_job
        lock_for_job(session, jid)
        saved_task = session.get(Task, task.id)
        budget = json.loads(saved_task.budget_json)
        budget["tool_calls"] = 1
        saved_task.budget_json = json.dumps(budget)
        assert d["service"].before_job(session, jid, claimed.attempts)
        session.commit()
    with d["usage"].job_context(jid):
        d["usage"].before_tool()  # The owner has consumed its actual tool cap.
    with d["factory"]() as session:
        events_before = session.scalar(select(func.count()).select_from(TaskEvent))
    calls = []
    async def duplicate(kind, payload):
        calls.append(payload)
        raise d["worker"].FatalError("research_operation_in_progress")
    monkeypatch.setattr(d["worker"], "dispatch", duplicate)
    running = {(jid, claimed.attempts)}
    asyncio.run(d["worker"]._run_one(d["factory"], claimed, running))
    with d["factory"]() as session:
        saved_job, saved_run = session.get(Job, jid), session.get(TaskRun, run.id)
        assert (saved_job.status, saved_job.locked_by, saved_job.attempts) == ("leased", claimed.worker_id, 1)
        assert not saved_job.cancel_requested and running == {(jid, claimed.attempts)} and len(calls) == 1
        assert saved_run.attempts == saved_run.tool_calls == 1
        assert session.get(Task, task.id).state == "working"
        assert session.scalar(select(func.count()).select_from(TaskEvent)) == events_before


def test_crash_uncertainty_projects_typed_blocker_instead_of_working_forever(lifecycle):
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    d = lifecycle
    jid = _admit(d)
    claimed = _claim(d)
    with d["factory"]() as session:
        assert d["service"].before_job(session, jid, claimed.attempts)
        session.add(ResearchDossier(id=identifier(jid), workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
            revision_id="rev_synthetic", kind="deep", state="running", created_at=d["clock"][0],
            summary=json.dumps({"_runtime": {"job_id": jid, "operations": {"synthetic_call": {"state": "calling"}}}})))
        session.commit()
    d["clock"][0] += timedelta(seconds=100)
    with d["factory"]() as session:
        assert d["jobs"].reclaim_stale(session, now=d["clock"][0]) == 1
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
    task, run, _ = _rows(d)
    assert task.state == run.state == "input_required"
    assert json.loads(task.blocker_json)["class"] == "needs_input"
    with d["factory"]() as session:
        view = d["service"].view(session, session.get(type(task), task.id))
        assert view["action"]["label"] == "Run research again"
        assert view["research_recovery"]["job_id"] == jid
        assert view["research_recovery"]["saved_href"].startswith("/v/ven_lifecycle/research")
        assert "reconciliation" not in view["blocker"]["reason"]


def test_pending_receipt_reconciliation_recovers_without_dispatch(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.tasks import skills
    d = lifecycle
    jid = _admit(d)
    with d["factory"]() as session:
        session.get(Job, jid).status = "succeeded"
        session.commit()
    monkeypatch.setattr(skills, "verify_result", lambda *a, **kw: {"verified": False, "pending": True,
        "evidence": [{"ref": "synthetic:queued_publication"}], "blocker": None})
    with d["factory"]() as session:
        d["service"].after_job(session, jid)
        session.commit()
    assert _rows(d)[2][0].state == "verifying"
    _verified(monkeypatch)
    with d["factory"]() as session:
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
        assert session.get(Job, jid).status == "succeeded"
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "research.deep")) == 1
    assert _rows(d)[0].state == "completed"


def test_crashed_admission_checkpoint_resumes_original_intent(lifecycle, monkeypatch):
    from app.db.models import Job
    from app.db.task_models import TaskStep
    from app.research import service as research
    d = lifecycle
    task_id = _create(d)
    _, run, steps = _rows(d, task_id)
    first = _claim(d, "tasks.execute")
    with d["factory"]() as session:
        step = session.get(TaskStep, steps[0].id)
        step.state = "admitting"
        step.inputs_json = json.dumps({**json.loads(step.inputs_json),
            "_admission_job_id": first.id, "_admission_attempt": first.attempts})
        session.commit()
    d["clock"][0] += timedelta(seconds=100)
    with d["factory"]() as session:
        assert d["jobs"].reclaim_stale(session, now=d["clock"][0]) == 1
        session.commit()
    retry = _claim(d, "tasks.execute")
    assert retry.id == first.id and retry.attempts == 2
    monkeypatch.setattr(research, "start", lambda *a, **kw: {"job_id": _admit(d,
        dedupe="crash-admission:" + run.id)})
    asyncio.run(d["execution"].execute({**retry.payload, "_job_id": retry.id,
        "_worker_id": retry.worker_id, "_job_attempt": retry.attempts}))
    with d["factory"]() as session:
        assert session.get(TaskStep, steps[0].id).job_id is not None
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "research.deep")) == 1


def test_uncertain_paid_call_blocks_new_calls_until_reconciled(lifecycle):
    from app.core.jobs import FatalError
    d = lifecycle
    jid = _admit(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 10)
        d["usage"].abort_call(invocation, definitive=False)
        with pytest.raises(FatalError):
            d["usage"].before_call(100, 10)
    assert _rows(d)[0].state == "input_required"


@pytest.mark.parametrize("uncertain", [False, True])
def test_saved_workflow_receipt_cannot_complete_while_task_usage_is_unconfirmed(lifecycle, monkeypatch, uncertain):
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    _verified(monkeypatch)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 10)
        if uncertain:
            d["usage"].abort_call(invocation, definitive=False)
    with d["factory"]() as session:
        d["service"].after_job(session, jid)
        session.commit()
    saved_task, saved_run, steps = _rows(d, task.id)
    assert steps[0].state == ("submitted" if uncertain else "completed")
    assert saved_run.state == saved_task.state == "input_required"
    assert saved_run.reserved_cost_micro == 100 and saved_run.reserved_tokens == 10
    assert json.loads(saved_task.blocker_json)["class"] == "needs_input"


def test_definitive_late_result_reconciles_uncertain_budget_exactly_once(lifecycle):
    from app.db.task_models import TaskRun
    d = lifecycle
    jid = _admit(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 10)
        d["usage"].abort_call(invocation, definitive=False)
        d["usage"].after_call(invocation, 70, 7)
        d["usage"].after_call(invocation, 70, 7)
    _, run, _ = _rows(d)
    with d["factory"]() as session:
        saved = session.get(TaskRun, run.id)
        assert (saved.reserved_cost_micro, saved.reserved_tokens) == (0, 0)
        assert (saved.cost_micro, saved.tokens_used) == (70, 7)


def test_cancelled_task_can_settle_inflight_call_without_starting_another(lifecycle):
    from app.core.jobs import FatalError
    from app.db.task_models import Task, TaskRun
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 10)
        with d["factory"]() as session:
            d["service"].cancel(session, session.get(Task, task.id))
            session.commit()
        d["usage"].after_call(invocation, 70, 7)
        with pytest.raises(FatalError):
            d["usage"].before_call(1, 1)
    with d["factory"]() as session:
        saved = session.get(TaskRun, run.id)
        assert saved.cost_micro == 70 and saved.tokens_used == 7
        assert saved.state == session.get(Task, task.id).state == "canceled"


def test_uncertain_settlement_never_resurrects_cancelled_task(lifecycle):
    from app.db.task_models import Task, TaskRun
    d = lifecycle
    jid = _admit(d)
    task, run, _ = _rows(d)
    with d["usage"].job_context(jid):
        invocation = d["usage"].before_call(100, 10)
        with d["factory"]() as session:
            d["service"].cancel(session, session.get(Task, task.id))
            session.commit()
        d["usage"].abort_call(invocation, definitive=False)
    with d["factory"]() as session:
        saved = session.get(TaskRun, run.id)
        assert saved.reserved_cost_micro == 100 and saved.reserved_tokens == 10
        assert saved.state == session.get(Task, task.id).state == "canceled"
