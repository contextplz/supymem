"""Tool handlers for concierge_tools."""

async def handle(args: dict) -> dict:
    return {"ok": True, "tool": "concierge_tools", "args": args}
