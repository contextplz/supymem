"""POST /tasks/{id}/run queues a worker job and returns 202 at once; the worker does the work."""
import asyncio

import pytest
from tests.company_os_harness import company_os_test_client as TestClient
from sqlalchemy import select

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def vid_task(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/run.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.planning.store import enqueue_task
    from tests.unit.planning.test_execute_completion import _researcher_assignee

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())
    vid = client.post("/ventures", json={"raw_idea": "invoice chaser for freelancers", "mode": "auto"}).json()["id"]
    task = enqueue_task(vid, {"title": "Run an overdue invoice reminder concierge test", "detail": "", "why_now": "x",
                              "tags": [_researcher_assignee(vid)], "est_minutes": 60, "runnable": True}, source="chat")
    return client, vid, task["id"]


def _jobs(tid):
    from app.db.models import Job
    from app.state import db

    with db()() as s:
        return [j for j in s.scalars(select(Job).where(Job.kind == "task.run")) if tid in j.payload_json]


def test_run_returns_202_enqueues_once_and_double_click_is_a_noop(vid_task, monkeypatch):
    from app.db.models import PlannedTask
    from app.planning import execute
    from app.state import db

    client, vid, tid = vid_task

    async def boom(*a, **k):
        raise AssertionError("the HTTP handler must not execute the task")

    monkeypatch.setattr(execute, "execute_planned_task", boom)
    first = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    again = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert first.status_code == 202 and first.json()["status"] == "running"
    assert again.json().get("already_running") is True
    assert len(_jobs(tid)) == 1
    with db()() as s:
        assert s.get(PlannedTask, tid).status == "running"


def test_worker_executes_research_but_does_not_close_unverified_concierge_work(vid_task, monkeypatch):
    from app.core.jobs import FatalError
    from app.db.models import PlannedTask, VentureDocument
    from app.planning import execute
    from app.state import db

    client, vid, tid = vid_task
    async def sources(*_a, **_kw):
        return {"mode": "live", "summary": "Source note only, no concierge test performed.",
                "hits": [{"url": "https://example.com/concierge"}],
                "pages": [{"url": "https://example.com/concierge", "excerpt": "A source page.", "chars": 14}]}
    monkeypatch.setattr("app.research.explore.explore_web", sources)
    assert client.post(f"/ventures/{vid}/tasks/{tid}/run").status_code == 202
    with pytest.raises(FatalError, match="^research_coverage_unverified$"):
        asyncio.run(execute.run_task_job({"venture_id": vid, "task_id": tid}))
    with db()() as s:
        row = s.get(PlannedTask, tid)
        assert row.status == "open" and row.runnable == 0 and row.finished_at is None
        assert row.last_error == "incomplete:research_coverage_unverified"
        assert "https://example.com/concierge" in s.get(VentureDocument, "task-note:" + tid).body_md


def test_worker_failure_reopens_task_posts_chat_and_fails_the_job(vid_task, monkeypatch):
    from app.db.models import PlannedTask
    from app.planning import execute
    from app.state import db

    client, vid, tid = vid_task
    from app.core.jobs import FatalError

    async def broken(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(execute, "execute_planned_task", broken)
    assert client.post(f"/ventures/{vid}/tasks/{tid}/run").status_code == 202
    with pytest.raises(FatalError, match="handler_failed:RuntimeError"):
        asyncio.run(execute.run_task_job({"venture_id": vid, "task_id": tid}))
    with db()() as s:
        row = s.get(PlannedTask, tid)
        assert row.status == "open" and row.last_error.startswith("run_failed:")
    assert "couldn’t finish" in client.get(f"/ventures/{vid}/chat").text
    # A retry after the failure queues a fresh job.
    assert client.post(f"/ventures/{vid}/tasks/{tid}/run").status_code == 202
    assert len(_jobs(tid)) == 2


def test_task_run_is_a_registered_worker_kind():
    from app.worker.catalog import enabled_kinds
    from app.worker.handlers import HANDLERS, install_default_handlers

    install_default_handlers()
    assert "task.run" in enabled_kinds() and "task.run" in HANDLERS
