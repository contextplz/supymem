"""Composio-backed social connect + publish for the Content engine.

Prefer this over first-party OAuth when ``COMPOSIO_API_KEY`` is set. LinkedIn
and Reddit can use Composio-managed OAuth apps; X/Twitter requires the
founder's own developer credentials registered as a custom auth config.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.content.oauth.providers import get_provider
from app.connections import local_config, toolkits
from app.content.platforms.base import PublishResult, dry_run_enabled, simulated
from app.db.models import ContentAccount
from app.core.local_mode import is_local
from app.state import db

log = logging.getLogger(__name__)

COMPOSIO_BASE = os.environ.get(
    "COMPOSIO_API_BASE", "https://backend.composio.dev/api/v3.1"
).rstrip("/")

#: company_os platform → Composio toolkit slug
TOOLKIT_SLUG: dict[str, str] = {
    "google_calendar": "googlecalendar",
    "linkedin": "linkedin",
    "reddit": "reddit",
    "x": "twitter",
}

POST_TOOL: dict[str, str] = {
    "linkedin": "LINKEDIN_CREATE_LINKED_IN_POST",
    "reddit": "REDDIT_CREATE_REDDIT_POST",
    "x": "TWITTER_CREATION_OF_A_POST",
}

PROFILE_TOOL: dict[str, str] = {
    "linkedin": "LINKEDIN_GET_MY_INFO",
    "reddit": "REDDIT_GET",
    "x": "TWITTER_USER_LOOKUP_ME",
}

#: Env vars that pin an already-created auth config id.
AUTH_CONFIG_ENV: dict[str, str] = {
    "google_calendar": "COMPOSIO_GOOGLECALENDAR_AUTH_CONFIG_ID",
    "linkedin": "COMPOSIO_LINKEDIN_AUTH_CONFIG_ID",
    "reddit": "COMPOSIO_REDDIT_AUTH_CONFIG_ID",
    "x": "COMPOSIO_TWITTER_AUTH_CONFIG_ID",
}

# Soft persist so auto-created auth configs survive ops restart (env still preferred).
_DATA_DIR = Path(__file__).resolve().parents[4] / "data"
_AUTH_CONFIG_CACHE: dict[str, Path] = {
    "linkedin": _DATA_DIR / "composio_linkedin_auth_config_id",
    "reddit": _DATA_DIR / "composio_reddit_auth_config_id",
    "x": _DATA_DIR / "composio_twitter_auth_config_id",
}


class ComposioSocialError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def api_key() -> str:
    if is_local():
        from app.connections.local_config import api_key as local_api_key
        return local_api_key()
    return os.environ.get("COMPOSIO_API_KEY", "").strip()


def configured() -> bool:
    if is_local():
        from app.connections import local_config
        return not local_config.missing("composio")
    return bool(api_key())


def user_id_for(workspace_id: str) -> str:
    """One Composio identity per Company OS workspace — binds channels to Mem/desk."""
    if is_local():
        return f"content:{workspace_id}"
    explicit = os.environ.get("COMPOSIO_CONTENT_USER_ID", "").strip()
    from app.admin.authn import require_signed_caller
    if explicit and not require_signed_caller():
        return explicit
    return f"content:{workspace_id}"


def callback_url() -> str:
    explicit = os.environ.get("COMPOSIO_CONTENT_CALLBACK_URL", "").strip()
    if explicit:
        return explicit
    origin = (
        os.environ.get("CONSOLE_URL")
        or os.environ.get("NEXT_PUBLIC_APP_URL")
        or "http://127.0.0.1:3100"
    ).rstrip("/")
    return f"{origin}/content"


def _headers() -> dict[str, str]:
    if is_local():
        from app.connections import local_config
        if local_config.missing("composio"):
            raise ComposioSocialError("not_configured", local_config.setup_message("composio"))
    key = api_key()
    if not key:
        raise ComposioSocialError("not_configured", "COMPOSIO_API_KEY is not set.")
    return {"x-api-key": key, "Content-Type": "application/json"}


def _toolkit_version(platform: str) -> str:
    # Social toolkits are versioned independently. A LinkedIn pin must never
    # accidentally become the Reddit or X execution version.
    toolkit = TOOLKIT_SLUG.get(platform, platform).upper()
    specific = os.environ.get(f"COMPOSIO_{toolkit}_TOOLKIT_VERSION", "").strip()
    return specific or os.environ.get("COMPOSIO_TOOLKIT_VERSION", "latest").strip() or "latest"


def _tool_platform(slug: str) -> str:
    for prefix, platform in (("LINKEDIN_", "linkedin"), ("REDDIT_", "reddit"),
                             ("TWITTER_", "x")):
        if slug.startswith(prefix):
            return platform
    raise ComposioSocialError("unknown_platform", "No social toolkit for this tool")


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    from app.connections import composio_pacing
    from app.connections import local_config
    if is_local():
        platform = (local_config.toolkit_for_tool(path.rsplit("/", 1)[-1]) if path.startswith("/tools/")
                    else str((params or {}).get("toolkit_slug") or "composio"))
    account = composio_pacing.account_key(path, json_body, params)
    composio_pacing.acquire(account)
    url = f"{COMPOSIO_BASE}{path}"
    try:
        with httpx.Client(timeout=timeout) as client:
            res = client.request(
                method, url, headers=_headers(), json=json_body, params=params
            )
    except httpx.RequestError:
        if is_local():
            raise ComposioSocialError("provider_unavailable", local_config.failure_message(platform)) from None
        raise
    if res.status_code == 429:
        composio_pacing.reject(account, res.headers.get("Retry-After"))
    try:
        data = res.json()
    except Exception:
        if is_local():
            raise ComposioSocialError("provider_unavailable", local_config.failure_message(platform)) from None
        data = {"raw": res.text[:500]}
    if res.status_code >= 400:
        msg = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("slug") or "")
            else:
                msg = str(err or data.get("message") or data.get("detail") or "")
        message = msg or f"Composio {path} returned HTTP {res.status_code}."
        code = "http_error"
        if is_local():
            from app.connections.composio_migration import missing_connection
            from app.gtm.supymem.composio_gmail import _DEFINITE, _RECONNECT
            if _RECONNECT.search(message):
                code = "needs_reconnect"
            elif _DEFINITE.search(message):
                code = "not_configured"
            missing = missing_connection(ComposioSocialError("http_error", message, status_code=res.status_code))
            message = ("Your connected account was not found. " if missing else "I could not confirm your request finished. ")
            message += local_config.failure_message(platform)
        raise ComposioSocialError(code, message, status_code=res.status_code)
    composio_pacing.check_response(account, data, res.headers.get("Retry-After"))
    return data if isinstance(data, dict) else {"data": data}


def toolkit_for(platform: str) -> str:
    key = (platform or "").strip().lower()
    if key in toolkits.TOOLKITS:
        return key
    if key not in TOOLKIT_SLUG:
        raise ComposioSocialError("unknown_platform", f"No Composio toolkit for {platform}")
    return TOOLKIT_SLUG[key]


def managed_auth_supported(platform: str) -> bool:
    """X has no Composio-managed app; LinkedIn and Reddit do."""
    return toolkit_for(platform) in {"googlecalendar", "linkedin", "reddit"}


def connectable(platform: str) -> bool:
    from app.core.local_mode import is_local

    if is_local():
        return not local_config.missing(platform)

    if not configured():
        return False
    if platform in toolkits.TOOLKITS:
        return bool(toolkits.auth_config_id(platform))
    if platform == "google_calendar":
        return bool(os.environ.get(AUTH_CONFIG_ENV[platform], "").strip())
    if _toolkit_version(platform) == "latest":
        return False
    if managed_auth_supported(platform):
        return True
    # X: need founder's app keys either as COMPOSIO_TWITTER_AUTH_CONFIG_ID
    # or as X_CLIENT_ID/SECRET so we can register a custom auth config.
    if os.environ.get(AUTH_CONFIG_ENV.get(platform, ""), "").strip():
        return True
    if _read_auth_config_cache(platform):
        return True
    provider = get_provider(platform)
    return provider.configured()


def _read_auth_config_cache(platform: str) -> str:
    path = _AUTH_CONFIG_CACHE.get(platform)
    if path is None:
        return ""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _write_auth_config_cache(platform: str, auth_id: str) -> None:
    if not auth_id:
        return
    path = _AUTH_CONFIG_CACHE.get(platform)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(auth_id.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist %s auth config id: %s", platform, exc)


def _pin_auth_config(platform: str, auth_id: str) -> str:
    env_name = AUTH_CONFIG_ENV[platform]
    os.environ[env_name] = auth_id
    _write_auth_config_cache(platform, auth_id)
    return auth_id


def ensure_auth_config(platform: str) -> str:
    """Return an auth config id, creating one when needed."""
    from app.core.local_mode import is_local

    if is_local():
        if local_config.missing(platform):
            raise ComposioSocialError("not_configured", local_config.setup_message(platform))
        return local_config.auth_config_id(platform)

    if platform in toolkits.TOOLKITS:
        auth_id = toolkits.auth_config_id(platform)
        if not auth_id:
            raise ComposioSocialError("not_connectable", "This connection is not available yet.")
        return auth_id
    env_name = AUTH_CONFIG_ENV[platform]
    explicit = os.environ.get(env_name, "").strip()
    if explicit:
        return explicit
    toolkit = toolkit_for(platform)

    def _list_ids() -> list[str]:
        listed = _request(
            "GET",
            "/auth_configs",
            params={"toolkit_slug": toolkit, "limit": 20},
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

    existing = os.environ.get(env_name, "").strip() or _read_auth_config_cache(platform)
    known = _list_ids()
    if existing and existing in known:
        return _pin_auth_config(platform, existing)
    if existing and existing not in known:
        # Stale id from a previous project/session — drop it.
        log.warning("ignoring stale %s=%s", env_name, existing[:8])
        os.environ.pop(env_name, None)
    if known:
        return _pin_auth_config(platform, known[0])

    if managed_auth_supported(platform):
        created = _request(
            "POST",
            "/auth_configs",
            json_body={
                "toolkit": {"slug": toolkit},
                "auth_config": {
                    "type": "use_composio_managed_auth",
                    "name": f"Company OS Content{platform}",
                },
            },
        )
        auth = created.get("auth_config") if isinstance(created.get("auth_config"), dict) else {}
        auth_id = str(auth.get("id") or created.get("id") or "")
        if not auth_id:
            raise ComposioSocialError(
                "auth_config_failed",
                f"Composio did not return an auth config for {platform}.",
            )
        return _pin_auth_config(platform, auth_id)

    # Custom OAuth for X (and any future toolkit without managed credentials).
    provider = get_provider(platform)
    if not provider.configured():
        raise ComposioSocialError(
            "needs_app_keys",
            f"Composio has no managed {platform} app. Set "
            f"{provider.client_id_env} and {provider.client_secret_env}, "
            "or create a custom auth config in the Composio dashboard.",
        )
    created = _request(
        "POST",
        "/auth_configs",
        json_body={
            "toolkit": {"slug": toolkit},
            "auth_config": {
                "type": "use_custom_auth",
                "auth_scheme": "OAUTH2",
                "name": f"Company OS Content{platform}",
                "credentials": {
                    "client_id": provider.client_id(),
                    "client_secret": provider.client_secret(),
                },
            },
        },
    )
    auth = created.get("auth_config") if isinstance(created.get("auth_config"), dict) else {}
    auth_id = str(auth.get("id") or created.get("id") or "")
    if not auth_id:
        raise ComposioSocialError(
            "auth_config_failed",
            f"Composio did not return a custom auth config for {platform}.",
        )
    return _pin_auth_config(platform, auth_id)


def start_connect(
    *,
    workspace_id: str,
    platform: str,
    callback: str | None = None,
) -> dict[str, str]:
    """Mint a Composio Connect Link and return it as authorize_url."""
    if not connectable(platform):
        if is_local():
            from app.connections import local_config
            raise ComposioSocialError("not_connectable", local_config.setup_message(platform))
        raise ComposioSocialError(
            "not_connectable",
            f"{platform} is not connectable via Composio yet.",
        )
    auth_config_id = ensure_auth_config(platform)
    uid = f"content:{workspace_id}" if platform in toolkits.TOOLKITS else user_id_for(workspace_id)
    if platform in toolkits.FOUNDER_TOOLKITS:
        from app.experiments.ledger import autopilot_enabled
        from app.experiments import tools
        if is_local() or autopilot_enabled() or tools.enabled() and platform in tools.READ_RUNNERS:
            from app.authz.workspace import subject_person_id
            from app.connections.revenue import founder_user_id
            from app.content.oauth.attempts import initiating_actor, OAuthError
            uid = founder_user_id(workspace_id)
            if subject_person_id(initiating_actor(workspace_id)) != uid:
                raise OAuthError("founder_required", f"You must own this workspace to connect {toolkits.TOOLKITS[platform]['name']} for your tests.")
    cb = (callback or "").strip() or callback_url()
    data = _request(
        "POST",
        "/connected_accounts/link",
        json_body={
            "auth_config_id": auth_config_id,
            "user_id": uid,
            "callback_url": cb,
            "allow_multiple": False,
        },
    )
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    redirect = (
        data.get("redirect_url")
        or data.get("redirectUrl")
        or nested.get("redirect_url")
        or nested.get("redirectUrl")
    )
    if not redirect:
        raise ComposioSocialError(
            "no_redirect",
            "Composio did not return a Connect Link.",
        )
    connection_id = data.get("connected_account_id") or nested.get("connected_account_id")
    if not connection_id:
        raise ComposioSocialError("unbound_connection", "Composio did not return the created account ID.")
    return {
        "connected_account_id": str(connection_id),
        "authorize_url": str(redirect),
        "redirect_url": str(redirect),
        "state": "composio",
        "redirect_uri": cb,
        "auth_via": "composio",
        "composio_user_id": uid,
        "auth_config_id": auth_config_id,
        "platform": platform,
    }


def _normalize_connection_items(
    items: Any,
    *,
    uid: str,
    platform: str | None,
    active_only: bool,
) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        items = items.get("items") or []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        entity = str(
            item.get("user_id") or item.get("entity_id") or item.get("userId") or ""
        )
        # Unbound list entries cannot prove ownership. Omitted user_id is only
        # safe for a directly fetched account matching a persisted start receipt.
        if entity != uid:
            continue
        status = str(item.get("status") or "").upper()
        if active_only and status not in {"ACTIVE", "SUCCESS", "CONNECTED"}:
            continue
        tk = item.get("toolkit") if isinstance(item.get("toolkit"), dict) else {}
        slug = str(tk.get("slug") or item.get("toolkit_slug") or "").lower()
        platform_name = next(
            (p for p, t in TOOLKIT_SLUG.items() if t == slug), None
        )
        if platform and platform_name != platform:
            continue
        if platform_name is None:
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "status": status or "ACTIVE",
                "platform": platform_name,
                "toolkit": slug,
                "user_id": entity or uid,
                "raw": item,
            }
        )
    return out


def list_connections(
    *,
    workspace_id: str,
    platform: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """List Composio connected accounts for this content workspace.

    Prefer toolkit-wide list then filter by user — Composio's combined
    ``user_ids`` + ``toolkit_slugs`` filter has intermittently dropped ACTIVE
    rows (same bug Gmail/Discord already worked around), which made keepalive
    mark LinkedIn as ``composio:MISSING``.
    """
    uid = user_id_for(workspace_id)
    toolkits = (
        [toolkit_for(platform)]
        if platform
        else list(dict.fromkeys(TOOLKIT_SLUG.values()))
    )
    out: list[dict[str, Any]] = []
    for toolkit in toolkits:
        data = _request(
            "GET",
            "/connected_accounts",
            params={"toolkit_slugs": toolkit, "limit": 100},
        )
        items = data.get("items") or data.get("data") or []
        out.extend(
            _normalize_connection_items(
                items, uid=uid, platform=platform, active_only=active_only
            )
        )
    if out:
        from app.content.oauth.callbacks import known_connections
        out.extend(known_connections(workspace_id, platform=platform, active_only=active_only,
                                     exclude={item["id"] for item in out}))
        return out
    # Fallback: original filtered query (may still drop rows on some projects).
    params: dict[str, Any] = {"user_ids": uid, "limit": 50}
    if platform:
        params["toolkit_slugs"] = toolkit_for(platform)
    data = _request("GET", "/connected_accounts", params=params)
    items = data.get("items") or data.get("data") or []
    out = _normalize_connection_items(items, uid=uid, platform=platform, active_only=active_only)
    from app.content.oauth.callbacks import known_connections
    out.extend(known_connections(workspace_id, platform=platform, active_only=active_only,
                                 exclude={item["id"] for item in out}))
    return out


def execute_tool(
    slug: str,
    *,
    user_id: str,
    connected_account_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    platform = _tool_platform(slug)
    if is_local():
        from app.connections import local_config
        from app.gtm.supymem.composio_gmail import _DEFINITE, _RECONNECT
        ensure_auth_config(platform)
    body: dict[str, Any] = {
        "user_id": user_id,
        "connected_account_id": connected_account_id,
        "arguments": arguments,
        "version": _toolkit_version(platform),
    }
    if _toolkit_version(platform) == "latest":
        body["dangerously_skip_version_check"] = True
    from app.connections.composio_migration import mark_social_reconnect, missing_connection
    try:
        data = _request("POST", f"/tools/execute/{slug}", json_body=body, timeout=60.0)
    except ComposioSocialError as exc:
        if missing_connection(exc) or is_local() and exc.code == "needs_reconnect":
            mark_social_reconnect(connected_account_id)
            message = (f"Reconnect your {local_config.app_name(platform)} account on the Connections screen, then try posting again."
                       if is_local() else "Reconnect your account to resume posting.")
            raise ComposioSocialError("needs_reconnect", message) from None
        raise
    if data.get("successful") is False:
        error = str(data.get("error") or "")
        if missing_connection(RuntimeError(error)) or is_local() and _RECONNECT.search(error):
            mark_social_reconnect(connected_account_id)
            message = (f"Reconnect your {local_config.app_name(platform)} account on the Connections screen, then try posting again."
                       if is_local() else "Reconnect your account to resume posting.")
            raise ComposioSocialError("needs_reconnect", message)
        raise ComposioSocialError(
            "not_configured" if is_local() and _DEFINITE.search(error) else "tool_failed",
            local_config.failure_message(platform) if is_local() else str(data.get("error") or f"{slug} failed."),
        )
    return data


def _find_linkedin_urn(payload: Any, *, depth: int = 0) -> str:
    if depth > 6 or payload is None:
        return ""
    if isinstance(payload, str) and payload.startswith("urn:li:") and (
        "share" in payload or "ugcPost" in payload or "activity" in payload
    ):
        return payload
    if isinstance(payload, dict):
        for value in payload.values():
            found = _find_linkedin_urn(value, depth=depth + 1)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload[:30]:
            found = _find_linkedin_urn(value, depth=depth + 1)
            if found:
                return found
    return ""


def _dig(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def enrich_profile(
    *,
    platform: str,
    user_id: str,
    connected_account_id: str,
) -> dict[str, str]:
    """Best-effort handle + author URN after a fresh connect."""
    tool = PROFILE_TOOL.get(platform)
    if not tool:
        return {"handle": connected_account_id, "author_urn": "", "external_account": connected_account_id}
    try:
        result = execute_tool(
            tool,
            user_id=user_id,
            connected_account_id=connected_account_id,
            arguments={},
        )
    except ComposioSocialError as exc:
        if exc.code == "needs_reconnect":
            raise
        log.warning("profile enrich failed for %s: %s", platform, exc.message)
        return {
            "handle": connected_account_id,
            "author_urn": "",
            "external_account": connected_account_id,
        }

    data = result.get("data") if isinstance(result.get("data"), dict) else result
    handle = ""
    author_urn = ""
    external = connected_account_id

    if platform == "linkedin":
        # Responses vary; look for id / sub / vanityName.
        person_id = (
            _dig(data, "id")
            or _dig(data, "sub")
            or _dig(data, "author")
            or _dig(data, "person_id")
            or _dig(data, "response_data", "id")
        )
        vanity = (
            _dig(data, "vanityName")
            or _dig(data, "localizedFirstName")
            or _dig(data, "name")
            or _dig(data, "response_data", "vanityName")
        )
        if isinstance(person_id, str) and person_id:
            if person_id.startswith("urn:li:"):
                author_urn = person_id
                external = person_id
            else:
                author_urn = f"urn:li:person:{person_id}"
                external = person_id
        if isinstance(vanity, str) and vanity:
            handle = vanity
        elif author_urn:
            handle = author_urn.rsplit(":", 1)[-1]
    elif platform == "reddit":
        name = (
            _dig(data, "name")
            or _dig(data, "username")
            or _dig(data, "response_data", "name")
        )
        if isinstance(name, str) and name:
            handle = name if name.startswith("u/") else f"u/{name}"
            external = name
    elif platform == "x":
        uname = (
            _dig(data, "username")
            or _dig(data, "data", "username")
            or _dig(data, "response_data", "username")
        )
        uid = (
            _dig(data, "id")
            or _dig(data, "data", "id")
            or _dig(data, "response_data", "id")
        )
        if isinstance(uname, str) and uname:
            handle = uname if uname.startswith("@") else f"@{uname}"
        if isinstance(uid, str) and uid:
            external = uid
            author_urn = uid

    return {
        "handle": handle or external,
        "author_urn": author_urn,
        "external_account": external or connected_account_id,
    }


def sync_workspace(workspace_id: str, *, platform: str | None = None, connections: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Pull active Composio connections into content_accounts for this workspace."""
    reauthorization = connections is not None
    if connections is None:
        connections = list_connections(workspace_id=workspace_id, platform=platform)
    uid = user_id_for(workspace_id)
    synced: list[dict[str, Any]] = []
    factory = db()
    now = utcnow()

    for conn in connections:
        ca_id = conn["id"]
        plat = conn["platform"]
        if not ca_id:
            continue
        profile = enrich_profile(
            platform=plat, user_id=uid, connected_account_id=ca_id
        )
        provider = None if plat == "google_calendar" else get_provider(plat)
        extra = {
            "auth_via": "composio",
            "composio_user_id": uid,
            "connected_account_id": ca_id,
            "toolkit": conn["toolkit"],
            "composio_project": os.environ.get("COMPOSIO_PROJECT", "v1"),
        }
        with factory() as session:
            # Match on the Composio connected-account id first.
            row = session.scalars(
                select(ContentAccount).where(
                    ContentAccount.workspace_id == workspace_id,
                    ContentAccount.platform == plat,
                    ContentAccount.external_account.in_(
                        [ca_id, profile["external_account"]]
                    ),
                )
            ).first()
            if row is not None and row.status == "revoked" and not reauthorization:
                continue  # Background reconciliation cannot undo a founder revoke.
            if row is None:
                row = ContentAccount(
                    id=new_id("cacc"),
                    workspace_id=workspace_id,
                    platform=plat,
                    external_account=profile["external_account"] or ca_id,
                    handle=profile["handle"],
                    author_urn=profile["author_urn"] or None,
                    status="active",
                    access_secret_ref=None,
                    refresh_secret_ref=None,
                    scopes_json=json.dumps(list(provider.default_scopes) if provider else []),
                    extra_json=json.dumps(extra),
                    expires_at=None,
                    daily_cap=provider.daily_cap_default if provider else 0,
                    posts_today=0,
                    day_key=None,
                    last_post_at=None,
                    health="ok",
                    paused_reason=None,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.external_account = profile["external_account"] or ca_id
                row.handle = profile["handle"] or row.handle
                if profile["author_urn"]:
                    row.author_urn = profile["author_urn"]
                row.status = "active"
                row.health = "ok"
                row.paused_reason = None
                row.last_error = None
                row.extra_json = json.dumps(extra)
                row.updated_at = now
            session.commit()
            session.refresh(row)
            synced.append(
                {
                    "id": row.id,
                    "platform": row.platform,
                    "handle": row.handle,
                    "external_account": row.external_account,
                    "status": row.status,
                    "auth_via": "composio",
                    "connected_account_id": ca_id,
                }
            )
    return synced


def account_composio_meta(account: ContentAccount) -> dict[str, str] | None:
    try:
        extra = json.loads(account.extra_json or "{}")
    except Exception:
        return None
    if not isinstance(extra, dict):
        return None
    if extra.get("auth_via") != "composio":
        return None
    ca = str(extra.get("connected_account_id") or "")
    uid = str(extra.get("composio_user_id") or "")
    if not ca or not uid:
        return None
    return {"connected_account_id": ca, "composio_user_id": uid}


def delete_connection(connected_account_id: str, *, revoke: bool = True) -> None:
    """Forget a Composio connected account. Optionally revoke upstream tokens."""
    if not connected_account_id:
        return
    path = f"/connected_accounts/{connected_account_id}"
    params = {"revoke_on_delete": "true"} if revoke else None
    try:
        _request("DELETE", path, params=params)
    except ComposioSocialError as exc:
        # Already gone is fine for a fresh start.
        if "404" in exc.message or "not found" in exc.message.lower():
            return
        raise


def forget_workspace_connections(
    workspace_id: str, *, platforms: list[str] | None = None
) -> list[str]:
    """Delete every Composio connection for this workspace's content user."""
    removed: list[str] = []
    for conn in list_connections(workspace_id=workspace_id, active_only=False):
        if platforms and conn["platform"] not in platforms:
            continue
        ca = conn.get("id") or ""
        if not ca:
            continue
        delete_connection(ca, revoke=True)
        removed.append(ca)
    return removed


def _stage_file_for_composio(
    *,
    path: str,
    tool_slug: str,
    toolkit_slug: str,
) -> dict[str, str]:
    """Upload a local file to Composio S3 and return FileUploadable {name,mimetype,s3key}."""
    import hashlib
    from pathlib import Path

    from composio_client import Composio as ComposioClient

    file_path = Path(path)
    if not file_path.is_file():
        raise ComposioSocialError("missing_image", f"image not found: {path}")
    raw = file_path.read_bytes()
    md5 = hashlib.md5(raw).hexdigest()
    suffix = file_path.suffix.lower()
    mimetype = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")

    client = ComposioClient(
        api_key=api_key(),
        base_url=COMPOSIO_BASE.replace("/api/v3.1", "").rstrip("/") or None,
    )
    # composio_client base_url often expects host without /api/v3.1 — try both.
    try:
        presign = client.files.create_presigned_url(
            filename=file_path.name,
            md5=md5,
            mimetype=mimetype,
            tool_slug=tool_slug,
            toolkit_slug=toolkit_slug,
        )
    except Exception:
        client = ComposioClient(api_key=api_key())
        presign = client.files.create_presigned_url(
            filename=file_path.name,
            md5=md5,
            mimetype=mimetype,
            tool_slug=tool_slug,
            toolkit_slug=toolkit_slug,
        )

    upload_url = getattr(presign, "new_presigned_url", None) or getattr(
        presign, "newPresignedUrl", None
    )
    s3key = getattr(presign, "key", None)
    if not upload_url or not s3key:
        raise ComposioSocialError(
            "presign_failed",
            f"Composio file presign incomplete: {presign!r}",
        )

    with httpx.Client(timeout=120.0) as http:
        put = http.put(
            str(upload_url),
            content=raw,
            headers={"Content-Type": mimetype, "Content-MD5": md5},
        )
        # Some S3 presigns reject Content-MD5 — retry without it.
        if put.status_code >= 400:
            put = http.put(
                str(upload_url),
                content=raw,
                headers={"Content-Type": mimetype},
            )
        if put.status_code >= 400:
            raise ComposioSocialError(
                "s3_put_failed",
                f"Composio S3 upload failed: {put.status_code} {put.text[:200]}",
            )

    return {
        "name": file_path.name,
        "mimetype": mimetype,
        "s3key": str(s3key),
    }


def publish(
    *,
    platform: str,
    body: str,
    connected_account_id: str,
    composio_user_id: str,
    author_urn: str = "",
    title: str | None = None,
    target: str | None = None,
    image_paths: list[str] | None = None,
) -> PublishResult:
    if dry_run_enabled():
        return simulated(platform)

    tool = POST_TOOL.get(platform)
    if not tool:
        return PublishResult(
            ok=False, mode="live", error=f"no composio post tool for {platform}", fatal=True
        )

    args: dict[str, Any]
    if platform == "linkedin":
        if not author_urn:
            profile = enrich_profile(
                platform="linkedin",
                user_id=composio_user_id,
                connected_account_id=connected_account_id,
            )
            author_urn = profile.get("author_urn") or ""
        if not author_urn:
            return PublishResult(
                ok=False,
                mode="live",
                error="LinkedIn author URN missing; reconnect LinkedIn.",
                fatal=True,
            )
        args = {
            "author": author_urn,
            "commentary": body,
            "visibility": "PUBLIC",
            "lifecycleState": "PUBLISHED",
        }
        paths = [p for p in (image_paths or []) if p]
        if paths:
            try:
                staged = _stage_file_for_composio(
                    path=paths[0],
                    tool_slug=tool,
                    toolkit_slug=TOOLKIT_SLUG["linkedin"],
                )
                args["images"] = [staged]
            except ComposioSocialError as exc:
                return PublishResult(
                    ok=False, mode="live", error=f"image_stage:{exc.message}", fatal=False
                )
            except Exception as exc:
                return PublishResult(
                    ok=False,
                    mode="live",
                    error=f"image_stage:{str(exc)[:200]}",
                    fatal=False,
                )
    elif platform == "reddit":
        subreddit = (target or "").lstrip("r/").strip()
        if not subreddit:
            return PublishResult(
                ok=False, mode="live", error="reddit target subreddit required", fatal=True
            )
        args = {
            "subreddit": subreddit,
            "title": (title or body.split("\n", 1)[0])[:300],
            "kind": "self",
            "text": body,
        }
    elif platform == "x":
        args = {"text": body}
    else:
        return PublishResult(ok=False, mode="live", error="unsupported platform", fatal=True)

    try:
        result = execute_tool(
            tool,
            user_id=composio_user_id,
            connected_account_id=connected_account_id,
            arguments=args,
        )
    except ComposioSocialError as exc:
        fatal = (exc.status_code in {401, 403} or exc.code in {"not_configured", "not_connectable", "needs_reconnect"}
                 if is_local() else "401" in exc.message or "403" in exc.message or "auth" in exc.message.lower())
        return PublishResult(ok=False, mode="live", error=exc.message[:300], fatal=fatal)

    data = result.get("data") if isinstance(result.get("data"), dict) else result
    external_id = ""
    external_url = ""
    if isinstance(data, dict):
        external_id = str(
            data.get("id")
            or data.get("post_id")
            or data.get("urn")
            or _dig(data, "data", "id")
            or _dig(data, "response_data", "id")
            or _find_linkedin_urn(data)
            or ""
        )
        external_url = str(
            data.get("url")
            or data.get("permalink")
            or _dig(data, "data", "url")
            or _dig(data, "response_data", "permalink")
            or ""
        )
    elif isinstance(data, str) and data.startswith("urn:li:"):
        external_id = data
    if platform == "linkedin" and external_id and not external_url:
        urn = external_id if external_id.startswith("urn:") else f"urn:li:share:{external_id}"
        external_url = f"https://www.linkedin.com/feed/update/{urn}/"
    if platform == "x" and external_id and not external_url:
        external_url = f"https://x.com/i/web/status/{external_id}"

    # Never mark live publish as ok without a content id we can verify.
    if not external_id:
        return PublishResult(
            ok=False,
            mode="live",
            error="publish_unverified:composio returned ok without external_id",
            fatal=False,
        )

    return PublishResult(
        ok=True,
        mode="live",
        external_id=external_id,
        external_url=external_url or None,
    )


def provider_overlays() -> dict[str, dict[str, Any]]:
    """Extra fields merged into provider_status() for the console."""
    out: dict[str, dict[str, Any]] = {}
    for platform in PROFILE_TOOL:
        can = connectable(platform)
        via = "composio" if can and configured() else "native"
        caveat = ""
        if platform == "x" and configured() and not connectable(platform):
            caveat = (
                "Composio has no managed X app. Add X_CLIENT_ID/X_CLIENT_SECRET "
                "so we can register a custom auth config, or use native OAuth."
            )
        elif can and managed_auth_supported(platform):
            caveat = "Connect uses Composio's OAuth. Tokens stay with Composio; this workspace stores the linked account."
        out[platform] = {
            "composio": configured(),
            "connectable": can or get_provider(platform).configured(),
            "auth_via_default": via if can else ("native" if get_provider(platform).configured() else "none"),
            "composio_managed": managed_auth_supported(platform) and configured(),
            "composio_caveat": caveat,
        }
    return out
