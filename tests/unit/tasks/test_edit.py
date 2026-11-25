"""Synthetic standing-definition edits; no current run/provider side effects."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def edits(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.api import handoffs_api
    from app.db.models import Base, Job, Person, Venture, Workspace
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from app.tasks.contracts import Budget
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (Person, Workspace, Venture, Job, Task, TaskRun, TaskStep, TaskEvent)])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    set_clock(lambda: now)
    monkeypatch.setattr(handoffs_api, "db", lambda: factory)
    with factory() as session:
        for person in ("usr_edit", "usr_other"):
            session.add(Person(id=person, display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="wsp_edit", owner_id="usr_edit", kind="company", entry_mode="idea", name="Synthetic", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_edit", workspace_id="wsp_edit", name="Synthetic", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.add(Task(id="tsk_edit", workspace_id="wsp_edit", venture_id="ven_edit", owner_person_id="usr_edit",
            title="Waitlist summary", goal="Every Friday, summarize waitlist sign-ups and replies", kind="scheduled", state="paused",
            scope_json=json.dumps({"estimate_seconds": 900, "steps": [{"title": "Summarize", "skill": "tasks.waitlist_summary"}]}),
            budget_json=Budget(tokens=1000).model_dump_json(), done_test_json=json.dumps({"type": "deterministic", "spec": "All step checks pass"}),
            created_at=now, updated_at=now, deadline_at=now + timedelta(minutes=15)))
        session.commit()
    yield factory, handoffs_api, now
    set_clock(None)
    engine.dispose()


def owner(**changes):
    from app.tenancy import bind
    return bind(**{"workspace_id": "wsp_edit", "venture_id": "ven_edit", "actor": "usr_edit", **changes})


def body(data, **changes):
    from app.tasks.contracts import Contract
    _, api, now = data
    goal = "Every Monday at 8, summarize waitlist sign-ups and replies"
    contract = Contract.model_validate({"title": "Updated summary", "goal": goal, "kind": "event", "trigger": {"event_type": "invented"},
        "steps": [{"title": "Summarize", "skill": "tasks.waitlist_summary"}]})
    return api.EditTaskBody(**{"goal": goal, "timezone": "Asia/Kolkata", "contract": contract,
                             "expected_updated_at": now.isoformat(), **changes})


@pytest.mark.parametrize("run_state", ["working", "paused", "queued", "input_required"])
def test_any_nonterminal_run_prevents_definition_edit(edits, run_state):
    from app.db.task_models import TaskRun
    factory, api, now = edits
    with factory() as session:
        session.add(TaskRun(id="trn_active", task_id="tsk_edit", run_key="current", state=run_state,
            started_at=now, deadline_at=now + timedelta(minutes=15)))
        session.commit()
    with owner(), pytest.raises(HTTPException) as failure:
        api.edit_task("ven_edit", "tsk_edit", body(edits))
    assert failure.value.status_code == 409


def test_ended_run_cannot_be_edited_while_linked_provider_job_is_still_leased(edits):
    from app.db.models import Job
    from app.db.task_models import TaskRun, TaskStep
    factory, api, now = edits
    with factory() as session:
        session.add(Job(id="job_settling", kind="task.run", workspace_id="wsp_edit", venture_id="ven_edit", status="leased", payload_json="{}", created_at=now))
        session.add(TaskRun(id="trn_ended", task_id="tsk_edit", run_key="ended", state="canceled", started_at=now,
            finished_at=now, deadline_at=now + timedelta(minutes=15)))
        session.flush()
        session.add(TaskStep(id="tst_ended", run_id="trn_ended", position=0, title="Settling", skill="task.run", job_id="job_settling",
            state="canceled", deadline_at=now + timedelta(minutes=15)))
        session.commit()
    with owner(), pytest.raises(HTTPException) as failure:
        api.edit_task("ven_edit", "tsk_edit", body(edits))
    assert failure.value.status_code == 409


def test_stale_revision_and_one_time_definition_fail_without_changes(edits):
    from app.db.task_models import Task
    factory, api, now = edits
    with owner(), pytest.raises(HTTPException) as failure:
        api.edit_task("ven_edit", "tsk_edit", body(edits, expected_updated_at=(now - timedelta(seconds=1)).isoformat()))
    assert failure.value.status_code == 409
    with factory() as session:
        task = session.get(Task, "tsk_edit")
        assert task.title == "Waitlist summary"
        task.kind = "one_time"
        session.commit()
    with owner(), pytest.raises(HTTPException) as failure:
        api.edit_task("ven_edit", "tsk_edit", body(edits))
    assert failure.value.status_code == 409


def test_future_definition_uses_code_owned_trigger_preserves_budget_and_waits_for_resume(edits):
    from app.db.task_models import Task
    factory, api, _ = edits
    with factory() as session:
        original_budget = session.get(Task, "tsk_edit").budget_json
    with owner():
        updated = api.edit_task("ven_edit", "tsk_edit", body(edits))
        assert updated["state"] == "paused" and updated["kind"] == "scheduled"
        assert updated["updated_at"] != body(edits).expected_updated_at
        assert api.resume("ven_edit", "tsk_edit")["state"] == "submitted"
    with factory() as session:
        task = session.get(Task, "tsk_edit")
        trigger = json.loads(task.trigger_json)
        assert trigger["tz"] == "Asia/Kolkata" and "BYDAY=MO" in trigger["rrule"] and "BYHOUR=8" in trigger["rrule"]
        assert "event_type" not in trigger
        assert task.budget_json == original_budget


def test_standing_task_cannot_be_edited_into_one_time_or_saved_without_preview(edits):
    _, api, _ = edits
    with owner():
        for payload in (body(edits, goal="Summarize waitlist sign-ups and replies"), body(edits, contract=None)):
            with pytest.raises(HTTPException) as failure:
                api.edit_task("ven_edit", "tsk_edit", payload)
            assert failure.value.status_code == 422


@pytest.mark.parametrize("change,status", [({"actor": "usr_other"}, 403), ({"workspace_id": "foreign"}, 404), ({"venture_id": "foreign"}, 404)])
def test_wrong_founder_or_retired_scope_cannot_edit(edits, change, status):
    _, api, _ = edits
    with owner(**change), pytest.raises(HTTPException) as failure:
        api.edit_task("ven_edit", "tsk_edit", body(edits))
    assert failure.value.status_code == status
