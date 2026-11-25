"""Connections + discover with fake GitHub (39 §8, 38 §9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/con.db")
    monkeypatch.setenv("CONNECTORS_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod
    from app.connectors.fakes import reset_fakes
    from app.connectors.registry import reset_registry
    from app.connectors.secrets import reset_store

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_fakes()
    reset_registry()
    reset_store()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def workspace(client: TestClient) -> str:
    return client.post(
        "/workspaces",
        json={
            "kind": "company",
            "name": "BringCo",
            "entry_mode": "existing_company",
        },
    ).json()["id"]


def test_create_list_revoke_github_fake(client: TestClient, workspace: str) -> None:
    before = client.get(f"/workspaces/{workspace}").json()
    assert before["observe_level"] == "O0"

    created = client.post(
        f"/workspaces/{workspace}/connections",
        json={"provider": "github", "auth_kind": "pat"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"].startswith("con_")
    assert body["provider"] == "github"
    assert body["status"] == "active"
    assert body["simulated"] is True
    assert body["external_account"] == "acme"

    after = client.get(f"/workspaces/{workspace}").json()
    assert after["observe_level"] == "O1"

    listed = client.get(f"/workspaces/{workspace}/connections").json()
    assert len(listed["items"]) == 1

    revoked = client.post(f"/connections/{body['id']}/revoke").json()
    assert revoked["status"] == "revoked"

    listed2 = client.get(f"/workspaces/{workspace}/connections").json()
    assert listed2["items"] == []


def test_catalog_lists_github_and_linear(client: TestClient) -> None:
    reply = client.get("/connectors")
    assert reply.status_code == 200, reply.text
    body = reply.json()
    names = {item["provider"] for item in body["items"]}
    assert names == {"github", "linear"}
    blob = str(body)
    assert "ghp_" not in blob
    assert "lin_api_" not in blob


def test_list_and_receipt_omit_fixture_token(client: TestClient, workspace: str) -> None:
    token = "ghp_fixturesimulatedtoken"
    created = client.post(
        f"/workspaces/{workspace}/connections",
        json={"provider": "github", "auth_kind": "pat", "token": token},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    listed = client.get(f"/workspaces/{workspace}/connections")
    receipt = client.get(f"/connections/{cid}/events")
    blob = listed.text + receipt.text + created.text
    assert token not in blob
    assert "secret_ref" not in listed.text
    assert "secret_value" not in blob


def test_discover_writes_company_map(client: TestClient, workspace: str) -> None:
    client.post(
        f"/workspaces/{workspace}/connections",
        json={"provider": "github"},
    )
    missing = client.get(f"/workspaces/{workspace}/map")
    assert missing.status_code == 404

    discovered = client.post(
        f"/workspaces/{workspace}/discover",
        json={"bump_observe_to": "O2"},
    )
    assert discovered.status_code == 200, discovered.text
    body = discovered.json()
    assert body["id"].startswith("cmp_")
    assert body["version"] == 1
    assert "product_surfaces" in body["content"]
    assert body["house_style"]["test_framework"] == "pytest"
    assert body["observe_level"] == "O2"

    mapped = client.get(f"/workspaces/{workspace}/map").json()
    assert mapped["version"] == 1
    # Inventory must not leak the fixture token.
    blob = str(mapped["content"])
    assert "ghp_fixturesimulatedtoken" not in blob
