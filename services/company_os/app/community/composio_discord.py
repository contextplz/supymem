"""Composio DISCORDBOT client — connect + REST actuation. Fail closed. Never fake done.

Uses toolkit slug ``discordbot`` / tool prefixes ``DISCORDBOT_*``.
The user toolkit ``DISCORD`` is intentionally not used for community ops.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from app.connections import local_config
from app.core.local_mode import is_local

log = logging.getLogger(__name__)

COMPOSIO_BASE = os.environ.get(
    "COMPOSIO_API_BASE", "https://backend.composio.dev/api/v3.1"
).rstrip("/")

TOOLKIT_SLUG = "discordbot"
AUTH_CONFIG_ENV = "COMPOSIO_DISCORDBOT_AUTH_CONFIG_ID"
# Back-compat alias if someone set the older name from the teaching doc
AUTH_CONFIG_ENV_ALIASES = ("COMPOSIO_DISCORDBOT_AUTH_CONFIG_ID", "COMPOSIO_DISCORD_AUTH_CONFIG_ID")

# Composio account statuses that can execute DISCORDBOT tools.
_ACTIVE_STATUSES = frozenset({"active", "connected", "enabled", "success"})
# Started OAuth / link but bot invite not finished — never treat as connected.
_PENDING_STATUSES = frozenset(
    {"initializing", "pending", "initiated", "initiating", "awaiting", "in_progress"}
)

# Soft persist so auto-created auth configs survive ops restart (env still preferred).
_AUTH_CONFIG_CACHE = (
    Path(__file__).resolve().parents[4] / "data" / "composio_discordbot_auth_config_id"
)


class DiscordBotError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        if is_local() and code == "not_configured":
            message = local_config.setup_message("discord")
        super().__init__(message)
        self.code = code
        self.message = message


def api_key() -> str:
    return local_config.api_key()


def configured() -> bool:
    if is_local():
        return not local_config.missing("discord")
    return bool(api_key())


def _read_auth_config_cache() -> str:
    try:
        if _AUTH_CONFIG_CACHE.is_file():
            return _AUTH_CONFIG_CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _write_auth_config_cache(auth_id: str) -> None:
    if not auth_id:
        return
    try:
        _AUTH_CONFIG_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _AUTH_CONFIG_CACHE.write_text(auth_id.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist DISCORDBOT auth config id: %s", exc)


def auth_config_id_from_env() -> str:
    if is_local():
        return local_config.auth_config_id("discord")
    for name in AUTH_CONFIG_ENV_ALIASES:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    cached = _read_auth_config_cache()
    if cached:
        # Hydrate process env so status pills show auth_config_env_set after restart.
        os.environ[AUTH_CONFIG_ENV] = cached
        return cached
    return ""


def default_user_id(workspace_id: str | None = None) -> str:
    explicit = (
        os.environ.get("COMPOSIO_COMMUNITY_USER_ID", "").strip()
        or os.environ.get("COMPOSIO_CONTENT_USER_ID", "").strip()
        or os.environ.get("SUPYMEM_DEFAULT_USER_ID", "").strip()
    )
    if explicit:
        return explicit
    if workspace_id:
        return f"community:{workspace_id}"
    return "local-user"


def callback_url() -> str:
    if is_local():
        return local_config.public_url("http://127.0.0.1:3100") + "/community"
    explicit = (
        os.environ.get("COMPOSIO_DISCORDBOT_CALLBACK_URL", "").strip()
        or os.environ.get("COMPOSIO_DISCORD_CALLBACK_URL", "").strip()
    )
    if explicit:
        return explicit
    origin = (
        os.environ.get("CONSOLE_URL")
        or os.environ.get("NEXT_PUBLIC_APP_URL")
        or "http://127.0.0.1:3100"
    ).rstrip("/")
    return f"{origin}/community"


def _headers() -> dict[str, str]:
    if is_local() and local_config.missing("discord"):
        raise DiscordBotError("not_configured", local_config.setup_message("discord"))
    key = api_key()
    if not key:
        raise DiscordBotError("not_configured", "COMPOSIO_API_KEY is not set.")
    return {"x-api-key": key, "Content-Type": "application/json"}


def _toolkit_version() -> str:
    return (
        os.environ.get("DISCORDBOT_TOOLKIT_VERSION", "").strip()
        or os.environ.get("COMPOSIO_TOOLKIT_VERSION", "latest").strip()
        or "latest"
    )


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    url = f"{COMPOSIO_BASE}{path}"
    with httpx.Client(timeout=timeout) as client:
        res = client.request(
            method, url, headers=_headers(), json=json_body, params=params
        )
    try:
        data = res.json()
    except Exception:
        data = {"raw": res.text[:500]}
    if res.status_code >= 400:
        msg = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("slug") or "")
            else:
                msg = str(err or data.get("message") or data.get("detail") or "")
        raise DiscordBotError(
            "http_error",
            local_config.failure_message("discord") if is_local() else msg or f"Composio {path} returned HTTP {res.status_code}.",
        )
    return data if isinstance(data, dict) else {"data": data}


def needs_you_status(workspace_id: str | None = None) -> dict[str, Any]:
    """Readiness without secrets — for /community/status and UI pills."""
    blockers: list[dict[str, str]] = []
    key_ok = configured()
    if is_local() and not key_ok:
        blockers.append({"code": "discord_settings_required", "message": local_config.setup_message("discord")})
    elif not key_ok:
        blockers.append(
            {
                "code": "composio_api_key_missing",
                "message": "Set COMPOSIO_API_KEY to use Discord via Composio.",
            }
        )
    auth_env = auth_config_id_from_env()
    if key_ok and not auth_env:
        blockers.append(
            {
                "code": "discordbot_auth_config_missing",
                "message": (
                    "COMPOSIO_DISCORDBOT_AUTH_CONFIG_ID not set. "
                    "Connect Discord will try to create/list a managed auth config; "
                    "or create one in the Composio dashboard for toolkit discordbot."
                ),
            }
        )
    return {
        "toolkit": TOOLKIT_SLUG,
        "toolkit_note": "Use DISCORDBOT (bot). User DISCORD toolkit is wrong for community ops.",
        "composio_configured": key_ok,
        "auth_config_env_set": bool(auth_env),
        "auth_config_env": AUTH_CONFIG_ENV if auth_env else None,
        "callback_url": callback_url(),
        "user_id": default_user_id(workspace_id),
        "can_create_guild": False,
        "guild_create_note": "Discord apps cannot create guilds. Founder creates server, then invites bot.",
        "triggers": 0,
        "triggers_note": "DISCORDBOT has 0 Composio triggers — realtime needs gateway/discord.py (NeedsYou).",
        "blockers": blockers,
        "ready_to_connect": key_ok,
    }


def ensure_auth_config() -> str:
    """Return auth config id for discordbot — env, list, or create managed."""
    if is_local():
        if local_config.missing("discord"):
            raise DiscordBotError("not_configured", local_config.setup_message("discord"))
        return local_config.auth_config_id("discord")

    if not configured():
        raise DiscordBotError("not_configured", "COMPOSIO_API_KEY is not set.")

    def _list_ids() -> list[str]:
        listed = _request(
            "GET",
            "/auth_configs",
            params={"toolkit_slug": TOOLKIT_SLUG, "limit": 20},
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

    existing = auth_config_id_from_env()
    known = _list_ids()
    if existing and existing in known:
        return existing
    if existing and existing not in known:
        log.warning("ignoring stale DISCORDBOT auth config id")
    if known:
        os.environ[AUTH_CONFIG_ENV] = known[0]
        _write_auth_config_cache(known[0])
        return known[0]

    created = _request(
        "POST",
        "/auth_configs",
        json_body={
            "toolkit": {"slug": TOOLKIT_SLUG},
            "auth_config": {
                "type": "use_composio_managed_auth",
                "name": "Company OS Community — DiscordBot",
            },
        },
    )
    auth = created.get("auth_config") if isinstance(created.get("auth_config"), dict) else {}
    auth_id = str(auth.get("id") or created.get("id") or "")
    if not auth_id:
        raise DiscordBotError(
            "auth_config_failed",
            "Composio did not return a discordbot auth config. "
            "Create one in the dashboard and set COMPOSIO_DISCORDBOT_AUTH_CONFIG_ID.",
        )
    os.environ[AUTH_CONFIG_ENV] = auth_id
    _write_auth_config_cache(auth_id)
    return auth_id


def start_connect(
    workspace_id: str | None = None,
    *,
    callback: str | None = None,
) -> dict[str, Any]:
    if not configured():
        raise DiscordBotError(
            "not_configured",
            "NeedsYou: COMPOSIO_API_KEY is not set.",
        )
    try:
        auth_config_id = ensure_auth_config()
    except DiscordBotError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DiscordBotError(
            "auth_config_failed",
            f"NeedsYou: could not resolve DISCORDBOT auth config ({exc}).",
        ) from exc

    uid = default_user_id(workspace_id)
    cb = (callback or "").strip() or callback_url()
    data = _request(
        "POST",
        "/connected_accounts/link",
        json_body={
            "auth_config_id": auth_config_id,
            "user_id": uid,
            "callback_url": cb,
            "allow_multiple": True,
        },
    )
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    redirect = (
        data.get("redirect_url")
        or data.get("redirectUrl")
        or nested.get("redirect_url")
        or nested.get("redirectUrl")
    )
    conn_id = data.get("id") or data.get("connection_id") or nested.get("id")
    if not redirect:
        raise DiscordBotError(
            "connect_link_missing",
            "NeedsYou: Composio did not return a Discord bot connect URL.",
        )
    return {
        "ok": True,
        "connection_id": conn_id,
        "redirect_url": redirect,
        "app_user_id": uid,
        "toolkit": TOOLKIT_SLUG,
        "auth_config_id": auth_config_id,
        "note": "Create the Discord server yourself, then complete this invite/OAuth.",
    }


def list_connections(workspace_id: str | None = None) -> list[dict[str, Any]]:
    if not configured():
        return []
    uid = default_user_id(workspace_id)
    # Prefer toolkit-wide list then filter by user — Composio's combined
    # user_ids+toolkit_slugs filter has intermittently dropped ACTIVE accounts.
    data = _request(
        "GET",
        "/connected_accounts",
        params={"toolkit_slugs": TOOLKIT_SLUG, "limit": 100},
    )
    items = data.get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        entity = str(
            it.get("user_id") or it.get("entity_id") or it.get("userId") or ""
        )
        if entity and entity != uid:
            continue
        out.append(it)
    if out:
        return out
    # Fallback: original filtered query
    data = _request(
        "GET",
        "/connected_accounts",
        params={"user_ids": uid, "toolkit_slugs": TOOLKIT_SLUG},
    )
    items = data.get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    return [it for it in items if isinstance(it, dict)]


def _is_snowflake(value: Any) -> bool:
    s = str(value or "").strip()
    return bool(s.isdigit() and 17 <= len(s) <= 20)


def discover_installed_guild(workspace_id: str | None = None) -> dict[str, Any]:
    """Best-effort guild discovery from the ACTIVE connected account.

    Composio often redacts ``data.guild.id`` in GET connected_accounts responses.
    When a real snowflake is present we return it; otherwise NeedsYou with steps.
    """
    active = active_connection(workspace_id)
    if not active or not active.get("id"):
        return {
            "ok": False,
            "guild_id": None,
            "error": "not_connected",
            "needs_you": True,
            "founder_steps": [
                "Create a Discord server (File → New Server, or + in the server list).",
                "Open Community Lab → Connect Discord and finish the bot invite into that server.",
                "Copy Server Settings → Widget → Server ID (enable Developer Mode → right-click server → Copy Server ID).",
                "Paste guild_id in section D → Provision skeleton.",
            ],
        }
    account_id = str(active["id"])
    guild_id: str | None = None
    guild_name: str | None = None
    redacted = False
    try:
        detail = _request("GET", f"/connected_accounts/{account_id}")
        data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
        guild = data.get("guild") if isinstance(data, dict) else None
        if not isinstance(guild, dict):
            # Some payloads nest under params
            params = detail.get("params") if isinstance(detail.get("params"), dict) else {}
            guild = params.get("guild") if isinstance(params, dict) else None
        if isinstance(guild, dict):
            raw_id = str(guild.get("id") or "").strip()
            raw_name = str(guild.get("name") or "").strip()
            if raw_id.upper() == "REDACTED" or raw_name.upper() == "REDACTED":
                redacted = True
            if _is_snowflake(raw_id):
                guild_id = raw_id
                guild_name = raw_name or None
            # system_channel_id is not guild_id but proves install happened
            elif guild.get("system_channel_id") or guild.get("owner_id"):
                redacted = redacted or True
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "guild_id": None,
            "connected_account_id": account_id,
            "error": str(exc),
            "needs_you": True,
            "founder_steps": [
                "Bot is connected but guild lookup failed — paste guild_id manually in section D.",
                "Discord → Server Settings → Widget, or enable Developer Mode → Copy Server ID.",
            ],
        }
    if guild_id:
        return {
            "ok": True,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "connected_account_id": account_id,
            "source": "connected_account.data.guild",
            "needs_you": False,
        }
    return {
        "ok": False,
        "guild_id": None,
        "connected_account_id": account_id,
        "guild_object_present": True,
        "guild_id_redacted": redacted,
        "error": "guild_id_redacted" if redacted else "guild_id_missing",
        "needs_you": True,
        "block_reason": (
            "NeedsYou: Discord bot is ACTIVE and was installed into a server, but "
            "Composio redacts guild_id from the connection payload. Paste the Server ID "
            "(Developer Mode → right-click server → Copy Server ID) into Provision."
        ),
        "founder_steps": [
            "Open Discord → the server where you invited the Company OS / Composio bot.",
            "Enable Developer Mode (User Settings → Advanced).",
            "Right-click the server icon → Copy Server ID.",
            "Paste into Community Lab → D. Provision → guild_id → Provision skeleton.",
            "Then Create invite and share that link (email / LinkedIn / GTM).",
        ],
    }


def _account_status(item: dict[str, Any]) -> str:
    return str(item.get("status") or item.get("connection_status") or "").lower()


def active_connection(workspace_id: str | None = None) -> dict[str, Any] | None:
    """Return a connection that can execute tools — ACTIVE only.

    INITIALIZING / pending accounts must not flip ``connected=true``; Composio
    rejects tool calls until OAuth + bot invite finish.
    """
    for it in list_connections(workspace_id):
        status = _account_status(it)
        if status in _ACTIVE_STATUSES:
            return it
        # Empty status with an id: legacy/ambiguous — only accept if not clearly pending.
        if it.get("id") and status == "":
            return it
    return None


def pending_connections(workspace_id: str | None = None) -> list[dict[str, Any]]:
    return [
        it
        for it in list_connections(workspace_id)
        if _account_status(it) in _PENDING_STATUSES
    ]


def connection_status(workspace_id: str | None = None) -> dict[str, Any]:
    base = needs_you_status(workspace_id)
    if not configured():
        return {
            **base,
            "connected": False,
            "accounts": [],
            "needs_you": True,
            "needs_you_reason": local_config.setup_message("discord") if is_local() else "COMPOSIO_API_KEY missing",
        }
    try:
        accounts = list_connections(workspace_id)
        active = active_connection(workspace_id)
        pending = [
            a for a in accounts if _account_status(a) in _PENDING_STATUSES
        ]
        connected = bool(active)
        reason = None
        if not connected:
            if pending:
                statuses = sorted(
                    {
                        str(a.get("status") or a.get("connection_status") or "pending")
                        for a in pending
                    }
                )
                reason = (
                    "DISCORDBOT connection started but not ACTIVE yet "
                    f"({', '.join(statuses)}). Finish Discord OAuth / invite the bot "
                    "into your server, then Refresh Discord status."
                )
            else:
                reason = (
                    "No active DISCORDBOT connection. Create a Discord server, "
                    "then use Connect Discord."
                )
        guild_hint = discover_installed_guild(workspace_id) if connected else {
            "ok": False,
            "guild_id": None,
            "needs_you": True,
        }
        needs_guild = bool(
            connected
            and not (guild_hint.get("ok") and guild_hint.get("guild_id"))
        )
        return {
            **base,
            "connected": connected,
            "accounts": [
                {
                    "id": a.get("id"),
                    "status": a.get("status") or a.get("connection_status"),
                    "toolkit": TOOLKIT_SLUG,
                }
                for a in accounts
            ],
            "pending_account_ids": [a.get("id") for a in pending if a.get("id")],
            "active_account_id": (active or {}).get("id"),
            "installed_guild": guild_hint,
            "needs_guild_id": needs_guild,
            # needs_you = connection gap only (keep provision unlocked when ACTIVE)
            "needs_you": not connected,
            "needs_you_reason": reason,
            "guild_needs_you_reason": (
                str(guild_hint.get("block_reason") or "") if needs_guild else None
            ),
            "founder_steps": (
                guild_hint.get("founder_steps")
                if needs_guild
                else (
                    [
                        "Create Discord server",
                        "Connect Discord (invite bot)",
                        "Paste guild_id → Provision",
                    ]
                    if not connected
                    else None
                )
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "connected": False,
            "accounts": [],
            "needs_you": True,
            "needs_you_reason": str(exc),
            "error": str(exc),
        }


def execute_tool(
    slug: str,
    *,
    user_id: str,
    connected_account_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not slug.upper().startswith("DISCORDBOT_"):
        raise DiscordBotError(
            "wrong_toolkit",
            f"Refusing non-DISCORDBOT tool {slug}. Community ops use DISCORDBOT only.",
        )
    body: dict[str, Any] = {
        "user_id": user_id,
        "connected_account_id": connected_account_id,
        "arguments": arguments,
        "version": _toolkit_version(),
    }
    if _toolkit_version() == "latest":
        body["dangerously_skip_version_check"] = True
    data = _request("POST", f"/tools/execute/{slug}", json_body=body, timeout=60.0)
    successful = data.get("successful")
    if successful is False:
        raise DiscordBotError("tool_failed", str(data.get("error") or f"{slug} failed."))
    return data


def list_toolkit_tools(limit: int = 50) -> dict[str, Any]:
    """Probe tool list for live evidence (no secrets)."""
    if not configured():
        return {"ok": False, "error": "COMPOSIO_API_KEY missing", "tools": []}
    data = _request(
        "GET",
        "/tools",
        params={"toolkit_slug": TOOLKIT_SLUG, "limit": limit},
    )
    items = data.get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    names: list[str] = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            slug = it.get("slug") or it.get("name") or it.get("tool_slug")
            if slug:
                names.append(str(slug))
    return {
        "ok": True,
        "toolkit": TOOLKIT_SLUG,
        "count": len(names),
        "sample": names[:40],
        "has_create_message": any("CREATE_MESSAGE" in n for n in names),
        "has_create_guild_channel": any("CREATE_GUILD_CHANNEL" in n for n in names),
        "has_list_guild": any("GUILD" in n for n in names),
    }


def _require_active(workspace_id: str | None) -> tuple[str, str]:
    active = active_connection(workspace_id)
    if not active or not active.get("id"):
        raise DiscordBotError(
            "not_connected",
            "NeedsYou: Discord bot not connected. Use Connect Discord first.",
        )
    return default_user_id(workspace_id), str(active["id"])


def list_guild_channels(workspace_id: str | None, guild_id: str) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_LIST_GUILD_CHANNELS",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"guild_id": guild_id},
    )


def create_guild_channel(
    workspace_id: str | None,
    *,
    guild_id: str,
    name: str,
    channel_type: int = 0,
) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_CREATE_GUILD_CHANNEL",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={
            "guild_id": guild_id,
            "name": name,
            "type": channel_type,
        },
    )


def create_guild_role(
    workspace_id: str | None,
    *,
    guild_id: str,
    name: str,
) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_CREATE_GUILD_ROLE",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"guild_id": guild_id, "name": name},
    )


def create_message(
    workspace_id: str | None,
    *,
    channel_id: str,
    content: str,
) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_CREATE_MESSAGE",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"channel_id": channel_id, "content": content},
    )


def create_channel_invite(
    workspace_id: str | None,
    *,
    channel_id: str,
    max_age: int = 86400,
    max_uses: int = 0,
) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_CREATE_CHANNEL_INVITE",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={
            "channel_id": channel_id,
            "max_age": max_age,
            "max_uses": max_uses,
        },
    )


def list_guild_members(
    workspace_id: str | None,
    *,
    guild_id: str,
    limit: int = 10,
) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_LIST_GUILD_MEMBERS",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"guild_id": guild_id, "limit": limit},
    )


def get_guild(workspace_id: str | None, guild_id: str) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_GET_GUILD",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"guild_id": guild_id},
    )


def list_guild_roles(workspace_id: str | None, guild_id: str) -> dict[str, Any]:
    user_id, account_id = _require_active(workspace_id)
    return execute_tool(
        "DISCORDBOT_LIST_GUILD_ROLES",
        user_id=user_id,
        connected_account_id=account_id,
        arguments={"guild_id": guild_id},
    )


def run_lab_action(
    workspace_id: str | None,
    action: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab actions — fail closed. Prefer list/status over posting into unknown servers."""
    p = dict(params or {})
    action = (action or "").strip().lower()
    try:
        if action in {"status", "connection_status"}:
            return {"ok": True, "action": action, "result": connection_status(workspace_id)}
        if action in {"list_tools", "probe_tools"}:
            return {"ok": True, "action": action, "result": list_toolkit_tools()}
        if action in {"discover_guild", "list_guilds", "installed_guild"}:
            hint = discover_installed_guild(workspace_id)
            return {
                "ok": bool(hint.get("ok")),
                "action": action,
                "result": hint,
                "status": "ok" if hint.get("ok") else "blocked",
                "block_reason": hint.get("block_reason"),
                "error": hint.get("error"),
            }
        if action == "list_channels":
            guild_id = str(p.get("guild_id") or "")
            if not guild_id:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": "NeedsYou: guild_id required",
                    "error": "guild_id_required",
                }
            return {
                "ok": True,
                "action": action,
                "result": list_guild_channels(workspace_id, guild_id),
            }
        if action == "list_members":
            guild_id = str(p.get("guild_id") or "")
            if not guild_id:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": "NeedsYou: guild_id required",
                    "error": "guild_id_required",
                }
            return {
                "ok": True,
                "action": action,
                "result": list_guild_members(
                    workspace_id, guild_id=guild_id, limit=int(p.get("limit") or 10)
                ),
            }
        if action == "create_invite":
            channel_id = str(p.get("channel_id") or "")
            if not channel_id:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": "NeedsYou: channel_id required",
                    "error": "channel_id_required",
                }
            return {
                "ok": True,
                "action": action,
                "result": create_channel_invite(workspace_id, channel_id=channel_id),
            }
        if action == "send_message":
            channel_id = str(p.get("channel_id") or "")
            content = str(p.get("content") or p.get("message") or "").strip()
            confirm = bool(p.get("confirm"))
            if not channel_id or not content:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": "NeedsYou: channel_id and content required",
                    "error": "message_params_required",
                }
            if not confirm:
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": "NeedsYou: set confirm=true to send a Discord message",
                    "error": "confirm_required",
                }
            return {
                "ok": True,
                "action": action,
                "result": create_message(
                    workspace_id, channel_id=channel_id, content=content
                ),
            }
        if action == "create_guild":
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": (
                    "NeedsYou: Discord bots cannot create guilds. "
                    "Create the server in the Discord app, then invite the bot."
                ),
                "error": "guild_create_forbidden",
            }
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": f"NeedsYou: unknown action {action}",
            "error": "unknown_action",
            "allowed": [
                "status",
                "list_tools",
                "discover_guild",
                "list_channels",
                "list_members",
                "create_invite",
                "send_message",
            ],
        }
    except DiscordBotError as exc:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": exc.message,
            "error": exc.code,
            "action": action,
        }
