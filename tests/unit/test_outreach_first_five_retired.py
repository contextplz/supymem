"""OT-02: POST /outreach/first-five is retired (410); collectors are not invoked."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot02.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import ensure_desk

    state_mod.reset_db_factory()
    with TestClient(main.create_app(), headers={"X-Workspace-Id": ensure_desk().id}) as c:
        yield c


def test_first_five_returns_410_and_does_not_sweep(client, monkeypatch):
    called: list[str] = []

    async def _sweep(*_a, **_k):
        called.append("sweep")
        return {"ok": True}

    def _prep(*_a, **_k):
        called.append("prep")
        return []

    monkeypatch.setattr("app.gtm.relationships.sweep.run_signal_sweep", _sweep)
    monkeypatch.setattr(
        "app.gtm.relationships.outreach.prepare_first_five", _prep
    )
    r = client.post("/api/v1/outreach/first-five", json={})
    assert r.status_code == 410
    body = r.json()
    assert body["blocked"] == "first_five_retired"
    assert body["use"] == "/outreach/ingest"
    assert called == []
