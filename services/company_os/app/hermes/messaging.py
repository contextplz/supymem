"""Platform messaging channels for Computer Use. Anthropic is not used."""

from __future__ import annotations

import os
from typing import Any

# Personal WhatsApp QR stays on the Mac. Cloud API + chat bots use our secrets.
PLATFORM_CHANNELS = (
    ("telegram", "TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    ("discord", "DISCORD_BOT_TOKEN", "DISCORD_BOT_TOKEN"),
    ("slack", "SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"),
    (
        "whatsapp_cloud",
        "WHATSAPP_CLOUD_ACCESS_TOKEN",
        "WHATSAPP_CLOUD_ACCESS_TOKEN",
    ),
)


def _val(name: str) -> str:
    return os.environ.get(name, "").strip()


def messaging_status() -> dict[str, Any]:
    armed: list[str] = []
    pending: list[str] = []
    extras: dict[str, str] = {}
    for channel, required, _env in PLATFORM_CHANNELS:
        if _val(required):
            armed.append(channel)
        else:
            pending.append(channel)
    if _val("WHATSAPP_CLOUD_ACCESS_TOKEN"):
        for key in (
            "WHATSAPP_CLOUD_PHONE_NUMBER_ID",
            "WHATSAPP_CLOUD_APP_ID",
            "WHATSAPP_CLOUD_APP_SECRET",
            "WHATSAPP_CLOUD_WABA_ID",
            "WHATSAPP_CLOUD_VERIFY_TOKEN",
        ):
            if _val(key):
                extras[key] = _val(key)
    return {
        "armed": armed,
        "pending": pending,
        "ignored": ["anthropic"],
        "whatsapp_personal": "qr_on_mac",
        "extras_present": sorted(extras),
    }


def messaging_env_lines() -> list[str]:
    lines: list[str] = []
    for _channel, required, env_name in PLATFORM_CHANNELS:
        value = _val(required)
        if value:
            lines.append(f"{env_name}={value}")
    if _val("WHATSAPP_CLOUD_ACCESS_TOKEN"):
        for key in (
            "WHATSAPP_CLOUD_PHONE_NUMBER_ID",
            "WHATSAPP_CLOUD_APP_ID",
            "WHATSAPP_CLOUD_APP_SECRET",
            "WHATSAPP_CLOUD_WABA_ID",
            "WHATSAPP_CLOUD_VERIFY_TOKEN",
            "WHATSAPP_CLOUD_WEBHOOK_HOST",
            "WHATSAPP_CLOUD_WEBHOOK_PATH",
        ):
            value = _val(key)
            if value:
                lines.append(f"{key}={value}")
        lines.append("WHATSAPP_ENABLED=true")
    telegram_allow = _val("TELEGRAM_ALLOWED_USERS")
    if telegram_allow:
        lines.append(f"TELEGRAM_ALLOWED_USERS={telegram_allow}")
    return lines


def messaging_gateway_yaml() -> str:
    status = messaging_status()
    armed = set(status["armed"])
    lines = [
        "gateway:",
        "  platforms:",
    ]
    for channel, _req, _env in PLATFORM_CHANNELS:
        on = "true" if channel in armed else "false"
        lines.append(f"    {channel}:")
        lines.append(f"      enabled: {on}")
    lines.append("    # anthropic: ignored — Supymem Computer Use uses workspace credits")
    return "\n".join(lines) + "\n"
