"""Deterministic pause classifiers for owned-browser computer use.

Page labels are untrusted data. These heuristics decide when Supymem must stop
and ask the founder, never when a model may proceed through a sensitive step.
"""
from __future__ import annotations

import re
from typing import Literal

PauseKind = Literal["password", "otp_2fa", "captcha", "terms", "payment", "send"]

PAUSE_MESSAGES: dict[PauseKind, str] = {
    "password": "Type your password in the Supymem browser window, then press Continue.",
    "otp_2fa": "Enter the 2FA or verification code yourself, then press Continue.",
    "captcha": "Complete the CAPTCHA yourself in the Supymem browser window, then press Continue.",
    "terms": "Accept the terms yourself in the Supymem browser window, then press Continue.",
    "payment": "Review and complete any payment yourself. Supymem will not enter card details.",
    "send": "Confirm and send yourself if you want to continue. Supymem will not send on its own.",
}

_PASSWORD = re.compile(
    r"password|passwd|passcode|current-password|new-password", re.I
)
_OTP = re.compile(
    r"\b(2fa|mfa|otp|totp|one[-\s]?time|verification code|authenticator|"
    r"security code|sms code|email code)\b",
    re.I,
)
_CAPTCHA = re.compile(
    r"captcha|recaptcha|hcaptcha|turnstile|challenge-platform|cf-challenge",
    re.I,
)
_TERMS = re.compile(
    r"\b(i agree|terms of (service|use)|privacy policy|accept (the )?terms|"
    r"terms and conditions|tos)\b",
    re.I,
)
_PAYMENT = re.compile(
    r"\b(credit card|card number|cvv|cvc|expir|billing|payment|pay now|"
    r"buy now|checkout|stripe|cardholder)\b",
    re.I,
)
_SEND = re.compile(
    r"\b(send|submit|post|publish|invite|create account|sign up|signup|"
    r"register|place order|confirm purchase|pay)\b",
    re.I,
)
_NAME_FIELD = re.compile(r"\b(name|full name|first name|last name|display name)\b", re.I)
_EMAIL_FIELD = re.compile(r"\b(e-?mail|email address)\b", re.I)
_FALSE_PASSWORD = re.compile(r"forgot\s+password|reset\s+password|password\s+manager", re.I)


def founder_prompt(kind: PauseKind) -> str:
    return PAUSE_MESSAGES[kind]


def field_kind(info: dict) -> str:
    """Classify an input for fill lookup. Never returns a secret value."""
    input_type = (info.get("input_type") or "").lower()
    autocomplete = (info.get("autocomplete") or "").lower()
    name = (info.get("name") or "").lower()
    label = (info.get("label") or "").lower()
    blob = " ".join((input_type, autocomplete, name, label))
    if input_type == "password" or "password" in autocomplete or _PASSWORD.search(name):
        return "password"
    if input_type == "email" or "email" in autocomplete or _EMAIL_FIELD.search(blob):
        return "email"
    if input_type in {"tel", "telephone"} or "tel" in autocomplete:
        return "tel"
    if _NAME_FIELD.search(blob):
        return "name"
    if input_type in {"checkbox", "radio"}:
        return "checkbox"
    if input_type == "submit" or info.get("tag") in {"BUTTON", "A"} and info.get("submit"):
        return "submit"
    return "text"


def classify_control(info: dict) -> PauseKind | None:
    """Return a pause kind for one control, or None if the agent may use it."""
    input_type = (info.get("input_type") or "").lower()
    autocomplete = (info.get("autocomplete") or "").lower()
    label = info.get("label") or ""
    name = info.get("name") or ""
    href = info.get("href") or ""
    role = (info.get("role") or "").lower()
    blob = " ".join((input_type, autocomplete, label, name, href, role, info.get("outer") or ""))

    if input_type == "password" or "password" in autocomplete or (
            _PASSWORD.search(name) and not _FALSE_PASSWORD.search(blob)):
        return "password"
    if _CAPTCHA.search(blob) or info.get("captcha"):
        return "captcha"
    if input_type in {"tel", "text", "number"} and _OTP.search(blob):
        return "otp_2fa"
    if autocomplete.startswith("one-time-code") or autocomplete == "one-time-code":
        return "otp_2fa"
    if info.get("tag") == "IFRAME" and _CAPTCHA.search(blob):
        return "captcha"
    if autocomplete.startswith("cc-") or input_type == "credit-card" or _PAYMENT.search(blob):
        if info.get("tag") in {"INPUT", "SELECT", "IFRAME"} or info.get("submit"):
            return "payment"
    if info.get("tag") in {"INPUT", "LABEL"} and input_type == "checkbox" and _TERMS.search(blob):
        return "terms"
    if info.get("terms_checkbox"):
        return "terms"
    if info.get("submit") or input_type == "submit" or (
            info.get("tag") in {"BUTTON", "INPUT", "A"} and _SEND.search(label)):
        # Navigational "Sign in" links without form submit stay clickable when
        # they are plain anchors without submit semantics.
        if info.get("tag") == "A" and info.get("href") and not info.get("submit"):
            if not re.search(r"\b(send|submit|publish|invite|pay|place order)\b", label, re.I):
                return None
        return "send"
    return None


def page_requires_pause(controls: list[dict], *, context: str = "") -> PauseKind | None:
    """Pick the highest-priority pause visible on the page."""
    priority: tuple[PauseKind, ...] = (
        "payment", "password", "otp_2fa", "captcha", "terms", "send",
    )
    found: set[PauseKind] = set()
    for control in controls:
        kind = classify_control(control)
        if kind:
            found.add(kind)
    if _CAPTCHA.search(context or ""):
        found.add("captcha")
    for kind in priority:
        if kind in found:
            return kind
    return None


def is_safe_type_target(info: dict) -> bool:
    """True when the agent may type a reviewed non-secret fill value."""
    if classify_control(info) is not None:
        return False
    kind = field_kind(info)
    return kind in {"email", "name", "text", "tel"} and info.get("tag") == "INPUT"
