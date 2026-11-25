"""Venture pause / resume: durable state, roster, and skip-if-paused guards."""

from __future__ import annotations

import sys
import types

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/pause.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
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
    from app import state

    state.reset_db_factory()
    from tests.company_os_harness import company_os_test_client as TestClient

    with TestClient(main.create_app()) as c:
        yield c


def _make_venture(client):
    r = client.post(
        "/ventures",
        json={"raw_idea": "a marketplace for restoring vintage synthesizers", "start": False},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _headers():
    return {"X-Subject": "founder"}


def test_pause_sets_state_visible_in_list_and_detail(client):
    vid = _make_venture(client)
    paused = client.post(
        f"/ventures/{vid}/pause",
        json={"reason": "hold spend"},
        headers=_headers(),
    )
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["id"] == vid
    assert body["paused"] is True
    assert body["paused_reason"] == "hold spend"
    assert body["paused_at"]
    assert body["status"]["code"] == "paused"
    assert "hold spend" in body["status"]["detail"]
    assert set(body["stopped"]) == {"onboarding", "agents", "tasks"}

    listed = client.get("/ventures").json()["items"]
    item = next(i for i in listed if i["id"] == vid)
    assert item["paused"] is True
    assert item["paused_reason"] == "hold spend"
    assert item["paused_at"]
    assert item["status"]["code"] == "paused"

    detail = client.get(f"/ventures/{vid}")
    assert detail.status_code == 200
    got = detail.json()
    assert got["paused"] is True
    assert got["paused_reason"] == "hold spend"
    assert got["paused_at"]
    assert got["status"]["code"] == "paused"


def test_double_pause_is_409(client):
    vid = _make_venture(client)
    first = client.post(f"/ventures/{vid}/pause", json={}, headers=_headers())
    assert first.status_code == 200, first.text
    second = client.post(f"/ventures/{vid}/pause", json={"reason": "again"}, headers=_headers())
    assert second.status_code == 409
    assert second.json()["detail"]["error"]["code"] == "already_paused"


def test_resume_clears_pause(client):
    vid = _make_venture(client)
    client.post(f"/ventures/{vid}/pause", json={"reason": "hold spend"}, headers=_headers())
    resumed = client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["paused"] is False
    assert body["resumed_at"]
    assert body["status"]["code"] != "paused"
    assert body["resumed"] == {"onboarding": False}

    detail = client.get(f"/ventures/{vid}").json()
    assert detail["paused"] is False
    assert detail["paused_reason"] is None
    assert detail["paused_at"] is None

    listed = next(i for i in client.get("/ventures").json()["items"] if i["id"] == vid)
    assert listed["paused"] is False


def test_resume_of_non_paused_is_409(client):
    vid = _make_venture(client)
    denied = client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    assert denied.status_code == 409
    assert denied.json()["detail"]["error"]["code"] == "not_paused"


def test_agents_go_idle_and_roster_survives_round_trip(client):
    vid = _make_venture(client)
    from app.org.heartbeat import list_live_status, mark_working
    from app.org.summary import persist_org, resolve_org
    from app.state import db
    from app.db.models import AgentInstance
    from core.clock import utcnow

    now = utcnow()
    with db()() as session:
        session.add(
            AgentInstance(
                id="ag_pause",
                venture_id=vid,
                spec_id="as_1",
                spec_version="1",
                status="active",
                created_at=now,
            )
        )
        session.commit()

    persist_org(
        vid,
        {
            "agents": [
                {
                    "id": "ag_pause",
                    "name": "Will",
                    "role": "orchestrator",
                    "status": "active",
                    "title": "Orchestrator",
                }
            ],
            "agent_count": 1,
            "source": "test",
        },
    )
    mark_working("ag_pause", vid, "Drafting the launch post")
    assert list_live_status(vid)[0]["state"] == "working"

    paused = client.post(f"/ventures/{vid}/pause", json={"reason": "hold"}, headers=_headers())
    assert paused.status_code == 200, paused.text
    assert paused.json()["stopped"]["agents"] >= 1
    live = list_live_status(vid)
    assert live and all(row["state"] != "working" for row in live)
    assert live[0]["state"] == "idle"

    roster = resolve_org(vid).get("agents") or []
    assert any(a.get("id") == "ag_pause" for a in roster)

    resumed = client.post(f"/ventures/{vid}/resume", json={}, headers=_headers())
    assert resumed.status_code == 200, resumed.text
    after = resolve_org(vid).get("agents") or []
    assert any(a.get("id") == "ag_pause" for a in after)
    assert len(after) == len(roster)


def test_pause_survives_reconstructing_store_from_disk(client, tmp_path):
    vid = _make_venture(client)
    client.post(f"/ventures/{vid}/pause", json={"reason": "reboot"}, headers=_headers())

    from app import state
    from app.ventures.lifecycle import is_paused, pause_fields

    pause_path = tmp_path / "ventures" / vid / "pause.json"
    assert pause_path.is_file()

    state.reset_db_factory()
    assert is_paused(vid) is True
    fields = pause_fields(vid)
    assert fields["paused"] is True
    assert fields["paused_reason"] == "reboot"

    listed = client.get("/ventures").json()["items"]
    item = next(i for i in listed if i["id"] == vid)
    assert item["paused"] is True
    assert item["paused_reason"] == "reboot"
    assert item["status"]["code"] == "paused"
