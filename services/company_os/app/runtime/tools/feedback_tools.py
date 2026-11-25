"""Tool handlers for feedback_tools."""

async def handle(args: dict) -> dict:
    return {"ok": True, "tool": "feedback_tools", "args": args}
