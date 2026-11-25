"""Tool handlers for gtm_tools."""

async def handle(args: dict) -> dict:
    return {"ok": True, "tool": "gtm_tools", "args": args}
