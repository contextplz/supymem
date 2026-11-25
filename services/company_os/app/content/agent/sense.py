"""Sense the world: comments, reactions, and sector posts the agent should notice."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.content import composio_social
from app.content.agent.policy import policy_for
from app.content.oauth import flow as oauth_flow
from app.db.models import ContentAccount, ContentPost
from app.state import db

log = logging.getLogger(__name__)


def _meta(account: dict[str, Any]) -> tuple[str, str] | None:
    factory = db()
    with factory() as session:
        row = session.get(ContentAccount, account["id"])
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
        return uid, ca


def _published(workspace_id: str, platform: str, limit: int = 15) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(ContentPost)
            .where(
                ContentPost.workspace_id == workspace_id,
                ContentPost.platform == platform,
                ContentPost.state == "published",
                ContentPost.external_id.is_not(None),
            )
            .order_by(ContentPost.published_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "external_id": r.external_id,
                "external_url": r.external_url,
                "body": (r.body or "")[:400],
            }
            for r in rows
            if r.external_id and not str(r.external_id).startswith("sim_")
        ]


def sense_workspace(workspace_id: str) -> list[dict[str, Any]]:
    """Collect fresh signals the agent has not necessarily acted on yet."""
    signals: list[dict[str, Any]] = []
    for account in oauth_flow.list_accounts(workspace_id):
        if account.get("status") != "active":
            continue
        platform = account["platform"]
        pol = policy_for(platform)
        meta = _meta(account)
        if meta is None:
            continue
        uid, ca = meta

        if pol.can_listen_comments or pol.can_listen_reactions:
            for post in _published(workspace_id, platform):
                if platform == "linkedin":
                    signals.extend(
                        _sense_linkedin_post(
                            uid=uid,
                            ca=ca,
                            post=post,
                            listen_comments=pol.can_listen_comments,
                            listen_reactions=pol.can_listen_reactions,
                        )
                    )
                elif platform == "reddit" and pol.can_listen_comments:
                    signals.extend(
                        _sense_reddit_post(uid=uid, ca=ca, post=post)
                    )

        if platform == "reddit" and pol.can_listen_sector:
            signals.extend(_sense_reddit_sector(uid=uid, ca=ca, workspace_id=workspace_id))

    return signals


def _sense_linkedin_post(
    *,
    uid: str,
    ca: str,
    post: dict[str, Any],
    listen_comments: bool,
    listen_reactions: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ext = post["external_id"]
    if listen_reactions:
        try:
            entity = (
                str(ext)
                if str(ext).startswith("urn:li:")
                else f"urn:li:share:{ext}"
            )
            raw = composio_social.execute_tool(
                "LINKEDIN_LIST_REACTIONS",
                user_id=uid,
                connected_account_id=ca,
                arguments={"entity": entity},
            )
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            out.append(
                {
                    "kind": "reactions",
                    "platform": "linkedin",
                    "external_id": f"react:{ext}",
                    "parent_post_id": post["id"],
                    "parent_external_id": ext,
                    "summary": "reactions on our post",
                    "payload": data if isinstance(data, dict) else {"raw": data},
                }
            )
        except Exception as exc:
            log.debug("linkedin reactions skip: %s", exc)

    if listen_comments:
        # LinkedIn toolkit lacks a dedicated "list comments" tool; use post content
        # as a heartbeat and treat CREATE_COMMENT as reply path once we have thread ids
        # from GET_POST_CONTENT when available.
        try:
            post_id = (
                str(ext)
                if str(ext).startswith("urn:li:")
                else f"urn:li:share:{ext}"
            )
            raw = composio_social.execute_tool(
                "LINKEDIN_GET_POST_CONTENT",
                user_id=uid,
                connected_account_id=ca,
                arguments={"post_id": post_id},
            )
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            commentary = ""
            if isinstance(data, dict):
                commentary = str(
                    data.get("commentary")
                    or data.get("text")
                    or data.get("message")
                    or ""
                )[:500]
            out.append(
                {
                    "kind": "post_snapshot",
                    "platform": "linkedin",
                    "external_id": f"snap:{ext}",
                    "parent_post_id": post["id"],
                    "parent_external_id": ext,
                    "summary": commentary or "post snapshot",
                    "payload": data if isinstance(data, dict) else {"raw": data},
                    "reply_target": ext,
                }
            )
        except Exception as exc:
            log.debug("linkedin post content skip: %s", exc)
    return out


def _sense_reddit_post(*, uid: str, ca: str, post: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ext = str(post["external_id"] or "")
    article = ext.replace("t3_", "") if ext.startswith("t3_") else ext
    if not article:
        return out
    try:
        raw = composio_social.execute_tool(
            "REDDIT_RETRIEVE_POST_COMMENTS",
            user_id=uid,
            connected_account_id=ca,
            arguments={"post_id": article, "sort": "new", "limit": 20},
        )
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        comments = []
        if isinstance(data, dict):
            comments = data.get("comments") or data.get("children") or data.get("items") or []
        if not isinstance(comments, list):
            comments = []
        for c in comments[:20]:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or c.get("name") or c.get("fullname") or "")
            body = str(c.get("body") or c.get("text") or "")[:600]
            if not cid or not body:
                continue
            out.append(
                {
                    "kind": "comment",
                    "platform": "reddit",
                    "external_id": f"comment:{cid}",
                    "parent_post_id": post["id"],
                    "parent_external_id": ext,
                    "summary": body,
                    "payload": c,
                    "reply_target": cid if cid.startswith("t1_") else f"t1_{cid}",
                    "author": c.get("author"),
                }
            )
    except Exception as exc:
        log.debug("reddit comments skip: %s", exc)
    return out


def _sense_reddit_sector(*, uid: str, ca: str, workspace_id: str) -> list[dict[str, Any]]:
    """Skim sector conversation so the agent is not only talking to itself."""
    from app.content import brief as brief_store

    current = brief_store.active(workspace_id)
    query = "B2B sales"
    if current:
        product = current.get("product") or {}
        if isinstance(product, dict):
            query = str(product.get("category") or product.get("name") or query)
        segs = (current.get("icp") or {}).get("segments") if isinstance(current.get("icp"), dict) else None
        if isinstance(segs, list) and segs and isinstance(segs[0], dict):
            query = str(segs[0].get("name") or segs[0].get("pain") or query)

    out: list[dict[str, Any]] = []
    try:
        raw = composio_social.execute_tool(
            "REDDIT_SEARCH_ACROSS_SUBREDDITS",
            user_id=uid,
            connected_account_id=ca,
            arguments={"query": query[:80], "sort": "new", "limit": 10},
        )
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        items = []
        if isinstance(data, dict):
            items = data.get("posts") or data.get("children") or data.get("items") or []
        if not isinstance(items, list):
            items = []
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or item.get("name") or "")
            title = str(item.get("title") or "")[:200]
            if not pid:
                continue
            out.append(
                {
                    "kind": "sector_post",
                    "platform": "reddit",
                    "external_id": f"sector:{pid}",
                    "summary": title,
                    "payload": item,
                    "reply_target": pid if pid.startswith("t3_") else f"t3_{pid}",
                    "query": query,
                }
            )
    except Exception as exc:
        log.debug("reddit sector skip: %s", exc)
    return out
