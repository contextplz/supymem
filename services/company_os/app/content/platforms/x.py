"""X posts via API v2.

Replies are deliberately unsupported: since February 2026 self-serve tiers get
403 on programmatic replies unless the author summoned the app, so the engine
posts and does not attempt conversation.
"""

from __future__ import annotations

import logging

import httpx

from app.content.platforms.base import (
    PublishResult,
    dry_run_enabled,
    is_fatal,
    simulated,
)

log = logging.getLogger(__name__)

TWEETS_URL = "https://api.x.com/2/tweets"


def publish_x(
    *,
    body: str,
    access_token: str,
    author_urn: str = "",
    title: str | None = None,
    target: str | None = None,
    handle: str | None = None,
) -> PublishResult:
    if dry_run_enabled():
        return simulated("x")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(TWEETS_URL, json={"text": body}, headers=headers)
    except httpx.HTTPError as exc:
        return PublishResult(ok=False, mode="live", error=f"network: {exc}"[:300])

    if res.status_code >= 400:
        return PublishResult(
            ok=False,
            mode="live",
            error=res.text[:300],
            fatal=is_fatal(res.status_code),
            status_code=res.status_code,
        )

    try:
        data = (res.json() or {}).get("data") or {}
    except Exception:
        data = {}
    tweet_id = str(data.get("id") or "")
    user = (handle or "i").lstrip("@") or "i"
    return PublishResult(
        ok=True,
        mode="live",
        external_id=tweet_id or None,
        external_url=(
            f"https://x.com/{user}/status/{tweet_id}" if tweet_id else "https://x.com/"
        ),
        status_code=res.status_code,
    )
