"""User-owned connection settings for local mode."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from app.core.local_mode import is_local
from app.core.local_secrets import get_secret, read_settings
from app.core.local_capabilities import blocked_message, enabled


def api_key() -> str:
    if is_local():
        return (get_secret("composio", "api_key") or "").strip()
    return os.environ.get("COMPOSIO_API_KEY", "").strip()


def toolkit_slug(provider: str) -> str:
    from app.content.composio_social import TOOLKIT_SLUG

    if provider == "discord":
        from app.community.composio_discord import TOOLKIT_SLUG as discord_toolkit
        return discord_toolkit
    return TOOLKIT_SLUG.get(provider, provider)


def toolkit_for_tool(slug: str) -> str:
    from app.connections.toolkits import TOOLKITS
    from app.content.composio_social import TOOLKIT_SLUG

    candidates = set(TOOLKITS) | set(TOOLKIT_SLUG.values())
    if is_local():
        candidates.update(read_settings("composio")["values"].get("auth_configs", {}))
    return next((toolkit for toolkit in sorted(candidates, key=len, reverse=True)
                 if slug.startswith((toolkit.upper() + "_", toolkit.replace("_", "").upper() + "_"))),
                slug.partition("_")[0].lower())


def app_name(provider: str) -> str:
    from app.connections.toolkits import TOOLKITS

    if provider in TOOLKITS:
        return TOOLKITS[provider]["name"]
    return {"gmail": "Gmail", "googlecalendar": "Google Calendar", "google_calendar": "Google Calendar",
            "linkedin": "LinkedIn", "x": "X", "twitter": "X", "discord": "Discord",
            "composio": "Composio", "content": "social accounts", "smtp": "email"}.get(provider, provider.title())


def auth_config_id(provider: str) -> str:
    if is_local():
        configs = read_settings("composio")["values"].get("auth_configs", {})
        value = configs.get(toolkit_slug(provider)) if isinstance(configs, dict) else None
        return value.strip() if isinstance(value, str) else ""
    return next((os.environ[name].strip() for name in auth_config_envs(provider)
                 if os.environ.get(name, "").strip()), "")


def failure_message(provider: str) -> str:
    fields = "Composio key"
    if provider not in {"composio", "content"}:
        fields += f" and {app_name(provider)} authorization ID (App authorization IDs)"
    return (f"Check your {fields} in Settings > Keys and connections > Your connected apps, "
            f"then reconnect your {app_name(provider)} account on the Connections screen and try again.")


def public_url(default: str = "http://127.0.0.1:3300") -> str:
    value = os.environ.get("SUPYMEM_PUBLIC_URL") or default
    message = "Use an HTTPS public URL or an HTTP localhost origin in your local settings, then try connecting again."
    try:
        parsed = urlsplit(value)
        valid = (
            bool(parsed.hostname) and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            and "\\" not in value and not any(character.isspace() or ord(character) < 32 for character in value)
            and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))
            and (parsed.port is None or 0 < parsed.port < 65536)
        )
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return f"{parsed.scheme}://{parsed.netloc}"


def auth_config_envs(provider: str) -> tuple[str, ...]:
    from app.connections import toolkits

    if provider in toolkits.TOOLKITS:
        return (toolkits.TOOLKITS[provider]["auth_config_env"],)
    if provider == "gmail":
        from app.gtm.supymem.composio_gmail import AUTH_CONFIG_ENV

        return (AUTH_CONFIG_ENV,)
    if provider == "discord":
        from app.community.composio_discord import AUTH_CONFIG_ENV_ALIASES

        return AUTH_CONFIG_ENV_ALIASES
    from app.content.composio_social import AUTH_CONFIG_ENV

    return (AUTH_CONFIG_ENV[provider],) if provider in AUTH_CONFIG_ENV else ()


def missing(provider: str) -> list[str]:
    if is_local():
        if provider == "smtp":
            values = read_settings("smtp")["values"]
            absent = ["smtp." + name for name in ("host", "user") if not str(values.get(name) or "").strip()]
            if not get_secret("smtp", "password"):
                absent.append("smtp.password")
            if not enabled("send_email"):
                absent.append("send_email")
            return absent
        absent = [] if api_key() else ["composio.api_key"]
        if provider not in {"composio", "content"} and not auth_config_id(provider):
            absent.append(f"composio.auth_configs[{toolkit_slug(provider)}]")
        if not enabled("connected_apps"):
            absent.append("connected_apps")
        return absent
    if provider == "smtp":
        return [name for name in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(name, "").strip()]
    auth_names = auth_config_envs(provider)
    if not auth_names and provider not in {"composio", "content"}:
        return []
    absent = [] if os.environ.get("COMPOSIO_API_KEY", "").strip() else ["COMPOSIO_API_KEY"]
    if auth_names and not any(os.environ.get(name, "").strip() for name in auth_names):
        absent.append(auth_names[0])
    return absent


def setup_message(provider: str) -> str:
    if is_local():
        if provider == "smtp":
            return blocked_message("send_email")
        message = f"For {app_name(provider)}: {blocked_message('connected_apps')}" if not enabled("connected_apps") else ""
        if provider not in {"composio", "content"} and not auth_config_id(provider):
            return ((message + " " if message else "")
                    + f"In Settings > Keys and connections > Your connected apps, save your {app_name(provider)} authorization ID in App authorization IDs. "
                    f"Open Connections, reconnect your {app_name(provider)} account, then try again.")
        return message or failure_message(provider)
    if provider == "smtp":
        return "Add your SMTP server and sender in local settings, then refresh your email connection."
    return (
        "Add your connection key and this app's authorization in local settings, then try connecting again. "
        "You can use SMTP email while you set this up."
    )
