"""Composio Gmail REST client — connect, list, send, reply, fetch.

Python port of vendor/research/email-supymem/lib/composio/gmail.ts.
Uses Composio HTTP v3.1 so we do not need the Node SDK.
"""

from __future__ import annotations

import logging
import os
import json
from datetime import datetime, timedelta
import re
from pathlib import Path
from typing import Any
from app.connections.composio_pacing import AdmissionLimited
from app.core.local_mode import is_local

import httpx

log = logging.getLogger(__name__)

COMPOSIO_BASE = os.environ.get("COMPOSIO_API_BASE", "https://backend.composio.dev/api/v3.1")
TOOL_TIMEOUT_S = 60

_AUTH_CONFIG_CACHE = (
    Path(__file__).resolve().parents[4] / "data" / "composio_gmail_auth_config_id"
)
AUTH_CONFIG_ENV = "COMPOSIO_GMAIL_AUTH_CONFIG_ID"


class DefiniteFailure(RuntimeError):
    """Composio refused the call before Gmail could act (4xx, or an auth/permission error), so nothing was sent."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ReconnectRequired(DefiniteFailure):
    def __init__(self, message: str = "gmail_reconnect_required"):
        super().__init__(message)


_DEFINITE = re.compile(r"auth|credential|token|expired|revoked|permission|scope|forbidden|unauthori[sz]ed|not connected|invalid[_ ]grant", re.I)
# Composio's own successful:false means Gmail refused (bad recipient, invalid header): nothing was sent.
# Only a timeout or server-side error inside that report leaves delivery genuinely uncertain.
_UNCERTAIN = re.compile(r"time ?out|timed out|unavailable|internal|\b5\d\d\b|connection|reset by peer|gateway", re.I)
_RECONNECT = re.compile(r"invalid[_ -]grant|(?:token|credential|authorization|authentication).*(?:expired|revoked|invalid)|(?:expired|revoked|invalid).*(?:token|credential)|login required|reauth|failed to refresh|refresh.*fail", re.I)


def configured() -> bool:
    if is_local():
        from app.gtm.local_mail import configured as local_configured
        return local_configured()
    # Auth config can be auto-created; API key alone is enough to start connect.
    return bool(os.environ.get("COMPOSIO_API_KEY", "").strip())


def default_user_id(workspace_id: str | None = None) -> str:
    """Use the established Gmail tenant identity, with a legacy local fallback."""
    wid = (workspace_id or "").strip()
    if wid:
        return user_id_for(wid)
    explicit = os.environ.get("SUPYMEM_DEFAULT_USER_ID", "").strip()
    if explicit and explicit.lower() not in {"local-user", "default"}:
        return explicit
    return "wsp_local"


def user_id_for(workspace_id: str) -> str:
    """Hosted Gmail connections never share the deployment's legacy user ID."""
    return f"gmail:{workspace_id}"


def _api_key() -> str:
    if is_local():
        from app.connections import local_config
        if local_config.missing("composio"):
            raise RuntimeError(local_config.setup_message("composio"))
        return local_config.api_key()
    key = os.environ.get("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COMPOSIO_API_KEY is not configured.")
    return key


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key(), "Content-Type": "application/json"}


def _toolkit_version() -> str:
    return os.environ.get("GMAIL_TOOLKIT_VERSION", "latest").strip() or "latest"


def callback_url() -> str:
    explicit = os.environ.get("COMPOSIO_GMAIL_CALLBACK_URL", "").strip()
    if explicit:
        return explicit
    origin = (
        os.environ.get("CONSOLE_URL")
        or os.environ.get("NEXT_PUBLIC_APP_URL")
        or "http://127.0.0.1:3100"
    ).rstrip("/")
    return f"{origin}/gtm"


def _request(method: str, path: str, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None, timeout: float = 45.0) -> dict[str, Any]:
    from app.connections import composio_pacing
    from app.connections import local_config
    if is_local():
        provider = (local_config.toolkit_for_tool(path.rsplit("/", 1)[-1]) if path.startswith("/tools/")
                    else str((params or {}).get("toolkit_slug") or "composio"))
    account = composio_pacing.account_key(path, json, params)
    composio_pacing.acquire(account)
    url = f"{COMPOSIO_BASE}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            res = client.request(method, url, headers=_headers(), json=json, params=params)
    except httpx.RequestError:
        if is_local():
            raise RuntimeError(local_config.failure_message(provider)) from None
        raise
    if res.status_code == 429:
        composio_pacing.reject(account, res.headers.get("Retry-After"))
    try:
        data = res.json()
    except Exception:
        if is_local():
            if 400 <= res.status_code < 500 and res.status_code != 408:
                raise DefiniteFailure(local_config.failure_message(provider), status_code=res.status_code) from None
            raise RuntimeError(local_config.failure_message(provider)) from None
        data = {"raw": res.text[:500]}
    if res.status_code >= 400:
        msg = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("slug") or "")
            else:
                msg = str(err or data.get("message") or data.get("detail") or "")
        text = msg or f"Composio {path} returned HTTP {res.status_code}."
        if is_local():
            from app.connections.composio_migration import missing_connection
            if missing_connection(DefiniteFailure(text, status_code=res.status_code)):
                text = "Your connected account was not found. "
            elif _RECONNECT.search(text):
                text = "Your authorization is invalid. "
            elif re.search(r"tool.*not found", text, re.I):
                text = "Your tool was not found. "
            else:
                text = "I could not confirm your request finished. "
            text += local_config.failure_message(provider)
        # A 4xx (other than 408) was rejected before execution; 5xx and timeouts leave the outcome unknown.
        if 400 <= res.status_code < 500 and res.status_code != 408:
            raise DefiniteFailure(text, status_code=res.status_code)
        raise RuntimeError(text)
    composio_pacing.check_response(account, data, res.headers.get("Retry-After"))
    return data if isinstance(data, dict) else {"data": data}


def _read_auth_config_cache() -> str:
    try:
        if _AUTH_CONFIG_CACHE.is_file():
            cached = json.loads(_AUTH_CONFIG_CACHE.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("project") == (os.environ.get("COMPOSIO_PROJECT", "v1").strip() or "v1"):
                return str(cached.get("id") or "")
    except (OSError, ValueError):
        pass
    return ""


def _write_auth_config_cache(auth_id: str) -> None:
    if not auth_id:
        return
    try:
        _AUTH_CONFIG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_CONFIG_CACHE.write_text(json.dumps({"id": auth_id.strip(),
            "project": os.environ.get("COMPOSIO_PROJECT", "v1").strip() or "v1"}), encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist Gmail auth config id: %s", exc)


def ensure_auth_config() -> str:
    """Return a live Gmail auth config id — env, cache, list, or create managed."""
    from app.connections import local_config
    from app.core.local_mode import is_local

    if is_local():
        if local_config.missing("gmail"):
            raise RuntimeError(local_config.setup_message("gmail"))
        return local_config.auth_config_id("gmail")

    explicit = os.environ.get(AUTH_CONFIG_ENV, "").strip()
    if explicit:
        return explicit

    def _list_ids() -> list[str]:
        listed = _request(
            "GET",
            "/auth_configs",
            params={"toolkit_slug": "gmail", "limit": 20},
        )
        items = listed.get("items") or listed.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or []
        ids: list[str] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id"):
                status = str(item.get("status") or "").lower()
                if status in {"", "enabled", "active"}:
                    ids.append(str(item["id"]))
        return ids

    existing = (
        os.environ.get(AUTH_CONFIG_ENV, "").strip() or _read_auth_config_cache()
    )
    known = _list_ids()
    if existing and existing in known:
        _write_auth_config_cache(existing)
        return existing
    if existing and existing not in known:
        log.warning("ignoring stale Gmail auth config id")
    if known:
        _write_auth_config_cache(known[0])
        return known[0]

    created = _request(
        "POST",
        "/auth_configs",
        json={
            "toolkit": {"slug": "gmail"},
            "auth_config": {
                "type": "use_composio_managed_auth",
                "name": "Company OS Community, Gmail",
            },
        },
    )
    auth = created.get("auth_config") if isinstance(created.get("auth_config"), dict) else {}
    auth_id = str(auth.get("id") or created.get("id") or "")
    if not auth_id:
        raise RuntimeError(
            "Composio did not return a Gmail auth config. "
            "Create one in the dashboard and set COMPOSIO_GMAIL_AUTH_CONFIG_ID."
        )
    _write_auth_config_cache(auth_id)
    return auth_id


def create_gmail_connection(
    app_user_id: str | None = None,
    *,
    workspace_id: str | None = None,
    callback: str | None = None,
) -> dict[str, Any]:
    # app_user_id is server-selected by managed OAuth, not raw request input.
    # Callers with a workspace must agree with its canonical identity.
    user_id = default_user_id(workspace_id) if workspace_id else (app_user_id or default_user_id())
    if workspace_id and app_user_id is not None and app_user_id != user_id:
        raise ValueError("Gmail identity must belong to the selected workspace.")
    auth_config_id = ensure_auth_config()
    cb = (callback or "").strip() or callback_url()
    payload = {
        "auth_config_id": auth_config_id,
        "user_id": user_id,
        "callback_url": cb,
        "allow_multiple": True,
    }
    data = _request("POST", "/connected_accounts/link", json=payload)
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    redirect = (
        data.get("redirect_url")
        or data.get("redirectUrl")
        or nested.get("redirect_url")
        or nested.get("redirectUrl")
    )
    conn_id = data.get("connected_account_id") or nested.get("connected_account_id")
    if not redirect or not conn_id:
        raise RuntimeError("Composio did not return a bound Gmail Connect Link.")
    return {
        "connection_id": conn_id,
        "redirect_url": redirect,
        "app_user_id": user_id,
        "auth_config_id": auth_config_id,
        "needs_you": True,
        "note": "Complete Gmail OAuth, then Refresh mail status.",
    }


def list_gmail_connections(
    app_user_id: str | None = None,
    *,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    user_id = default_user_id(workspace_id) if workspace_id else (app_user_id or default_user_id())
    # Toolkit-wide list then filter — combined filters have dropped ACTIVE rows.
    data = _request(
        "GET",
        "/connected_accounts",
        params={"toolkit_slugs": "gmail", "limit": 100},
    )
    items = data.get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        entity = str(it.get("user_id") or it.get("entity_id") or it.get("userId") or "")
        if entity != user_id:
            continue
        out.append(it)
    if out:
        return out
    data = _request(
        "GET",
        "/connected_accounts",
        params={"user_ids": user_id, "toolkit_slugs": "gmail"},
    )
    items = data.get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    return [it for it in items if isinstance(it, dict)
            and str(it.get("user_id") or it.get("entity_id") or it.get("userId") or "") == user_id]


def execute_tool(
    slug: str,
    *,
    user_id: str,
    connected_account_id: str,
    arguments: dict[str, Any],
    raise_on_error: bool = True,
    toolkit_version: str | None = None,
) -> dict[str, Any]:
    mail = slug.startswith("GMAIL_")
    if is_local() and mail:
        from app.gtm.local_mail import check_binding, read_tool
        check_binding(user_id, connected_account_id)
        return {"successful": True, "data": read_tool(slug, arguments)}
    if is_local():
        from app.connections import local_config
        from app.content import composio_social
        toolkit = local_config.toolkit_for_tool(slug)
        if local_config.missing(toolkit):
            raise DefiniteFailure(local_config.setup_message(toolkit))
        toolkit_version = toolkit_version or composio_social._toolkit_version(toolkit)
    version = toolkit_version or _toolkit_version()
    body: dict[str, Any] = {
        "user_id": user_id,
        "connected_account_id": connected_account_id,
        "arguments": arguments,
        "version": version,
    }
    if version == "latest":
        body["dangerously_skip_version_check"] = True
    from app.connections.composio_migration import mark_gmail_reconnect, missing_connection
    from app.connections.toolkits import mark_reconnect
    mark = mark_gmail_reconnect if mail else mark_reconnect
    reconnect = (f"Reconnect your {local_config.app_name(toolkit)} account on the Connections screen, then try again."
                 if is_local() else "gmail_reconnect_required")
    try:
        data = _request("POST", f"/tools/execute/{slug}", json=body, timeout=TOOL_TIMEOUT_S)
    except DefiniteFailure as exc:
        if exc.status_code == 404 and slug in {"GMAIL_LIST_HISTORY", "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "GMAIL_FETCH_MESSAGE_BY_THREAD_ID"} and not re.search(r"connected[_ ]?account|\bconnection\b", str(exc), re.I):
            raise
        if missing_connection(exc) or _RECONNECT.search(str(exc)):
            mark(connected_account_id)
            raise ReconnectRequired(reconnect) from None
        raise
    successful = data.get("successful")
    if successful is False and (missing_connection(RuntimeError(str(data.get("error") or "")))
            or _RECONNECT.search(str(data.get("error") or ""))
            or re.search(r"unauthori[sz]ed|unauthenticated", str(data.get("error") or ""), re.I)):
        mark(connected_account_id)
        raise ReconnectRequired(reconnect)
    if successful is False and raise_on_error:
        text = str(data.get("error") or f"{slug} failed.")
        failure = DefiniteFailure if _DEFINITE.search(text) or not _UNCERTAIN.search(text) else RuntimeError
        raise failure(local_config.failure_message(toolkit) if is_local() else text)
    if successful is False and is_local():
        data["error"] = local_config.failure_message(toolkit)
    return data


def get_gmail_profile(composio_user_id: str, connected_account_id: str) -> dict[str, Any]:
    result = execute_tool(
        "GMAIL_GET_PROFILE",
        user_id=composio_user_id,
        connected_account_id=connected_account_id,
        arguments={},
    )
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    email = None
    if isinstance(payload, dict):
        email = payload.get("emailAddress") or payload.get("email")
    return {"email": email if isinstance(email, str) else None, "raw": result}


def verify_gmail_profile(composio_user_id: str, connected_account_id: str) -> dict[str, Any]:
    """Execute one read-only provider call and return only bounded proof fields."""
    if is_local():
        from app.gtm.local_mail import check_binding, verify
        check_binding(composio_user_id, connected_account_id)
        return verify()
    result = execute_tool(
        "GMAIL_GET_PROFILE",
        user_id=composio_user_id,
        connected_account_id=connected_account_id,
        arguments={},
        raise_on_error=False,
    )
    payload = result.get("data") if isinstance(result.get("data"), dict) else {}
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    email = payload.get("emailAddress") or payload.get("email")
    log_id = result.get("log_id")
    return {
        "email": email if isinstance(email, str) and "@" in email and len(email) <= 320 else None,
        "log_id": log_id if isinstance(log_id, str) and 0 < len(log_id) <= 128 else None,
        "successful": result.get("successful") is True,
    }


def _payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    for key in ("data", "response_data"):
        if isinstance(result.get(key), dict):
            return _payload(result[key])
    return result


def message_headers(message: dict) -> dict[str, str]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = message.get("headers") or payload.get("headers") or {}
    pairs = headers.items() if isinstance(headers, dict) else ((item.get("name"), item.get("value")) for item in headers if isinstance(item, dict))
    return {str(name).casefold(): str(value or "") for name, value in pairs}


def fetch_message(*, composio_user_id: str, connected_account_id: str, message_id: str) -> dict:
    return _payload(execute_tool("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", user_id=composio_user_id,
        connected_account_id=connected_account_id, arguments={"message_id": message_id, "format": "full"}))


def fetch_thread(*, composio_user_id: str, connected_account_id: str, thread_id: str) -> dict:
    return _payload(execute_tool("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", user_id=composio_user_id,
        connected_account_id=connected_account_id, arguments={"thread_id": thread_id, "format": "full"}))


def fetch_history(*, composio_user_id: str, connected_account_id: str, start_history_id: str, page_token: str | None = None) -> dict:
    arguments = {"start_history_id": start_history_id, "history_types": ["messageAdded"], "max_results": 500}
    if page_token:
        arguments["page_token"] = page_token
    return _payload(execute_tool("GMAIL_LIST_HISTORY", user_id=composio_user_id,
        connected_account_id=connected_account_id, arguments=arguments))


def mailbox_slots_used_today(session, account, *, exclude_draft_id: str | None = None) -> int:
    from sqlalchemy import func, select
    from app.db.models import GtmEmailAccount, GtmEmailEvent, GtmTouch, Venture, VentureDocument
    from app.gtm.supymem.pipeline import _active_mailbox, _gmail_workspace_ids
    from core.clock import utcnow

    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    sender = (account.email or "").strip().casefold()
    accounts = list(session.scalars(select(GtmEmailAccount).where(func.lower(func.trim(GtmEmailAccount.email)) == sender)))
    from app.connections import mailboxes
    if mailboxes.enabled():
        return _send_slots_used_today(session, [row.workspace_id for row in accounts],
            mailbox_ids={row.id for row in accounts}, exclude_draft_id=exclude_draft_id)
    events = list(session.scalars(select(GtmEmailEvent).where(
        GtmEmailEvent.email_account_id.in_([row.id for row in accounts]), GtmEmailEvent.created_at >= start,
        GtmEmailEvent.event_type.in_(["gmail_reserved", "gmail_sent", "gmail_released"]))))
    released = {event.idempotency_key for event in events if event.event_type == "gmail_released"}
    held = [event for event in events if event.event_type == "gmail_reserved" and "gmail-release:" + event.id not in released]
    reserved_drafts = {json.loads(event.metadata_json or "{}").get("draft_id") for event in held}
    recorded_messages = set()
    recorded_touches = set()
    for event in events:
        if event.event_type == "gmail_sent":
            receipt = json.loads(event.metadata_json or "{}")
            recorded_messages.update(value for value in (receipt.get("provider_message_id"), receipt.get("rfc_message_id")) if value)
            if receipt.get("touch_id"):
                recorded_touches.add(receipt["touch_id"])
    workspace_ids = []
    for workspace_id in _gmail_workspace_ids(session, account.workspace_id):
        selected = _active_mailbox(session, workspace_id)
        if selected and (selected.email or "").strip().casefold() == sender:
            workspace_ids.append(workspace_id)
    touches = session.scalars(select(GtmTouch).where(GtmTouch.workspace_id.in_(workspace_ids),
        GtmTouch.direction == "out", GtmTouch.channel == "email", GtmTouch.simulated == 0, GtmTouch.sent_at >= start))
    legacy = [touch for touch in touches if touch.message_id not in recorded_messages and touch.id not in recorded_touches]
    legacy_outreach = sum((touch.logged_by or "").startswith("outreach:") for touch in legacy)
    pending = 0
    # ponytail: scan legacy drafts, index mailbox/draft reservations if history grows.
    documents = session.scalars(select(VentureDocument).join(Venture, Venture.id == VentureDocument.venture_id).where(
        Venture.workspace_id.in_(workspace_ids), VentureDocument.kind == "outreach_draft"))
    for document in documents:
        draft = json.loads(document.provenance_json or "{}")
        if draft.get("state") not in {"delivery_pending", "delivery_unconfirmed"} or document.id in reserved_drafts or document.id == exclude_draft_id:
            continue
        try:
            pending += datetime.fromisoformat(draft["delivery_reserved_at"]).replace(tzinfo=None) >= start
        except (KeyError, TypeError, ValueError):
            pending += 1
    counters = sum(int(row.sent_today or 0) for row in accounts if row.sent_count_date == start.date().isoformat())
    return len(held) + pending + max(counters + legacy_outreach, len(legacy))


def _send_slots_used_today(session, workspace_ids, *, mailbox_ids=None, venture_id=None, exclude_draft_id=None):
    from sqlalchemy import select
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmTouch, Venture, VentureDocument
    from core.clock import utcnow
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    document_query = select(VentureDocument).join(Venture, Venture.id == VentureDocument.venture_id).where(
        VentureDocument.kind == "outreach_draft")
    if mailbox_ids is None:
        document_query = document_query.where(Venture.workspace_id.in_(workspace_ids))
    # shortcut: per-mailbox quotas scan saved drafts, add mailbox-indexed draft storage when history grows.
    documents = list(session.scalars(document_query))
    drafts = {document.id: json.loads(document.provenance_json or "{}") for document in documents
        if (venture_id is None or document.venture_id == venture_id)}
    contact_ids = set(session.scalars(select(GtmContact.id).where(GtmContact.source.like(f"venture:{venture_id}:%")))) if venture_id else set()
    event_scope = GtmEmailEvent.email_account_id.in_(mailbox_ids) if mailbox_ids is not None else GtmEmailEvent.workspace_id.in_(workspace_ids)
    events = list(session.scalars(select(GtmEmailEvent).where(event_scope,
        GtmEmailEvent.created_at >= start, GtmEmailEvent.event_type.in_(
            ["gmail_reserved", "gmail_sent", "gmail_released", "sent_metadata"]))))
    released = {event.idempotency_key for event in events if event.event_type == "gmail_released"}
    scoped = [(event, json.loads(event.metadata_json or "{}")) for event in events
        if (mailbox_ids is None or event.email_account_id in mailbox_ids)]
    scoped = [(event, metadata) for event, metadata in scoped if venture_id is None or
        metadata.get("venture_id") == venture_id or metadata.get("draft_id") in drafts or event.contact_id in contact_ids]
    held = [(event, metadata) for event, metadata in scoped
        if event.event_type == "gmail_reserved" and "gmail-release:" + event.id not in released]
    reserved_drafts = {metadata.get("draft_id") for _, metadata in held}
    receipts = []
    for event, metadata in scoped:
        if event.event_type not in {"gmail_sent", "sent_metadata"}:
            continue
        if event.event_type == "sent_metadata" and metadata.get("sent_at"):
            try:
                if datetime.fromisoformat(metadata["sent_at"]).replace(tzinfo=None) < start:
                    continue
            except (TypeError, ValueError):
                pass
        receipts.append((event, metadata))
    messages = {(event.email_account_id, value) for event, metadata in receipts
        for value in (event.provider_message_id, metadata.get("rfc_message_id")) if value}
    touch_ids = {metadata.get("touch_id") for _, metadata in receipts}
    touch_accounts = {json.loads(event.metadata_json or "{}").get("touch_id"): event.email_account_id
        for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type == "sent_metadata",
            GtmEmailEvent.created_at >= start))}
    history_accounts = {}
    for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.event_type.in_(["sent", "SENT", "gmail_sent"]),
            GtmEmailEvent.created_at >= start)):
        metadata = json.loads(event.metadata_json or "{}")
        for message_id in (event.provider_message_id, metadata.get("rfc_message_id")):
            if message_id and event.email_account_id:
                history_accounts[(event.contact_id, message_id)] = event.email_account_id
    if mailbox_ids is not None:
        workspace_ids = set(workspace_ids) | {event.workspace_id for event in events if event.workspace_id}
    touches = session.scalars(select(GtmTouch).where(GtmTouch.workspace_id.in_(workspace_ids),
        GtmTouch.direction == "out", GtmTouch.channel == "email", GtmTouch.simulated == 0, GtmTouch.sent_at >= start))
    legacy = 0
    for touch in touches:
        if venture_id is not None and touch.venture_id != venture_id and touch.logged_by != f"outreach:{venture_id}":
            continue
        if touch.id in touch_ids:
            continue
        account_id = (getattr(touch, "email_account_id", None) or touch_accounts.get(touch.id)
                      or history_accounts.get((touch.contact_id, touch.message_id)))
        if mailbox_ids is not None and account_id not in mailbox_ids:
            continue
        if account_id and (account_id, touch.message_id) in messages:
            continue
        legacy += 1
    pending = 0
    for document_id, draft in drafts.items():
        if (draft.get("state") not in {"delivery_pending", "delivery_unconfirmed"}
                or document_id in reserved_drafts or document_id == exclude_draft_id
                or mailbox_ids is not None and draft.get("mailbox_id") not in mailbox_ids):
            continue
        try:
            pending += datetime.fromisoformat(draft["delivery_reserved_at"]).replace(tzinfo=None) >= start
        except (KeyError, TypeError, ValueError):
            pending += 1
    held_ids = {event.id for event, _ in held}
    completed = {(event.email_account_id, metadata.get("draft_id") or event.provider_message_id or metadata.get("touch_id"))
        for event, metadata in receipts if metadata.get("reservation_id") not in held_ids
        and metadata.get("draft_id") not in reserved_drafts}
    completed.discard((None, None))
    if venture_id is not None:
        return len(held) + len(completed) + legacy + pending
    accounts = session.scalars(select(GtmEmailAccount).where(GtmEmailAccount.workspace_id.in_(workspace_ids)))
    counters = sum(int(account.sent_today or 0) for account in accounts if account.sent_count_date == start.date().isoformat()
        and (mailbox_ids is None or account.id in mailbox_ids))
    confirmed = {metadata.get("reservation_id") for event, metadata in receipts if event.event_type == "gmail_sent"}
    uncertain = sum(event.id not in confirmed for event, _ in held)
    return max(len(held) + len(completed) + legacy, counters + uncertain) + pending


def workspace_slots_used_today(session, workspace_id, *, exclude_draft_id=None):
    from app.gtm.supymem.pipeline import _gmail_workspace_ids
    return _send_slots_used_today(session, _gmail_workspace_ids(session, workspace_id), exclude_draft_id=exclude_draft_id)


def venture_slots_used_today(session, venture_id, *, exclude_draft_id=None):
    from app.db.models import Venture
    venture = session.get(Venture, venture_id)
    if venture is None:
        return 0
    return _send_slots_used_today(session, [venture.workspace_id], venture_id=venture_id, exclude_draft_id=exclude_draft_id)


def send_gmail_email(
    *, composio_user_id: str, connected_account_id: str, to: str, subject: str, body: str, thread_id: str | None = None,
    draft_id: str | None = None,
    workspace_id: str | None = None,
    self_only: bool = False,
) -> dict[str, Any]:
    from sqlalchemy import select
    from app.autopilot.send_admission import try_send_admission
    from app.db.models import GtmEmailAccount, Workspace
    from app.state import db

    from app.connections.composio_migration import reconcile_gmail_project
    from app.connections import mailboxes
    if mailboxes.enabled() and not workspace_id:
        raise DefiniteFailure("mailbox_unavailable")
    if workspace_id and not mailboxes.enabled() and not is_local():
        reconcile_gmail_project(workspace_id)
    with db()() as session:
        account = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == connected_account_id,
            GtmEmailAccount.composio_user_id == composio_user_id, GtmEmailAccount.status == "active"))
        if account is None or not account.email:
            if mailboxes.enabled():
                raise DefiniteFailure("mailbox_unavailable")
            paused = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == connected_account_id,
                GtmEmailAccount.composio_user_id == composio_user_id, GtmEmailAccount.status == "needs_reconnect"))
            if paused:
                raise ReconnectRequired()
            raise DefiniteFailure("gtm_gmail_required")
        if is_local():
            from app.gtm.local_mail import check_binding
            check_binding(composio_user_id, connected_account_id)
        from app.gtm.supymem.pipeline import _gmail_workspace_ids
        if workspace_id is not None and account.workspace_id not in _gmail_workspace_ids(session, workspace_id):
            raise DefiniteFailure("mailbox_unavailable" if mailboxes.enabled() else "gtm_gmail_required")
        account_id, workspace_id, sender = account.id, workspace_id or account.workspace_id, account.email.strip().casefold()
        if mailboxes.enabled():
            try:
                mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=account_id)
                from app.autopilot.outreach import _EMAIL
                if not _EMAIL.fullmatch(account.email):
                    raise mailboxes.MailboxUnavailable("mailbox_unavailable")
            except mailboxes.MailboxUnavailable as exc:
                raise DefiniteFailure("mailbox_unavailable") from exc
    if not mailboxes.enabled():
        return _send_gmail_admitted(composio_user_id=composio_user_id, connected_account_id=connected_account_id,
            to=to, subject=subject, body=body, thread_id=thread_id, draft_id=draft_id,
            workspace_id=workspace_id, account_id=account_id, sender=sender, self_only=self_only)
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise DefiniteFailure("mailbox_unavailable")
        admission_scope = "owner:" + workspace.owner_id if workspace.owner_id else "workspace:" + workspace_id
    with try_send_admission("gmail-" + admission_scope) as workspace_admitted:
        if not workspace_admitted:
            raise DefiniteFailure("Another send is in progress. Nothing was sent. Try again after it completes.")
        return _send_gmail_admitted(composio_user_id=composio_user_id, connected_account_id=connected_account_id,
            to=to, subject=subject, body=body, thread_id=thread_id, draft_id=draft_id,
            workspace_id=workspace_id, account_id=account_id, sender=sender, self_only=self_only)


def _send_gmail_admitted(*, composio_user_id, connected_account_id, to, subject, body, thread_id, draft_id,
                        workspace_id, account_id, sender, self_only=False):
    from sqlalchemy import func, select
    from app.autopilot.send_admission import try_send_admission
    from app.connections import mailboxes
    from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmTouch
    from app.gtm.relationships.email_compliance import ADDRESS_PROMPT, validate_send
    from app.gtm.relationships.suppression import email_suppressed
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    with try_send_admission("mailbox:" + sender) as admitted:
        if not admitted:
            raise DefiniteFailure("Another send from this mailbox is in progress. Try again after it completes.")
        reservation_id = None
        contact_id = None
        venture_id = None
        document = None
        with db()() as session:
            account = session.get(GtmEmailAccount, account_id)
            if account is None or account.status != "active" or account.connected_account_id != connected_account_id:
                raise DefiniteFailure("mailbox_unavailable" if mailboxes.enabled() else "gtm_gmail_required")
            from app.db.models import GtmOutreachSettings
            settings = session.get(GtmOutreachSettings, workspace_id)
            if settings and not settings.send_enabled:
                raise DefiniteFailure("Workspace sending is paused. Review bounced addresses, then Resume sending in Find customers.")
            contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == workspace_id,
                func.lower(GtmContact.email) == to.strip().lower()))
            if contact is None:
                raise DefiniteFailure("Add this recipient and their source/permission in Find customers before sending.")
            if mailboxes.enabled() or is_local():
                try:
                    bound = mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=account_id)
                    if bound["composio_user_id"] != composio_user_id or bound["connected_account_id"] != connected_account_id:
                        raise mailboxes.MailboxUnavailable("mailbox_binding_changed")
                    if thread_id:
                        original = mailboxes.mailbox_for_conversation(session, contact_id=contact.id, thread_id=thread_id)
                        if original is None or original["mailbox_id"] != account_id:
                            raise mailboxes.MailboxUnavailable("conversation_unavailable")
                except mailboxes.MailboxUnavailable as exc:
                    raise DefiniteFailure("mailbox_unavailable") from exc
                start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
                previous = list(session.scalars(select(GtmEmailEvent).where(
                    GtmEmailEvent.contact_id == contact.id, GtmEmailEvent.event_type == "gmail_reserved")))
                releases = set(session.scalars(select(GtmEmailEvent.idempotency_key).where(
                    GtmEmailEvent.event_type == "gmail_released")))
                if any("gmail-release:" + event.id not in releases and (event.created_at >= start or not thread_id)
                       for event in previous) or session.scalar(select(GtmTouch.id).where(
                           GtmTouch.contact_id == contact.id, GtmTouch.direction == "out", GtmTouch.channel == "email",
                           GtmTouch.simulated == 0, GtmTouch.sent_at >= start).limit(1)):
                    raise DefiniteFailure("A delivery to this contact is already sent or unconfirmed. Check Gmail before trying again.")
                from app.autopilot.guard import workspace_send_max, effective_send_max, DEFAULT_SEND_MAX
                if workspace_slots_used_today(session, workspace_id, exclude_draft_id=draft_id) >= workspace_send_max(session, workspace_id):
                    raise DefiniteFailure("Your workspace daily send limit is used or reserved. Your drafts stay saved for another day.")
                from app.db.models import Venture, VentureDocument
                from app.db.supymem_models import AutopilotSetting
                document = session.get(VentureDocument, draft_id) if draft_id else None
                venture_id = document.venture_id if document else (
                    contact.source.split(":", 2)[1] if (contact.source or "").startswith("venture:") else None)
                venture = session.get(Venture, venture_id) if venture_id else None
                if venture_id and (venture is None or venture.workspace_id != workspace_id or venture.archived_at is not None):
                    raise DefiniteFailure("Your venture is unavailable. Nothing was sent.")
                if venture:
                    setting = session.get(AutopilotSetting, venture.id)
                    ceiling = effective_send_max(session, setting) if setting and setting.allow_send else DEFAULT_SEND_MAX
                    if venture_slots_used_today(session, venture.id, exclude_draft_id=draft_id) >= ceiling:
                        raise DefiniteFailure("Your venture daily send limit is used or reserved. Your drafts stay saved for another day.")
            contact_id = contact.id
            if contact and email_suppressed(session, workspace_id, contact, replied_ok=bool(thread_id)):
                raise DefiniteFailure("contact_suppressed")
            from app.gtm.email_learning import globally_suppressed
            if globally_suppressed(session, to):
                raise DefiniteFailure("contact_suppressed")
            identity = account.email
            if not mailboxes.enabled() and contact and (contact.source or "").startswith("venture:"):
                from app.autopilot.outreach import founder_signature
                identity = founder_signature(contact.source.split(":", 2)[1])
            try:
                validate_send(session, workspace_id, contact, body, identity,
                    draft=json.loads(document.provenance_json or "{}") if document else None)
            except ValueError as exc:
                if str(exc) == "gtm_draft_unsupported_claim":
                    raise DefiniteFailure('Your email contains a claim I cannot back up. Nothing was sent. Say "make it shorter", or tell me what to change.') from exc
                if is_local():
                    raise DefiniteFailure("Your email needs your current sender name, postal address, source and opt-out details. Update your local settings and refresh the draft, then try again.") from None
                raise DefiniteFailure(ADDRESS_PROMPT if str(exc) == "gtm_sender_address_required" else str(exc)) from exc
            from app.autopilot.guard import mailbox_send_max
            cap = mailbox_send_max(session, account)
            if mailbox_slots_used_today(session, account, exclude_draft_id=draft_id) >= cap:
                raise DefiniteFailure(f"Your Gmail daily cap ({cap} emails across your ventures) is used or reserved. Remaining drafts stay saved.")
            cold = contact and not contact.times_emailed and contact.lawful_basis != "consent"
            if cold:
                now = utcnow()
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                account_ids = select(GtmEmailAccount.id).where(func.lower(func.trim(GtmEmailAccount.email)) == sender)
                prior_reservations = list(session.scalars(select(GtmEmailEvent.id).where(GtmEmailEvent.email_account_id.in_(account_ids),
                    GtmEmailEvent.contact_id == contact.id, GtmEmailEvent.event_type == "cold_reserved", GtmEmailEvent.created_at < start)))
                prior_releases = set(session.scalars(select(GtmEmailEvent.idempotency_key).where(GtmEmailEvent.event_type == "cold_released")))
                if any("cold-release:" + ident not in prior_releases for ident in prior_reservations):
                    raise DefiniteFailure("A delivery to this contact is already sent or unconfirmed. Check Gmail before trying again.")
                held = list(session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id.in_(account_ids),
                    GtmEmailEvent.event_type == "cold_reserved", GtmEmailEvent.created_at >= start)))
                released = set(session.scalars(select(GtmEmailEvent.idempotency_key).where(
                    GtmEmailEvent.event_type == "cold_released", GtmEmailEvent.created_at >= start)))
                held = [event for event in held if "cold-release:" + event.id not in released]
                prior = select(GtmTouch.contact_id).where(GtmTouch.direction == "out", GtmTouch.simulated == 0,
                    GtmTouch.sent_at < start)
                old = set(session.scalars(select(GtmTouch.contact_id).join(GtmContact, GtmContact.id == GtmTouch.contact_id).where(
                    GtmTouch.workspace_id.in_(select(GtmEmailAccount.workspace_id).where(func.lower(func.trim(GtmEmailAccount.email)) == sender)),
                    GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at >= start,
                    GtmContact.lawful_basis != "consent", GtmTouch.contact_id.not_in(prior))))
                if mailboxes.enabled() and old:
                    physical_ids = set(session.scalars(account_ids))
                    known = {}
                    for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.contact_id.in_(old),
                            GtmEmailEvent.created_at >= start, GtmEmailEvent.email_account_id.is_not(None),
                            GtmEmailEvent.event_type.in_(["sent", "SENT", "gmail_sent", "sent_metadata"]))):
                        known.setdefault(event.contact_id, set()).add(event.email_account_id)
                    old -= {contact_id for contact_id, inbox_ids in known.items() if inbox_ids.isdisjoint(physical_ids)}
                cap = min(int(os.environ.get("OUTREACH_NEW_CONTACTS_PER_MAILBOX_DAY", "25")), int(account.daily_send_limit or 25))
                if len(held) + len(old - {event.contact_id for event in held}) >= cap:
                    reset = (start + timedelta(days=1)).isoformat()
                    raise DefiniteFailure(f"Mailbox daily cap ({cap} new cold contacts) reached. Resets at {reset} UTC. Remaining drafts stay saved.")
                if any(event.contact_id == contact.id for event in held):
                    raise DefiniteFailure("A delivery to this contact is already sent or unconfirmed. Check Gmail before trying again.")
                reservation_id = new_id("gtmee")
                session.add(GtmEmailEvent(id=reservation_id, workspace_id=workspace_id, email_account_id=account_id,
                    contact_id=contact.id, event_type="cold_reserved",
                    metadata_json=json.dumps({"draft_id": draft_id}) if draft_id else None, created_at=now))
                session.commit()
            quota_reservation_id = new_id("gtmee")
            session.add(GtmEmailEvent(id=quota_reservation_id, workspace_id=workspace_id, email_account_id=account_id,
                contact_id=contact_id, event_type="gmail_reserved",
                **({"idempotency_key": f"gmail:{account_id}:{quota_reservation_id}"} if mailboxes.enabled() else {}),
                metadata_json=json.dumps({"draft_id": draft_id} | ({"venture_id": venture_id} if venture_id else {})), created_at=utcnow()))
            session.commit()
        try:
            result = _send_gmail_email(composio_user_id=composio_user_id, connected_account_id=connected_account_id,
                to=to, subject=subject, body=body, thread_id=thread_id, **({"self_only": True} if self_only else {}))
        except (DefiniteFailure, AdmissionLimited):
            with db()() as session:
                session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=workspace_id, email_account_id=account_id,
                    event_type="gmail_released", idempotency_key="gmail-release:" + quota_reservation_id, created_at=utcnow()))
                if reservation_id:
                    session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=workspace_id, email_account_id=account_id,
                        event_type="cold_released", idempotency_key="cold-release:" + reservation_id, created_at=utcnow()))
                session.commit()
            raise
        with db()() as session:
            session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=workspace_id, email_account_id=account_id,
                contact_id=contact_id, event_type="gmail_sent", provider_message_id=result.get("provider_message_id"),
                metadata_json=json.dumps(result | {"reservation_id": quota_reservation_id}), created_at=utcnow()))
            session.commit()
        if reservation_id:
            with db()() as session:
                session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=workspace_id, email_account_id=account_id,
                    contact_id=contact_id, event_type="cold_sent", provider_message_id=result.get("provider_message_id"),
                    metadata_json=json.dumps(result | {"reservation_id": reservation_id}), created_at=utcnow()))
                session.commit()
        return result


def _send_gmail_email(
    *,
    composio_user_id: str,
    connected_account_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    self_only: bool = False,
) -> dict[str, Any]:
    if is_local():
        from app.gtm.local_mail import check_binding, send
        check_binding(composio_user_id, connected_account_id)
        return send(to=to, subject=subject, body=body, thread_id=thread_id, **({"self_only": True} if self_only else {}))
    if self_only:
        raise DefiniteFailure("Open your desktop app and use your saved Email connection to send to yourself.")
    if thread_id:
        result = execute_tool(
            "GMAIL_REPLY_TO_THREAD",
            user_id=composio_user_id,
            connected_account_id=connected_account_id,
            arguments={
                "thread_id": thread_id,
                "message_body": body,
                "recipient_email": to,
                "is_html": False,
            },
        )
    else:
        result = execute_tool(
            "GMAIL_SEND_EMAIL",
            user_id=composio_user_id,
            connected_account_id=connected_account_id,
            arguments={
                "recipient_email": to,
                "subject": subject,
                "body": body,
                "is_html": False,
            },
        )
    payload = _payload(result)
    provider_id = payload.get("id") or payload.get("message_id") or payload.get("messageId")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise RuntimeError("gmail_receipt_unconfirmed")
    returned_thread = payload.get("threadId") or payload.get("thread_id") or thread_id
    rfc_id = message_headers(payload).get("message-id")
    if isinstance(provider_id, str) and not rfc_id:
        try:
            message = fetch_message(composio_user_id=composio_user_id, connected_account_id=connected_account_id, message_id=provider_id)
            rfc_id = message_headers(message).get("message-id")
            returned_thread = returned_thread or message.get("threadId") or message.get("thread_id")
        except Exception:
            log.warning("Sent Gmail metadata lookup failed; tracked-thread sync will retry it.")
    return {
        "provider_message_id": provider_id if isinstance(provider_id, str) else None,
        "thread_id": returned_thread if isinstance(returned_thread, str) else None,
        "rfc_message_id": rfc_id,
    }


def fetch_inbound_from(*, composio_user_id: str, connected_account_id: str, from_email: str) -> Any:
    result = execute_tool(
        "GMAIL_FETCH_EMAILS",
        user_id=composio_user_id,
        connected_account_id=connected_account_id,
        arguments={
            "query": f"from:{from_email} newer_than:30d",
            "max_results": 10,
            "include_payload": False,
            "verbose": False,
        },
    )
    return result.get("data")


def fetch_query(*, composio_user_id: str, connected_account_id: str, query: str, max_results: int = 15, include_payload: bool = False, page_token: str | None = None) -> Any:
    result = execute_tool(
        "GMAIL_FETCH_EMAILS",
        user_id=composio_user_id,
        connected_account_id=connected_account_id,
        arguments={
            "query": query,
            "max_results": max_results,
            "include_payload": include_payload,
            "verbose": False,
            **({"page_token": page_token} if page_token is not None else {}),
        },
    )
    return result.get("data")
