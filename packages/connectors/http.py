"""Shared HTTP helper for connectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 10.0


async def get_json(url: str, **kwargs):
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        r = await client.get(url, **kwargs)
        r.raise_for_status()
        return r.json()


@dataclass(frozen=True)
class Reply:
    """A provider response with the status left for the caller to interpret.

    Connector adapters map status codes to their own errors (401 means expired,
    429 means back off), so this helper deliberately does not raise.
    """

    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    bytes_in: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: Any | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Reply:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(
            method, url, headers=headers, json=json, params=params
        )
    try:
        body = r.json()
    except ValueError:
        body = r.text
    return Reply(
        status=r.status_code,
        body=body,
        headers={k.lower(): v for k, v in r.headers.items()},
        bytes_in=len(r.content or b""),
    )


def retry_after_seconds(headers: dict[str, str], *, default: float = 60.0) -> float:
    """``Retry-After`` in seconds, falling back to the provider's reset header."""
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return default
    reset = lowered.get("x-ratelimit-reset") or lowered.get(
        "x-ratelimit-requests-reset"
    )
    if reset:
        try:
            import time

            return max(0.0, float(reset) - time.time())
        except ValueError:
            return default
    return default
