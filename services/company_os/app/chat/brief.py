"""Grounded company briefing — who is working, blockers, next, how work runs."""

from __future__ import annotations

from typing import Any

from app.org.characters import enrich_org
from app.org.summary import resolve_org
from app.planning.store import list_current_tasks
from app.ventures.lifecycle import is_paused


def company_snapshot(venture_id: str, *, workspace_id: str | None = None,
                     name: str = "", stage: str = "", autonomy: str = "L1") -> dict[str, Any]:
    org = enrich_org(resolve_org(venture_id))
    agents = {str(a.get("id") or ""): a for a in (org.get("agents") or []) if isinstance(a, dict)}
    live: list[dict[str, Any]] = []
    live_status_unconfirmed = False
    try:
        from app.org.heartbeat import list_live_status

        live = list_live_status(venture_id)
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "brief.live_status")
        live = []
        live_status_unconfirmed = True

    working: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in live:
        state = str(row.get("state") or "")
        aid = str(row.get("agent_id") or "")
        person = agents.get(aid) or {}
        item = {
            "name": person.get("name") or aid or "Teammate",
            "role": person.get("title") or person.get("role") or "",
            "desc": row.get("current_task_desc") or "",
            "reason": row.get("blocked_reason") or "",
        }
        if state == "working":
            working.append(item)
        elif state == "blocked":
            blocked.append(item)

    running = list_current_tasks(venture_id, statuses=("running",))
    open_tasks = list_current_tasks(venture_id, statuses=("open",))
    paused = False
    try:
        paused = is_paused(venture_id)
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "brief.pause_status")
        paused = None

    workflow = None
    if workspace_id:
        try:
            from app.ventures.run_status import snapshot

            workflow = snapshot(venture_id, workspace_id, paused=paused)
        except Exception as exc:  # noqa: BLE001
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "brief.workflow_status")
            workflow = {"state": "unknown", "activity": "Live workflow status is unavailable."}

    return {
        "name": name,
        "stage": stage,
        "autonomy": autonomy or "L1",
        "paused": paused,
        "working": working,
        "live_status_unconfirmed": live_status_unconfirmed,
        "blocked": blocked,
        "running": running,
        "open": open_tasks,
        "hired": len(agents),
        "workflow": workflow,
    }


def render_brief(snap: dict[str, Any]) -> str:
    name = snap.get("name") or "This venture"
    stage = snap.get("stage") or "unknown"
    autonomy = snap.get("autonomy") or "L1"
    paused = bool(snap.get("paused"))
    mode = "pause status unconfirmed" if snap.get("paused") is None else "paused" if paused else "autopilot on"
    workflow = snap.get("workflow") or {}
    if workflow.get("state") in {"queued", "working", "needs_you", "failed", "cancelled"}:
        stage = workflow.get("stage") or stage
    lines = [f"**{name}**: `{stage}` · {mode} · {autonomy}", ""]

    if workflow.get("state") in {"queued", "working", "needs_you", "failed", "cancelled", "unknown"}:
        lines += ["**Now**", f"- {workflow.get('stage') or 'Workflow'}: {workflow.get('activity') or 'Status unavailable.'}", ""]
        action = workflow.get("action") or {}
        if workflow.get("state") == "needs_you" and action:
            lines += ["**Needs you**", f"- {action.get('label') or 'Review the saved action'}", ""]
        lines += ["**Next**", f"- {workflow.get('next_step') or 'Open Now to inspect the saved work.'}"]
        return "\n".join(lines)

    working = list(snap.get("working") or [])
    running = list(snap.get("running") or [])
    lines.append("**Being done now**")
    if working or running:
        seen: set[str] = set()
        for row in running:
            title = str(row.get("title") or "Working")
            owner = row.get("owner_name") or "unassigned"
            key = title.lower()
            seen.add(key)
            lines.append(f"- {owner}: {title}")
        for row in working:
            desc = str(row.get("desc") or "Working")
            if desc.lower() in seen:
                continue
            role = f" ({row['role']})" if row.get("role") else ""
            lines.append(f"- {row.get('name')}{role}: {desc}")
    elif snap.get("live_status_unconfirmed"):
        lines.append("- Live teammate activity could not be checked. Open Now to review the saved work.")
    else:
        lines.append("- Nobody is mid-task right now. Autopilot claims the next open item about every 20 seconds.")

    lines.append("")
    lines.append("**Blockers**")
    blocked = list(snap.get("blocked") or [])
    if paused:
        lines.append("- The company is paused. Nothing runs until you resume.")
    elif snap.get("paused") is None:
        lines.append("- Pause status is unconfirmed. Open Now to check it before resuming work.")
    elif blocked:
        for row in blocked[:4]:
            reason = row.get("reason") or "stuck"
            lines.append(f"- {row.get('name')}: {reason}")
    else:
        lines.append("- None. Nothing needs you. The company should keep running.")

    lines.append("")
    lines.append("**Next**")
    open_tasks = list(snap.get("open") or [])
    if open_tasks:
        for i, row in enumerate(open_tasks[:5], 1):
            owner = f" ({row['owner_name']})" if row.get("owner_name") else ""
            lines.append(f"{i}. {row.get('title')}{owner}")
    else:
        lines.append("- Nothing queued. Ask me to plan the next five, or give someone a job.")

    lines.append("")
    lines.append("**How work runs**")
    lines.append(
        "Autopilot picks the next open task. Coding work runs locally in your project; "
        "other work produces saved files and my update. I answer questions from your saved information; "
        "I do not turn a question into a task."
    )
    return "\n".join(lines)


def brief_reply(venture_id: str, *, workspace_id: str | None = None,
                name: str = "", stage: str = "", autonomy: str = "L1") -> dict[str, Any]:
    snap = company_snapshot(venture_id, workspace_id=workspace_id,
                            name=name, stage=stage, autonomy=autonomy)
    action = (snap.get("workflow") or {}).get("action") or {}
    actions = ([{"label": action["label"], "href": action["href"], "style": "primary"}]
               if action.get("label") and action.get("href") else [])
    actions.extend([
        {"label": "Now", "href": f"/v/{venture_id}/now", **({"style": "primary"} if not actions else {})},
        {"label": "Team", "href": f"/v/{venture_id}/org"},
        {"label": "Work", "href": f"/v/{venture_id}/tasks"},
    ])
    return {
        "kind": "text",
        "body_md": render_brief(snap),
        "citations": [],
        "actions": actions,
    }
