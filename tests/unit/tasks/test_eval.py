"""Offline qualification cases over real SQLite task checkpoints and receipts."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.unit.tasks import test_task_lifecycle as workflow_checks

# Reuse the production-path SQLite lifecycle fixture, not a second task engine.
lifecycle = workflow_checks.lifecycle


def owned_workflow(d):
    from app.db.models import PlannedTask, VentureDocument
    from app.tasks import progress
    engine = d["factory"].kw["bind"]
    for model in (PlannedTask, VentureDocument):
        model.__table__.create(engine, checkfirst=True)
    contract = workflow_checks._contract(title="Offline qualification", goal="Prepare a synthetic research note",
        estimate_seconds=60, budget={"wall_seconds": 180}, steps=[{"title": "Read public sources", "skill": "task.run"}])
    task_id = workflow_checks._create(d, contract)
    _, run, steps = workflow_checks._rows(d, task_id)
    with d["factory"]() as session, d["service"].step_context(run.id, steps[0].id):
        job_id = d["jobs"].enqueue(session, "task.run", {"task_id": "planned_eval", "_actor_subject": "usr_lifecycle"},
            workspace_id="ws_lifecycle", venture_id="ven_lifecycle", dedupe_key="offline-eval:" + run.id)
        session.commit()
    claimed = workflow_checks._claim(d, "task.run")
    with d["factory"]() as session:
        assert d["service"].before_job(session, job_id, claimed.attempts)
        session.commit()
    return task_id, run.id, steps[0].id, job_id, progress


def test_frozen_clock_and_no_progress_are_enforced_from_owned_tool_observations(lifecycle, record_property, monkeypatch):
    from app.db.task_models import Task, TaskEvent, TaskRun
    from app.tasks import progress
    from core.clock import as_utc
    d = lifecycle
    monkeypatch.setattr(progress, "db", lambda: d["factory"])
    task_id, run_id, _, job_id, progress = owned_workflow(d)
    dispatched = 0
    with d["usage"].job_context(job_id):
        for _ in range(2):
            progress.before_search("synthetic public query", workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
            dispatched += 1
            progress.record_tool("explore_web", {"query": "synthetic public query"}, {"ok": True, "results": []},
                source_count=0, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
        with pytest.raises(progress.ResearchStopped, match="reflection_required"):
            progress.before_search("synthetic public query", workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
    with d["factory"]() as session:
        observed = session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.run_id == run_id, TaskEvent.kind == "tool_observed"))
        assert observed == dispatched == 2
    record_property("stuck_extra_tool_calls_prevented", 1)
    d["clock"][0] += timedelta(seconds=61)
    with d["factory"]() as session:
        d["service"].fire_due(session, now=d["clock"][0])
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
        run = session.get(TaskRun, run_id)
        eta_count = session.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.run_id == run_id, TaskEvent.kind == "eta_slip"))
        assert eta_count == 1 and session.get(Task, task_id).state == "working"
        record_property("deadline_estimate_seconds", int((as_utc(run.deadline_at) - as_utc(run.started_at)).total_seconds()))
        record_property("eta_slip_observed_seconds", int((d["clock"][0] - as_utc(run.started_at)).total_seconds()))
    d["clock"][0] += timedelta(seconds=120)
    with d["factory"]() as session:
        d["service"].fire_due(session, now=d["clock"][0])
        session.commit()
        task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
        assert task.state == "input_required" and json.loads(task.blocker_json)["class"] == "budget_exhausted"
        record_property("wall_budget_stop_observed_seconds", int((d["clock"][0] - as_utc(run.started_at)).total_seconds()))


@pytest.mark.parametrize("scenario,expected,state", [
    ("login", "needs_auth", "auth_required"), ("policy", "policy", "rejected"),
    ("impossible", "impossible", "failed"), ("missing_input", "needs_input", "input_required"),
    ("budget", "budget_exhausted", "input_required"),
])
def test_required_blocker_from_authoritative_backend_boundary(lifecycle, scenario, expected, state, record_property, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.models import PlannedTask
    from app.db.task_models import Task, TaskRun
    from app.tasks import progress
    d = lifecycle
    monkeypatch.setattr(progress, "db", lambda: d["factory"])
    task_id, run_id, _, job_id, progress = owned_workflow(d)
    if scenario in {"login", "policy"}:
        with d["usage"].job_context(job_id), pytest.raises(progress.ResearchStopped):
            progress.record_tool("explore_web", {"query": "synthetic public query"},
                {"ok": False, "error": "login_required" if scenario == "login" else "policy_refused"},
                source_count=0, workspace_id="ws_lifecycle", venture_id="ven_lifecycle")
    elif scenario == "budget":
        with d["factory"]() as session:
            task = session.get(Task, task_id)
            limits = json.loads(task.budget_json)
            limits["cost_micro"] = 0
            task.budget_json = json.dumps(limits)
            session.commit()
        with d["usage"].job_context(job_id), pytest.raises(FatalError):
            d["usage"].before_call(1, 1)  # The transport is never reached.
    else:
        with d["factory"]() as session:
            now = d["clock"][0]
            session.add(PlannedTask(id="planned_eval", workspace_id="ws_lifecycle", venture_id="ven_lifecycle",
                title="Synthetic saved work", why_now="Offline qualification", status="done" if scenario == "impossible" else "open",
                finished_at=now if scenario == "impossible" else None, created_at=now, updated_at=now))
            session.flush()
            d["service"].after_job(session, job_id)
            session.commit()
    with d["factory"]() as session:
        task, run = session.get(Task, task_id), session.get(TaskRun, run_id)
        observed = json.loads(task.blocker_json)
        assert observed["class"] == expected and task.state == run.state == state
        assert observed["reason"] and observed["unblock_action"]
        assert scenario not in observed["reason"] or scenario == "policy"
        record_property("expected_blocker", expected)
        record_property("observed_blocker", observed["class"])
