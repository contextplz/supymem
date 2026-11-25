"""Stdio MCP wrapper — same tools and resources as core HTTP (41 §3)."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.mcp import resources as resource_mod
from app.mcp import tools as tool_mod


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    req_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if method in {"initialize", "notifications/initialized"}:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "companyos", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_mod.TOOL_SPECS}}
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
        result = await tool_mod.call_tool(name, arguments, surface="mcp")
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def serve() -> None:
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = await handle(payload)
        sys.stdout.write(json.dumps(reply, default=str) + "\n")
        sys.stdout.flush()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
