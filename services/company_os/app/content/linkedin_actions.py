"""LinkedIn organic actions via Composio (preferred) + Unipile DMs.

Honest capability surface for Marketing Lab / content agent. Does not fake
success for actions LinkedIn's API or Composio toolkit cannot perform
(InMail inbox, create-reaction, edit post, etc.).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any

from core.clock import utcnow
from sqlalchemy import select

from app.content import composio_social
from app.content.platforms.base import PublishResult, dry_run_enabled
from app.db.models import ContentAccount
from app.state import db

log = logging.getLogger(__name__)

UNIPILE_SEND_ACTIONS = frozenset(
    {
        "send_dm",
        "send_connection",
        "accept_invitation",
        "resolve_profile",
        "list_invitations",
        "read_inbox",
    }
)

#: Action → backend + live feasibility (API/connector limits, not product wishes).
CAPABILITY_MATRIX: list[dict[str, Any]] = [
    {
        "action": "connect_oauth",
        "built": True,
        "backend": "composio",
        "live": True,
        "notes": "Composio Connect Link; stores connected_account_id on content_accounts.",
    },
    {
        "action": "create_text_post",
        "built": True,
        "backend": "composio LINKEDIN_CREATE_LINKED_IN_POST",
        "live": True,
        "notes": "Requires w_member_social + author URN.",
    },
    {
        "action": "create_image_post",
        "built": True,
        "backend": "composio CREATE_LINKED_IN_POST + file stage",
        "live": True,
        "notes": "Stages local image via Composio S3 then attaches images[].",
    },
    {
        "action": "create_video_post",
        "built": True,
        "backend": "composio UPLOAD_VIDEO + CREATE_VIDEO_POST",
        "live": True,
        "notes": "MP4 path or public video_url; upload then publish.",
    },
    {
        "action": "create_url_share",
        "built": True,
        "backend": "composio CREATE_ARTICLE_OR_URL_SHARE",
        "live": True,
        "notes": "Article/URL share via UGC Posts API.",
    },
    {
        "action": "comment_on_post",
        "built": True,
        "backend": "composio CREATE_COMMENT_ON_POST",
        "live": True,
        "notes": "Policy-gated (CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES). ToS-sensitive.",
    },
    {
        "action": "reply_to_comment",
        "built": True,
        "backend": "composio CREATE_COMMENT_ON_POST + parent_comment",
        "live": True,
        "notes": "Nested reply via parent_comment URN.",
    },
    {
        "action": "share_repost",
        "built": True,
        "backend": "composio CREATE_LINKED_IN_POST reshareContext",
        "live": True,
        "notes": "Repost with optional commentary.",
    },
    {
        "action": "like_react",
        "built": False,
        "backend": "none",
        "live": False,
        "notes": "Composio exposes LIST_REACTIONS only; no create-reaction tool.",
    },
    {
        "action": "send_dm",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: LinkedIn DM send raises ComplianceError. Clipboard only.",
    },
    {
        "action": "resolve_profile",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: Unipile profile resolve is not a send path; still blocked.",
    },
    {
        "action": "send_connection",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: Unipile invitations raise ComplianceError.",
    },
    {
        "action": "list_invitations",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: Unipile invitation list is disabled.",
    },
    {
        "action": "accept_invitation",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: Accept+reply via Unipile raises ComplianceError.",
    },
    {
        "action": "read_inbox",
        "built": False,
        "backend": "never",
        "live": False,
        "notes": "OT-45: Unipile inbox/chat send helpers are disabled.",
    },
    {
        "action": "delete_post",
        "built": True,
        "backend": "composio DELETE_POST",
        "live": True,
        "notes": "Deletes share/ugcPost by URN.",
    },
    {
        "action": "edit_post",
        "built": False,
        "backend": "none",
        "live": False,
        "notes": "LinkedIn member posts API does not offer a reliable edit in this toolkit.",
    },
    {
        "action": "get_post",
        "built": True,
        "backend": "composio GET_POST_CONTENT",
        "live": True,
        "notes": (
            "Read post snapshot for verify / agent sense. "
            "May return Forbidden under w_member_social-only Share-on-LinkedIn scopes."
        ),
    },
    {
        "action": "list_reactions",
        "built": True,
        "backend": "composio LIST_REACTIONS",
        "live": True,
        "notes": "Read-only engagement signal.",
    },
]


def _truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_post_urn(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("urn:li:"):
        return raw
    if raw.isdigit():
        return f"urn:li:share:{raw}"
    return raw


def resolve_account(workspace_id: str) -> dict[str, Any] | None:
    """Active LinkedIn content account + Composio meta, or None."""
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(ContentAccount).where(
                ContentAccount.workspace_id == workspace_id,
                ContentAccount.platform == "linkedin",
                ContentAccount.status == "active",
            )
        ).first()
        if row is None:
            return None
        try:
            extra = json.loads(row.extra_json or "{}")
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        return {
            "id": row.id,
            "handle": row.handle,
            "author_urn": row.author_urn or "",
            "external_account": row.external_account,
            "paused_reason": row.paused_reason,
            "posts_today": row.posts_today,
            "daily_cap": row.daily_cap,
            "last_error": row.last_error,
            "auth_via": extra.get("auth_via"),
            "connected_account_id": str(extra.get("connected_account_id") or ""),
            "composio_user_id": str(extra.get("composio_user_id") or ""),
        }


def heal_connection(workspace_id: str) -> dict[str, Any]:
    """Re-sync Composio LinkedIn → content_accounts; clear stale CA ids."""
    if not composio_social.configured():
        return {
            "ok": False,
            "needs_you": True,
            "error": "COMPOSIO_API_KEY missing",
            "code": "composio_missing",
        }
    try:
        conns = composio_social.list_connections(
            workspace_id=workspace_id, platform="linkedin", active_only=False
        )
    except composio_social.ComposioSocialError as exc:
        return {"ok": False, "error": exc.message, "code": exc.code}

    active = [
        c
        for c in conns
        if str(c.get("status") or "").upper() in {"ACTIVE", "SUCCESS", "CONNECTED"}
    ]
    if not active:
        try:
            link = composio_social.start_connect(
                workspace_id=workspace_id, platform="linkedin"
            )
        except composio_social.ComposioSocialError as exc:
            link = {"error": exc.message}
        return {
            "ok": False,
            "needs_you": True,
            "code": "linkedin_reconnect",
            "error": "No ACTIVE Composio LinkedIn connection for this content user.",
            "reconnect": link,
            "connections": [
                {k: c.get(k) for k in ("id", "status", "user_id")} for c in conns
            ],
        }

    synced = composio_social.sync_workspace(workspace_id, platform="linkedin")
    account = resolve_account(workspace_id)
    return {
        "ok": True,
        "synced": synced,
        "account": account,
        "active_connection_id": active[0].get("id"),
        "composio_user_id": composio_social.user_id_for(workspace_id),
    }


def unpause_account(workspace_id: str, *, reason_prefix: str | None = "e2e") -> dict[str, Any]:
    """Clear pause holds (e.g. e2e dry-run hold) and reset today's counter."""
    now = utcnow()
    cleared: list[str] = []
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == workspace_id,
                    ContentAccount.platform == "linkedin",
                    ContentAccount.status == "active",
                )
            )
        )
        for row in rows:
            pause = (row.paused_reason or "").strip()
            if reason_prefix and pause and reason_prefix.lower() not in pause.lower():
                # Still clear empty-ish automation holds; leave founder kill switches.
                if pause.lower() in {
                    "founder stopped posting",
                    "revoked by founder",
                }:
                    continue
            row.paused_reason = None
            row.posts_today = 0
            row.daily_cap = max(int(row.daily_cap or 1), 3)
            row.last_post_at = now - timedelta(hours=48)
            row.health = "ok"
            row.last_error = None
            row.updated_at = now
            cleared.append(row.id)
        session.commit()
    return {"ok": True, "cleared": cleared, "count": len(cleared)}


def readiness(workspace_id: str) -> dict[str, Any]:
    """Lab-facing readiness for each LinkedIn action."""
    account = resolve_account(workspace_id)
    composio_ok = composio_social.configured()
    unipile_ok = bool(
        (os.environ.get("UNIPILE_API_KEY") or "").strip()
        and (os.environ.get("UNIPILE_ACCOUNT_ID") or "").strip()
    )
    replies = _truthy("CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES", default=True)
    dry = dry_run_enabled()
    connected = bool(
        account
        and account.get("auth_via") == "composio"
        and account.get("connected_account_id")
        and account.get("author_urn")
    )
    paused = bool(account and account.get("paused_reason"))

    actions: dict[str, Any] = {}
    for row in CAPABILITY_MATRIX:
        action = row["action"]
        live_flag = row["live"]
        if live_flag is True:
            workable = connected and composio_ok and not dry
            if action in {"comment_on_post", "reply_to_comment"}:
                workable = workable and replies
            if action == "connect_oauth":
                workable = composio_ok
        elif live_flag == "unipile_if_configured":
            workable = False
        else:
            workable = False
        blocker = None
        if action in UNIPILE_SEND_ACTIONS:
            blocker = "linkedin_send_forbidden"
            workable = False
        elif not row["built"]:
            blocker = row["notes"]
        elif action in {
            "send_dm",
            "resolve_profile",
            "send_connection",
            "list_invitations",
            "accept_invitation",
            "read_inbox",
        } and not unipile_ok:
            blocker = "UNIPILE_API_KEY + UNIPILE_ACCOUNT_ID required; else human_queue."
        elif not composio_ok and str(row["backend"]).startswith("composio"):
            blocker = "COMPOSIO_API_KEY missing"
        elif action != "connect_oauth" and str(row["backend"]).startswith("composio") and not connected:
            blocker = "LinkedIn not connected via Composio (or author URN missing)."
        elif dry and workable is False and row["built"] and live_flag is True:
            blocker = "CONTENT_DRY_RUN is on"
        elif paused and action not in {
            "connect_oauth",
            "like_react",
            "edit_post",
            "list_invitations",
            "resolve_profile",
            "read_inbox",
        }:
            blocker = f"account paused: {account.get('paused_reason')}" if account else "paused"
        actions[action] = {
            **row,
            "workable_now": bool(workable) and not (paused and action not in {"connect_oauth"}),
            "blocker": blocker,
        }

    return {
        "workspace_id": workspace_id,
        "composio": composio_ok,
        "connected": connected,
        "dry_run": dry,
        "unipile": unipile_ok,
        "replies_allowed": replies,
        "account": account,
        "actions": actions,
        "matrix": CAPABILITY_MATRIX,
    }


def _meta_or_error(workspace_id: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    account = resolve_account(workspace_id)
    if account is None:
        return None, {"ok": False, "error": "no_active_linkedin_account", "needs_you": True}
    if account.get("auth_via") != "composio":
        return None, {
            "ok": False,
            "error": "linkedin_not_composio",
            "needs_you": True,
            "message": "Reconnect LinkedIn via Composio on Content Studio.",
        }
    uid = str(account.get("composio_user_id") or "")
    ca = str(account.get("connected_account_id") or "")
    author = str(account.get("author_urn") or "")
    if not uid or not ca:
        return None, {"ok": False, "error": "composio_meta_missing", "needs_you": True}
    if not author:
        # Best-effort enrich.
        try:
            profile = composio_social.enrich_profile(
                platform="linkedin", user_id=uid, connected_account_id=ca
            )
            author = profile.get("author_urn") or ""
        except Exception as exc:
            log.warning("enrich author failed: %s", exc)
    if not author:
        return None, {
            "ok": False,
            "error": "author_urn_missing",
            "needs_you": True,
            "message": "Reconnect LinkedIn so we can store the person URN.",
        }
    return {"uid": uid, "ca": ca, "author": author, "account_id": account["id"]}, None


def _tool_result(raw: dict[str, Any], *, action: str) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw.get("data")
    external_id = ""
    if isinstance(data, dict):
        external_id = str(
            data.get("id")
            or data.get("urn")
            or data.get("post_id")
            or composio_social._find_linkedin_urn(data)  # noqa: SLF001
            or ""
        )
    elif isinstance(data, str) and data.startswith("urn:li:"):
        external_id = data
    url = None
    if external_id and external_id.startswith("urn:li:"):
        url = f"https://www.linkedin.com/feed/update/{external_id}/"
    return {
        "ok": bool(raw.get("successful", True)) and not raw.get("error"),
        "action": action,
        "mode": "live",
        "external_id": external_id or None,
        "external_url": url,
        "error": raw.get("error"),
        "raw": {k: raw.get(k) for k in ("successful", "error") if k in raw},
    }


def execute(
    *,
    workspace_id: str,
    action: str,
    body: str = "",
    target_urn: str = "",
    parent_comment_urn: str = "",
    url: str = "",
    video_path: str = "",
    video_url: str = "",
    image_path: str = "",
    title: str = "",
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Run one LinkedIn lab/agent action. Never fakes live success."""
    action = (action or "").strip().lower()
    aliases = {
        "post": "create_text_post",
        "text_post": "create_text_post",
        "image_post": "create_image_post",
        "video_post": "create_video_post",
        "url_share": "create_url_share",
        "article": "create_url_share",
        "comment": "comment_on_post",
        "reply": "reply_to_comment",
        "repost": "share_repost",
        "share": "share_repost",
        "delete": "delete_post",
        "dm": "send_dm",
        "message": "send_dm",
        "react": "like_react",
        "like": "like_react",
        "get": "get_post",
        "reactions": "list_reactions",
        "heal": "heal_connection",
        "unpause": "unpause_account",
        "connect": "connect_oauth",
        "invite": "send_connection",
        "connection": "send_connection",
        "send_invite": "send_connection",
        "connection_request": "send_connection",
        "profile": "resolve_profile",
        "resolve": "resolve_profile",
        "invitations": "list_invitations",
        "list_invites": "list_invitations",
        "accept": "accept_invitation",
        "accept_invite": "accept_invitation",
        "inbox": "read_inbox",
        "chats": "read_inbox",
    }
    action = aliases.get(action, action)

    if action == "heal_connection":
        return heal_connection(workspace_id)
    if action == "unpause_account":
        return unpause_account(workspace_id)
    if action == "connect_oauth":
        try:
            link = composio_social.start_connect(
                workspace_id=workspace_id, platform="linkedin"
            )
            return {"ok": True, "action": action, "reconnect": link}
        except composio_social.ComposioSocialError as exc:
            return {"ok": False, "action": action, "error": exc.message, "needs_you": True}

    if action == "like_react":
        return {
            "ok": False,
            "action": action,
            "error": "not_supported",
            "needs_you": False,
            "message": (
                "Composio LinkedIn toolkit has LIST_REACTIONS only, "
                "no create-reaction / like tool. Cannot fake a like."
            ),
        }
    if action == "edit_post":
        return {
            "ok": False,
            "action": action,
            "error": "not_supported",
            "message": "LinkedIn edit-post is not available in the Composio toolkit.",
        }

    if action in UNIPILE_SEND_ACTIONS:
        from app.gtm.relationships.compliance import assert_unipile_outreach_forbidden

        assert_unipile_outreach_forbidden()
    read_actions = {
        "resolve_profile",
        "list_invitations",
        "read_inbox",
        "get_post",
        "list_reactions",
    }
    if action not in read_actions:
        if dry_run_enabled():
            return {
                "ok": True,
                "action": action,
                "mode": "simulated",
                "target_urn": target_urn or None,
                "body": (body or "")[:500],
                "note": "Outbound execution is disabled; no LinkedIn write performed.",
            }
        from app.approvals.external import consume_external_approval

        allowed, reason = consume_external_approval(
            workspace_id=workspace_id,
            approval_id=approval_id,
            action="linkedin_action",
            target={
                "action": action,
                "target_urn": target_urn,
                "parent_comment_urn": parent_comment_urn,
                "url": url,
                "title": title,
                "body": body,
                "image_path": image_path,
                "video_path": video_path,
                "video_url": video_url,
            },
        )
        if not allowed:
            return {
                "ok": False,
                "action": action,
                "needs_you": True,
                "error": reason,
                "message": "Approve this exact LinkedIn action before it can run.",
            }

    if action in {
        "send_dm",
        "resolve_profile",
        "send_connection",
        "list_invitations",
        "accept_invitation",
        "read_inbox",
    }:
        return _unipile_action(
            workspace_id=workspace_id,
            action=action,
            body=body,
            target_urn=target_urn,
            parent_comment_urn=parent_comment_urn,
        )

    meta, err = _meta_or_error(workspace_id)
    if err:
        return {**err, "action": action}
    assert meta is not None

    try:
        if action == "create_text_post":
            result = composio_social.publish(
                platform="linkedin",
                body=body,
                connected_account_id=meta["ca"],
                composio_user_id=meta["uid"],
                author_urn=meta["author"],
            )
            return _publish_as_action(result, action)
        if action == "create_image_post":
            if not image_path:
                return {"ok": False, "action": action, "error": "image_path_required"}
            result = composio_social.publish(
                platform="linkedin",
                body=body,
                connected_account_id=meta["ca"],
                composio_user_id=meta["uid"],
                author_urn=meta["author"],
                image_paths=[image_path],
            )
            return _publish_as_action(result, action)
        if action == "create_video_post":
            return _video_post(
                meta=meta, body=body, video_path=video_path, video_url=video_url, title=title
            )
        if action == "create_url_share":
            return _url_share(meta=meta, body=body, url=url)
        if action == "comment_on_post":
            return _comment(
                meta=meta,
                target_urn=target_urn,
                body=body,
                parent_comment_urn="",
            )
        if action == "reply_to_comment":
            return _comment(
                meta=meta,
                target_urn=target_urn,
                body=body,
                parent_comment_urn=parent_comment_urn or target_urn,
            )
        if action == "share_repost":
            return _repost(meta=meta, target_urn=target_urn, body=body)
        if action == "delete_post":
            urn = _normalize_post_urn(target_urn)
            if not urn:
                return {"ok": False, "action": action, "error": "target_urn_required"}
            raw = composio_social.execute_tool(
                "LINKEDIN_DELETE_POST",
                user_id=meta["uid"],
                connected_account_id=meta["ca"],
                arguments={"post_urn": urn},
            )
            return _tool_result(raw, action=action)
        if action == "get_post":
            urn = _normalize_post_urn(target_urn)
            if not urn:
                return {"ok": False, "action": action, "error": "target_urn_required"}
            raw = composio_social.execute_tool(
                "LINKEDIN_GET_POST_CONTENT",
                user_id=meta["uid"],
                connected_account_id=meta["ca"],
                arguments={"post_id": urn},
            )
            out = _tool_result(raw, action=action)
            out["data"] = raw.get("data")
            return out
        if action == "list_reactions":
            urn = _normalize_post_urn(target_urn)
            if not urn:
                return {"ok": False, "action": action, "error": "target_urn_required"}
            raw = composio_social.execute_tool(
                "LINKEDIN_LIST_REACTIONS",
                user_id=meta["uid"],
                connected_account_id=meta["ca"],
                arguments={"entity": urn},
            )
            out = _tool_result(raw, action=action)
            out["data"] = raw.get("data")
            return out
    except composio_social.ComposioSocialError as exc:
        msg = exc.message
        # Stale CA → try heal once for write paths.
        if "No connected account" in msg or "connected account" in msg.lower():
            healed = heal_connection(workspace_id)
            return {
                "ok": False,
                "action": action,
                "error": msg,
                "healed": healed,
                "needs_you": not healed.get("ok"),
                "message": (
                    "Composio account mapping was stale; heal attempted. "
                    "If still failing, reconnect LinkedIn via Connect Link."
                ),
            }
        return {"ok": False, "action": action, "error": msg[:400]}
    except Exception as exc:
        return {"ok": False, "action": action, "error": str(exc)[:400]}

    return {"ok": False, "action": action, "error": f"unknown_action:{action}"}


def _publish_as_action(result: PublishResult, action: str) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "action": action,
        "mode": result.mode,
        "external_id": result.external_id,
        "external_url": result.external_url,
        "error": result.error,
        "fatal": result.fatal,
    }


def _comment(
    *,
    meta: dict[str, str],
    target_urn: str,
    body: str,
    parent_comment_urn: str,
) -> dict[str, Any]:
    if not _truthy("CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES", default=True):
        return {
            "ok": False,
            "action": "comment_on_post",
            "error": "replies_disabled",
            "message": "Set CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES=true to allow comments.",
        }
    text = (body or "").strip()
    urn = _normalize_post_urn(target_urn)
    if not urn or not text:
        return {"ok": False, "error": "target_urn_and_body_required"}
    # For nested replies, object = root post; target_urn = parent comment.
    root = urn
    target = urn
    parent = (parent_comment_urn or "").strip()
    if parent:
        target = parent
        # object must stay root share/ugcPost — if caller passed comment as target, require root in target_urn
        if "comment:" in urn:
            return {
                "ok": False,
                "error": "root_post_urn_required",
                "message": "Pass the root share/ugcPost URN as target_urn and parent comment as parent_comment_urn.",
            }
    args: dict[str, Any] = {
        "actor": meta["author"],
        "object": root,
        "target_urn": target,
        "message": {"text": text},
    }
    if parent:
        args["parent_comment"] = parent
    raw = composio_social.execute_tool(
        "LINKEDIN_CREATE_COMMENT_ON_POST",
        user_id=meta["uid"],
        connected_account_id=meta["ca"],
        arguments=args,
    )
    return _tool_result(raw, action="reply_to_comment" if parent else "comment_on_post")


def _repost(*, meta: dict[str, str], target_urn: str, body: str) -> dict[str, Any]:
    urn = _normalize_post_urn(target_urn)
    if not urn:
        return {"ok": False, "action": "share_repost", "error": "target_urn_required"}
    args: dict[str, Any] = {
        "author": meta["author"],
        "commentary": (body or "").strip() or " ",
        "visibility": "PUBLIC",
        "lifecycleState": "PUBLISHED",
        "reshareContext": {"parent": urn},
    }
    raw = composio_social.execute_tool(
        "LINKEDIN_CREATE_LINKED_IN_POST",
        user_id=meta["uid"],
        connected_account_id=meta["ca"],
        arguments=args,
    )
    return _tool_result(raw, action="share_repost")


def _url_share(*, meta: dict[str, str], body: str, url: str) -> dict[str, Any]:
    link = (url or "").strip()
    if not link:
        return {"ok": False, "action": "create_url_share", "error": "url_required"}
    commentary = (body or "").strip() or link
    args = {
        "author": meta["author"],
        "lifecycleState": "PUBLISHED",
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": commentary},
                "shareMediaCategory": "ARTICLE",
                "media": [
                    {
                        "status": "READY",
                        "originalUrl": link,
                        "title": {"text": commentary[:200]},
                    }
                ],
            }
        },
    }
    raw = composio_social.execute_tool(
        "LINKEDIN_CREATE_ARTICLE_OR_URL_SHARE",
        user_id=meta["uid"],
        connected_account_id=meta["ca"],
        arguments=args,
    )
    return _tool_result(raw, action="create_url_share")


def _video_post(
    *,
    meta: dict[str, str],
    body: str,
    video_path: str,
    video_url: str,
    title: str,
) -> dict[str, Any]:
    commentary = (body or "").strip()
    if not commentary:
        return {"ok": False, "action": "create_video_post", "error": "body_required"}
    upload_args: dict[str, Any] = {}
    if video_path:
        try:
            staged = composio_social._stage_file_for_composio(  # noqa: SLF001
                path=video_path,
                tool_slug="LINKEDIN_UPLOAD_VIDEO",
                toolkit_slug="linkedin",
            )
            upload_args["file"] = staged
        except composio_social.ComposioSocialError as exc:
            return {"ok": False, "action": "create_video_post", "error": f"stage:{exc.message}"}
    elif video_url:
        upload_args["video_url"] = video_url.strip()
    else:
        return {
            "ok": False,
            "action": "create_video_post",
            "error": "video_path_or_video_url_required",
        }

    uploaded = composio_social.execute_tool(
        "LINKEDIN_UPLOAD_VIDEO",
        user_id=meta["uid"],
        connected_account_id=meta["ca"],
        arguments=upload_args,
    )
    data = uploaded.get("data") if isinstance(uploaded.get("data"), dict) else uploaded
    video_urn = ""
    if isinstance(data, dict):
        video_urn = str(
            data.get("video_urn")
            or data.get("urn")
            or data.get("id")
            or composio_social._dig(data, "response_data", "video_urn")  # noqa: SLF001
            or ""
        )
    if not video_urn:
        return {
            "ok": False,
            "action": "create_video_post",
            "error": "upload_missing_video_urn",
            "raw": {k: uploaded.get(k) for k in ("successful", "error")},
        }
    args: dict[str, Any] = {
        "video_urn": video_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
    }
    if title:
        args["title"] = title
    raw = composio_social.execute_tool(
        "LINKEDIN_CREATE_VIDEO_POST",
        user_id=meta["uid"],
        connected_account_id=meta["ca"],
        arguments=args,
    )
    return _tool_result(raw, action="create_video_post")


def _unipile_action(
    *,
    workspace_id: str,
    action: str,
    body: str = "",
    target_urn: str = "",
    parent_comment_urn: str = "",
) -> dict[str, Any]:
    """Unipile DM/invite/inbox is structurally forbidden (OT-45)."""
    _ = (workspace_id, body, target_urn, parent_comment_urn)
    from app.gtm.relationships.compliance import assert_unipile_outreach_forbidden

    assert_unipile_outreach_forbidden()
    raise RuntimeError(f"unipile_unreachable:{action}")


def _send_dm(*, workspace_id: str, body: str, target_urn: str) -> dict[str, Any]:
    from app.gtm.relationships.compliance import assert_unipile_outreach_forbidden

    _ = (workspace_id, body, target_urn)
    assert_unipile_outreach_forbidden()
    raise RuntimeError("unipile_unreachable:send_dm")
