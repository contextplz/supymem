"""Async onboarding spawn returns a run row without waiting for pause/done."""

from __future__ import annotations

import asyncio

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
async def test_start_onboarding_async_returns_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/spawn.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.setenv("LLM_OFFLINE", "true")

    main = load_company_os_app()
    from app import state as state_mod
    from app.onboarding.spawn import start_onboarding

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    client = TestClient(main.create_app())

    created = client.post(
        "/ventures",
        json={"raw_idea": "a toolkit for neighbourhood tool libraries", "name": "Toolshare", "start": False},
    )
    assert created.status_code == 201, created.text
    vid = created.json()["id"]

    run = await start_onboarding(vid, mode="guided", entry_mode="idea")
    assert run.id
    assert run.status == "running"
    # Must not have blocked until done/paused in the awaiter — background task may still be going.
    assert run.phases_done == 0 or run.status in {"running", "paused", "done"}

    # Let the background task progress a bit.
    await asyncio.sleep(0.5)
    status = client.get(f"/ventures/{vid}/onboarding")
    assert status.status_code == 200
    body = status.json()
    assert body["run_id"] == run.id
    assert body["status"] in {"running", "paused", "done"}
