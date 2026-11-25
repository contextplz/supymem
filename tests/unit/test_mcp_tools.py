"""MCP tools call the same services as REST (41 §3)."""

from __future__ import annotations

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mcp.db")
    monkeypatch.setenv("CONNECTORS_MODE", "local")
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    return main


@pytest.mark.asyncio
async def test_tool_catalog_and_list_workspaces(app_env) -> None:
    from app.mcp import tools
    from app.workspaces.service import create_workspace

    names = {t["name"] for t in tools.TOOL_SPECS}
    assert {
        "list_workspaces",
        "get_status",
        "request_work",
        "get_engagement",
        "answer_question",
        "approve",
        "list_opportunities",
        "run_workflow",
        "get_activity",
        "request_secret",
    } <= names

    create_workspace(kind="venture", name="MCPCo")
    result = await tools.call_tool("list_workspaces", {})
    assert any(w["name"] == "MCPCo" for w in result["items"])


@pytest.mark.asyncio
async def test_run_workflow_tool_shadow(app_env) -> None:
    from app.mcp import tools
    from app.workflows import service as wf
    from app.workspaces.service import create_workspace

    ws = create_workspace(kind="company", name="Flow", entry_mode="existing_company")
    flow = await wf.create_workflow(
        workspace_id=ws.id,
        name="Nightly",
        shadow_mode=True,
        steps=[
            {
                "n": 1,
                "label": "Rule",
                "action": {"kind": "rule", "expr": "true"},
            },
            {
                "n": 2,
                "label": "Write",
                "action": {
                    "kind": "connector_write",
                    "provider": "slack",
                    "op": "post_message",
                    "target": "#ops",
                },
            },
        ],
    )
    result = await tools.call_tool(
        "run_workflow", {"workflow_id": flow.id, "shadow_mode": True}
    )
    assert result["shadow"] is True
    assert result["simulated_writes"]
    assert result["simulated_writes"][0]["executed"] is False


@pytest.mark.asyncio
async def test_get_status_and_activity(app_env) -> None:
    from app.mcp import tools
    from app.workspaces.service import create_workspace

    ws = create_workspace(kind="venture", name="StatusCo")
    status = await tools.call_tool("get_status", {"workspace_id": ws.id})
    assert status["found"] is True
    assert status["name"] == "StatusCo"

    activity = await tools.call_tool(
        "get_activity", {"workspace_id": ws.id, "limit": 10}
    )
    assert "items" in activity
