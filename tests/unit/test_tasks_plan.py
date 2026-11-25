"""POST /tasks/plan fills the queue without an onboarding run."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.mark.parametrize("idea", ["Stock handover tools for warehouses", "Swimming lessons for families"])
def test_empty_work_button_matches_its_actual_destination(client, monkeypatch, idea):
    from app.ventures import next_step

    created = client.post("/ventures", json={"raw_idea": idea})
    assert created.status_code == 201
    venture_id = created.json()["id"]
    monkeypatch.setattr(next_step, "_facts", lambda _: {"idea_state": "open", "plan_state": None})
    monkeypatch.setattr(next_step, "founder_next_step", lambda _: next_step._step(
        "idea.clarify", "Idea", "Review your saved answers", "Your answers are saved.",
        "Review your saved answers", f"/v/{venture_id}/context"))
    reply = client.get(f"/ventures/{venture_id}/tasks")
    assert reply.status_code == 200
    assert reply.json()["empty_action"] == {
        "label": "Review your saved answers", "href": f"/v/{venture_id}/context"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tasks_plan.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.mark.parametrize("idea", ["Inventory alerts for warehouses", "Walking clubs for families"])
def test_plan_then_run_writes_artifact(client: TestClient, idea):
    import asyncio
    from app.planning.execute import execute_planned_task, work_kind
    from app.planning.store import get_task

    created = client.post(
        "/ventures",
        json={"raw_idea": idea, "start": False},
    )
    assert created.status_code == 201, created.text
    vid = created.json()["id"]

    planned = client.post(f"/ventures/{vid}/tasks/plan")
    assert planned.status_code == 200, planned.text
    tasks = planned.json()["tasks"]
    task_ids = {task["id"] for task in tasks}
    assert task_ids and all(task_id.startswith("tsk_") for task_id in task_ids)

    slate = client.get(f"/ventures/{vid}/slate")
    assert slate.status_code == 200, slate.text
    assert {item["id"] for item in slate.json()["items"]} == task_ids

    saved_tasks = [get_task(vid, task["id"]) for task in tasks]
    research = [task for task in saved_tasks if task["runnable"]
        and work_kind(task["title"], task["tags"], task["detail"]) == "research"]
    assert research, tasks
    tid = research[0]["id"]

    ran = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert ran.status_code == 202, ran.text
    assert ran.json()["status"] == "running"
    body = asyncio.run(execute_planned_task(vid, tid, already_claimed=True))
    if body["status"] == "blocked":
        saved = get_task(vid, tid)
        assert saved["status"] == "open" and saved["finished_at"] is None, saved
        assert saved["last_error"].startswith("incomplete:") and not saved["runnable"], saved
        assert body["completion"]["mode"] == "unchanged", body
        if body.get("failure_code"):
            assert body["failure_code"] in {"research_sources_unverified", "research_coverage_unverified"}, body
            assert body["failure_code"] == saved["last_error"].removeprefix("incomplete:"), body
        assert "Status: not done" in Path(body["artifact"]).read_text()
    else:
        assert body["status"] in {"done", "needs_you"}, body
    assert Path(body["artifact"]).is_file()
