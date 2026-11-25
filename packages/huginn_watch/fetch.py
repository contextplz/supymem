"""Default HTTP fetch with a localhost refuse — Huginn Faraday, without Rails."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

_UA = "HuginnWatch/1.0 (+https://github.com/huginn/huginn)"
_MAX = 512_000


def fetch_url(url: str, options: dict[str, Any] | None = None) -> str:
    opts = options or {}
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Ignoring a non-HTTP url: {url!r}")
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost"):
        raise ValueError("refusing local URL fetch")
    headers = {"User-Agent": str(opts.get("user_agent") or _UA), "Accept": "*/*"}
    extra = opts.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    req = urllib.request.Request(url, headers=headers)
    timeout = float(opts.get("timeout_s") or 20)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(_MAX + 1)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
    if len(body) > _MAX:
        body = body[:_MAX]
    return body.decode("utf-8", errors="replace")
