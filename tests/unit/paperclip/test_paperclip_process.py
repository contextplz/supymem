"""Paperclip process ports: goal fallback, budget hard-stop, atomic checkout."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/paperclip.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.delenv("COMPANY_OS_LLM_BUDGET_USD_MONTH", raising=False)
    main = load_company_os_app()
    from tests.company_os_harness import configure_local_test_model

    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "invoice chaser for freelancers who hate chasing"},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], monkeypatch


def test_resolve_issue_goal_id_matches_paperclip():
    load_company_os_app()
    from app.paperclip.goals import resolve_issue_goal_id, resolve_next_issue_goal_id

    assert (
        resolve_issue_goal_id(project_id=None, goal_id=None, default_goal_id="goal-1")
        == "goal-1"
    )
    assert (
        resolve_issue_goal_id(
            project_id=None,
            goal_id="goal-2",
            project_goal_id="goal-3",
            default_goal_id="goal-1",
        )
        == "goal-2"
    )
    assert (
        resolve_issue_goal_id(
            project_id="project-1",
            goal_id=None,
            project_goal_id="goal-2",
            default_goal_id="goal-1",
        )
        == "goal-2"
    )
    assert (
        resolve_issue_goal_id(
            project_id="project-1",
            goal_id=None,
            project_goal_id=None,
            default_goal_id="goal-1",
        )
        is None
    )
    assert (
        resolve_next_issue_goal_id(
            current_project_id=None,
            current_goal_id=None,
            current_project_goal_id=None,
            default_goal_id="goal-1",
        )
        == "goal-1"
    )
    assert (
        resolve_next_issue_goal_id(
            current_project_id=None,
            current_goal_id="goal-1",
            current_project_goal_id=None,
            project_id="project-1",
            goal_id=None,
            project_goal_id="goal-2",
            default_goal_id="goal-1",
        )
        == "goal-2"
    )
    assert (
        resolve_next_issue_goal_id(
            current_project_id="project-1",
            current_goal_id=None,
            current_project_goal_id="goal-2",
            default_goal_id="goal-1",
        )
        == "goal-2"
    )
    assert (
        resolve_next_issue_goal_id(
            current_project_id="project-1",
            current_goal_id="goal-explicit",
            current_project_goal_id="goal-2",
            project_id="project-2",
            project_goal_id="goal-3",
            default_goal_id="goal-1",
        )
        == "goal-explicit"
    )


def test_budget_status_from_observed_matches_paperclip():
    load_company_os_app()
    from app.paperclip.budget import budget_status_from_observed

    assert budget_status_from_observed(10, 0, 80) == "ok"
    assert budget_status_from_observed(50, 100, 80) == "ok"
    assert budget_status_from_observed(80, 100, 80) == "warning"
    assert budget_status_from_observed(100, 100, 80) == "hard_stop"
    assert budget_status_from_observed(101, 100, 80) == "hard_stop"


def test_hard_stop_unlimited_when_cap_unset(env):
    _client, vid, monkeypatch = env
    monkeypatch.delenv("COMPANY_OS_LLM_BUDGET_USD_MONTH", raising=False)
    from app.paperclip.budget import hard_stop_for_venture

    assert hard_stop_for_venture(vid) is None


def test_legacy_monthly_spend_cap_never_stops_venture_work(env):
    _client, vid, monkeypatch = env
    monkeypatch.setenv("COMPANY_OS_LLM_BUDGET_USD_MONTH", "0.0001")
    from app.actuation.social import workspace_id_for
    from app.llm.pricing import record_usage
    from app.paperclip.budget import hard_stop_for_venture

    record_usage(
        workspace_id_for(vid) or "platform",
        model="default",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert hard_stop_for_venture(vid) is None


def test_claim_task_is_compare_and_swap(env):
    client, vid, _monkey = env
    from app.paperclip.checkout import claim_task
    from app.planning.store import enqueue_task, get_task

    task = enqueue_task(
        vid,
        {
            "title": "Write a one-pager",
            "detail": "Keep it short.",
            "why_now": "Need a public artifact.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    first = claim_task(vid, task["id"])
    assert first is not None
    assert first["status"] == "running"
    second = claim_task(vid, task["id"])
    assert second is None
    assert get_task(vid, task["id"])["status"] == "running"


def test_claim_next_blocks_while_one_is_running(env):
    _client, vid, _monkey = env
    from app.paperclip.checkout import claim_next_open_task, claim_task
    from app.planning.store import enqueue_task

    a = enqueue_task(
        vid,
        {
            "title": "First open job",
            "detail": "A",
            "why_now": "Rank one.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    enqueue_task(
        vid,
        {
            "title": "Second open job",
            "detail": "B",
            "why_now": "Rank two.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    first = claim_task(vid, a["id"])
    assert first is not None
    second = claim_next_open_task(vid)
    assert second is None


def test_task_run_embeds_goal_ancestry(env):
    client, vid, monkeypatch = env
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Outline your agenda",
            "detail": "List the questions you want answered.",
            "why_now": "We need to understand the customer's workflow.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    ran = client.post(f"/ventures/{vid}/tasks/{task['id']}/run")
    if ran.status_code == 202:
        import asyncio
        import json
        from app.db.models import Job
        from app.planning import execute
        from app.state import db

        results = []
        run_task = execute.execute_planned_task

        async def observe(*args, **kwargs):
            result = await run_task(*args, **kwargs)
            results.append(result)
            return result

        monkeypatch.setattr(execute, "execute_planned_task", observe)
        with db()() as session:
            payload = json.loads(session.get(Job, ran.json()["job_id"]).payload_json)
        assert payload["venture_id"] == vid and payload["task_id"] == task["id"]
        asyncio.run(execute.run_task_job(payload))
        artifact = results[0].get("artifact")
        if results[0]["status"] == "blocked":
            from pathlib import Path

            assert "Status: not done" in Path(artifact).read_text()
        else:
            assert results[0]["status"] in {"done", "needs_you"}
    else:
        assert ran.status_code == 200, ran.text
        artifact = ran.json().get("artifact")
    assert artifact
    text = open(artifact, encoding="utf-8").read()
    assert "Why this work exists" in text
    assert "invoice chaser" in text.lower()
    assert "We need to understand the customer's workflow" in text
