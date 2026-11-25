"""Tool handlers for build_tools."""

async def handle(args: dict) -> dict:
    return {"ok": True, "tool": "build_tools", "args": args}
