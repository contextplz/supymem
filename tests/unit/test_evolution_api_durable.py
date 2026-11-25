"""Evolution never invents product evidence and only returns durable history."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/evolution.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    with TestClient(main.create_app()) as running:
        yield running


def _venture(client: TestClient) -> str:
    response = client.post("/ventures", json={"raw_idea": "A measured product"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_evolution_does_not_run_from_synthetic_evidence(client: TestClient):
    venture_id = _venture(client)
    response = client.post(f"/ventures/{venture_id}/evolution/cycle", json={})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "evolution_not_ready"

    from app import state as state_mod
    state_mod.MEM[f"evolution:{venture_id}"] = [{"status": "fake"}]
    assert client.get(f"/ventures/{venture_id}/evolution").json() == {"cycles": []}


def test_evolution_lists_only_durable_cycle_receipts(client: TestClient):
    venture_id = _venture(client)
    from app import state as state_mod
    from app.db.models import EvolutionCycle

    now = datetime.now(timezone.utc)
    with state_mod.db()() as session:
        session.add(EvolutionCycle(
            id="ec_verified", venture_id=venture_id, trigger="founder_request",
            token_budget=1000, tokens_used=250, proposals_generated=1,
            proposals_promoted=0, status="completed", started_at=now,
            finished_at=now,
        ))
        session.commit()

    items = client.get(f"/ventures/{venture_id}/evolution").json()["cycles"]
    assert items == [{
        "id": "ec_verified",
        "trigger": "founder_request",
        "status": "completed",
        "tokens_used": 250,
        "proposals_generated": 1,
        "proposals_promoted": 0,
        "started_at": now.replace(tzinfo=None).isoformat(),
        "finished_at": now.replace(tzinfo=None).isoformat(),
    }]
