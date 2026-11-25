"""The social agent tick — keep-alive, sense, decide, act, remember."""

from __future__ import annotations

import logging
import os
from typing import Any

from app.content.agent import act, decide, keepalive, memory, sense
from app.content.agent.policy import agent_enabled, content_blocked_message, policy_for
from app.core.local_mode import is_local
from app.content.engine import record_event, tick as content_publish_tick
from app.content.oauth import flow as oauth_flow
from app.content.platforms.base import dry_run_enabled

log = logging.getLogger(__name__)


async def run_agent_tick(*, workspace_id: str) -> dict[str, Any]:
    """One full agent cycle. Safe to call every ~2 minutes forever."""
    summary: dict[str, Any] = {
        "workspace_id": workspace_id,
        "agent": True,
        "enabled": agent_enabled(),
        "dry_run": dry_run_enabled(),
        "keepalive": {},
        "signals": 0,
        "actions": [],
        "results": [],
        "publish": None,
    }
    if not agent_enabled():
        summary["reason"] = "agent_disabled"
        if is_local():
            summary["next_step"] = content_blocked_message()
        return summary

    # 1) Stay connected — without this the agent is a corpse.
    try:
        summary["keepalive"] = keepalive.check_and_heal(workspace_id)
    except Exception as exc:
        log.exception("agent keepalive failed")
        summary["keepalive"] = {"error": str(exc)[:200]}
        record_event(
            workspace_id=workspace_id,
            kind="agent.keepalive_error",
            payload={"error": str(exc)[:300]},
        )

    healthy_platforms = {
        a["platform"]
        for a in (summary["keepalive"].get("accounts") or [])
        if a.get("healthy") and a.get("local_status") == "active"
    }
    # Also trust list_accounts if keepalive omitted native.
    for acc in oauth_flow.list_accounts(workspace_id):
        if acc.get("status") == "active":
            healthy_platforms.add(acc["platform"])

    if not healthy_platforms:
        summary["reason"] = "no_healthy_channels"
        if is_local():
            summary["next_step"] = "Open Content to draft with your model key, then copy the text or download Markdown and publish it yourself."
        record_event(
            workspace_id=workspace_id,
            kind="agent.blocked",
            payload={"reason": "no_healthy_channels", "reconnect": summary["keepalive"].get("needs_reconnect")},
        )
        return summary

    # 2) Broadcast path — keep the original content engine posting loop alive.
    if os.environ.get("CONTENT_ENGINE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    } or os.environ.get("CONTENT_AGENT_ALSO_PUBLISH", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            # Ensure publish tick can run even if CONTENT_ENGINE_ENABLED is off —
            # the agent owns the loop now.
            os.environ.setdefault("CONTENT_ENGINE_ENABLED", "true")
            summary["publish"] = await content_publish_tick(workspace_id=workspace_id)
        except Exception as exc:
            log.exception("agent publish tick failed")
            summary["publish"] = {"error": str(exc)[:200]}

    # 3) Sense the environment.
    try:
        signals = sense.sense_workspace(workspace_id)
    except Exception as exc:
        log.exception("agent sense failed")
        signals = []
        summary["sense_error"] = str(exc)[:200]

    fresh = []
    for sig in signals:
        ext = str(sig.get("external_id") or "")
        if not ext or memory.already_handled(workspace_id, ext):
            continue
        fresh.append(sig)
        memory.mark_seen(workspace_id, sig)
    summary["signals"] = len(fresh)

    # 4) Decide + act.
    try:
        actions = decide.decide(workspace_id=workspace_id, signals=fresh)
    except Exception as exc:
        log.exception("agent decide failed")
        actions = []
        summary["decide_error"] = str(exc)[:200]

    # Cap fan-out per tick so we never look like a spam bot.
    max_actions = int(os.environ.get("CONTENT_AGENT_MAX_ACTIONS", "3"))
    actions = actions[: max(0, max_actions)]
    summary["actions"] = actions

    try:
        results = act.execute_actions(workspace_id=workspace_id, actions=actions)
    except Exception as exc:
        log.exception("agent act failed")
        results = [{"ok": False, "error": str(exc)[:200]}]
    summary["results"] = results

    for action, result in zip(actions, results, strict=False):
        memory.mark_action(workspace_id, action, result)
        record_event(
            workspace_id=workspace_id,
            kind="agent.acted" if result.get("ok") else "agent.act_failed",
            platform=str(action.get("platform") or "") or None,
            post_id=action.get("parent_post_id") if isinstance(action.get("parent_post_id"), str) else None,
            payload={"action": action, "result": result},
        )

    record_event(
        workspace_id=workspace_id,
        kind="agent.tick",
        payload={
            "signals": summary["signals"],
            "actions": len(actions),
            "ok": sum(1 for r in results if r.get("ok")),
            "dry_run": summary["dry_run"],
            "needs_reconnect": summary["keepalive"].get("needs_reconnect") or [],
        },
    )
    return summary


def agent_status(workspace_id: str) -> dict[str, Any]:
    accounts = oauth_flow.list_accounts(workspace_id)
    policies = {
        p: {
            "can_post": policy_for(p).can_post,
            "can_reply": policy_for(p).can_reply,
            "can_listen_comments": policy_for(p).can_listen_comments,
            "can_listen_reactions": policy_for(p).can_listen_reactions,
            "can_listen_sector": policy_for(p).can_listen_sector,
            "reply_caveat": policy_for(p).reply_caveat,
        }
        for p in ("linkedin", "reddit", "x")
    }
    return {
        "enabled": agent_enabled(),
        "dry_run": dry_run_enabled(),
        "accounts": accounts,
        "policies": policies,
        "mode": "agent",
        "loop": "keepalive → sense → decide → act → remember",
    }
