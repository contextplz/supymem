"""Workflow API: create, run (shadow), pause, list runs (40 §8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/wf.db")
    monkeypatch.setenv("CONNECTORS_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def workspace(client: TestClient) -> str:
    return client.post(
        "/workspaces",
        json={"kind": "company", "name": "OpsCo", "entry_mode": "existing_company"},
    ).json()["id"]


def _steps():
    return [
        {
            "n": 1,
            "label": "Check threshold",
            "action": {"kind": "rule", "expr": "delta > 500", "vars": {"delta": 1200}},
        },
        {
            "n": 2,
            "label": "Analyse",
            "action": {"kind": "agent", "capability": "data_analysis", "prompt_ref": "x"},
        },
        {
            "n": 3,
            "label": "Post summary",
            "action": {
                "kind": "connector_write",
                "provider": "slack",
                "op": "post_message",
                "target": "#finance",
                "args": {"text": "3 payouts unreconciled"},
            },
        },
    ]


def test_create_list_and_get(client: TestClient, workspace: str) -> None:
    r = client.post(
        f"/workspaces/{workspace}/workflows",
        json={
            "name": "Weekly reconcile",
            "description": "Pull payouts and flag mismatches",
            "shadow_mode": True,
            "steps": _steps(),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("wfl_")
    assert body["status"] == "shadow"
    assert body["shadow_mode"] is True

    listed = client.get(f"/workspaces/{workspace}/workflows").json()
    assert any(w["id"] == body["id"] for w in listed["items"])

    got = client.get(f"/workflows/{body['id']}").json()
    assert got["name"] == "Weekly reconcile"
    assert len(got["steps"]) == 3


def test_shadow_run_logs_writes_without_executing(client: TestClient, workspace: str) -> None:
    created = client.post(
        f"/workspaces/{workspace}/workflows",
        json={"name": "Shadow me", "shadow_mode": True, "steps": _steps()},
    ).json()

    run = client.post(f"/workflows/{created['id']}/run", json={})
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["shadow"] is True
    assert body["status"] == "succeeded"
    assert len(body["simulated_writes"]) == 1
    assert body["simulated_writes"][0]["executed"] is False
    assert body["simulated_writes"][0]["provider"] == "slack"

    history = client.get(f"/workflows/{created['id']}/runs").json()
    assert len(history["items"]) == 1


def test_pause_and_resume(client: TestClient, workspace: str) -> None:
    created = client.post(
        f"/workspaces/{workspace}/workflows",
        json={"name": "Pausable", "steps": _steps()},
    ).json()
    paused = client.post(
        f"/workflows/{created['id']}/pause", json={"reason": "cost spike"}
    ).json()
    assert paused["status"] == "paused"
    assert paused["paused_reason"] == "cost spike"

    blocked = client.post(f"/workflows/{created['id']}/run", json={})
    assert blocked.status_code == 409

    resumed = client.post(f"/workflows/{created['id']}/resume").json()
    assert resumed["status"] == "shadow"
