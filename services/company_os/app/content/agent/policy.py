"""Per-platform capability policy for the social agent.

The agent always *decides*; this module says which actions may execute.
LinkedIn comments are API-capable but User-Agreement-sensitive — enabled when
CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES=true (intended for throwaway test accounts).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.core.local_mode import is_local


@dataclass(frozen=True, slots=True)
class PlatformPolicy:
    platform: str
    can_post: bool
    can_reply: bool
    can_listen_comments: bool
    can_listen_reactions: bool
    can_listen_sector: bool
    reply_caveat: str = ""


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def agent_enabled() -> bool:
    if is_local():
        try:
            from app.core.local_capabilities import enabled
        except ImportError:
            # shortcut: model setup alone gates content until the owner-switch module is available.
            from app.core.providers_local import LocalProviderError, load_local_config

            try:
                load_local_config()
            except LocalProviderError:
                return False
            return True
        return enabled("content")
    if os.environ.get("CONTENT_AGENT_ENABLED") is not None:
        return _truthy("CONTENT_AGENT_ENABLED")
    # Default on whenever Composio is configured — this is an agent product.
    return bool(os.environ.get("COMPOSIO_API_KEY", "").strip())


def content_blocked_message() -> str:
    try:
        from app.core.local_capabilities import blocked_message
    except ImportError:
        return "Save your model provider, key and chat model in Keys and connections, then request your draft again."
    return blocked_message("content")


def policy_for(platform: str) -> PlatformPolicy:
    key = (platform or "").strip().lower()
    if key == "linkedin":
        allow_replies = _truthy("CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES", default=True)
        return PlatformPolicy(
            platform="linkedin",
            can_post=True,
            can_reply=allow_replies,
            can_listen_comments=True,
            can_listen_reactions=True,
            can_listen_sector=False,  # no public feed search in toolkit
            reply_caveat=(
                ""
                if allow_replies
                else "Set CONTENT_AGENT_ALLOW_LINKEDIN_REPLIES=true to allow comment replies."
            ),
        )
    if key == "reddit":
        return PlatformPolicy(
            platform="reddit",
            can_post=True,
            can_reply=True,
            can_listen_comments=True,
            can_listen_reactions=False,
            can_listen_sector=True,
            reply_caveat="",
        )
    if key == "x":
        return PlatformPolicy(
            platform="x",
            can_post=True,
            can_reply=False,  # self-serve API blocks unsolicited replies
            can_listen_comments=False,
            can_listen_reactions=False,
            can_listen_sector=False,
            reply_caveat="X blocks programmatic replies on self-serve unless summoned.",
        )
    return PlatformPolicy(
        platform=key or "unknown",
        can_post=False,
        can_reply=False,
        can_listen_comments=False,
        can_listen_reactions=False,
        can_listen_sector=False,
        reply_caveat="unknown platform",
    )
