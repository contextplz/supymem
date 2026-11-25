"""OT-46: pilots CRUD is workspace-scoped."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot46.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.relationships.ingest import ingest_paste
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    other = create_workspace(
        kind="company",
        name="TenantBPilots",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    ingested = ingest_paste(
        desk.id,
        source="profile_paste",
        raw_text='Sam\nsam@sample-tools.test\nFounder\n"we still run this workflow in a spreadsheet"',
        enrich=False,
    )
    return {"main": main, "wid": desk.id, "other": other.id, "cid": ingested["contact_id"]}


def test_pilot_crud(setup):
    headers = {"X-Workspace-Id": setup["wid"]}
    other = {"X-Workspace-Id": setup["other"]}
    with TestClient(setup["main"].create_app()) as client:
        created = client.post(
            "/api/v1/outreach/pilots",
            json={
                "contact_id": setup["cid"],
                "use_case": "on-call pages",
                "success_criterion": "cut pages by half in 14 days",
            },
            headers=headers,
        )
        assert created.status_code == 200, created.text
        pid = created.json()["id"]
        listed = client.get("/api/v1/outreach/pilots", headers=headers)
        assert any(p["id"] == pid for p in listed.json()["items"])
        leaked = client.get("/api/v1/outreach/pilots", headers=other)
        assert leaked.status_code == 200
        assert leaked.json()["items"] == []
        patched = client.patch(
            f"/api/v1/outreach/pilots/{pid}",
            json={"permission_to_demo": True},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["permission_to_demo_at"]
        foreign = client.patch(
            f"/api/v1/outreach/pilots/{pid}",
            json={"paid_decision": True},
            headers=other,
        )
        assert foreign.status_code == 404
