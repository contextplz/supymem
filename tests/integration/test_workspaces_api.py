"""Workspace + /me route contract (35 §9, 46 T10.8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/wsapi.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def test_me_returns_person_and_desk(client: TestClient) -> None:
    body = client.get("/me").json()
    assert body["person"]["id"]
    assert body["desk_id"]
    kinds = {w["kind"] for w in body["workspaces"]}
    assert "desk" in kinds


def test_me_is_idempotent(client: TestClient) -> None:
    """Calling it twice must not mint a second Desk."""
    first = client.get("/me").json()
    second = client.get("/me").json()
    assert first["desk_id"] == second["desk_id"]
    desks = [w for w in second["workspaces"] if w["kind"] == "desk"]
    assert len(desks) == 1


def test_create_and_fetch_workspace(client: TestClient) -> None:
    created = client.post(
        "/workspaces", json={"kind": "company", "name": "Acme", "entry_mode": "existing_company"}
    )
    assert created.status_code == 201, created.text
    ws = created.json()
    assert ws["id"].startswith("wsp_")
    assert ws["kind"] == "company"
    assert ws["observe_level"] == "O0"
    assert ws["tenancy"] == "local"

    fetched = client.get(f"/workspaces/{ws['id']}").json()
    assert fetched["name"] == "Acme"
    assert fetched["members"][0]["role"] == "owner"


def test_list_is_grouped_by_kind(client: TestClient) -> None:
    client.get("/me")
    client.post("/workspaces", json={"kind": "venture", "name": "Synths"})
    body = client.get("/workspaces").json()
    assert {"desk", "venture", "company"} <= set(body["grouped"])
    assert any(w["name"] == "Synths" for w in body["grouped"]["venture"])


def test_second_desk_is_refused_with_a_readable_error(client: TestClient) -> None:
    client.get("/me")
    r = client.post("/workspaces", json={"kind": "desk"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "desk_exists"


def test_unknown_workspace_is_404(client: TestClient) -> None:
    r = client.get("/workspaces/wsp_nope")
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "workspace_not_found"


def test_rename(client: TestClient) -> None:
    ws = client.post("/workspaces", json={"kind": "venture", "name": "Old"}).json()
    renamed = client.patch(f"/workspaces/{ws['id']}", json={"name": "New"}).json()
    assert renamed["name"] == "New"


def test_autonomy_change_is_validated_and_audited(client: TestClient) -> None:
    ws = client.post("/workspaces", json={"kind": "venture", "name": "Dial"}).json()

    bad = client.post(f"/workspaces/{ws['id']}/autonomy", json={"level": "L9"})
    assert bad.status_code == 422

    good = client.post(f"/workspaces/{ws['id']}/autonomy", json={"level": "L3"})
    assert good.status_code == 200
    assert good.json()["autonomy_level"] == "L3"

    from app.db.models import EventLog
    from app.state import db
    from sqlalchemy import select

    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(EventLog).where(EventLog.topic == "governance.autonomy.changed")
        ).all()
        assert rows, "autonomy change was not audited"
        row = rows[-1]
        assert row.workspace_id == ws["id"]
        assert row.actor_id
        assert row.actor_kind in {"person", "system"}
        assert row.surface == "api"
        assert '"from": "L1"' in row.payload_json
        assert '"to": "L3"' in row.payload_json


def test_observe_level_change_reports_cancelled_work(client: TestClient) -> None:
    ws = client.post(
        "/workspaces", json={"kind": "company", "entry_mode": "existing_company"}
    ).json()
    up = client.post(f"/workspaces/{ws['id']}/observe-level", json={"level": "O4"})
    assert up.status_code == 200
    assert up.json()["observe_level"] == "O4"
    assert up.json()["cancelled_work"] == []

    down = client.post(f"/workspaces/{ws['id']}/observe-level", json={"level": "O1"})
    assert down.status_code == 200
    assert down.json()["observe_level"] == "O1"
    assert "cancelled_work" in down.json()


def test_venture_creation_makes_a_workspace(client: TestClient) -> None:
    created = client.post(
        "/ventures",
        json={"name": "Held", "raw_idea": "a marketplace for restoring vintage synths"},
    )
    assert created.status_code == 201, created.text
    ws_id = created.json()["workspace_id"]
    assert ws_id and ws_id.startswith("wsp_")
    assert client.get(f"/workspaces/{ws_id}").status_code == 200
