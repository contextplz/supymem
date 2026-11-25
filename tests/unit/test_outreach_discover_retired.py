"""OT-01: POST /outreach/discover is retired (410); discover_leads is not called."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot01.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import ensure_desk

    state_mod.reset_db_factory()
    with TestClient(main.create_app(), headers={"X-Workspace-Id": ensure_desk().id}) as c:
        yield c


def test_discover_returns_410_and_does_not_call_discover_leads(client, monkeypatch):
    called: list[int] = []

    def _boom(*_a, **_k):
        called.append(1)
        raise AssertionError("discover_leads must not run")

    monkeypatch.setattr("app.gtm.relationships.discover.discover_leads", _boom)
    r = client.post("/api/v1/outreach/discover", json={"limit": 3})
    assert r.status_code == 410
    body = r.json()
    assert body["blocked"] == "web_icp_discover_retired"
    assert body["use"] == "/outreach/ingest"
    assert called == []
