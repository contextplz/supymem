"""Lightweight MCP-style HTTP surface for local testing (41 §3).

Full MCP SDK/SSE is optional; these routes call the same tool handlers a stdio
server would, so REST and MCP cannot drift.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.audit.writer import audit, use_actor, AuditActor
from app.mcp import resources as resource_mod
from app.mcp import tools as tool_mod
from app.workspaces.resolve import LOCAL_PERSON_ID

router = APIRouter(prefix="/mcp", tags=["mcp"])


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    actor_id: str | None = None


@router.get("/tools")
def list_tools():
    return {"tools": tool_mod.TOOL_SPECS}


@router.get("/resources")
def list_resources():
    return {"resources": resource_mod.list_resources()}


@router.get("/resources/read")
def read_resource(uri: str):
    return resource_mod.read_resource(uri)


@router.post("/tools/{name}")
async def invoke_tool(name: str, body: ToolCall | None = None):
    body = body or ToolCall(name=name)
    actor_id = body.actor_id or LOCAL_PERSON_ID
    actor = AuditActor(
        actor_id="mcp:local",
        actor_kind="token",
        on_behalf_of=actor_id,
        surface="mcp",
    )
    with use_actor(actor):
        result = await tool_mod.call_tool(
            name, body.arguments, actor_id=actor_id, surface="mcp"
        )
    audit(
        "mcp.tool",
        workspace_id=body.arguments.get("workspace_id"),
        actor_id=actor.actor_id,
        actor_kind="token",
        on_behalf_of=actor_id,
        surface="mcp",
        target_ref=name,
        payload={"tool": name},
    )
    return result


@router.post("/jsonrpc")
async def jsonrpc(payload: dict[str, Any]):
    """Minimal JSON-RPC 2.0 shim over the same tools (stdio-compatible shape)."""
    req_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tool_mod.TOOL_SPECS},
        }
    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": resource_mod.list_resources()},
        }
    if method == "resources/read":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": resource_mod.read_resource(str(params.get("uri") or "")),
        }
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = dict(params.get("arguments") or {})
        actor = AuditActor(
            actor_id="mcp:local",
            actor_kind="token",
            on_behalf_of=LOCAL_PERSON_ID,
            surface="mcp",
        )
        with use_actor(actor):
            result = await tool_mod.call_tool(
                name, arguments, actor_id=LOCAL_PERSON_ID, surface="mcp"
            )
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }
