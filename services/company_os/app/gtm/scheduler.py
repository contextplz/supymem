"""GTM scheduler — nothing publishes while channel unarmed; dry-run default."""

from __future__ import annotations

from typing import Any

from app.gtm.channels.landing import LandingAdapter
from app.gtm.channels.reddit import RedditAdapter
from app.gtm.channels.x import XAdapter
from app.gtm.channels.instagram import InstagramAdapter
from app.gtm.channels.email import EmailAdapter
from app.gtm.channels.linkedin import LinkedInAdapter

_ADAPTERS = {
    "landing": LandingAdapter,
    "reddit": RedditAdapter,
    "x": XAdapter,
    "instagram": InstagramAdapter,
    "email": EmailAdapter,
    "linkedin": LinkedInAdapter,
}


async def schedule_post(
    channel: str,
    payload: dict[str, Any],
    *,
    armed: bool,
    dry_run: bool | None = None,
    warden_allow: bool = False,
) -> dict[str, Any]:
    if not armed:
        return {"ok": False, "error": "channel_unarmed"}
    cls = _ADAPTERS[channel]
    adapter = cls(dry_run=dry_run)
    return await adapter.publish(payload, warden_allow=warden_allow)
