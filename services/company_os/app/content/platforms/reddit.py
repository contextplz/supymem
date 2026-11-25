"""Reddit self-post submission, plus a live fetch of the target's own rules.

Reddit returns HTTP 200 with an errors array on refusal, so the status code
alone is not the signal. Rules are fetched before every submission because a
subreddit can change them between one post and the next.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.content.oauth.providers import REDDIT
from app.content.platforms.base import (
    PublishResult,
    dry_run_enabled,
    is_fatal,
    simulated,
)

log = logging.getLogger(__name__)

SUBMIT_URL = "https://oauth.reddit.com/api/submit"
RULES_URL = "https://oauth.reddit.com/r/{subreddit}/about/rules"
ABOUT_URL = "https://oauth.reddit.com/r/{subreddit}/about"


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": REDDIT.user_agent or "companyos-content/1.0",
    }


def fetch_rules(*, subreddit: str, access_token: str) -> dict[str, Any]:
    """Rule text and posting requirements for one subreddit.

    The engine feeds these to the linter so a draft is checked against the
    community's actual rules rather than a snapshot from when it was written.
    """
    out: dict[str, Any] = {"subreddit": subreddit, "rules": [], "fetched": False}
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.get(
                RULES_URL.format(subreddit=subreddit), headers=_headers(access_token)
            )
            about = client.get(
                ABOUT_URL.format(subreddit=subreddit), headers=_headers(access_token)
            )
    except httpx.HTTPError as exc:
        out["error"] = f"network: {exc}"[:200]
        return out

    if res.status_code < 400:
        try:
            payload = res.json() or {}
        except Exception:
            payload = {}
        rules = []
        for rule in payload.get("rules") or []:
            if isinstance(rule, dict):
                rules.append(
                    {
                        "short_name": str(rule.get("short_name") or ""),
                        "description": str(rule.get("description") or "")[:600],
                        "kind": str(rule.get("kind") or ""),
                    }
                )
        out["rules"] = rules
        out["fetched"] = True
    else:
        out["error"] = f"rules {res.status_code}"

    if about.status_code < 400:
        try:
            data = (about.json() or {}).get("data") or {}
        except Exception:
            data = {}
        out["subscribers"] = data.get("subscribers")
        out["submission_type"] = data.get("submission_type")
        out["over18"] = data.get("over18")
        out["quarantine"] = data.get("quarantine")
    return out


def publish_reddit(
    *,
    body: str,
    access_token: str,
    author_urn: str = "",
    title: str | None = None,
    target: str | None = None,
) -> PublishResult:
    subreddit = (target or "").lstrip("/").removeprefix("r/").strip()
    if not subreddit:
        return PublishResult(
            ok=False, mode="live", error="no target subreddit", fatal=True
        )
    if not title:
        return PublishResult(
            ok=False, mode="live", error="reddit posts need a title", fatal=True
        )
    if dry_run_enabled():
        return simulated("reddit")

    form = {
        "sr": subreddit,
        "kind": "self",
        "title": title[:300],
        "text": body,
        "api_type": "json",
        "resubmit": "false",
        "sendreplies": "true",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(SUBMIT_URL, data=form, headers=_headers(access_token))
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
        payload = (res.json() or {}).get("json") or {}
    except Exception:
        payload = {}
    errors = payload.get("errors") or []
    if errors:
        # e.g. [["SUBREDDIT_NOTALLOWED", "you aren't allowed to post there", "sr"]]
        joined = "; ".join(
            " ".join(str(part) for part in err) if isinstance(err, list) else str(err)
            for err in errors
        )
        return PublishResult(
            ok=False,
            mode="live",
            error=joined[:300],
            fatal=True,
            status_code=res.status_code,
        )

    data = payload.get("data") or {}
    return PublishResult(
        ok=True,
        mode="live",
        external_id=str(data.get("id") or data.get("name") or "") or None,
        external_url=str(data.get("url") or "") or None,
        status_code=res.status_code,
    )
