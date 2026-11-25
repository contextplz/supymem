from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_managed_tenancy_requires_account_subject():
    client = TestClient(create_app())
    # Create a workspace first
    r = client.post("/workspaces", json={"kind": "venture", "name": "T", "entry_mode": "idea"})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]

    bad = client.post(
        f"/workspaces/{wid}/tenancy",
        json={"tenancy": "managed", "account_subject": "founder"},
    )
    assert bad.status_code == 403

    ok = client.post(
        f"/workspaces/{wid}/tenancy",
        json={"tenancy": "managed", "account_subject": "usr_deadbeef"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["tenancy"] == "managed"

    back = client.post(
        f"/workspaces/{wid}/tenancy",
        json={"tenancy": "local"},
    )
    assert back.status_code == 200
    assert back.json()["tenancy"] == "local"
