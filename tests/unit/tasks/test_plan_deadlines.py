"""Synthetic plan date persistence/readback on a minimal SQLite schema."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def plans(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.api import tasks_api
    from app.db.models import Base, Job, PlannedTask, Venture
    from app.db.supymem_models import ExecutionPlan, FounderAction, PlanMilestone, PlanTask, ResourceEstimate
    from app.planning import compiler, review
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Job, PlannedTask, Venture, ExecutionPlan, FounderAction, PlanMilestone, PlanTask, ResourceEstimate)])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    set_clock(lambda: now)
    monkeypatch.setattr(compiler, "db", lambda: factory)
    monkeypatch.setattr(tasks_api, "db", lambda: factory)
    monkeypatch.setattr(compiler, "_current", lambda *args, **kwargs: ({"resources": {"execution_budget": {}}, "dependency_digest": "synthetic"}, False))
    monkeypatch.setattr(review, "findings", lambda *args: [])
    with factory() as session:
        session.add(Venture(id="ven_plan", workspace_id="wsp_plan", name="Synthetic plan", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.add(ExecutionPlan(id="pln_deadline", workspace_id="wsp_plan", venture_id="ven_plan", version=1,
            state="founder_review", summary="Synthetic strategy", structured="{}", created_at=now))
        session.commit()
    yield factory, compiler, tasks_api, now
    set_clock(None)
    engine.dispose()


def persist(data, days=7):
    from app.db.supymem_models import ExecutionPlan
    factory, compiler, _, _ = data
    doc = {"_runtime": {"context": {"capabilities": []}, "resources": {"execution_budget": {}}}}
    proposal = {"summary": "Synthetic strategy", "goal_first_paid_user": "Verify demand", "target_days": days,
        "milestones": [{"key": "m1", "title": "Review evidence", "description": "Review a saved source", "kind": "research", "included": True,
            "depends_on": [], "acceptance": ["A note is saved"], "tasks": [{"key": "t1", "title": "Review evidence",
            "description": "Read the saved evidence", "capability_id": "founder.action", "implementation": "founder_action",
            "founder_action": None}]}]}
    with factory() as session:
        row = session.get(ExecutionPlan, "pln_deadline")
        compiler._persist(session, row, doc, proposal)
        row.structured = json.dumps(doc)
        session.commit()
    return doc


@pytest.mark.parametrize("days,expected", [(7, "2026-10-15T12:00:00+00:00"), (None, "2026-10-22T12:00:00+00:00")])
def test_draft_deadline_is_persisted_and_serialized_as_an_estimate(plans, days, expected):
    persist(plans, days)
    view = plans[1].get_plan("pln_deadline")
    task = view["milestones"][0]["tasks"][0]
    assert task["due_at"] == expected and task["deadline_is_estimate"] is True


def test_first_approval_rebases_once_and_repeat_approval_cannot_postpone_it(plans, monkeypatch):
    from app.db.supymem_models import ExecutionPlan, PlanTask
    from app.authz import roles
    from app.workspaces import service as workspaces
    factory, compiler, _, now = plans
    persist(plans)
    monkeypatch.setattr(roles, "role_allows", lambda *args: True)
    monkeypatch.setattr(workspaces, "role_of", lambda *args: "founder")
    monkeypatch.setattr(compiler, "advance", lambda session, row, state, **kwargs: setattr(row, "state", state))
    monkeypatch.setattr(compiler, "_ensure_approved_work", lambda *args: [])
    monkeypatch.setattr("app.analytics.events.emit", lambda *args, **kwargs: None)
    from core.clock import set_clock
    set_clock(lambda: now + timedelta(days=2))
    token = compiler.get_plan("pln_deadline")["review_token"]
    compiler.approve_plan("pln_deadline", person_id="usr_synthetic", review_token=token)
    first = compiler.get_plan("pln_deadline")["milestones"][0]["tasks"][0]["due_at"]
    assert first == "2026-10-17T12:00:00+00:00"
    set_clock(lambda: now + timedelta(days=5))
    token = compiler.get_plan("pln_deadline")["review_token"]
    compiler.approve_plan("pln_deadline", person_id="usr_synthetic", review_token=token)
    assert compiler.get_plan("pln_deadline")["milestones"][0]["tasks"][0]["due_at"] == first
    with factory() as session:
        row = session.get(ExecutionPlan, "pln_deadline")
        assert row.approved_at.date().isoformat() == "2026-10-10"
        assert session.scalar(select(PlanTask)).due_at.date().isoformat() == "2026-10-17"


def test_missing_legacy_deadline_uses_original_approval_and_foreign_tasks_are_untouched(plans):
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
    factory, compiler, _, now = plans
    doc = persist(plans)
    with factory() as session:
        row = session.get(ExecutionPlan, "pln_deadline")
        row.approved_at = now
        own = session.scalar(select(PlanTask))
        own.due_at = None
        milestone = session.scalar(select(PlanMilestone))
        session.add(PlanTask(id="pt_foreign", milestone_id=milestone.id, workspace_id="foreign", venture_id="ven_plan",
            order=2, title="Foreign work", created_at=now, due_at=None))
        session.flush()
        compiler._approval_deadlines(session, row, doc)
        assert own.due_at == now + timedelta(days=7)
        assert session.get(PlanTask, "pt_foreign").due_at is None


def test_existing_work_queue_forwards_plan_task_deadline(plans, monkeypatch):
    from app.db.models import PlannedTask
    from app.db.supymem_models import ExecutionPlan
    factory, _, api, now = plans
    doc = persist(plans)
    task_id = doc["entity_ids"]["tasks"]["m1:t1"]
    work = {"id": "work_synthetic", "title": "Review evidence", "status": "open", "source": "approved_plan:pln_deadline",
            "tags": ["plan_task:" + task_id], "rank": 1}
    with factory() as session:
        session.get(ExecutionPlan, "pln_deadline").state = "approved"
        session.add(PlannedTask(id=work["id"], workspace_id="wsp_plan", venture_id="ven_plan", title="Review evidence",
            why_now="Review the approved evidence", status="open", source=work["source"], created_at=now, updated_at=now))
        session.commit()
    monkeypatch.setattr(api, "list_open_tasks", lambda *args: [dict(work)])
    monkeypatch.setattr(api, "list_tasks_by_status", lambda *args, **kwargs: [])
    monkeypatch.setattr(api, "current_plan_work", lambda *args: True)
    item = api.list_venture_tasks("ven_plan")["items"][0]
    assert item["due_at"] == "2026-10-15T12:00:00+00:00" and item["deadline_is_estimate"] is True
