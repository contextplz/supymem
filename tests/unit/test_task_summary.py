"""POST /tasks/{id}/summary writes a founder brief on click."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture(params=["Inventory alerts for warehouses", "Walking clubs for families"])
def idea(request):
    return request.param


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/task_summary.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def _venture_with_task(client: TestClient, idea: str) -> tuple[str, dict]:
    created = client.post(
        "/ventures",
        json={"raw_idea": idea},
    )
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    from app.planning.store import enqueue_task

    task = enqueue_task(vid, {"title": "Draft an interview invitation", "detail":
        f"Draft an interview invitation about {idea}. Do not send, publish or contact anyone.",
        "runnable": True, "tags": ["draft"]})
    return vid, task


def _run_task(client, venture_id, task):
    import asyncio
    import json
    from app.db.models import Job
    from app.planning.execute import run_task_job
    from app.state import db

    response = client.post(f"/ventures/{venture_id}/tasks/{task['id']}/run")
    if response.status_code == 202:
        with db()() as session:
            job = session.get(Job, response.json()["job_id"])
            assert job.kind == "task.run" and job.venture_id == venture_id
            payload = json.loads(job.payload_json)
        assert payload["venture_id"] == venture_id and payload["task_id"] == task["id"]
        asyncio.run(run_task_job(payload))
    else:
        assert response.status_code == 200, response.text


def test_queued_task_summary_is_grounded(client: TestClient, idea):
    vid, task = _venture_with_task(client, idea)
    r = client.post(f"/ventures/{vid}/tasks/{task['id']}/summary", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "grounded"
    assert body["phase"] == "queued"
    assert task["title"].split()[0] in body["body_md"]
    assert "saved for your review" in body["body_md"].lower()
    assert "nothing starts automatically" in body["body_md"].lower()
    assert body["cached"] is False

    again = client.post(f"/ventures/{vid}/tasks/{task['id']}/summary", json={})
    assert again.status_code == 200
    assert again.json()["cached"] is True
    assert again.json()["body_md"] == body["body_md"]


def test_done_task_summary_mentions_finish(client: TestClient, idea):
    vid, task = _venture_with_task(client, idea)
    _run_task(client, vid, task)
    r = client.post(
        f"/ventures/{vid}/tasks/{task['id']}/summary",
        json={"refresh": True},
    )
    assert r.status_code == 200, r.text
    text = r.json()["body_md"].lower()
    assert r.json()["phase"] == "done"
    assert "finished" in text or "done" in text


def test_live_title_matches_planned_task(client: TestClient, idea):
    vid, task = _venture_with_task(client, idea)
    _run_task(client, vid, task)
    r = client.post(
        f"/ventures/{vid}/tasks/live-agt_demo/summary",
        json={"title": f"Just finished {task['title']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phase"] == "done"
    assert body.get("task_id") == task["id"]
    assert task["title"].split()[0] in (body.get("title") or body["body_md"])


def test_unknown_line_still_explains_the_title_without_claiming_execution(client: TestClient, idea):
    created = client.post(
        "/ventures",
        json={"raw_idea": idea},
    )
    vid = created.json()["id"]
    r = client.post(
        f"/ventures/{vid}/tasks/live-agt_demo/summary",
        json={"title": "Working on Recruit design partners"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phase"] == "not planned"
    assert "You have not saved or started this work yet" in body["body_md"]
    assert "design partners" in body["body_md"].lower() or "Recruit" in body["body_md"]
