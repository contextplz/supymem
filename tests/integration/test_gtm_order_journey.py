"""The founder's GTM route survives reload without starting a campaign."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/gtm-order.db")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as api:
        yield api


def test_market_first_is_durable_and_does_not_queue_a_campaign(client: TestClient):
    created = client.post("/ventures", json={"name": "Market Choice", "raw_idea": "A useful team app"})
    assert created.status_code == 201, created.text
    venture_id = created.json()["id"]

    saved = client.put(f"/ventures/{venture_id}/gtm-order", json={"gtm_order": "market_first"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"venture_id": venture_id, "gtm_order": "market_first"}
    assert client.get(f"/ventures/{venture_id}").json()["gtm_order"] == "market_first"
    assert client.get(f"/ventures/{venture_id}/gtm").json()["ok"] is None
    other_account = client.put(
        f"/ventures/{venture_id}/gtm-order",
        json={"gtm_order": "product_first"},
        headers={"X-Subject": "another-founder"},
    )
    assert other_account.status_code == 404
    assert client.get(f"/ventures/{venture_id}").json()["gtm_order"] == "market_first"

    from app import state
    state.reset_db_factory()
    state.MEM.clear()
    assert client.get(f"/ventures/{venture_id}").json()["gtm_order"] == "market_first"


def test_route_change_only_accepts_known_choices(client: TestClient):
    created = client.post("/ventures", json={"name": "Route Choice", "raw_idea": "A useful team app"})
    venture_id = created.json()["id"]
    assert client.put(f"/ventures/{venture_id}/gtm-order", json={"gtm_order": "market_first"}).status_code == 200
    invalid = client.put(f"/ventures/{venture_id}/gtm-order", json={"gtm_order": "send_now"})
    assert invalid.status_code == 422
    assert client.get(f"/ventures/{venture_id}").json()["gtm_order"] == "market_first"
    assert client.put(f"/ventures/{venture_id}/gtm-order", json={"gtm_order": "product_first"}).status_code == 200
    assert client.get(f"/ventures/{venture_id}").json()["gtm_order"] == "product_first"
