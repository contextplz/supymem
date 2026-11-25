"""MCP resources: charter, map, house-style, deliverables."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app


def test_resource_catalog_and_empty_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mcp_r.db")
    load_company_os_app()
    from app.mcp import resources
    from app.workspaces.service import create_workspace

    names = {r["name"] for r in resources.list_resources()}
    assert names == {"charter", "map", "house-style", "deliverables"}

    ws = create_workspace(kind="venture", name="ResCo")
    body = resources.read_resource(f"companyos://workspace/{ws.id}/charter")
    assert body["body"]["charter"] is None
    unknown = resources.read_resource("companyos://nope")
    assert unknown["error"]["code"] == "unknown_resource"
