"""Aggregated connection status + connect dispatch for the Console hub.

Presence-only — never returns secrets. Delegates OAuth to existing helpers
(Gmail, Composio social, Discordbot).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from core.clock import as_utc, utcnow

from app.connections import local_config
from app.core.local_mode import is_local

log = logging.getLogger(__name__)

PROVIDERS = (
    "composio",
    "gmail",
    "google_calendar",
    "linkedin",
    "discord",
    "reddit",
    "x",
    "unipile",
    "hunter",
    "google_ads",
    "meta_ads",
)


def _console_origin() -> str:
    if is_local():
        return local_config.public_url("http://127.0.0.1:3100")
    return (
        os.environ.get("CONSOLE_URL")
        or os.environ.get("NEXT_PUBLIC_APP_URL")
        or "http://127.0.0.1:3100"
    ).rstrip("/")


def _hub_callback(provider: str) -> str:
    return f"{_console_origin()}/connections?connected={provider}"


def _env_card(*names: str, note: str | None = None) -> dict[str, Any]:
    missing = [n for n in names if not (os.environ.get(n) or "").strip()]
    return {
        "ok": not missing,
        "ready": not missing,
        "configured": not missing,
        "missing": missing,
        "note": note,
        "action": None if not missing else "fix",
        "connectable": False,
    }


def _smtp_card() -> dict[str, Any]:
    if is_local() and not local_config.enabled("send_email"):
        return {"ok": False, "ready": False, "configured": False, "missing": local_config.missing("smtp"),
            "hint": local_config.setup_message("smtp"), "action": "fix", "connectable": False}
    from app.gtm.local_mail import settings
    try:
        settings()
    except ValueError as exc:
        return {"ok": False, "ready": False, "configured": False, "missing": ["Email settings"],
            "hint": str(exc), "action": "fix", "connectable": False}
    return {"ok": True, "ready": True, "configured": True, "missing": [],
        "hint": None, "action": None, "connectable": False}


def _gmail_accounts_query(workspace_id: str):
    from sqlalchemy import select
    from app.db.models import GtmEmailAccount

    return select(GtmEmailAccount).where(GtmEmailAccount.workspace_id == workspace_id,
        GtmEmailAccount.provider == "gmail").order_by(GtmEmailAccount.updated_at.desc(), GtmEmailAccount.id)


def _account_slice(accounts: list[dict[str, Any]], platform: str) -> dict[str, Any]:
    matches = [
        a
        for a in accounts
        if str(a.get("platform") or "").lower() == platform
        and str(a.get("status") or "") != "revoked"
    ]
    active = [a for a in matches if str(a.get("status") or "") == "active"]
    paused = [a for a in matches if a.get("paused_reason")]
    top = active[0] if active else (matches[0] if matches else None)
    paused_reason = (paused[0].get("paused_reason") if paused else None) or (
        top.get("paused_reason") if top else None
    )
    return {
        "ok": bool(active),
        "ready": bool(active),
        "connected": bool(matches),
        "needs_reconnect": not active and any(a.get("status") == "needs_reconnect" for a in matches),
        "active": len(active),
        "paused_reason": paused_reason,
        "last_error": (matches[0].get("last_error") if matches else None),
        "handle": (active[0].get("handle") if active else None)
        or (matches[0].get("handle") if matches else None),
        "auth_via": (top.get("auth_via") if top else None),
        "last_sync": (top.get("updated_at") if top else None)
        or (top.get("last_synced_at") if top else None),
    }


def _blocker(
    severity: str,
    message: str,
    code: str | None = None,
    *,
    operator_detail: str | None = None,
) -> dict[str, Any]:
    """Founder-facing message stays free of env key names; operator_detail keeps them for ops."""
    out: dict[str, Any] = {"severity": severity, "message": message}
    if code:
        out["code"] = code
    if operator_detail:
        out["operator_detail"] = operator_detail
    return out


FOUNDER_SETUP = {
    "unipile_missing": "LinkedIn outreach is not set up on Supymem yet.",
    "hunter_missing": "Email enrichment is not set up on Supymem yet.",
    "google_ads_env": "Google Ads is not set up on Supymem yet.",
    "meta_ads_env": "Meta Ads is not set up on Supymem yet.",
}


PLATFORM_COMPOSIO_SETUP = (
    "Supymem's connection service needs operator setup. "
    "This version does not accept a personal Composio API key."
)


def _saved_discord_status(workspace_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.community import store
    from app.db.models import EventLog
    from app.state import db

    with db()() as session:
        row = session.scalar(select(EventLog).where(EventLog.workspace_id == workspace_id,
            EventLog.topic == "connections.discord.status").order_by(EventLog.seq.desc()).limit(1))
        if row:
            status = json.loads(row.payload_json or "{}")
            if isinstance(status, dict):
                return {**status, "last_sync": as_utc(row.ts).isoformat()}
    server = next((server for server in store.list_servers(workspace_id)
        if server.get("connected_account_id")), {})
    return {"connected": server.get("status") in {"connected", "provisioned"},
            "connection_id": server.get("connected_account_id"), "guild_id": server.get("guild_id")}


def refresh_discord_status(workspace_id: str) -> dict[str, Any]:
    if is_local():
        missing = local_config.missing("discord")
        if missing:
            return {"ok": False, "connected": False, "needs_you": True, "missing": missing,
                    "message": local_config.setup_message("discord")}
    from app.audit.writer import audit
    from app.community import service as community_service
    from app.credits import metered

    with metered.charge_sync(workspace_id=workspace_id, purpose="connections.status", provider="composio",
                            estimate_micro=0, payer="founder") as meter:
        status = community_service.discord_status(workspace_id)
        meter.record_micro(0)
    audit("connections.discord.status", workspace_id=workspace_id, payload={
        "connected": bool(status.get("connected") or status.get("ok")),
        "connection_id": status.get("active_account_id") or next(iter(status.get("pending_account_ids") or []), None),
        "guild_id": (status.get("installed_guild") or {}).get("guild_id"),
        "status": "pending" if status.get("pending_account_ids") else None,
    })
    return status


def hub_status(workspace_id: str) -> dict[str, Any]:
    """Merge marketing / community / GTM / content readiness — no secrets."""
    from app.content.oauth import flow
    from app.marketing.media.probes import google_ads_available, meta_ads_available

    accounts = flow.list_accounts(workspace_id)
    local = is_local()
    connections: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    # —— Composio platform ——
    composio_missing = local_config.missing("composio") if local else ([] if local_config.api_key() else ["COMPOSIO_API_KEY"])
    composio_ok = not composio_missing
    connections["composio"] = {
        "id": "composio",
        "name": "Composio",
        "ok": composio_ok,
        "ready": composio_ok,
        "missing": composio_missing,
        "blocker": None
        if composio_ok
        else _blocker("NeedsYou" if local else "NeedsOperator",
                      local_config.setup_message("composio") if local else PLATFORM_COMPOSIO_SETUP, "composio_missing"),
        "action": None if composio_ok else "fix",
        "connectable": False,
        "note": "Platform key for Gmail, LinkedIn, Reddit, Discord, and X connect links.",
        "last_sync": None,
    }
    if not composio_ok:
        blockers.append(connections["composio"]["blocker"])

    # —— Gmail (AI GTM / Marketing Lab mail) ——
    gmail: dict[str, Any] = {
        "id": "gmail",
        "name": "Gmail",
        "ok": False,
        "ready": False,
        "configured": False,
        "connected": False,
        "missing": [],
        "blocker": None,
        "action": "connect",
        "connectable": False,
        "account_email": None,
        "last_sync": None,
        "hint": None,
    }
    try:
        from app.gtm.supymem import composio_gmail
        from app.gtm.supymem.pipeline import mail_readiness

        gmail_missing = local_config.missing("gmail") if local else []
        gmail_cfg = not gmail_missing if local else composio_gmail.configured()
        if local:
            from app.autopilot.guard import mailbox_send_max
            from app.connections import mailboxes
            from app.db.models import Workspace
            from app.state import db

            multi = mailboxes.enabled()
            today = utcnow().date().isoformat()
            with db()() as session:
                saved = session.scalars(_gmail_accounts_query(workspace_id)).all()
                linked = [account for account in saved if account.status != "revoked"]
                active = [account for account in linked if account.status == "active"]
                workspace = session.get(Workspace, workspace_id) if multi else None
                account_views = [{"id": account.id, "email": account.email, "status": account.status,
                    "daily_send_limit": mailbox_send_max(session, account),
                    "sent_today": account.sent_today if account.sent_count_date == today else 0,
                    **({"is_default": bool(workspace and workspace.default_gmail_mailbox_id == account.id),
                        "needs_reconnect": account.status != "active", "updated_at": account.updated_at.isoformat()}
                        if multi else {})} for account in linked]
            mail = {"connected": bool(linked), "ready": bool(active),
                "needs_reconnect": bool(linked) and not active,
                "account_email": (active or linked)[0].email if linked else None,
                "accounts": account_views, **({"multi_gmail_v1": True} if multi else {})}
        else:
            mail = mail_readiness(workspace_id)
        connected = bool(mail.get("connected"))
        ready = bool(mail.get("ready") or connected and not local)
        hint = None
        if not gmail_cfg:
            hint = local_config.setup_message("gmail") if local else PLATFORM_COMPOSIO_SETUP
        elif not ready:
            hint = "Reconnect Gmail on Connections, then sync your account." if mail.get("needs_reconnect") else "Click Connect to authorize Gmail via Composio."
        gmail.update(
            {
                "ok": ready and gmail_cfg,
                "ready": ready and gmail_cfg,
                "configured": gmail_cfg,
                "connected": connected,
                "needs_reconnect": bool(mail.get("needs_reconnect")),
                "missing": gmail_missing if local else ([] if gmail_cfg else ["COMPOSIO_API_KEY"]),
                "account_email": mail.get("account_email"),
                "accounts": mail.get("accounts") or [],
                "dry_run": mail.get("dry_run"),
                "send_enabled": mail.get("send_enabled"),
                "hint": hint,
                "connectable": gmail_cfg,
                "action": "sync" if connected else ("connect" if gmail_cfg else "fix"),
                "blocker": None
                if (ready and gmail_cfg)
                else _blocker(
                    "NeedsYou",
                    hint or "Gmail not connected.",
                    "gmail_reconnect_required" if mail.get("needs_reconnect") else "gmail_not_ready",
                ),
            }
        )
        if mail.get("multi_gmail_v1"):
            gmail["multi_gmail_v1"] = True
    except Exception as exc:  # noqa: BLE001
        gmail["hint"] = str(exc)[:160]
        gmail["blocker"] = _blocker("NeedsYou", gmail["hint"], "gmail_error")
        gmail["action"] = "fix"
    connections["gmail"] = gmail
    if gmail.get("blocker") and not gmail.get("ready"):
        blockers.append(gmail["blocker"])

    # —— LinkedIn / Reddit / X (Content Composio social) ——
    for platform, label in (
        ("google_calendar", "Google Calendar"),
        ("linkedin", "LinkedIn"),
        ("reddit", "Reddit"),
        ("x", "X / Twitter"),
    ):
        slice_ = _account_slice(accounts, platform)
        connectable = False
        needs: list[str] = []
        hint = None
        local_missing = local_config.missing(platform) if local else []
        action: str | None = "connect"
        try:
            from app.content import composio_social

            if local_missing:
                needs = local_missing
                hint = local_config.setup_message(platform)
            elif composio_social.configured():
                connectable = composio_social.connectable(platform)
                if not connectable and platform == "x":
                    needs = ["X_CLIENT_ID", "X_CLIENT_SECRET"]
                    # Only list actually missing.
                    needs = [n for n in needs if not (os.environ.get(n) or "").strip()]
                    if not (os.environ.get("COMPOSIO_TWITTER_AUTH_CONFIG_ID") or "").strip():
                        if "COMPOSIO_TWITTER_AUTH_CONFIG_ID" not in needs:
                            # Prefer app keys message; auth config is alternative.
                            pass
                    hint = (
                        "NeedsYou: set X_CLIENT_ID + X_CLIENT_SECRET "
                        "(or COMPOSIO_TWITTER_AUTH_CONFIG_ID) for Composio custom OAuth."
                    )
            else:
                needs = ["COMPOSIO_API_KEY"]
                hint = PLATFORM_COMPOSIO_SETUP
        except Exception as exc:  # noqa: BLE001
            hint = str(exc)[:160]

        paused = slice_.get("paused_reason")
        ready = bool(slice_.get("ok")) and not local_missing
        if local_missing:
            action = "fix"
        elif slice_.get("needs_reconnect"):
            hint = f"Reconnect your {label} to resume posting."
            action = "connect" if connectable else "fix"
        elif (
            paused
            and str(paused).startswith("composio:")
            and not ready
        ):
            # Local row exists but remote Composio account is gone — reconnect.
            hint = (
                f"{paused} — remote Composio account missing. "
                "Click Connect to mint a new OAuth link (auth config auto-ensured)."
            )
            action = "connect" if connectable else "fix"
        elif ready:
            action = "sync"
            # Stale paused_reason after a heal should not keep the card red.
            if paused and str(paused).startswith("composio:"):
                hint = f"Recovered from {paused}; Sync if status drifts."
        elif connectable:
            action = "connect"
        else:
            action = "fix"

        blocker = None
        if not ready:
            blocker = _blocker(
                "NeedsYou",
                hint
                or paused
                or slice_.get("last_error")
                or f"Connect {label} via Composio.",
                f"{platform}_not_ready",
            )
            blockers.append(blocker)

        connections[platform] = {
            "id": platform,
            "name": label,
            **slice_,
            "ok": ready,
            "ready": ready,
            "missing": needs,
            "hint": hint,
            "blocker": blocker,
            "action": action,
            "connectable": connectable,
            "composio_managed": platform in {"linkedin", "reddit"},
        }

    # —— Discord (DISCORDBOT) ——
    discord_card: dict[str, Any] = {
        "id": "discord",
        "name": "Discord (DISCORDBOT)",
        "ok": False,
        "ready": False,
        "connected": False,
        "missing": [],
        "blocker": None,
        "action": "connect",
        "connectable": composio_ok,
        "last_sync": None,
        "hint": None,
    }
    try:
        discord_missing = local_config.missing("discord") if local else []
        st = _saved_discord_status(workspace_id) if not discord_missing else {}
        connected = bool(st.get("connected") or st.get("ok"))
        reason = st.get("needs_you_reason") or st.get("block_reason") or st.get("hint")
        discord_card.update(
            {
                "ok": connected,
                "ready": connected,
                "connected": connected,
                "status": st.get("status"),
                "guild_id": st.get("guild_id"),
                "connection_id": st.get("connection_id"),
                "last_sync": st.get("last_sync"),
                "hint": local_config.setup_message("discord") if discord_missing else (None if connected else (reason or "Connect Discord via Composio.")),
                "connectable": composio_ok and not discord_missing,
                "action": "fix" if discord_missing else ("sync" if connected or (local and st.get("connection_id")) else ("connect" if composio_ok else "fix")),
                "blocker": None
                if connected
                else _blocker(
                    "NeedsYou",
                    local_config.setup_message("discord") if discord_missing else (reason or "Discord bot not connected."),
                    "discord_not_connected",
                ),
                "missing": discord_missing if local else ([] if composio_ok else ["COMPOSIO_API_KEY"]),
            }
        )
        if discord_card.get("blocker"):
            blockers.append(discord_card["blocker"])
    except Exception as exc:  # noqa: BLE001
        discord_card["hint"] = str(exc)[:160]
        discord_card["blocker"] = _blocker(
            "NeedsYou", discord_card["hint"], "discord_error"
        )
        blockers.append(discord_card["blocker"])
    connections["discord"] = discord_card

    # —— Unipile (env only — do not store secrets in browser) ——
    unipile_missing = [
        n
        for n in ("UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID")
        if not (os.environ.get(n) or "").strip()
    ]
    # Soft: DSN has a default; key alone is what Marketing Lab historically checked.
    unipile_ready = not unipile_missing
    connections["unipile"] = {
        "id": "unipile",
        "name": "Unipile",
        "ok": unipile_ready,
        "ready": unipile_ready,
        "missing": unipile_missing,
        "note": (
            None
            if unipile_ready
            else FOUNDER_SETUP["unipile_missing"]
        ),
        "operator_note": (
            "Set UNIPILE_API_KEY + UNIPILE_ACCOUNT_ID in the server env "
            "(optional UNIPILE_DSN). Secrets stay in the vault/env — not the browser. "
            "Without Unipile, LinkedIn DMs stay on the human queue."
        ),
        "docs": "https://developer.unipile.com/",
        "action": None if unipile_ready else "fix",
        "connectable": False,
        "blocker": None
        if unipile_ready
        else _blocker(
            "NeedsYou",
            FOUNDER_SETUP["unipile_missing"],
            "unipile_missing",
            operator_detail="Missing: " + ", ".join(unipile_missing),
        ),
        "last_sync": None,
    }
    if connections["unipile"]["blocker"]:
        blockers.append(connections["unipile"]["blocker"])

    # —— Hunter ——
    hunter = _env_card(
        "HUNTER_API_KEY",
        note="Email enrichment for GTM / Marketing Lab lead gen.",
    )
    hunter.update(
        {
            "id": "hunter",
            "name": "Hunter",
            "note": None if hunter["ok"] else FOUNDER_SETUP["hunter_missing"],
            "operator_note": hunter.get("note"),
            "blocker": None
            if hunter["ok"]
            else _blocker(
                "NeedsYou",
                FOUNDER_SETUP["hunter_missing"],
                "hunter_missing",
                operator_detail="HUNTER_API_KEY is not set.",
            ),
        }
    )
    connections["hunter"] = hunter
    if hunter["blocker"]:
        blockers.append(hunter["blocker"])

    # —— Ads ——
    google_ads = google_ads_available()
    connections["google_ads"] = {
        "id": "google_ads",
        "name": "Google Ads",
        **google_ads,
        "ready": bool(google_ads.get("ok")),
        "action": None if google_ads.get("ok") else "fix",
        "connectable": False,
        "docs": "https://developers.google.com/google-ads/api/docs/start",
        "note": None if google_ads.get("ok") else FOUNDER_SETUP["google_ads_env"],
        "operator_note": (
            None
            if google_ads.get("ok")
            else "Missing: " + ", ".join(google_ads.get("missing") or [])
        ),
        "blocker": None
        if google_ads.get("ok")
        else _blocker(
            "NeedsYou",
            FOUNDER_SETUP["google_ads_env"],
            "google_ads_env",
            operator_detail="Missing: " + ", ".join(google_ads.get("missing") or []),
        ),
        "last_sync": None,
    }
    if connections["google_ads"]["blocker"]:
        blockers.append(connections["google_ads"]["blocker"])

    meta_ads = meta_ads_available()
    connections["meta_ads"] = {
        "id": "meta_ads",
        "name": "Meta Ads",
        **meta_ads,
        "ready": bool(meta_ads.get("ok")),
        "action": None if meta_ads.get("ok") else "fix",
        "connectable": False,
        "docs": "https://developers.facebook.com/docs/marketing-apis/",
        "note": None if meta_ads.get("ok") else FOUNDER_SETUP["meta_ads_env"],
        "operator_note": (
            None
            if meta_ads.get("ok")
            else "Missing: " + ", ".join(meta_ads.get("missing") or [])
        ),
        "blocker": None
        if meta_ads.get("ok")
        else _blocker(
            "NeedsYou",
            FOUNDER_SETUP["meta_ads_env"],
            "meta_ads_env",
            operator_detail="Missing: " + ", ".join(meta_ads.get("missing") or []),
        ),
        "last_sync": None,
    }
    if connections["meta_ads"]["blocker"]:
        blockers.append(connections["meta_ads"]["blocker"])

    from app.connections import toolkits

    visible_toolkits = list(toolkits.TOOLKITS) if local else toolkits.configured_toolkits()
    for slug in visible_toolkits:
        entry = toolkits.TOOLKITS[slug]
        missing = local_config.missing(slug) if local else []
        configured = not missing
        linked = [account for account in toolkits.accounts(workspace_id, slug) if account["outcome"] != "revoked"] if configured else []
        active = any(account["outcome"] == "connected" for account in linked)
        connections[slug] = {
            "id": slug, "name": entry["name"], "toolkit_slug": slug,
            "configured": configured, "connectable": configured and composio_ok,
            "connected": bool(linked), "ready": active and configured and composio_ok, "ok": active and configured and composio_ok,
            "missing": missing, "hint": local_config.setup_message(slug) if missing else None,
            "needs_reconnect": bool(linked) and not active,
            "action": "fix" if missing else "sync" if active else "connect", "note": entry["why"],
            "experiment_goals": entry["goals"], "read_only": bool(entry.get("read_only")),
            "accounts": [{"id": account["id"]} for account in linked],
        }

    if local:
        smtp = _smtp_card()
        smtp.update({"id": "smtp", "name": "SMTP email", "default": True})
        connections["smtp"] = smtp
    order = list(PROVIDERS) + visible_toolkits
    if local:
        order = ["smtp", *order]
        from app.core.local_capabilities import surface_visible

        connections = {name: card for name, card in connections.items() if surface_visible("connections", name)}
        order = [name for name in order if name in connections]
        blockers = [card["blocker"] for card in connections.values() if card.get("blocker")]
    ready_n = sum(1 for c in connections.values() if c.get("ready") or c.get("ok"))
    total = len(order) if local else len(connections)
    return {
        "workspace_id": workspace_id,
        "checked_at": utcnow().isoformat(),
        "ready_count": ready_n,
        "total_count": total,
        "connections": connections,
        "order": order,
        "blockers": blockers,
        "note": (
            "Presence-only credentials. Secrets never returned. "
            "OAuth Connect opens Composio in a new tab where applicable."
        ),
        **({"email_provider": "smtp"} if local else {}),
    }


def disconnect_accounts(workspace_id: str, provider: str) -> dict[str, str | None]:
    from sqlalchemy import select
    from app.content.oauth import attempts
    from app.db.models import ContentAccount
    from app.state import db

    actor = attempts.initiating_actor(workspace_id)
    from app.connections import toolkits

    if provider in toolkits.TOOLKITS:
        return {account["id"]: None if account["outcome"] == "revoked" else account["connected_account_id"]
                for account in toolkits.accounts(workspace_id, provider)}
    with db()() as session:
        query = (_gmail_accounts_query(workspace_id) if provider == "gmail"
                 else select(ContentAccount).where(ContentAccount.workspace_id == workspace_id,
                    ContentAccount.platform == provider))
        accounts = session.scalars(query).all()
        connections = {}
        for account in accounts:
            attempts.authorize_actor(account.workspace_id, actor)
            if account.status == "revoked":
                connections[account.id] = None
            elif provider == "gmail":
                connections[account.id] = account.connected_account_id
            else:
                try:
                    extra = json.loads(account.extra_json or "{}")
                except ValueError:
                    extra = {}
                connection_id = extra.get("connected_account_id") if isinstance(extra, dict) and extra.get("auth_via") == "composio" else None
                connections[account.id] = connection_id or f"native:{account.auth_generation}"
        return connections


def disconnect_provider(workspace_id: str, provider: str, *, accounts: dict[str, str]) -> None:
    from app.content import composio_social
    from app.content.oauth import flow
    from app.credits import metered
    from app.db.models import GtmEmailAccount
    from app.db.session import serialize_writes
    from app.state import db

    if not isinstance(accounts, dict) or not accounts or any(not isinstance(account_id, str) or not isinstance(connection_id, str)
                                                          for account_id, connection_id in accounts.items()):
        raise ValueError("connection_accounts_required")
    current = disconnect_accounts(workspace_id, provider)
    if any(account_id not in current or current[account_id] not in {None, connection_id} for account_id, connection_id in accounts.items()):
        raise ValueError("connection_changed_review_again")
    if provider != "gmail":
        for account_id, connection_id in accounts.items():
            if current[account_id] is not None:
                flow.revoke_account(account_id, workspace_id=workspace_id, expected_connection_id=connection_id)
        return
    for account_id, connection_id in accounts.items():
        with metered.charge_sync(workspace_id=workspace_id, purpose="connections.disconnect", provider="composio", estimate_micro=0, payer="founder") as meter:
            with db()() as session:
                serialize_writes(session, key="gmail-disconnect:" + workspace_id)
                account = session.scalar(_gmail_accounts_query(workspace_id).where(GtmEmailAccount.id == account_id).with_for_update())
                if account is None or account.connected_account_id != connection_id:
                    raise ValueError("connection_changed_review_again")
                if account.status != "revoked":
                    composio_social.delete_connection(account.connected_account_id, revoke=True)
                    account.status, account.updated_at = "revoked", utcnow()
                    session.commit()
            meter.record_micro(0)


def disconnect_mailbox(workspace_id: str, mailbox_id: str, *, confirmed: bool, expected_updated_at: str) -> None:
    from app.connections.mailboxes import MailboxUnavailable, enabled
    from app.db.models import GtmEmailAccount
    from app.state import db

    if not enabled() and not is_local():
        raise MailboxUnavailable("multi_gmail_disabled")
    if confirmed is not True:
        raise ValueError("connection_confirmation_required")
    with db()() as session:
        account = session.scalar(_gmail_accounts_query(workspace_id).where(GtmEmailAccount.id == mailbox_id))
        if account is None:
            raise MailboxUnavailable("mailbox_unavailable")
        if account.updated_at.isoformat() != expected_updated_at:
            raise ValueError("connection_changed_review_again")
        connection_id = account.connected_account_id
    disconnect_provider(workspace_id, "gmail", accounts={mailbox_id: connection_id})


def connect_provider(
    workspace_id: str,
    provider: str,
    *,
    return_to: str | None = None,
    intent: str | None = None,
    mailbox_id: str | None = None,
) -> dict[str, Any]:
    """Start connect / sync for a provider. Returns redirect_url or NeedsYou."""
    key = (provider or "").strip().lower()
    if key == "twitter":
        key = "x"
    if key == "discordbot":
        key = "discord"

    if is_local():
        if key == "gmail":
            key = "smtp"
        from app.core.local_capabilities import unavailable_message

        if reason := unavailable_message("connections", key):
            return {"ok": False, "provider": key, "needs_you": True, "message": reason}
        if key == "smtp":
            smtp = _smtp_card()
            return {"ok": smtp["configured"], "provider": key, "needs_you": not smtp["configured"],
                "missing": smtp["missing"], "message": smtp["hint"] or "Your email is connected through your saved SMTP settings. Open Find customers and ask me to send your reviewed outreach."}
        missing = local_config.missing(key)
        if missing:
            return {"ok": False, "provider": key, "needs_you": True, "missing": missing,
                    "message": local_config.setup_message(key)}
        try:
            callback = _hub_callback(key)
        except ValueError as exc:
            return {"ok": False, "provider": key, "needs_you": True, "message": str(exc)}
    else:
        callback = return_to or _hub_callback(key)

    if key == "composio":
        ok = bool(local_config.api_key())
        return {
            "ok": ok,
            "provider": key,
            "needs_you": not ok,
            "missing": [] if ok else ["COMPOSIO_API_KEY"],
            "message": None
            if ok
            else local_config.setup_message(key) if is_local() else PLATFORM_COMPOSIO_SETUP,
        }

    if key == "gmail":
        from app.gtm.supymem import composio_gmail
        from app.content.oauth import callbacks
        from app.content.oauth.attempts import OAuthError

        if not is_local() and not composio_gmail.configured():
            return {
                "ok": False,
                "provider": key,
                "needs_you": True,
                "missing": ["COMPOSIO_API_KEY"],
                "message": PLATFORM_COMPOSIO_SETUP,
            }
        try:
            from app.connections.mailboxes import enabled
            options = {"intent": intent, "mailbox_id": mailbox_id} if enabled() else {}
            result = callbacks.start_managed(workspace_id=workspace_id, platform="gmail", return_to=return_to, **options)
        except OAuthError as exc:
            log.warning("connector.hub.connect_rejected", extra={"provider": key, "code": exc.code})
            return {"ok": False, "provider": key, "needs_you": True,
                    "error": exc.code, "message": exc.message}
        redirect = result.get("redirect_url")
        return {
            "ok": bool(redirect),
            "provider": key,
            "redirect_url": redirect,
            "authorize_url": redirect,
            "auth_via": "composio",
            "needs_you": True,
            "note": result.get("note")
            or "Complete Gmail OAuth in the new tab, then Refresh.",
            "open_in_new_tab": True,
            **({"oauth_state": result.get("state"), "intent": intent or "add", "mailbox_id": mailbox_id} if enabled() else {}),
        }

    from app.connections import toolkits

    if key in {"google_calendar", "linkedin", "reddit", "x"} or key in toolkits.TOOLKITS:
        from app.content import composio_social
        from app.content.oauth import callbacks
        from app.content.oauth.attempts import OAuthError

        try:
            if not composio_social.connectable(key):
                if is_local():
                    return {"ok": False, "provider": key, "needs_you": True,
                            "message": local_config.setup_message(key)}
                if key in toolkits.TOOLKITS:
                    return {"ok": False, "provider": key, "needs_you": True,
                            "message": "This connection is not available yet. You can keep working without it."}
                missing: list[str] = []
                if not composio_social.configured():
                    missing = ["COMPOSIO_API_KEY"]
                elif key == "x":
                    missing = [
                        n
                        for n in ("X_CLIENT_ID", "X_CLIENT_SECRET")
                        if not (os.environ.get(n) or "").strip()
                    ]
                    if not missing and not (
                        os.environ.get("COMPOSIO_TWITTER_AUTH_CONFIG_ID") or ""
                    ).strip():
                        missing = ["X_CLIENT_ID", "X_CLIENT_SECRET"]
                return {
                    "ok": False,
                    "provider": key,
                    "needs_you": True,
                    "missing": missing,
                    "message": "This connection is unavailable right now. You can keep working without it or contact support.",
                    "operator_detail": (
                        f"{key} is not connectable yet. "
                        + (
                            "Set " + ", ".join(missing)
                            if missing
                            else "Check Composio auth config."
                        )
                    ),
                }
            # The managed callback binds exact workspace, actor, account and
            # auth config before Supymem accepts the provider connection.
            started = callbacks.start_managed(workspace_id=workspace_id, platform=key,
                                              return_to=return_to if key in toolkits.TOOLKITS else None)
            redirect = started.get("authorize_url") or started.get("redirect_url")
            return {
                "ok": bool(redirect),
                "provider": key,
                "redirect_url": redirect,
                "authorize_url": redirect,
                "auth_via": started.get("auth_via", "composio"),
                "auth_config_id": started.get("auth_config_id"),
                "needs_you": True,
                "note": "Complete authorization, then return here and refresh your connection status.",
                "open_in_new_tab": True,
            }
        except OAuthError as exc:
            log.warning("connector.hub.connect_rejected", extra={"provider": key, "code": exc.code})
            return {"ok": False, "provider": key, "needs_you": True,
                    "error": exc.code, "message": exc.message}

    if key == "discord":
        from app.community import composio_discord as discord
        from app.community import service as community_service

        try:
            result = discord.start_connect(workspace_id, callback=callback)
            if result.get("connection_id"):
                from app.audit.writer import audit
                audit("connections.discord.status", workspace_id=workspace_id,
                      payload={"connected": False, "connection_id": result["connection_id"], "status": "pending"})
            redirect = result.get("redirect_url")
            return {
                "ok": bool(redirect),
                "provider": key,
                "redirect_url": redirect,
                "authorize_url": redirect,
                "connection_id": result.get("connection_id"),
                "auth_config_id": result.get("auth_config_id"),
                "needs_you": True,
                "note": result.get("note"),
                "open_in_new_tab": True,
            }
        except discord.DiscordBotError as exc:
            return {
                "ok": False,
                "provider": key,
                "needs_you": True,
                "error": exc.code,
                "message": exc.message,
            }
        except Exception:  # noqa: BLE001
            # Delegate to community service wrapper for consistent lab logging.
            result = community_service.connect_discord(workspace_id)
            if result.get("connection_id"):
                from app.audit.writer import audit
                audit("connections.discord.status", workspace_id=workspace_id,
                      payload={"connected": False, "connection_id": result["connection_id"], "status": "pending"})
            if result.get("redirect_url"):
                result = {**result, "authorize_url": result.get("redirect_url")}
            return {"provider": key, "open_in_new_tab": True, **result}

    if key == "unipile":
        missing = [
            n
            for n in ("UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID")
            if not (os.environ.get(n) or "").strip()
        ]
        return {
            "ok": not missing,
            "provider": key,
            "needs_you": bool(missing),
            "missing": missing,
            "message": None if not missing else FOUNDER_SETUP["unipile_missing"],
            "operator_detail": (
                None
                if not missing
                else (
                    "Set "
                    + ", ".join(missing)
                    + " in the server environment (do not paste secrets in the browser). "
                    "Optional: UNIPILE_DSN. Then Refresh status."
                )
            ),
            "docs": "https://developer.unipile.com/",
        }

    if key == "hunter":
        missing = [
            n for n in ("HUNTER_API_KEY",) if not (os.environ.get(n) or "").strip()
        ]
        return {
            "ok": not missing,
            "provider": key,
            "needs_you": bool(missing),
            "missing": missing,
            "message": None if not missing else FOUNDER_SETUP["hunter_missing"],
            "operator_detail": None
            if not missing
            else "Set HUNTER_API_KEY in the server environment, then Refresh.",
            "docs": None,
        }

    if key == "google_ads":
        from app.marketing.adapters import ads_google

        miss = ads_google.missing_env()
        return {
            "ok": not miss,
            "provider": key,
            "needs_you": bool(miss),
            "missing": miss,
            "message": None if not miss else FOUNDER_SETUP["google_ads_env"],
            "operator_detail": None
            if not miss
            else "Set " + ", ".join(miss) + " (test MCC). Campaigns always start PAUSED.",
            "docs": "https://developers.google.com/google-ads/api/docs/start",
        }

    if key == "meta_ads":
        from app.marketing.adapters import ads_meta

        miss = ads_meta.missing_env()
        return {
            "ok": not miss,
            "provider": key,
            "needs_you": bool(miss),
            "missing": miss,
            "message": None if not miss else FOUNDER_SETUP["meta_ads_env"],
            "operator_detail": None
            if not miss
            else "Set " + ", ".join(miss) + ". Campaigns always start PAUSED.",
            "docs": "https://developers.facebook.com/docs/marketing-apis/",
        }

    return {
        "ok": False,
        "provider": key,
        "needs_you": True,
        "error": "unknown_provider",
        "message": f"Unknown provider: {provider}",
    }


def sync_provider(workspace_id: str, provider: str) -> dict[str, Any]:
    """Refresh remote account state after OAuth return."""
    key = (provider or "").strip().lower()
    if key == "twitter":
        key = "x"
    if key == "discordbot":
        key = "discord"

    if is_local():
        missing = local_config.missing(key)
        if missing:
            return {"ok": False, "provider": key, "needs_you": True, "missing": missing,
                    "message": local_config.setup_message(key)}

    from app.connections import toolkits

    if key in toolkits.TOOLKITS:
        from app.content.composio_social import ComposioSocialError
        from app.content.oauth.attempts import initiating_actor

        initiating_actor(workspace_id)
        if not toolkits.auth_config_id(key):
            return {"ok": False, "provider": key, "message": "This connection is not available yet."}
        try:
            toolkits.sync(workspace_id, key)
        except ComposioSocialError:
            return {"ok": False, "provider": key, "message": "Your connection could not be checked. Try again."}
        return {"ok": True, "provider": key}

    if key in {"google_calendar", "linkedin", "reddit", "x", "content"}:
        from app.content import composio_social
        from app.content.oauth import flow

        synced: list[dict[str, Any]] = []
        if composio_social.configured():
            try:
                from app.credits import metered

                with metered.charge_sync(workspace_id=workspace_id, purpose="connections.status", provider="composio", estimate_micro=0, payer="founder") as meter:
                    if is_local() and key == "content":
                        for platform in composio_social.AUTH_CONFIG_ENV:
                            if not local_config.missing(platform):
                                synced.extend(composio_social.sync_workspace(workspace_id, platform=platform))
                    else:
                        synced = composio_social.sync_workspace(workspace_id, platform=None if key == "content" else key)
                    meter.record_micro(0)
            except composio_social.ComposioSocialError as exc:
                return {
                    "ok": False,
                    "provider": key,
                    "error": exc.code,
                    "message": exc.message,
                }
        return {
            "ok": True,
            "provider": key,
            "synced": synced,
            "accounts": flow.list_accounts(workspace_id),
        }

    if key == "gmail":
        from app.gtm.supymem.pipeline import mail_readiness, sync_gmail_accounts

        try:  # pull a freshly authorized account in even if the browser never reached our callback
            sync_gmail_accounts(workspace_id)
        except Exception:  # noqa: BLE001 - readiness below still reports what is saved
            log.warning("connector.hub.gmail_sync_failed", exc_info=True)
        return {"ok": True, "provider": key, "mail": mail_readiness(workspace_id)}

    if key == "discord":
        return {
            "ok": True,
            "provider": key,
            "status": refresh_discord_status(workspace_id),
        }

    # Env-only providers: just re-read status.
    return {"ok": True, "provider": key, "status": hub_status(workspace_id)}
