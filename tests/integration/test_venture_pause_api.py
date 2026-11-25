"""Pause / resume HTTP contract: dashboard, tasks, onboarding restart."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pause_api.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    if "argon2" not in sys.modules:
        argon2 = types.ModuleType("argon2")
        exceptions = types.ModuleType("argon2.exceptions")

        class PasswordHasher:
            pass

        class VerifyMismatchError(Exception):
            pass

        argon2.PasswordHasher = PasswordHasher  # type: ignore[attr-defined]
        exceptions.VerifyMismatchError = VerifyMismatchError  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "argon2", argon2)
        monkeypatch.setitem(sys.modules, "argon2.exceptions", exceptions)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def _make_venture(client: TestClient) -> str:
    r = client.post(
        "/ventures",
        json={"raw_idea": "invoice chaser for freelancers who hate chasing", "start": False},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _headers() -> dict[str, str]:
    return {"X-Subject": "founder"}


def test_dashboard_reports_paused_without_touching_governance(client: TestClient):
    vid = _make_venture(client)
    before = client.get(f"/ventures/{vid}/dashboard")
    assert before.status_code == 200, before.text
    gov_before = before.json()["governance"]

    paused = client.post(
        f"/ventures/{vid}/pause",
        json={"reason": "hold spend"},
        headers=_headers(),
    )
    assert paused.status_code == 200, paused.text

    dash = client.get(f"/ventures/{vid}/dashboard")
    assert dash.status_code == 200, dash.text
    body = dash.json()
    status = body["venture"]["status"]
    assert status["code"] == "paused"
    assert "hold spend" in status["detail"]
    gov = body["governance"]
    assert gov["autonomy"] == gov_before["autonomy"]
    assert gov["kill_switch"] == gov_before["kill_switch"]
    assert gov["pending_approvals"] == gov_before["pending_approvals"]
    assert gov["caps"]["spend_daily_usd"] == gov_before["caps"]["spend_daily_usd"]
    assert gov["caps"]["agents_max"] == gov_before["caps"]["agents_max"]


def test_run_task_returns_409_while_paused(client: TestClient):
    vid = _make_venture(client)
    planned = client.post(f"/ventures/{vid}/tasks/plan")
    assert planned.status_code == 200, planned.text
    tasks = planned.json()["tasks"]
    assert tasks
    tid = tasks[0]["id"]

    paused = client.post(f"/ventures/{vid}/pause", json={"reason": "hold"}, headers=_headers())
    assert paused.status_code == 200, paused.text

    ran = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert ran.status_code == 409, ran.text
    assert ran.json()["detail"]["error"]["code"] == "venture_paused"

    client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    after = client.post(f"/ventures/{vid}/tasks/{tid}/run")
    assert after.status_code == 202, after.text
    assert after.json()["status"] == "running"


def test_resume_restarts_previously_running_onboarding(client: TestClient):
    vid = _make_venture(client)
    from app.db.models import Venture
    from app.onboarding.state import create_run, load_run
    from app.state import db

    with db()() as session:
        ven = session.get(Venture, vid)
        assert ven is not None
        wid = ven.workspace_id

    run = create_run(vid, workspace_id=wid)
    assert run.status == "running"

    paused = client.post(f"/ventures/{vid}/pause", json={"reason": "hold"}, headers=_headers())
    assert paused.status_code == 200, paused.text
    assert paused.json()["stopped"]["onboarding"] is True
    halted = load_run(run.id)
    assert halted is not None
    assert halted.status == "paused"
    assert halted.paused_reason == "venture_paused"

    resumed = client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["resumed"]["onboarding"] is True
    again = load_run(run.id)
    assert again is not None
    assert again.status == "running"
    assert again.paused_reason is None


def test_pause_unknown_or_archived_is_404(client: TestClient):
    missing = client.post("/ventures/ven_nope/pause", json={}, headers=_headers())
    assert missing.status_code == 404

    vid = _make_venture(client)
    from app.ventures.lifecycle import CONFIRM_PHRASE

    deleted = client.post(
        f"/ventures/{vid}/delete",
        json={"confirmation": CONFIRM_PHRASE},
        headers=_headers(),
    )
    assert deleted.status_code == 200, deleted.text
    paused = client.post(f"/ventures/{vid}/pause", json={}, headers=_headers())
    assert paused.status_code == 404
    resumed = client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    assert resumed.status_code == 404
