"""OT-32: hosted ingest without AUP is 403; local desk still pastes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot32.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_API_KEY", "ot32-ops-key")
    main = load_company_os_app()
    from app import state as state_mod
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk
    from app.workspaces.service import create_workspace

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    hosted = create_workspace(
        kind="company",
        name="HostedAUP",
        entry_mode="existing_company",
        owner_id=LOCAL_PERSON_ID,
        tenancy="managed",
    )
    return {"main": main, "desk": desk.id, "hosted": hosted.id}


def _ingest_payload() -> dict:
    return {
        "source": "profile_paste",
        "raw_text": "Sam Builder\nsam@sample-tools.test\nFounder\n",
        "enrich": False,
    }


def test_hosted_ingest_without_aup_is_403(setup):
    headers = {
        "X-Workspace-Id": setup["hosted"],
        "X-API-Key": "ot32-ops-key",
    }
    with TestClient(setup["main"].create_app()) as client:
        r = client.post("/api/v1/outreach/ingest", json=_ingest_payload(), headers=headers)
        assert r.status_code == 403
        assert r.json()["detail"] == "aup_required"
        acc = client.post("/api/v1/outreach/aup", json={"accepted": True}, headers=headers)
        assert acc.status_code == 200
        ok = client.post("/api/v1/outreach/ingest", json=_ingest_payload(), headers=headers)
        assert ok.status_code == 200


def test_local_desk_ingest_skips_aup(setup):
    headers = {"X-Workspace-Id": setup["desk"], "X-API-Key": "ot32-ops-key"}
    with TestClient(setup["main"].create_app()) as client:
        r = client.post("/api/v1/outreach/ingest", json=_ingest_payload(), headers=headers)
        assert r.status_code == 200
