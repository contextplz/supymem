"""OT-08: reply IDOR — tenant B cannot write tenant A's contact.

This request succeeds on main today: POST /api/v1/outreach/reply with
tenant A's contact_id and tenant B's X-Workspace-Id returned 200.
"""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot08.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.supymem.factory import funnel
    from app.gtm.supymem.pipeline import ingest_leads
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="TenantB",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    ingested = ingest_leads(
        desk_a.id,
        [
            {
                "first_name": "Ada",
                "email": "ada@sample-analytics.test",
                "email_confidence": "safe",
                "organization": "Analytical",
                "source": "test",
            }
        ],
        enrich=False,
    )
    contact_id = ingested["ingested"] and ingested["contacts"][0]["id"]
    return {
        "main": main,
        "desk_a": desk_a.id,
        "ws_b": ws_b.id,
        "contact_id": contact_id,
        "funnel_b_before": funnel(ws_b.id),
    }


def test_reply_with_foreign_contact_is_404(setup):
    from app.gtm.supymem.factory import funnel

    with TestClient(setup["main"].create_app()) as client:
        stolen = client.post(
            "/api/v1/outreach/reply",
            json={
                "contact_id": setup["contact_id"],
                "body": "thanks, let's talk",
                "classification": "human_positive",
            },
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert stolen.status_code == 404
        ok = client.post(
            "/api/v1/outreach/reply",
            json={
                "contact_id": setup["contact_id"],
                "body": "thanks, let's talk",
                "classification": "human_positive",
            },
            headers={"X-Workspace-Id": setup["desk_a"]},
        )
        assert ok.status_code == 200
        stolen_get = client.get(
            f"/api/v1/outreach/contacts/{setup['contact_id']}",
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert stolen_get.status_code == 404
        stolen_draft = client.post(
            "/api/v1/outreach/drafts",
            json={"contact_id": setup["contact_id"], "kind": "connect"},
            headers={"X-Workspace-Id": setup["ws_b"]},
        )
        assert stolen_draft.status_code == 404
    after = funnel(setup["ws_b"])
    assert after == setup["funnel_b_before"]
