"""Founder instructions inject into a live harness turn instead of a second task."""

from __future__ import annotations

import json

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/live.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "office plant watering club"},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"]


@pytest.mark.asyncio
async def test_live_instruction_does_not_enqueue(env):
    _client, vid = env
    from harness import Inbox

    from app.chat.context import assemble
    from app.chat.handlers.change import handle
    from app.planning.store import list_tasks
    from app.runtime.live_turns import register, unregister

    inbox = Inbox()
    register(vid, inbox)
    try:
        before = {
            row["id"]
            for row in list_tasks(vid, statuses=("open", "running", "done"))
            if row.get("source") == "chat"
        }
        ctx = assemble(vid)
        out = await handle(ctx, "please find NYC office plants only")
        assert "in flight" in out["body_md"].lower() or "second task" in out["body_md"].lower()
        after = {
            row["id"]
            for row in list_tasks(vid, statuses=("open", "running", "done"))
            if row.get("source") == "chat"
        }
        assert after == before
        assert any("NYC" in (m.get("content") or "") for m in inbox.next_step)
    finally:
        unregister(vid)


@pytest.mark.parametrize("opening,summary", [
    ("A settlement reconciliation platform for treasury teams at payment companies", "settlement discrepancies"),
    ("A wearable asthma monitor for people living with asthma", "breathing patterns"),
])
@pytest.mark.parametrize("outcome", ["run", "summary", "paused", "pause_unknown", "enqueue_failed", "approval_required", "context"])
@pytest.mark.asyncio
async def test_requested_work_starts_without_a_second_click(env, monkeypatch, opening, summary, outcome):
    from sqlalchemy import select

    from app.chat.context import assemble
    from app.chat.handlers import change
    from app.core import jobs
    from app.db.models import Job, Venture
    from app.planning import execute
    from app.planning.store import list_tasks
    from app.state import db
    from app.ventures.lifecycle import pause

    _client, vid = env
    with db()() as session:
        session.get(Venture, vid).raw_idea = opening
        session.commit()
    if outcome == "paused":
        pause(vid)
    elif outcome == "pause_unknown":
        def unavailable(*args, **kwargs):
            raise RuntimeError("pause status unavailable")
        monkeypatch.setattr(change, "is_paused", unavailable)
    elif outcome == "enqueue_failed":
        original = jobs.enqueue
        def fail_enqueue(session, kind, *args, **kwargs):
            if kind == "task.run":
                raise RuntimeError("enqueue unavailable")
            return original(session, kind, *args, **kwargs)
        monkeypatch.setattr(jobs, "enqueue", fail_enqueue)
    elif outcome == "approval_required":
        def refuse(*args, **kwargs):
            raise execute.TaskRunError(409, "dedicated_workflow_required", "You must use the approved workflow for this task.")
        monkeypatch.setattr(execute, "_precheck", refuse)
    message = f"Please write a summary of {summary} for {opening}." if outcome == "summary" else "Run it for me"
    if outcome == "context":
        message = f"The product is {opening}."
    reply = await change.handle(assemble(vid), message)
    tasks = [task for task in list_tasks(vid, statuses=("open", "running", "done")) if task.get("source") == "chat"]
    assert len(tasks) == (0 if outcome == "context" else 1)
    with db()() as session:
        runs = session.scalars(select(Job).where(Job.venture_id == vid, Job.kind == "task.run")).all()
        if outcome == "context":
            assert not runs
        elif outcome in {"run", "summary"}:
            assert tasks[0]["status"] == "running"
            assert len(runs) == 1 and runs[0].workspace_id == assemble(vid).venture["workspace_id"]
            assert json.loads(runs[0].payload_json) == {
                "task_id": tasks[0]["id"], "venture_id": vid,
                "workspace_id": assemble(vid).venture["workspace_id"],
            }
            assert "not started" not in reply["body_md"]
            assert not any(action.get("action") == "task.run" or action.get("confirmation") for action in reply["actions"])
        else:
            assert tasks[0]["status"] == "open" and not runs
            if outcome == "enqueue_failed":
                assert tasks[0]["last_error"] == "enqueue_failed"
    if outcome != "context":
        assert reply["actions"]
    assert "—" not in reply["body_md"]
