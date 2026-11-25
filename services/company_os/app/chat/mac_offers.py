"""Charlie's "Let Supymem do it on your Mac" offer for Mac-doable blockers.

The offer only *prepares* the existing reviewed device work order (POST
device-work-orders/from-chat). The founder approves in the review; voice can
never approve. Secrets (passwords, payment, 2FA) are founder-only: no offer.
"""
from __future__ import annotations

import os
import re
from typing import Any

from app.chat.recovery import button

# Codes here alias into app/chat/blockers.py GUIDES via blockers.ALIASES (one vocabulary).
# Goals must classify to a supported prepare path (see tests).
GOALS: dict[str, tuple[str, str]] = {
    "mac_screen_recording": (
        "Open System Settings on this Mac, go to Privacy & Security, Screen Recording, and point at "
        "the Supymem toggle. Do not change any toggle.", "com.apple.systempreferences"),
    "mac_accessibility": (
        "Open System Settings on this Mac, go to Privacy & Security, Accessibility, and point at "
        "the Supymem toggle. Do not change any toggle.", "com.apple.systempreferences"),
    "cursor_open": ("Inspect the venture workspace with Cursor, read-only.", "cursor"),
    "live_site_check": ("Open the live site and check the waitlist in my browser.", "browser"),
    "signup_page": (
        "Go to {url} in my browser and fill in the non-secret signup fields. "
        "Do not enter passwords, payment details or codes.", "browser"),
}
FOUNDER_ONLY = {"password_required", "payment_required", "two_factor_code"}
_KEYWORDS = (  # (regex on blocker/next-step text, code)
    (r"screen recording", "mac_screen_recording"), (r"accessibility permission", "mac_accessibility"),
    (r"\b(?:password|log ?in credentials)\b", "password_required"),
    (r"\b(?:card|payment|billing details)\b", "payment_required"),
    (r"\b(?:2fa|two-factor|verification code)\b", "two_factor_code"),
)
FOUNDER_ONLY_NOTE = ("This one is yours to do: passwords, payment details and verification codes stay "
                     "with you. Supymem never types or sees them.")
_NEEDS_PERMS = ("screen-recording", "accessibility")

# Authority is a leading imperative, not an incidental/quoted Mac mention.
# Mirror the client gate in reviewableDeviceRequest.ts and its parity fixture.
MAC_CONTROL_LEADIN = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"use\s+(?:my|this|the)\s+mac\s+to\s+", re.I,
)
_EXPLICIT_MAC_REQUEST = re.compile(
    MAC_CONTROL_LEADIN.pattern +
    r"(?:open|launch|go to|visit|navigate|browse|search|click|press|select|clear|"
    r"type|write|enter|scroll|inspect|read|report|describe)\b", re.I,
)


def non_authorizing_mac_mention(text: str) -> bool:
    """A declined command or quoted example cannot prepare a device review."""
    if re.match(r"^\s*(?:please\s+)?(?:do\s+not|don['’]t|never)\s+use\s+(?:my|this|the)\s+mac\s+to\b", text, re.I):
        return True
    return bool(not _EXPLICIT_MAC_REQUEST.match(text)
                and re.search(r"[\"'“]\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
                              r"use\s+(?:my|this|the)\s+mac\s+to\b", text, re.I))


def explicit_mac_control_request(text: str) -> bool:
    if not _EXPLICIT_MAC_REQUEST.match(text or ""):
        return False
    from app.chat.intent import requested_venture_screen

    return not (re.search(r"\bsupymem\b", text, re.I)
                and requested_venture_screen(text) is not None)


def direct_mac_offer(text: str) -> dict:
    """API fallback: offer the existing client's scoped prepare, never execute."""
    return {
        "kind": "navigation",
        "body_md": "This needs a scoped Mac task review. Prepare the exact request below, then open "
                   "the saved review and approve it before execution. No Mac task has been prepared or run yet.",
        "actions": [{"label": "Prepare Mac task for review", "action": "mac.task.prepare",
                     "style": "primary", "text": text, "payload": {"text": text}}],
        "citations": [],
    }


def code_from_text(text: str) -> str | None:
    low = (text or "").lower()
    return next((code for pat, code in _KEYWORDS if re.search(pat, low)), None)


NATIVE_UNAVAILABLE_REPLY = {
    "kind": "text",
    "body_md": "Native Mac app control is coming soon in beta, so I can't run Mac tasks yet. "
               "I can give you the manual steps instead. Nothing was prepared or run.",
    "actions": [button("mac.task")], "citations": [],
}


def native_mac_enabled() -> bool:
    """Exact-value gate shared with device orders: only the string "1" turns native Mac control on."""
    return os.environ.get("DEVICE_DESKTOP_EXECUTION_ENABLED") == "1"


def cu_offer(venture_id: str, blocker_code: str | None, context: dict[str, Any] | None = None) -> dict | None:
    """Chat action for a Mac-doable blocker, else None. `venture_id` is kept for per-venture gating."""
    ctx = context or {}
    if blocker_code not in GOALS:
        return None
    template, app = GOALS[blocker_code]
    if not native_mac_enabled():
        if (app != "browser" or os.getenv("DEVICE_BROWSER_EXECUTION_ENABLED") != "1"
                or os.getenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED") != "1"):
            return None
        return {"label": "Prepare a browser task", "href": f"/v/{venture_id}/settings#settings-computer-use", "style": "primary"}
    if "{url}" in template:
        url = str(ctx.get("url") or "")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[^\s]*)?", url):
            return None
        template = template.format(url=url)
    perms = ctx.get("permissions")
    if isinstance(perms, dict) and any(perms.get(p) != "granted" for p in _NEEDS_PERMS):
        return {"label": "Grant Mac permissions", "href": f"/v/{venture_id}/settings#computer-use", "style": "primary"}
    return {"label": "Let Supymem do it on your Mac", "action": "mac.task.prepare", "style": "primary",
            "text": template, "payload": {"goal": template, "app": app, "blocker_code": blocker_code}}
