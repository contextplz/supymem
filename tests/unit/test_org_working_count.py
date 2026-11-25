"""Working means a fresh heartbeat — hired/active is not enough."""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/working.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def _venture(client: TestClient) -> str:
    created = client.post(
        "/ventures",
        json={"raw_idea": "lobbylock apartment buildings with a shared waitlist"},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _roster():
    return [
        {"id": "maya", "name": "Maya", "role": "research", "status": "active"},
        {"id": "cole", "name": "Cole", "role": "builder", "status": "active"},
        {"id": "lina", "name": "Lina", "role": "design", "status": "running"},
    ]


def test_summarize_hired_active_is_not_working():
    load_company_os_app()
    from app.org.summary import summarize_agents

    summary = summarize_agents(_roster())
    assert summary["hired"] == 3
    assert summary["working"] == 0
    assert summary["active"] == 0


def test_summarize_working_is_fresh_heartbeat_only(client: TestClient):
    vid = _venture(client)
    from app.org.heartbeat import HEARTBEAT_TIMEOUT_SECONDS, mark_working
    from app.org.summary import persist_org, summarize_agents
    from app import state
    from app.db.models import AgentLiveStatus
    from core.clock import utcnow

    persist_org(vid, {"agents": _roster()})
    summary = summarize_agents(_roster(), venture_id=vid)
    assert summary["working"] == 0

    mark_working("maya", vid, "Writing the brief")
    summary = summarize_agents(_roster(), venture_id=vid)
    assert summary["working"] == 1
    assert summary["hired"] == 3

    with state.db()() as session:
        row = session.get(AgentLiveStatus, "maya")
        row.last_heartbeat = utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS + 5)
        session.commit()
    summary = summarize_agents(_roster(), venture_id=vid)
    assert summary["working"] == 0


def test_dashboard_team_tile_does_not_call_hired_working(client: TestClient):
    vid = _venture(client)
    from app.org.heartbeat import mark_working
    from app.org.summary import persist_org

    persist_org(vid, {"agents": _roster()})
    dash = client.get(f"/ventures/{vid}/dashboard").json()
    team = (dash.get("tiles") or {}).get("team") or dash.get("team")
    assert team["hired"] == 3
    assert team["working"] == 0
    assert "working" not in (team.get("note") or "")

    mark_working("maya", vid, "Scanning demand")
    dash = client.get(f"/ventures/{vid}/dashboard").json()
    team = (dash.get("tiles") or {}).get("team") or dash.get("team")
    assert team["working"] == 1
    assert "1 working" in team["note"]
    assert "3 hired" in team["note"]


def test_operational_status_omits_zero_working():
    from app.org.summary import operational_status

    s = operational_status(
        stage="live",
        onboarding_status="done",
        agent_summary={"hired": 11, "working": 0, "watching": 0},
        signals={"has_site": True, "has_team": True},
    )
    assert s["code"] == "live"
    assert "11 teammates" in s["detail"]
    assert "working" not in s["detail"]
