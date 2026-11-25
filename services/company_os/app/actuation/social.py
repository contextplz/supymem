"""Helpers for social actuation: credentials, copy, and workspace lookup."""

from __future__ import annotations

import os
import re
from typing import Any

from core.env_contract import parse_app_env

from app.db.models import Venture
from app.state import MEM, db

_SOCIAL_TITLE = (
    "social",
    "post",
    "twitter",
    "instagram",
    "linkedin",
    "facebook",
    "tiktok",
    "handle",
    "outreach",
)

_POST_CHAT = re.compile(
    r"\b("
    r"(post|tweet|publish|share)\b.{0,40}\b(twitter|x\b|linkedin|instagram|facebook|tiktok|social)|"
    r"(tweet this|post this|publish this|post to (twitter|x|linkedin|instagram))"
    r")\b",
    re.I,
)


def channels_dry_run() -> bool:
    return os.environ.get("CHANNELS_DRY_RUN", "true").lower() != "false"


def env_social_key() -> tuple[str, str] | None:
    if parse_app_env() != "local":
        return None
    if key := (os.getenv("ZERNIO_API_KEY") or "").strip():
        return ("zernio", key)
    if key := (os.getenv("OUTSTAND_API_KEY") or "").strip():
        return ("outstand", key)
    return None


def _workspace_id(venture_id: str) -> str | None:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        return ven.workspace_id if ven else None


def vault_social_key(venture_id: str) -> str | None:
    """Read a Zernio key the founder pasted into Connect (never logged)."""
    try:
        from app.connectors.secrets import store

        # Legacy concierge writes used the venture ID as the vault namespace.
        # Resolve that exact binding rather than trusting arbitrary task refs.
        value = store().find(workspace_id=venture_id, provider="social", key="vault:social", requester="social.post")
        return value.strip() if value else None
    except Exception:
        pass
    return None


def resolve_social_key(venture_id: str) -> tuple[str, str] | None:
    workspace_id = _workspace_id(venture_id)
    if workspace_id:
        try:
            from app.connectors.secrets import store
            from app.secrets.catalog import slot_for_provider

            vault = store()
            for provider, key in (("zernio", "ZERNIO_API_KEY"), ("outstand", "OUTSTAND_API_KEY")):
                spec = slot_for_provider(provider)
                bindings = [(provider, key)]
                if spec:
                    bindings.insert(0, (spec.provider, spec.env_keys[0]))
                for bound_provider, bound_key in dict.fromkeys(bindings):
                    value = vault.find(workspace_id=workspace_id, provider=bound_provider, key=bound_key, requester="social.post")
                    if value and value.strip():
                        return provider, value.strip()
        except Exception:
            pass
    vault = vault_social_key(venture_id)
    if vault:
        return ("zernio", vault)
    return env_social_key()


def founder_social_connected(venture_id: str) -> bool:
    blob = dict(MEM.get(f"founder_channels:{venture_id}") or {})
    if parse_app_env() == "local" and blob.get("social"):
        return True
    return resolve_social_key(venture_id) is not None


def looks_like_social_work(title: str, tags: list | None = None) -> bool:
    blob = f"{title} {' '.join(str(t) for t in (tags or []))}".lower()
    return any(needle in blob for needle in _SOCIAL_TITLE)


def looks_like_social_chat(text: str) -> bool:
    return bool(_POST_CHAT.search(text or ""))


def compose_post_text(*, title: str = "", detail: str = "", chat: str = "") -> str:
    """Short public copy. Prefer an explicit body after a colon in chat."""
    chat = (chat or "").strip()
    if chat:
        if ":" in chat:
            _, _, rest = chat.partition(":")
            if rest.strip():
                return rest.strip()[:280]
        return chat[:280]
    body = (detail or "").strip() or (title or "").strip()
    if not body:
        return "Shipping an update."
    first = body.split("\n", 1)[0].strip()
    return first[:280]


def wanted_platforms(payload: dict[str, Any] | None = None, *, chat: str = "") -> list[str]:
    payload = payload or {}
    raw = payload.get("platforms") or payload.get("platform")
    names: list[str] = []
    if isinstance(raw, str) and raw.strip():
        names.append(raw)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("platform"):
                names.append(str(item["platform"]))
    blob = f"{chat} {payload.get('channel') or ''}".lower()
    for name in ("twitter", "linkedin", "instagram", "facebook", "tiktok", "threads"):
        if name in blob and name not in names:
            names.append(name)
    if " x " in f" {blob} " or blob.startswith("x ") or "to x" in blob:
        if "twitter" not in names:
            names.append("twitter")
    return names


def workspace_id_for(venture_id: str) -> str | None:
    return _workspace_id(venture_id)
