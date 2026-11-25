"""Execute agent actions through Composio (post / reply) and record outcomes."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.content import composio_social
from app.content.agent.policy import policy_for
from app.content.platforms.base import dry_run_enabled
from app.db.models import ContentAccount, ContentPost
from app.state import db

log = logging.getLogger(__name__)


def _account_meta(workspace_id: str, platform: str) -> tuple[str, str, str] | None:
    """composio_user_id, connected_account_id, author_urn."""
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ContentAccount).where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.platform == platform,
                ContentAccount.status == "active",
            )
        ).first()
        if row is None:
            return None
        try:
            extra = json.loads(row.extra_json or "{}")
        except Exception:
            return None
        if not isinstance(extra, dict) or extra.get("auth_via") != "composio":
            return None
        ca = str(extra.get("connected_account_id") or "")
        uid = str(extra.get("composio_user_id") or "")
        if not ca or not uid:
            return None
        return uid, ca, row.author_urn or ""


def execute_actions(
    *, workspace_id: str, actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        results.append(_execute_one(workspace_id=workspace_id, action=action))
    return results


def _execute_one(*, workspace_id: str, action: dict[str, Any]) -> dict[str, Any]:
    kind = str(action.get("kind") or "")
    platform = str(action.get("platform") or "")
    pol = policy_for(platform)

    if kind == "observe":
        return {"ok": True, "kind": kind, "platform": platform, "mode": "memory"}

    if kind == "reply":
        if not pol.can_reply:
            return {
                "ok": False,
                "kind": kind,
                "platform": platform,
                "error": pol.reply_caveat or "replies disabled",
            }
        return _reply(workspace_id=workspace_id, action=action)

    if kind == "post":
        if not pol.can_post:
            return {"ok": False, "kind": kind, "platform": platform, "error": "post disabled"}
        return _post(workspace_id=workspace_id, action=action)

    return {"ok": False, "kind": kind, "error": "unknown_action"}


def _reply(*, workspace_id: str, action: dict[str, Any]) -> dict[str, Any]:
    platform = str(action.get("platform") or "")
    target = str(action.get("target") or "")
    body = str(action.get("body") or "").strip()
    if not target or not body:
        return {"ok": False, "kind": "reply", "error": "missing target/body"}

    if dry_run_enabled():
        return {
            "ok": True,
            "kind": "reply",
            "platform": platform,
            "mode": "simulated",
            "target": target,
            "body": body,
        }
    from app.approvals.external import consume_external_approval

    allowed, reason = consume_external_approval(
        workspace_id=workspace_id,
        approval_id=str(action.get("approval_id") or "") or None,
        action="content_reply",
        target={"platform": platform, "target": target, "body": body},
    )
    if not allowed:
        return {"ok": False, "kind": "reply", "platform": platform, "error": reason}

    meta = _account_meta(workspace_id, platform)
    if meta is None:
        return {"ok": False, "kind": "reply", "error": "no_active_account"}
    uid, ca, _author = meta

    try:
        if platform == "linkedin":
            from app.content import linkedin_actions

            out = linkedin_actions.execute(
                workspace_id=workspace_id,
                action="comment_on_post",
                body=body,
                target_urn=target,
            )
            return {
                "ok": bool(out.get("ok")),
                "kind": "reply",
                "platform": platform,
                "mode": out.get("mode") or "live",
                "target": target,
                "external_id": out.get("external_id"),
                "error": out.get("error"),
                "raw": out.get("raw") or {},
            }
        if platform == "reddit":
            raw = composio_social.execute_tool(
                "REDDIT_POST_REDDIT_COMMENT",
                user_id=uid,
                connected_account_id=ca,
                arguments={"thing_id": target, "text": body},
            )
        else:
            return {"ok": False, "kind": "reply", "error": "unsupported platform"}
    except Exception as exc:
        return {"ok": False, "kind": "reply", "platform": platform, "error": str(exc)[:300]}

    return {
        "ok": True,
        "kind": "reply",
        "platform": platform,
        "mode": "live",
        "target": target,
        "raw": {k: raw.get(k) for k in ("successful", "error") if isinstance(raw, dict)},
    }


def _post(*, workspace_id: str, action: dict[str, Any]) -> dict[str, Any]:
    platform = str(action.get("platform") or "")
    body = str(action.get("body") or "").strip()
    if not body:
        return {"ok": False, "kind": "post", "error": "empty body"}
    if dry_run_enabled():
        return {
            "ok": True,
            "kind": "post",
            "platform": platform,
            "mode": "simulated",
            "body": body,
        }
    from app.approvals.external import consume_external_approval

    allowed, reason = consume_external_approval(
        workspace_id=workspace_id,
        approval_id=str(action.get("approval_id") or "") or None,
        action="content_agent_post",
        target={
            "platform": platform,
            "target": str(action.get("target") or ""),
            "title": str(action.get("title") or ""),
            "body": body,
        },
    )
    if not allowed:
        return {"ok": False, "kind": "post", "platform": platform, "error": reason}
    meta = _account_meta(workspace_id, platform)
    if meta is None:
        return {"ok": False, "kind": "post", "error": "no_active_account"}
    uid, ca, author = meta
    result = composio_social.publish(
        platform=platform,
        body=body,
        connected_account_id=ca,
        composio_user_id=uid,
        author_urn=author,
        title=action.get("title"),
        target=action.get("target"),
    )
    return {"ok": result.ok, "kind": "post", "platform": platform, **result.to_dict()}
