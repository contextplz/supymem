"""Synthetic saved results are read only; no model, send, or device effect runs."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tests.company_os_harness import load_company_os_app


@pytest.fixture
def results(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.api import handoffs_api
    from app.db.models import Base, GtmContact, GtmTouch, Job, Person, PlannedTask, Venture, VentureDocument, Workspace
    from app.db.supymem_models import VentureWaitlistEntry
    from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep
    from core.clock import set_clock
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[model.__table__ for model in (
        Person, Workspace, Venture, VentureDocument, PlannedTask, Job, Task, TaskRun, TaskStep, TaskEvent,
        VentureWaitlistEntry, GtmContact, GtmTouch)])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    set_clock(lambda: now)
    monkeypatch.setattr(handoffs_api, "db", lambda: factory)
    with factory() as session:
        session.add(Person(id="usr_result", display_name="Synthetic founder", created_at=now))
        session.flush()
        session.add(Workspace(id="wsp_result", owner_id="usr_result", kind="company", entry_mode="idea", name="Synthetic", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_result", workspace_id="wsp_result", name="Synthetic", raw_idea="Fixture", stage="draft", created_at=now, updated_at=now))
        session.add(Task(id="tsk_result", workspace_id="wsp_result", venture_id="ven_result", owner_person_id="usr_result",
            title="Prepare a short note", goal="Prepare a short note", kind="one_time", state="completed",
            done_test_json=json.dumps({"type": "deterministic", "spec": "All step checks pass"}),
            created_at=now, updated_at=now, deadline_at=now + timedelta(minutes=15)))
        session.add(Job(id="job_result", kind="task.run", workspace_id="wsp_result", venture_id="ven_result", status="succeeded",
            payload_json=json.dumps({"task_id": "planned_result", "private_provider_body": "NEVER SHOW PROVIDER PAYLOAD", "credits": 12345}), created_at=now))
        session.add(PlannedTask(id="planned_result", workspace_id="wsp_result", venture_id="ven_result", title="Prepare a short note",
            why_now="Founder requested it", status="done", finished_at=now, created_at=now, updated_at=now))
        session.add(VentureDocument(id="task-note:planned_result", venture_id="ven_result", kind="task_note", title="Prepare a short note",
            body_md="# Saved note\n\nStatus: completed\nTask ID: tsk_result\n\nA useful saved update.\n",
            provenance_json=json.dumps({"task_id": "planned_result", "source": "task.run"}), created_at=now, updated_at=now))
        session.flush()
        session.add(TaskRun(id="trn_result", task_id="tsk_result", run_key="current", state="completed", started_at=now,
            finished_at=now, deadline_at=now + timedelta(minutes=15)))
        session.flush()
        session.add(TaskStep(id="tst_result", run_id="trn_result", position=0, title="Prepare note", skill="task.run", state="completed",
            job_id="job_result", inputs_json=json.dumps({"task_id": "planned_result"}),
            evidence_json=json.dumps([{"ref": "venture_document:task-note:planned_result", "state": "completed"}]),
            deadline_at=now + timedelta(minutes=15)))
        session.commit()
    yield factory, handoffs_api, now
    set_clock(None)
    engine.dispose()


def owner(**changes):
    from app.tenancy import bind
    return bind(**{"workspace_id": "wsp_result", "venture_id": "ven_result", "actor": "usr_result", **changes})


def get(data):
    with owner():
        return data[1].task_result("ven_result", "tsk_result")


def test_completed_result_is_revalidated_and_never_dumps_ids_or_private_job_fields(results):
    result = get(results)
    assert result["status"] == "verified" and result["partial"] is False
    assert "A useful saved update" in result["body_md"]
    assert not any(value in result["body_md"] for value in ("tsk_result", "planned_result", "job_result", "PROVIDER PAYLOAD", "12345", "Status:"))


def test_blocked_generic_work_keeps_only_trusted_same_scope_partial_note(results):
    from app.db.models import PlannedTask
    from app.db.task_models import Task, TaskRun, TaskStep
    factory, _, _ = results
    with factory() as session:
        session.get(Task, "tsk_result").state = "input_required"
        session.get(TaskRun, "trn_result").state = "input_required"
        session.get(TaskStep, "tst_result").state = "input_required"
        session.get(PlannedTask, "planned_result").status = "open"
        session.commit()
    result = get(results)
    assert result["status"] == "partial" and result["partial"] is True
    assert "has not passed" in result["body_md"] and "A useful saved update" in result["body_md"]


@pytest.mark.parametrize("change", [{"venture_id": "foreign"}, {"provenance_json": '{}'}, {"title": "Different request"}])
def test_mismatched_document_cannot_be_used_even_as_partial_result(results, change):
    from app.db.models import VentureDocument
    factory, _, _ = results
    with factory() as session:
        row = session.get(VentureDocument, "task-note:planned_result")
        for key, value in change.items():
            setattr(row, key, value)
        session.commit()
    result = get(results)
    assert result["status"] == "unconfirmed" and "A useful saved update" not in result["body_md"]


@pytest.mark.parametrize("case", ["secret", "oversized"])
def test_secret_shaped_or_oversized_note_is_withheld_wholesale(results, case):
    from app.db.models import VentureDocument
    factory, _, _ = results
    content = "sk-" + "x" * 24 if case == "secret" else "safe prose " * 2000
    with factory() as session:
        session.get(VentureDocument, "task-note:planned_result").body_md = "Status: completed\n" + content
        session.commit()
    result = get(results)
    assert result["status"] == "withheld" and content not in result["body_md"]
    assert len(result["body_md"].encode()) <= 16_384


@pytest.mark.parametrize("change,status", [({"actor": "usr_other"}, 403), ({"workspace_id": "foreign"}, 404), ({"venture_id": "foreign"}, 404)])
def test_result_reader_requires_exact_current_owner_and_scope(results, change, status):
    with owner(**change), pytest.raises(HTTPException) as failure:
        results[1].task_result("ven_result", "tsk_result")
    assert failure.value.status_code == status


@pytest.mark.parametrize("archived", ["workspace", "venture"])
def test_archived_scope_cannot_read_or_control_saved_tasks(results, archived):
    from app.db.models import Venture, Workspace
    from app.db.task_models import Task
    factory, api, now = results
    with factory() as session:
        row = session.get(Workspace, "wsp_result") if archived == "workspace" else session.get(Venture, "ven_result")
        row.archived_at = now
        session.commit()
    with owner():
        for action in (lambda: api.list_tasks("ven_result"), lambda: api.task_result("ven_result", "tsk_result"),
                       lambda: api.cancel_task("tsk_result")):
            with pytest.raises(HTTPException) as failure:
                action()
            assert failure.value.status_code == 404
    with factory() as session:
        assert session.get(Task, "tsk_result").state == "completed"


def test_task_owner_guard_uses_fresh_custody_after_an_earlier_cached_read(results):
    from app.db.models import Workspace
    from sqlalchemy import update
    factory, api, now = results
    with factory() as session, owner():
        cached = session.get(Workspace, "wsp_result")
        assert api._owner(session, "ven_result")[0] is cached
        session.execute(update(Workspace).where(Workspace.id == cached.id).values(archived_at=now),
            execution_options={"synchronize_session": False})
        assert cached.archived_at is None
        with pytest.raises(HTTPException) as failure:
            api._owner(session, "ven_result")
        assert failure.value.status_code == 404 and cached.archived_at is None


def test_new_unfinished_run_never_presents_previous_completion_as_current(results):
    from app.db.task_models import TaskRun
    factory, _, now = results
    with factory() as session:
        session.add(TaskRun(id="trn_new", task_id="tsk_result", run_key="new", state="working",
            started_at=now + timedelta(seconds=1), deadline_at=now + timedelta(minutes=30)))
        session.commit()
    result = get(results)
    assert result["status"] == "pending" and result["partial"] is True
    assert "A useful saved update" not in result["body_md"]


def test_report_result_recomputes_source_receipt_and_rejects_changed_body(results):
    from app.db.models import VentureDocument
    from app.db.task_models import TaskStep
    from app.tasks.reports import write_report
    factory, _, now = results
    with factory() as session:
        receipt = write_report(session, task_id="tsk_result", run_id="trn_result", workspace_id="wsp_result", venture_id="ven_result", now=now)
        step = session.get(TaskStep, "tst_result")
        step.skill, step.job_id, step.evidence_json = "tasks.waitlist_summary", None, json.dumps(receipt["evidence"])
        session.commit()
    result = get(results)
    assert result["status"] == "verified" and "Waitlist sign-ups" in result["body_md"]
    with factory() as session:
        session.get(VentureDocument, "task-report:trn_result").body_md = "A forged report claims many sign-ups"
        session.commit()
    result = get(results)
    assert result["status"] == "unconfirmed" and "forged" not in result["body_md"]


def test_old_rubric_judgment_cannot_certify_a_changed_saved_artifact(results):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun
    from app.tasks.verification import artifact_snapshot
    factory, _, _ = results
    with factory() as session:
        task, run = session.get(Task, "tsk_result"), session.get(TaskRun, "trn_result")
        task.tolerance = 0.8
        task.done_test_json = json.dumps({"type": "rubric", "spec": {"predicate": "rubric", "criteria": ["A useful update is saved"]}})
        snapshot = artifact_snapshot(session, task, run)
        run.evaluation_json = json.dumps({"state": "complete", "valid": True, "score": 1,
            "artifact_digest": snapshot["artifact_digest"], "contract_digest": snapshot["contract_digest"]})
        session.commit()
    assert get(results)["status"] == "verified"
    with factory() as session:
        note = session.get(VentureDocument, "task-note:planned_result")
        note.body_md += "A changed result after the original judgment."
        session.commit()
    result = get(results)
    assert result["status"] == "partial" and result["partial"] is True
    assert "has not passed" in result["body_md"]
