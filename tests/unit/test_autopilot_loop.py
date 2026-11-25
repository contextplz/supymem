"""Autopilot should run real work, ask the founder, and not remix the slate."""

from __future__ import annotations

import asyncio

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/autopilot.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.setenv("CODING_AGENT_LIVE", "0")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.delenv("ZERNIO_API_KEY", raising=False)
    monkeypatch.delenv("OUTSTAND_API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    # These are offline workflow tests, not a live search-provider acceptance.
    # Keep real research rendering/artifacts while isolating external reads.
    from tests.fixtures.chat_eval.runner import install_replay_web

    install_replay_web(monkeypatch)
    with TestClient(main.create_app()) as c:
        yield c


def _venture(client: TestClient, idea: str = "invoice chaser for freelancers who hate chasing") -> str:
    created = client.post(
        "/ventures",
        json={"raw_idea": idea},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _run_queued_task(client: TestClient, vid: str, tid: str) -> dict:
    """Queue through HTTP, then execute the actual worker before reading results."""
    from app.core.jobs import lease
    from app.db.models import Job
    from app.planning.execute import _venture_repo
    from app.planning.store import get_task
    from app.state import db
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one

    queued = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert queued.status_code == 202, queued.text
    assert queued.json()["queued"] is True
    assert queued.json()["status"] == "running"
    assert get_task(vid, tid)["status"] == "running"
    install_default_handlers()
    with db()() as session:
        jobs = lease(session, "autopilot-loop-owned-test", kinds=["task.run"], n=1)
        session.commit()
    assert len(jobs) == 1 and jobs[0].venture_id == vid and jobs[0].payload["task_id"] == tid
    asyncio.run(_run_one(db(), jobs[0], {jobs[0].id}))
    with db()() as session:
        job = session.get(Job, jobs[0].id)
        job_status, job_error = job.status, job.last_error
    saved = get_task(vid, tid)
    assert saved["status"] != "running", saved
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in saved["title"].lower())[:48] or "task"
    artifact = _venture_repo(vid) / "tasks" / f"{slug}.md"
    assert artifact.is_file() and artifact.stat().st_size > 0
    if saved["status"] == "done":
        assert job_status == "succeeded" and job_error is None
        assert saved["finished_at"] is not None
        assert ("Status: completed" in artifact.read_text()
                or "Status: simulated draft prepared (not published)" in artifact.read_text())
        return {**saved, "status": "done", "artifact": artifact}
    assert saved["status"] == "open" and saved["runnable"] is False, saved
    error = saved["last_error"] or ""
    assert error.startswith(("needs_you:", "incomplete:")), saved
    if error in {"incomplete:research_sources_unverified", "incomplete:research_coverage_unverified"}:
        assert job_status == "failed_dead" and job_error == error.removeprefix("incomplete:")
        assert saved["finished_at"] is None and "Status: not done" in artifact.read_text()
    else:
        assert job_status == "succeeded" and job_error is None
    return {**saved, "status": "needs_you" if error.startswith("needs_you:") else "blocked"}


def _run_task_or_require_coding_tool(client: TestClient, venture_id: str, task: dict) -> bool:
    from app.planning.execute import _is_local_build_task
    from app.planning.store import get_task

    before = get_task(venture_id, task["id"])
    assert before is not None
    if not _is_local_build_task(before):
        _run_queued_task(client, venture_id, task["id"])
        return True
    response = client.post(f"/ventures/{venture_id}/tasks/{task['id']}/run")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "build_tool_needs_setup"
    assert "Settings > Keys and connections > Coding tools" in response.json()["detail"]["message"]
    assert get_task(venture_id, task["id"]) == before
    return False


@pytest.mark.parametrize("idea", ["invoice chaser for freelancers who hate chasing", "Meal planning app for families"])
def test_slate_keeps_completed_and_setup_blocked_tasks_without_reminting(client: TestClient, idea):
    from app.core.local_mode import is_local
    from app.planning.store import enqueue_task

    vid = _venture(client, idea)
    planned = client.post(f"/ventures/{vid}/tasks/plan")
    assert planned.status_code == 200, planned.text
    tasks = planned.json()["tasks"]
    assert len(tasks) >= 1
    if is_local():
        tasks.append(enqueue_task(vid, {"title": "Build a private prototype",
            "detail": f"Create a working local prototype for {idea}.", "tags": ["prototype"], "runnable": True}))
    first_ids = {row["id"] for row in tasks}
    setup_blocked = set()

    for row in tasks:
        if not _run_task_or_require_coding_tool(client, vid, row):
            setup_blocked.add(row["id"])
    if is_local():
        assert setup_blocked

    slate = client.get(f"/ventures/{vid}/slate")
    assert slate.status_code == 200, slate.text
    items = slate.json().get("items") or []
    new_ids = {item.get("id") for item in items if item.get("id")}
    assert not (new_ids - first_ids), new_ids
    open_tasks = client.get(f"/ventures/{vid}/tasks").json().get("items") or []
    runnable = [row for row in open_tasks if row.get("runnable", True)]
    assert len(runnable) == len(setup_blocked) and {row["id"] for row in runnable} == setup_blocked


@pytest.mark.parametrize("idea", ["invoice chaser for freelancers who hate chasing", "Meal planning app for families"])
def test_execute_marks_agent_idle(client: TestClient, idea):
    from app.planning.execute import work_kind

    vid = _venture(client, idea)
    planned = client.post(f"/ventures/{vid}/tasks/plan")
    tid = next(row["id"] for row in planned.json()["tasks"]
        if work_kind(row["title"], row.get("tags") or [], row.get("detail") or "") == "research")
    ran = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert ran.status_code == 202, ran.text
    import asyncio
    from app.planning.execute import execute_planned_task

    out = asyncio.run(execute_planned_task(vid, tid, already_claimed=True))
    assert out["status"] in {"done", "needs_you", "blocked"}
    live = client.get(f"/ventures/{vid}/agents/live").json()
    rows = live.get("items") or live.get("agents") or (live if isinstance(live, list) else [])
    working = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("state") == "working"
        and str(row.get("current_task_desc") or "").startswith("Working on")
    ]
    assert working == [], live


def test_outreach_asks_for_social_handles(client: TestClient):
    vid = _venture(client)
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Post the founder outreach",
            "detail": "Post a short ask to the market.",
            "why_now": "Need channels before publishing.",
            "tags": ["probe"],
            "runnable": True,
        },
    )
    body = _run_queued_task(client, vid, task["id"])
    assert body["status"] == "needs_you"
    humans = client.get(f"/ventures/{vid}/human-tasks").json().get("items") or []
    assert any("social" in (item.get("title") or "").lower() or item.get("provider") == "social" for item in humans)
    from app.desktop_notices import pending

    notices = pending()
    notice = next(
        item
        for item in notices
        if item["kind"] == "human_task" and "needs you" in item["title"].lower()
    )
    human = next(item for item in humans if item.get("provider") == "social")
    assert notice["target_path"] == f"/v/{vid}#attention-{human['id']}"
    dash = client.get(f"/ventures/{vid}/dashboard").json()
    assert any(item.get("provider") == "social" for item in (dash.get("needs_you") or []))
    queued = client.get(f"/ventures/{vid}/tasks").json().get("items") or []
    parked = next(row for row in queued if row["id"] == task["id"])
    assert parked.get("runnable") is False


def test_founder_can_request_a_visible_manual_alternative(client: TestClient):
    vid = _venture(client)
    from app.concierge.human import ensure_human_task

    task = ensure_human_task(
        vid,
        title="Connect social posting",
        why="Outreach delivery is blocked.",
        provider="social",
    )
    changed = client.post(
        f"/ventures/{vid}/human-tasks/{task['id']}/alternative",
        json={"reason": "I cannot create another account today."},
    )
    assert changed.status_code == 200, changed.text
    alternative = changed.json()["task"]
    assert alternative["status"] == "open"
    assert alternative["category"] == "alternative"
    assert "manual" in alternative["title"].lower()
    assert any("send" in step["text"].lower() for step in alternative["steps"])
    dashboard = client.get(f"/ventures/{vid}/dashboard").json()
    visible = next(item for item in dashboard["needs_you"] if item["id"] == task["id"])
    assert visible["title"] == alternative["title"]
    assert visible["steps"] == alternative["steps"]


def test_connect_unparks_outreach_and_does_not_use_settings(client: TestClient):
    vid = _venture(client)
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Post the founder outreach",
            "detail": "Post a short ask to the market.",
            "why_now": "Need channels before publishing.",
            "tags": ["probe"],
            "runnable": True,
        },
    )
    ran = _run_queued_task(client, vid, task["id"])
    assert ran["status"] == "needs_you"
    dash = client.get(f"/ventures/{vid}/dashboard").json()
    social = next(item for item in dash["needs_you"] if item.get("provider") == "social")
    assert social["href"] == f"/v/{vid}/connect/social"
    assert "/settings" not in (social["href"] or "")
    guide = client.get(f"/ventures/{vid}/connect/social")
    assert guide.status_code == 200, guide.text
    body = guide.json()
    assert body["guide"]["open_url"]
    assert any((step.get("url") or "").startswith("http") for step in body["guide"]["steps"])
    walk = body["guide"].get("walkthrough") or []
    assert len(walk) >= 4
    signup = next(item for item in walk if item.get("id") == "signup")
    assert "Get started" in " ".join(signup.get("look_for") or [])
    assert signup.get("cursor")
    assert signup.get("click")
    saved = client.post(
        f"/ventures/{vid}/connect/social",
        json={"fields": {"x": "@founders"}, "skipped": False},
    )
    assert saved.status_code == 200, saved.text
    queued = client.get(f"/ventures/{vid}/tasks").json().get("items") or []
    row = next(item for item in queued if item["id"] == task["id"])
    assert row.get("runnable") is True
    humans = client.get(f"/ventures/{vid}/human-tasks").json().get("items") or []
    assert not any(item.get("provider") == "social" for item in humans)
    ran2 = _run_queued_task(client, vid, task["id"])
    assert ran2["status"] == "done"
    assert "**Simulated**" in ran2["artifact"].read_text()
    assert "Published live" not in ran2["artifact"].read_text()


@pytest.mark.asyncio
async def test_driver_does_not_open_social_asks_without_founder_opt_in(client: TestClient):
    vid = _venture(client)
    from unittest.mock import patch

    from app.will.driver import drive_open_work

    with patch(
        "app.will.driver.list_active",
        return_value=[{"id": vid, "paused": False, "stage": "live"}],
    ):
        await drive_open_work()
    humans = client.get(f"/ventures/{vid}/human-tasks").json().get("items") or []
    assert not any(item.get("provider") == "social" for item in humans)


@pytest.mark.parametrize("title,provider", [
    ("Test a paid same-evening vet house-call offer", "payments"),
    ("Test a paid same-evening vet house-call offer using Stripe", "stripe"),
    ("Test a paid same-evening vet house-call offer using Razorpay", "razorpay"),
])
def test_paid_offer_waits_for_the_named_or_unnamed_merchant(title, provider):
    from app.planning.execute import _founder_gate

    gate = _founder_gate(title, [])
    assert gate is not None
    assert gate["provider"] == provider
    assert "Customer payment collection stays disabled" in gate["why"]
    assert "separately approved merchant setup" in gate["why"]


def test_unnamed_paid_offer_saves_decision_without_requesting_stripe(client, monkeypatch):
    from app.chat.store import list_messages
    from app.planning.store import enqueue_task
    from app.secrets import need

    vid = _venture(client)
    requested = []
    monkeypatch.setattr(need, "request_for_provider", lambda *args, **kwargs: requested.append(args))
    task = enqueue_task(vid, {
        "title": "Test a paid same-evening vet house-call offer",
        "detail": "Customer collection is not enabled; merchant choice requires separate approval.",
        "tags": [], "runnable": True,
    }, source="chat")
    result = _run_queued_task(client, vid, task["id"])
    assert result["status"] == "needs_you"
    assert result["last_error"] == "needs_you:payments"
    assert requested == []
    humans = client.get(f"/ventures/{vid}/human-tasks").json()["items"]
    held = next(row for row in humans if row["provider"] == "payments")
    assert held["category"] == "decision"
    text = "\n".join(row["body_md"] for row in list_messages(vid))
    assert "Choose the merchant" in text and "no payment credential was requested" in text
    assert "A secure form opened" not in text
    assert "Customer payment collection stays disabled" in text


@pytest.mark.parametrize("idea", ["invoice chaser for freelancers who hate chasing", "Meal planning app for families"])
def test_refill_unused_skips_titles_already_done(client: TestClient, idea):
    vid = _venture(client, idea)
    planned = client.post(f"/ventures/{vid}/tasks/plan")
    tasks = planned.json()["tasks"]
    first_titles = {row["title"] for row in tasks}
    for row in tasks:
        _run_task_or_require_coding_tool(client, vid, row)
    from app.planning.cycle import refill_unused_work
    from app.planning.execute import is_product_work, work_kind

    open_left = client.get(f"/ventures/{vid}/tasks").json().get("items") or []
    unfinished_product = [
        row
        for row in open_left
        if row.get("status") in {"open", "running"}
        and work_kind(row.get("title") or "", row.get("tags") or []) != "research"
    ]
    filled = refill_unused_work(vid)
    if unfinished_product:
        assert filled == []
        return
    assert filled, "expected unused product playbook items after research-only first five"
    new_titles = {row["title"] for row in filled}
    assert new_titles.isdisjoint(first_titles), new_titles & first_titles
    assert all(is_product_work(row["title"], row.get("tags") or []) for row in filled)


@pytest.mark.asyncio
async def test_scheduler_survives_a_job_crash():
    from app.will.scheduler import WillScheduler

    hits = {"n": 0}

    async def boom() -> None:
        hits["n"] += 1
        raise RuntimeError("job exploded")

    scheduler = WillScheduler()
    scheduler.register("boom", 0.05, boom)
    scheduler.start()
    await asyncio.sleep(0.2)
    await scheduler.stop()
    assert hits["n"] >= 2
