"""Park in-flight work the founder told us to stop."""

from __future__ import annotations

from app.chat.context import ChatContext
from app.planning.store import cancel_matching_work


def handle(ctx: ChatContext, text: str) -> dict:
    cancelled = cancel_matching_work(ctx.venture["id"], text)
    if not cancelled:
        return {
            "kind": "text",
            "body_md": "Nothing matching was open. Tell me the task title if you want a specific item parked.",
            "actions": [{"label": "Open cockpit", "href": f"/v/{ctx.venture['id']}"}],
            "citations": [],
        }
    lines = [f"Parked {len(cancelled)} item(s). Autopilot will not refill that slice."]
    for row in cancelled[:6]:
        lines.append(f"- **{row.get('title')}**")
    return {
        "kind": "text",
        "body_md": "\n".join(lines),
        "actions": [{"label": "Open cockpit", "href": f"/v/{ctx.venture['id']}", "style": "primary"}],
        "citations": [],
    }
