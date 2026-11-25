"""OT-06: unset ops API key fails closed for managed tenancy."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


def test_managed_tenancy_without_key_is_401(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ot06.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.db.models import Workspace
    from app.state import db
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    desk = ensure_desk(LOCAL_PERSON_ID)
    with db()() as session:
        row = session.get(Workspace, desk.id)
        assert row is not None
        row.tenancy = "managed"
        session.commit()

    with TestClient(main.create_app()) as client:
        r = client.get(
            "/api/v1/outreach/prospects",
            headers={"X-Workspace-Id": desk.id},
        )
        assert r.status_code == 401
