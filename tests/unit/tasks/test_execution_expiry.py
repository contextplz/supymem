"""Production session expiration across actual Task admission boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import inspect, select

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def expired_factory(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/expiry.db"
    monkeypatch.setenv("COMPANY_OS_DB_URL", url)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    load_company_os_app()
    from app import state
    from app.db.append_only import install_append_only_triggers
    from app.db.models import Base, Person, Venture, Workspace
    from app.db.session import make_engine, make_session_factory
    from app.db.supymem_models import VentureWaitlist, VentureWaitlistEntry
    from llm_gateway import sole

    engine = make_engine(url)
    Base.metadata.create_all(engine)
    install_append_only_triggers(engine)
    factory = make_session_factory(engine)
    assert factory.kw.get("expire_on_commit", True) is True
    previous = state.get_factory()
    state.set_factory(factory)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with factory() as session:
        session.add(Person(id="usr_expiry", display_name="Fictional founder", created_at=now))
        session.flush()
        session.add(Workspace(id="ws_expiry", owner_id="usr_expiry", kind="company", entry_mode="idea",
            name="Fictional workspace", created_at=now, updated_at=now))
        session.flush()
        session.add(Venture(id="ven_expiry", workspace_id="ws_expiry", name="Fictional venture",
            raw_idea="Fictional fixture only", stage="draft", created_at=now, updated_at=now))
        session.flush()
        session.add(VentureWaitlist(venture_id="ven_expiry", workspace_id="ws_expiry", enabled=False,
            enabled_by="usr_expiry", consent_version="fictional", updated_at=now))
        session.flush()
        session.add(VentureWaitlistEntry(id="signup_expiry", venture_id="ven_expiry", workspace_id="ws_expiry",
            email="fixture@example.invalid", consent_version="fictional", consent_text="Fictional fixture",
            is_test=False, created_at=now - timedelta(hours=1)))
        session.commit()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail("Database-only admission must not construct an SDK client")
    monkeypatch.setattr(sole, "create_async_client_from_config", forbidden)
    try:
        yield factory, calls
    finally:
        state.set_factory(previous)
        engine.dispose()


def admitted(factory, goal, skill):
    from app.db.task_models import TaskRun
    from app.tasks import service
    from app.tasks.contracts import Contract
    with factory() as session:
        task = service.create(session, workspace_id="ws_expiry", venture_id="ven_expiry", owner_person_id="usr_expiry",
            contract=Contract(title=goal, goal=goal, steps=[{"title": "Prepare requested result", "skill": skill}]))
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id))
        identifiers = task.id, run.id, run.control_job_id
        session.commit()
        assert inspect(task).expired and "goal" in inspect(task).expired_attributes
    return identifiers


def execute(identifiers):
    from app.tasks import execution
    from app.tenancy import bind
    with bind(workspace_id="ws_expiry", venture_id="ven_expiry", actor="usr_expiry"):
        asyncio.run(execution.execute({"task_run_id": identifiers[1], "_job_id": identifiers[2],
            "workspace_id": "ws_expiry", "venture_id": "ven_expiry", "_job_attempt": 1}))


def test_free_report_completes_with_actual_expiring_production_factory(expired_factory):
    from app.db.models import VentureDocument
    from app.db.task_models import Task, TaskRun
    from app.tasks.reports import verify_report
    factory, calls = expired_factory
    goal = "Summarize waitlist signups and replies"
    identifiers = admitted(factory, goal, "tasks.waitlist_summary")
    execute(identifiers)
    with factory() as session:
        assert session.get(Task, identifiers[0]).goal == goal
        assert session.get(TaskRun, identifiers[1]).state == "completed"
        assert session.get(VentureDocument, "task-report:" + identifiers[1]) is not None
        receipt = verify_report(session, task_id=identifiers[0], run_id=identifiers[1],
            workspace_id="ws_expiry", venture_id="ven_expiry")
        assert receipt["verified"] and receipt["evidence"][0]["signup_count"] == 1
    assert not calls


@pytest.mark.parametrize("goal", ["Draft a fictional update for freight coordinators", "Draft a fictional update for hiking families"])
def test_generic_admission_keeps_original_goal_after_expiring_commit(expired_factory, goal):
    from app.db.models import Job, PlannedTask
    from app.db.task_models import Task, TaskRun, TaskStep
    factory, calls = expired_factory
    identifiers = admitted(factory, goal, "task.run")
    execute(identifiers)
    with factory() as session:
        planned = session.scalar(select(PlannedTask).where(PlannedTask.detail == goal))
        assert planned and planned.title == planned.detail == goal
        task, run = session.get(Task, identifiers[0]), session.get(TaskRun, identifiers[1])
        step = session.scalar(select(TaskStep).where(TaskStep.run_id == run.id))
        job = session.get(Job, step.job_id)
        assert job and (job.kind, job.workspace_id, job.venture_id) == ("task.run", "ws_expiry", "ven_expiry")
        assert json.loads(job.payload_json)["task_id"] == planned.id and task.goal == goal
        assert run.state != "completed"  # Admission alone cannot certify the generated draft.
    assert not calls
