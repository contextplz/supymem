"""Apply steer ops immediately, then start explicitly requested work."""

from __future__ import annotations

import asyncio
import re

from app.chat.context import ChatContext
from app.org.characters import enrich_org, match_mention, owner_for_work
from app.org.summary import resolve_org
from app.planning.store import enqueue_task
from app.ventures.lifecycle import is_paused

_LEAD_IN = re.compile(
    r"^\s*(hey\s+|guys\s+|please\s+|can you\s+|could you\s+|would you\s+|go ahead and\s+)+",
    re.I,
)


def _title_for(text: str) -> str:
    title = _LEAD_IN.sub("", (text or "").strip())
    title = title.rstrip("?.!").strip()
    if len(title) > 80:
        title = title[:77] + "…"
    return title or "New work"


async def handle(ctx: ChatContext, text: str) -> dict:
    from app.chat.intent import looks_like_instruction, looks_like_question
    from app.chat.steer import apply_steer, parse_steer

    if looks_like_question(text) and not looks_like_instruction(text):
        from app.chat.handlers.status import handle as status_handle

        return status_handle(ctx, text)

    from app.munder.control import control_for
    from app.runtime.live_turns import is_live, steer_live

    vid = ctx.venture["id"]
    absorbed = False
    if looks_like_instruction(text):
        control_for(vid).steer(vid, text)
        absorbed = steer_live(vid, text)

    steered = await apply_steer(vid, text)
    leftover = str(steered.get("leftover") or "").strip()
    leftover_is_question = bool(
        leftover and looks_like_question(leftover) and not looks_like_instruction(leftover)
    )
    if steered.get("handled"):
        body = "\n".join(steered.get("lines") or []) or "Done."
        actions = []
        if steered.get("site_changed"):
            from app.chat.site_actions import live_page_note

            # The copy draft is not the deployed page: never let "updated" read as "the live site changed".
            if (note := live_page_note(vid, text)) is not None:
                body += "\n\n" + note[0]
                actions.extend(note[1])
            href = next(
                (c.get("href") for c in (steered.get("citations") or []) if c.get("href")),
                f"/v/{ctx.venture['id']}/site",
            )
            actions.append({"label": "Open site", "href": href, "style": "primary"})
        # Applied ops already mutated the company. Never enqueue leftover here:
        # residual question clauses must not become tasks, and residual
        # instruction fragments are usually the same work already applied.
        return {
            "kind": "text",
            "body_md": body,
            "actions": actions,
            "citations": steered.get("citations") or [],
        }

    if leftover_is_question:
        from app.chat.handlers.status import handle as status_handle

        return status_handle(ctx, leftover or text)

    if absorbed and is_live(vid):
        return {
            "kind": "text",
            "body_md": (
                "Got it, the run already in flight will take that into account. "
                "I did not queue a second task."
            ),
            "actions": [],
            "citations": [],
        }

    title = _title_for(text)
    org = enrich_org(resolve_org(ctx.venture["id"]))
    agents = list(org.get("agents") or [])
    owner = match_mention(text, agents) or owner_for_work(title, ["chat"], agents)
    tags = ["chat"]
    if parse_steer(text):
        tags.append("steer")
    task = enqueue_task(
        ctx.venture["id"],
        {
            "title": title[:120],
            "detail": text,
            "why_now": f"You asked for this: “{text[:80]}”",
            "tags": tags,
            "est_minutes": 30,
            "runnable": True,
            "owner_name": (owner or {}).get("name"),
            "owner_role": (owner or {}).get("title") or (owner or {}).get("role"),
            "owner_id": (owner or {}).get("id"),
        },
        source="chat",
    )
    who = task.get("owner_name") or (owner or {}).get("name") or "the next free teammate"
    paused = False
    try:
        paused = is_paused(ctx.venture["id"])
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, vid, "change.pause_status")
        paused = None
    if paused is None:
        follow = "I could not check whether the company is paused. Open Now to review it."
    elif paused:
        follow = "The company is paused, so nothing runs until you resume."
    elif looks_like_instruction(text):
        from app.planning.execute import TaskRunError, start_planned_task

        try:
            started = await asyncio.to_thread(start_planned_task, vid, task["id"])
        except Exception as exc:
            from app.chat.recovery import log_founder_exception

            log_founder_exception(exc, vid, "change.start")
            from fastapi import HTTPException
            from app.core.local_mode import is_local

            if is_local() and isinstance(exc, HTTPException) and isinstance(exc.detail, dict) \
                    and exc.detail.get("code") == "build_tool_needs_setup":
                from app.chat.execute import local_capability_reply

                if setup := await asyncio.to_thread(local_capability_reply, "build_with_my_tool"):
                    return setup
            follow = (f"{exc.message} " if isinstance(exc, TaskRunError)
                      else "Your request was saved, but I could not start it. ")
            follow += "Open Tasks to review what it needs, then retry."
        else:
            state = "complete" if started["status"] == "done" else "starting"
            return {
                "kind": "next_up",
                "body_md": f"Your request is {state} with {who}. You can check its progress in Tasks.",
                "actions": [{"label": "Check progress", "href": f"/v/{vid}/tasks"}],
                "citations": [],
            }
    else:
        follow = "Your request is queued, not started. Use Run now to start it, or ask me to check its status."
    return {
        "kind": "next_up",
        "body_md": f"Queued **{task['title']}** for {who}. {follow}",
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
