"""Dismissing blocked work reconciles the durable founder-attention queue."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/dismiss_attention.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as running:
        yield running


def test_last_dismissed_provider_blocker_clears_attention(client: TestClient):
    created = client.post("/ventures", json={"raw_idea": "a simple founder tool"})
    assert created.status_code == 201, created.text
    venture_id = created.json()["id"]

    from app.concierge.human import ensure_human_task
    from app.db.models import PlannedTask
    from app.planning.store import enqueue_task
    from app.state import db

    social_attention = ensure_human_task(
        venture_id,
        title="Connect a reviewed social channel",
        why="Two planned tasks are waiting for the same provider.",
        provider="social",
    )
    decision_attention = ensure_human_task(
        venture_id,
        title="Choose the next experiment",
        why="This unrelated decision must remain visible.",
        provider="decision",
    )
    blocked = [
        enqueue_task(
            venture_id,
            {
                "title": title,
                "detail": "Wait for a reviewed channel.",
                "why_now": "Founder input is required.",
                "tags": ["probe"],
                "runnable": False,
            },
        )
        for title in ("Prepare outreach one", "Prepare outreach two")
    ]
    factory = db()
    with factory() as session:
        for task in blocked:
            row = session.get(PlannedTask, task["id"])
            row.last_error = "needs_you:social"
        session.commit()

    first = client.post(f"/ventures/{venture_id}/tasks/{blocked[0]['id']}/dismiss")
    assert first.status_code == 200, first.text
    after_first = client.get(f"/ventures/{venture_id}/human-tasks").json()["items"]
    assert any(item["id"] == social_attention["id"] for item in after_first)

    second = client.post(f"/ventures/{venture_id}/tasks/{blocked[1]['id']}/dismiss")
    assert second.status_code == 200, second.text
    after_second = client.get(f"/ventures/{venture_id}/human-tasks").json()["items"]
    assert not any(item["id"] == social_attention["id"] for item in after_second)
    assert any(item["id"] == decision_attention["id"] for item in after_second)
