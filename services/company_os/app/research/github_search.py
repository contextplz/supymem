"""Bounded public-repository research through the real GitHub REST API.

One request, one page, and no retries: the durable research runner owns metering
and retry decisions. Returned descriptions are untrusted source material. License
fields are GitHub's observed metadata, not a compatibility assessment.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import httpx
from connectors.http import Reply, request_json, retry_after_seconds
from sqlalchemy import select

from app.connectors.github.adapter import API, API_VERSION
from app.connectors.github.scopes import METADATA_READ
from app.connectors.protocol import AuthExpired, ConnectorError, RateLimited
from app.connectors.secrets import store
from app.connectors.service import row_to_view
from app.db.models import Connection
from app.state import db

MAX_QUERY_LENGTH = 240
MAX_RESULTS = 10
_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}"
)
_NONPUBLIC_QUERY = re.compile(
    r"(?:\b(?:is|visibility):\s*(?:private|internal)\b|-(?:is|visibility):\s*public\b)",
    re.IGNORECASE,
)
_SPDX = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,99}")
_LICENSE_URL = re.compile(re.escape(API) + r"/licenses/[a-z0-9][a-z0-9.+-]{0,99}")


def _credentials(workspace_id: str) -> tuple[str | None, str]:
    from app.core.local_mode import is_local
    unavailable = ("Your saved GitHub connection is unavailable. Open Settings > Keys and connections > Connected apps, reconnect GitHub, then retry."
        if is_local() else "github workspace credential is unavailable")
    with db()() as session:
        rows = session.scalars(
            select(Connection)
            .where(
                Connection.workspace_id == workspace_id,
                Connection.provider == "github",
                Connection.status == "active",
                Connection.simulated == 0,
                Connection.revoked_at.is_(None),
            )
            .order_by(Connection.created_at.desc(), Connection.id.asc())
        )
        for row in rows:
            connection = row_to_view(row)
            if (
                METADATA_READ not in connection.scopes
                or METADATA_READ in connection.declined_scopes
            ):
                continue
            # An eligible workspace connection never silently switches identity.
            if not connection.secret_ref or not connection.secret_ref.startswith(
                "vault:"
            ):
                raise ConnectorError(unavailable)
            try:
                token = store().get(
                    connection.secret_ref, requester=f"github-research:{connection.id}"
                )
                return _token(token, "workspace"), "workspace"
            except Exception:
                raise ConnectorError(unavailable) from None

    if not is_local():
        token = os.environ.get("RESEARCH_GITHUB_TOKEN")
        if token:
            return _token(token, "platform"), "platform"
    return None, "anonymous"


def _token(value: Any, source: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in value.strip())
    ):
        raise ConnectorError(f"github {source} credential is unavailable")
    return value.strip()


def _check(reply: Reply) -> None:
    headers = {key.lower(): value for key, value in reply.headers.items()}
    message = reply.body.get("message", "") if isinstance(reply.body, dict) else ""
    secondary_limit = isinstance(message, str) and (
        "rate limit" in message.lower() or "abuse detection" in message.lower()
    )
    if reply.status == 429 or (
        reply.status == 403
        and (
            headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in headers
            or secondary_limit
        )
    ):
        raise RateLimited(retry_after_seconds(headers), provider="github")
    if reply.status in (401, 403):
        raise AuthExpired("github", reply.status)
    if reply.status != 200:
        raise ConnectorError(f"github search returned {reply.status}")


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _item(row: Any) -> dict[str, Any] | None:
    # Require the explicit public flag even when authenticated search is scoped.
    if not isinstance(row, dict) or row.get("private") is not False:
        return None
    if row.get("visibility", "public") != "public":
        return None
    full_name = row.get("full_name")
    if not isinstance(full_name, str) or not _REPOSITORY.fullmatch(full_name):
        return None
    if full_name.split("/", 1)[1] in {".", ".."}:
        return None
    url = f"https://github.com/{full_name}"
    if row.get("html_url") not in (url, f"{url}/"):
        return None
    description = row.get("description")
    snippet = (
        " ".join(
            "".join(
                c for c in description[:8000] if c.isprintable() or c.isspace()
            ).split()
        )[:2000]
        if isinstance(description, str)
        else ""
    )
    license_data = row.get("license")
    license_data = license_data if isinstance(license_data, dict) else {}
    spdx, license_url = license_data.get("spdx_id"), license_data.get("url")
    stars, archived = row.get("stargazers_count"), row.get("archived")
    return {
        "title": full_name,
        "url": url,
        "snippet": snippet,
        "provider": "github",
        "repository": {
            "full_name": full_name,
            "private": False,
            "stars": stars if type(stars) is int and 0 <= stars <= 2**63 - 1 else None,
            "pushed_at": _timestamp(row.get("pushed_at")),
            "archived": archived if isinstance(archived, bool) else None,
            "license_spdx": spdx
            if isinstance(spdx, str) and _SPDX.fullmatch(spdx)
            else None,
            "license_url": license_url
            if isinstance(license_url, str) and _LICENSE_URL.fullmatch(license_url)
            else None,
        },
    }


async def search_public_repositories(
    query: str, workspace_id: str, limit: int = 5
) -> dict[str, Any]:
    """Search one public GitHub page using the exact workspace's credential.

    Without an eligible connection, only the dedicated RESEARCH_GITHUB_TOKEN is
    considered; otherwise GitHub's anonymous public API is used. Missing selected
    workspace secrets, auth errors and rate limits are explicit failures.
    """
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_LENGTH
        or any(ord(c) < 32 or ord(c) == 127 for c in query)
        or _NONPUBLIC_QUERY.search(query)
    ):
        raise ValueError(
            "github research requires a public search query of 1-240 characters"
        )
    if (
        not isinstance(workspace_id, str)
        or not workspace_id.strip()
        or len(workspace_id) > 128
        or any(ord(c) < 33 or ord(c) == 127 for c in workspace_id)
    ):
        raise ValueError("github research requires a workspace id")
    if type(limit) is not int or limit < 1:
        raise ValueError("github research limit must be a positive integer")
    limit = min(limit, MAX_RESULTS)
    token, source = _credentials(workspace_id)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        reply = await request_json(
            "GET",
            f"{API}/search/repositories",
            headers=headers,
            params={"q": f"{query.strip()} is:public", "per_page": limit, "page": 1},
            timeout=10.0,
        )
    except httpx.HTTPError:
        raise ConnectorError("github search request failed") from None
    _check(reply)
    if not isinstance(reply.body, dict) or not isinstance(
        reply.body.get("items"), list
    ):
        raise ConnectorError("github search returned an invalid response")
    rows = reply.body["items"]
    items, seen = [], set()
    for row in rows[:limit]:
        item = _item(row)
        if item is not None and item["url"].lower() not in seen:
            items.append(item)
            seen.add(item["url"].lower())
    return {
        "items": items,
        "provider": "github",
        "credential_source": source,
        "incomplete": reply.body.get("incomplete_results") is True
        or len(items) < len(rows),
    }
