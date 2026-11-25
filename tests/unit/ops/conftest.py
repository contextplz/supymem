"""Shared fixtures for ops unit tests."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def ops_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/ops_unit.db")
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("OPS_EXECUTOR", "mock")
    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.delenv("OPS_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    main = load_company_os_app()
    from app import state as state_mod
    from app.ops.services import activity as activity_svc
    from app.workspaces.resolve import LOCAL_PERSON_ID, ensure_desk

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    activity_svc.clear_subscribers()
    desk = ensure_desk(LOCAL_PERSON_ID)
    return {"main": main, "workspace_id": desk.id}
