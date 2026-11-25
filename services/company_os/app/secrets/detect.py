"""Detect secret-shaped text so chat can refuse it. Never persist the value."""

from __future__ import annotations

import re

from app.secrets.catalog import guess_slot_from_prefix

# Recognized credential formats and explicit secret assignments.
_PREFIX = re.compile(
    r"(?i)\b("
    r"sk-ant-|sk-proj-|sk-or-|sk_test_|sk_live_|sk-|zn_|"
    r"ghp_|github_pat_|gho_|glpat-|"
    r"sb_secret_[A-Za-z0-9_-]{10,}|sb_publishable_[A-Za-z0-9_-]{10,}|"
    r"postgres(?:ql)?://[^\s:/]+:[^\s@]+@|"
    r"AKIA[0-9A-Z]{8,}|"
    r"lin_api_|"
    r"vck_[A-Za-z0-9_-]{20,}|"
    r"apikey_[a-f0-9]{32}_[a-f0-9]{64}|"
    r"rzp_live_|rzp_test_|rzp_|"
    r"AIza[0-9A-Za-z_-]{20,}|"
    r"xai-[A-Za-z0-9]{20,}|"
    r"whsec_|"
    r"Bearer [A-Za-z0-9._\-]{20,}"
    r")"
)
_ASSIGNED = re.compile(
    r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|password|private[_-]?key)"
    r"\s*(?:[:=]|\bis\b)\s*\S{12,}"
)


def looks_like_secret(text: str) -> bool:
    blob = (text or "").strip()
    if len(blob) < 12:
        return False
    if _PREFIX.search(blob):
        return True
    if _ASSIGNED.search(blob):
        return True
    return False


def withheld_user_copy() -> str:
    return "A credential was withheld. Use the secure form, not chat."


def refuse_chat_copy(label: str) -> str:
    name = label or "that credential"
    return (
        f"Don't paste {name} in chat. A secure form opened. "
        "Whatever you type there goes to the local `.env` and vault. "
        "I never see the value."
    )


def classify_paste(text: str) -> str:
    spec = guess_slot_from_prefix((text or "").strip())
    if spec is not None:
        return spec.id
    return "generic.credential"
