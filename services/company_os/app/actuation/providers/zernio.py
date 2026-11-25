"""Zernio REST client — the live social aggregator (docs.zernio.com).

Base URL is ``https://zernio.com/api/v1``. Older configs used
``https://api.zernio.com/v1/posts``; we accept both and normalize.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE = "https://zernio.com/api/v1"
_PLATFORM_ALIASES = {
    "x": "twitter",
    "twitter": "twitter",
    "linkedin": "linkedin",
    "instagram": "instagram",
    "facebook": "facebook",
    "threads": "threads",
    "reddit": "reddit",
    "bluesky": "bluesky",
    "tiktok": "tiktok",
    "youtube": "youtube",
}


def api_base_url() -> str:
    raw = (os.getenv("ZERNIO_API_URL") or DEFAULT_BASE).strip().rstrip("/")
    if raw.endswith("/posts"):
        raw = raw[: -len("/posts")]
    return raw or DEFAULT_BASE


def _headers(api_key: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def normalize_platform(name: str) -> str:
    key = (name or "").strip().lower()
    return _PLATFORM_ALIASES.get(key, key)


def account_id(row: dict[str, Any]) -> str:
    return str(row.get("_id") or row.get("id") or row.get("accountId") or "").strip()


def account_platform(row: dict[str, Any]) -> str:
    return normalize_platform(str(row.get("platform") or ""))


def parse_accounts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("accounts") or data.get("data") or data.get("items") or []
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and account_id(row):
            out.append(row)
    return out


def select_platforms(
    accounts: list[dict[str, Any]],
    *,
    wanted: list[str] | None = None,
) -> list[dict[str, str]]:
    """Pick Zernio ``{platform, accountId}`` targets from connected accounts."""
    active = [
        row
        for row in accounts
        if row.get("isActive", True) is not False and account_id(row)
    ]
    wanted_norm = [normalize_platform(p) for p in (wanted or []) if p]
    chosen: list[dict[str, str]] = []
    seen: set[str] = set()
    pool = active
    for row in pool:
        plat = account_platform(row)
        aid = account_id(row)
        if not aid:
            continue
        if wanted_norm and plat not in wanted_norm:
            continue
        key = f"{plat}:{aid}"
        if key in seen:
            continue
        seen.add(key)
        chosen.append({"platform": plat or "twitter", "accountId": aid})
    return chosen


def extract_post_id(data: dict[str, Any]) -> str | None:
    post = data.get("post") if isinstance(data.get("post"), dict) else data
    for key in ("_id", "id", "post_id", "postId", "external_id"):
        value = post.get(key) if isinstance(post, dict) else None
        if value:
            return str(value)
    return None


def extract_post_url(data: dict[str, Any]) -> str | None:
    post = data.get("post") if isinstance(data.get("post"), dict) else data
    if not isinstance(post, dict):
        return None
    for key in ("url", "post_url", "permalink", "shareUrl"):
        value = post.get(key)
        if value:
            return str(value)
    platforms = post.get("platforms")
    if isinstance(platforms, list):
        for row in platforms:
            if not isinstance(row, dict):
                continue
            for key in ("url", "postUrl", "permalink"):
                if row.get(key):
                    return str(row[key])
    return None


async def list_accounts(client: httpx.AsyncClient, api_key: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"{api_base_url()}/accounts",
        headers=_headers(api_key),
    )
    response.raise_for_status()
    return parse_accounts(response.json())


async def create_post(
    client: httpx.AsyncClient,
    api_key: str,
    body: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"{api_base_url()}/posts",
        headers=_headers(api_key, idempotency_key=idempotency_key),
        json=body,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"raw": data}
