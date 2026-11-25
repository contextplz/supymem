"""Ops task service lifecycle."""

from __future__ import annotations

def test_create_and_get_task(ops_env):
    from app.ops.services import tasks as tasks_svc

    ws = ops_env["workspace_id"]
    task = tasks_svc.create_task(
        ws, title="Write a blog post", agent_type="social_media", priority=2
    )
    assert task["id"].startswith("opt_")
    assert task["status"] == "pending"
    assert task["priority"] == 2

    fetched = tasks_svc.get_task(ws, task["id"])
    assert fetched is not None
    assert fetched["id"] == task["id"]


def test_get_task_not_found(ops_env):
    from app.ops.services import tasks as tasks_svc

    assert tasks_svc.get_task(ops_env["workspace_id"], "opt_missing0000") is None


def test_update_task_status(ops_env):
    from app.ops.services import tasks as tasks_svc

    ws = ops_env["workspace_id"]
    task = tasks_svc.create_task(ws, title="Test", agent_type="finance")
    updated = tasks_svc.update_task_status(
        ws, task["id"], "completed", result_summary="Done"
    )
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["result_summary"] == "Done"


def test_list_tasks_filter_by_status(ops_env):
    from app.ops.services import tasks as tasks_svc

    ws = ops_env["workspace_id"]
    tasks_svc.create_task(ws, title="Task A", agent_type="finance")
    t2 = tasks_svc.create_task(ws, title="Task B", agent_type="social_media")
    tasks_svc.update_task_status(ws, t2["id"], "completed")

    pending = tasks_svc.list_tasks(ws, status="pending")
    completed = tasks_svc.list_tasks(ws, status="completed")
    assert all(t["status"] == "pending" for t in pending)
    assert all(t["status"] == "completed" for t in completed)


def test_create_and_finish_agent_run(ops_env):
    from app.ops.services import tasks as tasks_svc

    ws = ops_env["workspace_id"]
    task = tasks_svc.create_task(ws, title="Run me", agent_type="finance")
    run = tasks_svc.create_agent_run(ws, "finance", task_id=task["id"])
    finished = tasks_svc.finish_agent_run(
        ws, run["id"], "completed", output={"summary": "ok"}, duration_secs=0.1
    )
    assert finished is not None
    assert finished["status"] == "completed"
