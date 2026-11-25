"""LinkedIn member posts via the versioned /rest/posts endpoint.

Scope is w_member_social, which the self-serve "Share on LinkedIn" product
grants. Posting as a company page needs w_organization_social and the reviewed
Community Management API, which is a separate application.
"""

from __future__ import annotations

import logging
import os

import httpx

from app.content.platforms.base import (
    PublishResult,
    dry_run_enabled,
    is_fatal,
    simulated,
)

log = logging.getLogger(__name__)

POSTS_URL = "https://api.linkedin.com/rest/posts"
DEFAULT_VERSION = "202506"


def _version() -> str:
    return os.environ.get("LINKEDIN_API_VERSION", DEFAULT_VERSION).strip() or DEFAULT_VERSION


def publish_linkedin(
    *,
    body: str,
    access_token: str,
    author_urn: str,
    title: str | None = None,
    target: str | None = None,
) -> PublishResult:
    if dry_run_enabled():
        return simulated("linkedin")
    if not author_urn:
        return PublishResult(
            ok=False,
            mode="live",
            error="no author urn on the connected account",
            fatal=True,
        )

    payload = {
        "author": author_urn,
        "commentary": body,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "LinkedIn-Version": _version(),
        "X-Restli-Protocol-Version": "2.0.0",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(POSTS_URL, json=payload, headers=headers)
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

    urn = res.headers.get("x-restli-id") or res.headers.get("X-RestLi-Id") or ""
    if not urn:
        try:
            urn = str((res.json() or {}).get("id") or "")
        except Exception:
            urn = ""
    url = (
        f"https://www.linkedin.com/feed/update/{urn}/"
        if urn
        else "https://www.linkedin.com/feed/"
    )
    return PublishResult(
        ok=True,
        mode="live",
        external_id=urn or None,
        external_url=url,
        status_code=res.status_code,
    )
