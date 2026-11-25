"""Unit tests for CommunityDepartmentAdapter fail-closed behavior."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.community.adapter import CommunityDepartmentAdapter


@pytest.mark.asyncio
async def test_adapter_supports_community_kinds():
    adapter = CommunityDepartmentAdapter()
    assert adapter.supports("community.evaluate_thesis")
    assert adapter.supports("community.provision_discord")
    assert not adapter.supports("find_threads")


@pytest.mark.asyncio
async def test_adapter_evaluate_thesis_without_db_persist_via_service(monkeypatch):
    """Adapter calls service; monkeypatch persist path to avoid DB in unit test."""
    from app.community import service
    from app.community.thesis import sample_build_research

    captured: dict = {}

    def fake_run(workspace_id, payload, persist=True):
        captured["workspace_id"] = workspace_id
        captured["persist"] = persist
        from app.community.thesis import evaluate_thesis

        thesis = evaluate_thesis(payload)
        return {"ok": True, "thesis": thesis.to_dict()}

    monkeypatch.setattr(service, "run_thesis", fake_run)
    adapter = CommunityDepartmentAdapter()
    out = await adapter.run(
        {
            "kind": "community.evaluate_thesis",
            "workspace_id": "ws_unit",
            "payload": sample_build_research(),
        }
    )
    assert out["ok"] is True
    assert out["status"] == "done"
    assert captured["workspace_id"] == "ws_unit"
    assert out["result"]["thesis"]["verdict"] == "build_owned"


@pytest.mark.asyncio
async def test_adapter_provision_fail_closed(monkeypatch):
    from app.community import service

    def fake_provision(workspace_id, guild_id=None, plan_id=None):
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "NeedsYou: Discord bot not connected",
            "error": "not_connected",
        }

    monkeypatch.setattr(service, "provision_discord", fake_provision)
    adapter = CommunityDepartmentAdapter()
    out = await adapter.run(
        {
            "kind": "community.provision_discord",
            "workspace_id": "ws_unit",
            "payload": {},
        }
    )
    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert "NeedsYou" in (out.get("block_reason") or "")
