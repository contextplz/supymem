"""OT-03: signal-sweep is 403; ArmBody exists so the ops router loads."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def test_ops_api_router_imports():
    load_company_os_app()
    from app.api.ops_api import ArmBody, router

    assert ArmBody is not None
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/v1/outreach/signal-sweep" in paths
    assert "/api/v1/outreach/arm" in paths


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot03.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import ensure_desk

    state_mod.reset_db_factory()
    with TestClient(main.create_app(), headers={"X-Workspace-Id": ensure_desk().id}) as c:
        yield c


def test_signal_sweep_is_403(client, monkeypatch):
    called: list[int] = []

    async def _sweep(*_a, **_k):
        called.append(1)
        return {}

    monkeypatch.setattr("app.gtm.relationships.sweep.run_signal_sweep", _sweep)
    r = client.post("/api/v1/outreach/signal-sweep", json={})
    assert r.status_code == 403
    assert r.json()["blocked"] == "layer_a_http_disabled"
    assert called == []
