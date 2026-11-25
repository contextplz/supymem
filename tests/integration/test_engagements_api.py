"""Integration tests for engagements API (36 §9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/eng_api.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("COMPANY_OS_ENGAGEMENTS_DIR", str(tmp_path / "engagements"))
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


@pytest.fixture()
def desk_id(client: TestClient) -> str:
    return client.get("/me").json()["desk_id"]


def test_create_errand_and_fetch(client: TestClient, desk_id: str) -> None:
    created = client.post(
        f"/workspaces/{desk_id}/engagements",
        json={"text": "what does this error mean: KeyError on line 12"},
    )
    assert created.status_code == 201, created.text
    eng = created.json()
    assert eng["id"].startswith("eng_")
    assert eng["size"] == "errand"
    assert eng["status"] in {"delivered", "working", "planned"}

    fetched = client.get(f"/engagements/{eng['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["request_verbatim"].startswith("what does this error")


def test_list_open_engagements(client: TestClient, desk_id: str) -> None:
    client.post(
        f"/workspaces/{desk_id}/engagements",
        json={"text": "bump the copyright year in the footer", "advance": False},
    )
    listed = client.get(f"/workspaces/{desk_id}/engagements?status=open")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) >= 1


def test_project_pauses_for_goahead(client: TestClient, desk_id: str) -> None:
    created = client.post(
        f"/workspaces/{desk_id}/engagements",
        json={"text": "migrate billing to Stripe and also rewrite the checkout"},
    )
    assert created.status_code == 201, created.text
    eng = created.json()
    assert eng["size"] == "project"
    assert eng["status"] == "awaiting_goahead"
    assert eng["cost_usd"] <= 0.02

    go = client.post(f"/engagements/{eng['id']}/goahead")
    assert go.status_code == 200, go.text
    assert go.json()["status"] in {"delivered", "working", "in_review", "planned", "blocked"}


def test_abandon(client: TestClient, desk_id: str) -> None:
    created = client.post(
        f"/workspaces/{desk_id}/engagements",
        json={
            "text": "migrate billing to Stripe across every page",
            "advance": False,
        },
    )
    eng = created.json()
    abandoned = client.post(
        f"/engagements/{eng['id']}/abandon",
        json={"reason": "changed my mind"},
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "abandoned"


def test_unknown_engagement_404(client: TestClient) -> None:
    r = client.get("/engagements/eng_nope")
    assert r.status_code == 404
