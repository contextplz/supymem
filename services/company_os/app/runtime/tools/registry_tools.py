"""Tool handlers for registry_tools."""

async def handle(args: dict) -> dict:
    return {"ok": True, "tool": "registry_tools", "args": args}
