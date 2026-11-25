"""OT-09: GET /ai-gtm/status honors X-Workspace-Id."""

from __future__ import annotations

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def test_status_header_b_does_not_return_a_funnel(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot09.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.gtm.supymem.pipeline import ingest_leads
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk_a = ensure_desk(LOCAL_PERSON_ID)
    ws_b = create_workspace(
        kind="company",
        name="B",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
    )
    ingest_leads(
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
    with TestClient(main.create_app()) as client:
        a = client.get(
            "/api/v1/ai-gtm/status",
            headers={"X-Workspace-Id": desk_a.id},
        )
        b = client.get(
            "/api/v1/ai-gtm/status",
            headers={"X-Workspace-Id": ws_b.id},
        )
    assert a.status_code == 200
    assert b.status_code == 200
    a_funnel = a.json()["funnel"]
    b_funnel = b.json()["funnel"]
    assert a_funnel != b_funnel
    assert int(a_funnel.get("email_ready") or 0) >= 1
    assert int(b_funnel.get("email_ready") or 0) == 0
