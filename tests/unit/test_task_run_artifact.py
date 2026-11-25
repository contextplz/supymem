"""Running requested private writing leaves a grounded, unsent artifact."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.mark.acceptance
@pytest.mark.parametrize("idea", ["Inventory alerts for warehouses", "Walking clubs for families"])
def test_run_task_writes_artifact(tmp_path, monkeypatch, idea):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/tasks.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")

    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())

    created = client.post(
        "/ventures",
        json={"raw_idea": idea, "start": False},
    )
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    from app.planning.store import enqueue_task

    task = enqueue_task(vid, {"title": "Draft an interview invitation", "detail":
        f"Draft an interview invitation about {idea}. Do not send, publish or contact anyone.",
        "tags": ["docs"], "runnable": True})
    tid = task["id"]

    ran = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert ran.status_code == 202, ran.text
    assert ran.json()["status"] == "running"
    import asyncio
    from app.planning.execute import execute_planned_task

    body = asyncio.run(execute_planned_task(vid, tid, already_claimed=True))
    assert body["status"] == "done", body
    assert body.get("artifact")
    assert Path(body["artifact"]).is_file()
    text = Path(body["artifact"]).read_text()
    assert "Status: draft for your review; not sent or published." in text and "Interview invitation draft" in text
    assert created.json()["name"] in text
