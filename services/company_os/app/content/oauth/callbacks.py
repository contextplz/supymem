"""Narrow public callback contracts for native and brokered authorization.

Composio owns its provider OAuth exchange and token lifecycle. Supymem owns the
signed founder intent and immutable connected-account receipt. Callback query
parameters never select a workspace, a user, an account, or a return URL.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Any
from urllib.parse import urlencode, urlparse, quote

from fastapi import HTTPException, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.content.oauth import attempts
from app.connections.toolkits import TOOLKITS
from app.content.oauth.attempts import OAuthError
from app.tenancy import bind

log = logging.getLogger(__name__)
PLATFORMS = {"google_calendar", "linkedin", "reddit", "x", "gmail"} | TOOLKITS.keys()


def start_managed(*, workspace_id: str, platform: str, return_to: str | None = None,
                  intent: str | None = None, mailbox_id: str | None = None) -> dict[str, str]:
    from app.content.oauth.flow import _safe_return_to, default_redirect_uri, public_base_url
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    from app.credits import metered

    if platform not in PLATFORMS:
        raise OAuthError("unsupported_provider", "This provider is not supported.")
    from app.connections import local_config
    from app.core.local_mode import is_local

    if is_local() and local_config.missing(platform):
        raise OAuthError("connection_settings_required", local_config.setup_message(platform))

    gmail_options = {}
    if platform == "gmail":
        from app.connections.mailboxes import enabled
        if enabled():
            if intent not in {None, "add", "reconnect"} or (intent != "reconnect" and mailbox_id is not None):
                raise OAuthError("gmail_intent_invalid", "Choose an inbox to reconnect, or add another inbox.")
            if intent == "reconnect":
                from app.db.models import GtmEmailAccount
                from app.gtm.supymem.pipeline import _gmail_workspace_ids
                from app.state import db
                with db()() as session:
                    account = session.get(GtmEmailAccount, mailbox_id) if mailbox_id else None
                    if (account is None or account.provider != "gmail" or account.status == "revoked"
                            or account.workspace_id not in _gmail_workspace_ids(session, workspace_id)):
                        raise OAuthError("mailbox_unavailable", "This inbox is unavailable. Choose one of your inboxes.")
                    attempts.authorize_actor(account.workspace_id, attempts.initiating_actor(workspace_id))
            gmail_options = {"gmail_intent": intent or "add", "gmail_mailbox_id": mailbox_id}
    callback = (f"{public_base_url()}/api/v1/ai-gtm/gmail/callback" if platform == "gmail"
                else default_redirect_uri(platform))
    state = attempts.create(workspace_id=workspace_id, platform=platform,
                            redirect_uri=callback, return_to=_safe_return_to(return_to, workspace_id=workspace_id), auth_method="composio", **gmail_options)
    # Do not collide with state supplied by the provider/broker's own protocol.
    callback += "?" + urlencode({"supymem_state": state})
    try:
        with metered.charge_sync(workspace_id=workspace_id, purpose="connections.connect", provider="composio", estimate_micro=0, payer="founder") as meter:
            if platform == "gmail":
                result = composio_gmail.create_gmail_connection(composio_gmail.user_id_for(workspace_id), callback=callback)
                url = result["redirect_url"]
                user_id, connection_id = result["app_user_id"], result["connection_id"]
            else:
                result = composio_social.start_connect(workspace_id=workspace_id, platform=platform, callback=callback)
                url = result["authorize_url"]
                user_id, connection_id = result["composio_user_id"], result["connected_account_id"]
            meter.record_micro(0)
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise OAuthError("invalid_connect_link", "Provider did not return a secure Connect Link.")
        attempts.bind_connection(state, user_id=user_id, auth_config_id=result["auth_config_id"], connection_id=connection_id)
    except OAuthError:
        raise
    except Exception as exc:
        log.warning("connector.oauth.start_failed", extra={"provider": platform, "error_type": type(exc).__name__})
        message = (local_config.failure_message(platform) if is_local()
                   else "Authorization provider is unavailable. Try connecting again.")
        raise OAuthError("provider_unavailable", message) from exc
    return {"authorize_url": str(url), "redirect_url": str(url), "state": state,
            "auth_via": "composio", "platform": platform, "redirect_uri": callback}


def _verified_connection(attempt: dict[str, Any], *, active_only: bool = True) -> dict[str, Any]:
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    from app.credits import metered

    platform, account_id = attempt["platform"], attempt["connected_account_id"]
    path = "/connected_accounts/" + quote(account_id, safe="")
    with metered.charge_sync(workspace_id=attempt["workspace_id"], purpose="connections.status", provider="composio", estimate_micro=0, payer="founder") as meter:
        data = (composio_gmail._request("GET", path) if platform == "gmail"
                else composio_social._request("GET", path))
        meter.record_micro(0)
    toolkit = data.get("toolkit") or {}
    auth = data.get("auth_config") or {}
    expected_toolkit = "gmail" if platform == "gmail" else composio_social.toolkit_for(platform)
    user = data.get("user_id") or data.get("entity_id") or data.get("userId")
    # New Composio versions omit user_id. Ownership then derives exclusively
    # from the exact account ID returned to our authenticated start request.
    if (data.get("id") != account_id or toolkit.get("slug") != expected_toolkit
            or auth.get("id") != attempt["auth_config_id"] or auth.get("is_disabled")
            or (active_only and str(data.get("status") or "").upper() != "ACTIVE")
            or (user is not None and user != attempt["provider_user_id"])
            or str((data.get("experimental") or {}).get("account_type", "PRIVATE")).upper() != "PRIVATE"):
        raise OAuthError("connection_mismatch", "The provider did not verify the requested connection.")
    # Only these metadata fields may enter the sync layer. Never provider tokens.
    return {"id": account_id, "status": str(data.get("status") or "UNKNOWN").upper(), "platform": platform,
            "toolkit": expected_toolkit, "user_id": attempt["provider_user_id"], "auth_config_id": attempt["auth_config_id"]}


def known_connections(workspace_id: str, *, platform: str | None = None, active_only: bool = True,
                      exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """Read omitted-user accounts only through authenticated immutable bindings."""
    from app.gtm.supymem.composio_gmail import DefiniteFailure
    from app.content.composio_social import ComposioSocialError
    from app.connections.composio_migration import mark_gmail_reconnect, mark_social_reconnect, missing_connection
    from app.db.models import GtmEmailAccount, GtmEmailEvent, ContentAccount
    from app.state import db
    import json
    from sqlalchemy import select
    excluded = set(exclude or set())
    with db()() as session:
        excluded.update(session.scalars(select(GtmEmailAccount.connected_account_id).where(
            GtmEmailAccount.workspace_id == workspace_id, GtmEmailAccount.status == "needs_reconnect")))
        for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.workspace_id == workspace_id,
                GtmEmailEvent.event_type == "composio_project")):
            migration = json.loads(event.metadata_json or "{}")
            if migration.get("previous_connected_account_id") != migration.get("connected_account_id"):
                excluded.add(migration.get("previous_connected_account_id"))
        excluded.update(json.loads(row.extra_json or "{}").get("connected_account_id") for row in session.scalars(
            select(ContentAccount).where(ContentAccount.workspace_id == workspace_id, ContentAccount.status == "needs_reconnect")))
    out = []
    for receipt in attempts.successful_receipts(workspace_id, platform=platform):
        if receipt["platform"] in TOOLKITS:
            continue
        if receipt["connected_account_id"] in excluded:
            continue
        try:
            out.append(_verified_connection(receipt, active_only=active_only))
        except (DefiniteFailure, ComposioSocialError) as exc:
            if not missing_connection(exc):
                raise
            if receipt["platform"] == "gmail":
                mark_gmail_reconnect(receipt["connected_account_id"])
            else:
                mark_social_reconnect(receipt["connected_account_id"])
            if not active_only and receipt["platform"] == "gmail":
                out.append({"id": receipt["connected_account_id"], "user_id": receipt["provider_user_id"], "status": "DELETED"})
        except OAuthError:
            continue  # Revoked, shared or reassigned accounts must not be imported.
    return out


def _complete_managed(attempt: dict[str, Any]) -> None:
    if attempt["platform"] == "gmail":
        from app.connections.mailboxes import enabled
        if enabled() and attempt.get("gmail_intent") == "reconnect":
            from app.db.models import GtmEmailAccount
            from app.gtm.supymem.pipeline import _gmail_workspace_ids
            from app.state import db
            with db()() as session:
                account = session.get(GtmEmailAccount, attempt.get("gmail_mailbox_id")) if attempt.get("gmail_mailbox_id") else None
                if (account is None or account.provider != "gmail" or account.status == "revoked"
                        or account.workspace_id not in _gmail_workspace_ids(session, attempt["workspace_id"])):
                    raise OAuthError("mailbox_unavailable", "This inbox is unavailable. Choose one of your inboxes.")
                attempts.authorize_actor(account.workspace_id, attempt["actor_subject"])
    connection = _verified_connection(attempt)
    if attempt["platform"] in TOOLKITS:
        return
    if attempt["platform"] == "gmail":
        from app.gtm.supymem.pipeline import sync_gmail_accounts
        from app.connections.mailboxes import enabled
        options = {"intent": attempt.get("gmail_intent"), "mailbox_id": attempt.get("gmail_mailbox_id")} if enabled() else {}
        result = sync_gmail_accounts(attempt["workspace_id"], connections=[connection], **options)
        if result["synced"] != 1:
            raise OAuthError("sync_failed", "The connected Gmail account could not be saved.")
    else:
        from app.content.composio_social import sync_workspace
        result = sync_workspace(attempt["workspace_id"], platform=attempt["platform"], connections=[connection])
        if len(result) != 1:
            raise OAuthError("sync_failed", "The connected account could not be saved.")


def callback_response(platform: str, request: Request) -> Response:
    from app.content.oauth import flow
    from app.core.local_mode import is_local

    local = is_local()
    retry = "Open Connections and reconnect this app to get a new authorization link."
    next_step = {"message": retry} if local else {}
    params = request.query_params
    if platform not in PLATFORMS or any(len(params.getlist(k)) > 1 for k in ("state", "supymem_state", "code", "error")):
        raise HTTPException(400, detail={"code": "invalid_callback", **next_step})
    managed, native = params.get("supymem_state"), params.get("state")
    if bool(managed) == bool(native):
        raise HTTPException(400, detail={"code": "invalid_state", **next_step})
    try:
        attempt = attempts.consume(managed or native or "", platform)
        if (attempt["auth_method"] == "composio") != bool(managed):
            raise OAuthError("invalid_method", "Authorization method does not match.")
        target = flow._safe_return_to(attempt["return_to"], workspace_id=attempt["workspace_id"])
    except OAuthError as exc:
        raise HTTPException(400, detail={"code": exc.code, **next_step}) from exc
    status = "failed"
    with bind(workspace_id=attempt["workspace_id"], venture_id="", actor=attempt["actor_subject"]):
        try:
            if not params.get("error"):
                if managed:
                    _complete_managed(attempt)
                elif params.get("code"):
                    flow.complete_attempt(attempt, code=params["code"])
                else:
                    raise OAuthError("missing_code", "Provider did not supply an authorization code.")
                status = "connected"
        except Exception as exc:
            # Never log provider bodies, codes, state, secrets or profile values.
            log.warning("connector.oauth.callback_failed", extra={"provider": platform,
                        "error_type": type(exc).__name__, "workspace_id": attempt["workspace_id"]})
    attempts.record_outcome(attempt["id"], status)
    log.info("connector.oauth.completed", extra={"provider": platform, "outcome": status,
             "workspace_id": attempt["workspace_id"], "actor_subject": attempt["actor_subject"]})
    separator = "&" if "?" in target else "?"
    result = target + separator + urlencode({"provider": platform, "oauth": status})
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if not local:
        return RedirectResponse(result, status_code=303, headers=headers)
    href = escape(result, quote=True)
    message = "Your app is connected. Open Connections to see it." if status == "connected" else retry
    headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    return HTMLResponse(f'<!doctype html><html lang="en"><meta charset="utf-8">'
                        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                        f'<meta name="color-scheme" content="light"><title>Connections</title>'
                        f'<main><h1>Connections</h1><p>{message}</p><a href="{href}">Open Connections</a></main></html>',
                        headers=headers)
