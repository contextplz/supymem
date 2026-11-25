"""Rewrite the current or next task instead of inventing a second queue."""

from __future__ import annotations

import re

from app.chat.context import ChatContext
from app.planning.store import retarget_front_task

_TO = re.compile(
    r"\b(?:to|instead:?)\s+(.+)$",
    re.I | re.S,
)


def parse_new_title(text: str) -> str | None:
    raw = (text or "").strip()
    hit = _TO.search(raw)
    if not hit:
        return None
    title = hit.group(1).strip(" .")
    title = re.sub(r"^(do|work on|build|write|make)\s+", "", title, flags=re.I)
    return title[:120] or None


def handle(ctx: ChatContext, text: str) -> dict:
    title = parse_new_title(text)
    if not title:
        return {
            "kind": "text",
            "body_md": "Tell me what to switch to, for example: “change the current task to rewrite the landing headline.”",
            "actions": [],
            "citations": [],
        }
    task = retarget_front_task(ctx.venture["id"], title, detail=text)
    owner = f" ({task['owner_name']})" if task.get("owner_name") else ""
    why = task.get("why_now") or ""
    if "retargeted in-flight" in why:
        verb = "stopped the in-flight work and queued"
    elif (task.get("status") or "open") == "running":
        verb = "updated the task in flight"
    else:
        verb = "rewrote the next item"
    return {
        "kind": "text",
        "body_md": f"I {verb}: **{task['title']}**{owner}.",
        "actions": [
            {
                "name": "task.run",
                "action": "task.run",
                "label": "Run now",
                "task_id": task["id"],
            }
        ],
        "citations": [],
    }
