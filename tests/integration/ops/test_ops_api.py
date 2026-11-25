"""Polsia-compatible /api/v1 ops routes (doc 49)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_api.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_config_roundtrip(client: TestClient) -> None:
    missing = client.get("/api/v1/config")
    assert missing.status_code == 404
    put = client.put("/api/v1/config", json={"name": "Acme", "kpis": {"mrr_usd": 1200}})
    assert put.status_code == 200, put.text
    assert put.json()["name"] == "Acme"
    got = client.get("/api/v1/config")
    assert got.status_code == 200
    assert got.json()["kpis"]["mrr_usd"] == 1200


def test_trigger_agent(client: TestClient) -> None:
    client.put("/api/v1/config", json={"name": "Acme"})
    r = client.post(
        "/api/v1/agents/social_media/trigger",
        json={"task_title": "Post about new feature"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["task_id"]
    assert body["task_id"].startswith("opt_")
    # Polsia contract is "queued"; sync executor may also surface final status.
    assert body["status"] in {"queued", "completed", "failed"}

    task = client.get(f"/api/v1/tasks/{body['task_id']}")
    assert task.status_code == 200
    assert task.json()["status"] in {"completed", "failed", "in_progress", "pending"}

    activity = client.get("/api/v1/dashboard/activity").json()
    assert len(activity) >= 1


def test_dashboard_summary(client: TestClient) -> None:
    client.put("/api/v1/config", json={"name": "Acme", "kpis": {"mrr_usd": 5000}})
    r = client.get("/api/v1/dashboard/summary")
    assert r.status_code == 200
    data = r.json()
    assert "tasks_today_total" in data
    assert data["kpis"]["mrr_usd"] == 5000


def test_social_posts_list(client: TestClient) -> None:
    r = client.get("/api/v1/social/posts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_invalid_agent_rejected(client: TestClient) -> None:
    r = client.post("/api/v1/agents/not_real/trigger")
    assert r.status_code == 422


def test_agents_status(client: TestClient) -> None:
    r = client.get("/api/v1/agents/status")
    assert r.status_code == 200
    data = r.json()
    assert data == []


def test_api_key_required_when_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_api_key.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv("OPS_API_KEY", "secret")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    with TestClient(main.create_app()) as c:
        assert c.get("/api/v1/health").status_code == 200
        denied = c.get("/api/v1/config")
        assert denied.status_code in {401, 403}
        put = c.put(
            "/api/v1/config",
            json={"name": "Keyed"},
            headers={"X-API-Key": "secret"},
        )
        assert put.status_code == 200, put.text
        ok = c.get("/api/v1/config", headers={"X-API-Key": "secret"})
        assert ok.status_code == 200


def test_operations_read_filters_reject_invalid_or_foreign_scope(client):
    for path in ('tasks', 'dashboard/summary', 'tasks/missing', 'agents/status'):
        response = client.get(f'/api/v1/{path}?venture_id=missing')
        assert response.status_code == 404
    for query in ('limit=0', 'limit=201', 'status=invented', 'venture_id='):
        assert client.get(f'/api/v1/tasks?{query}').status_code == 422
