"""Page fetch: Agent-Reach specialist channels → OpenWorker → Jina."""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

_UA = "Mozilla/5.0 (compatible; CompanyOS/1.0; +research)"
_MAX_BYTES = 512_000


def fetch_page_markdown(url: str, *, timeout_s: float = 25.0) -> str:
    """Return page body as readable text/markdown-ish.

    Specialist platforms (YouTube, GitHub, RSS, V2EX, Twitter, …) go through
    Agent-Reach first. Ordinary pages keep the OpenWorker → Jina cascade.
    """
    from app.core.local_mode import is_local
    if is_local():
        import asyncio

        from app.research.local_providers import fetch_page
        return asyncio.run(fetch_page(url, timeout_s=timeout_s))
    # 0) Agent-Reach specialist channel (never the generic WebChannel here)
    try:
        from app.research.agent_reach_bridge import read_url as reach_read
        from app.research.agent_reach_bridge import vendored

        if vendored():
            specialist = reach_read(url, specialist_only=True)
            if specialist and specialist.strip():
                return specialist
    except Exception:
        pass

    # 1) OpenWorker guarded fetch
    try:
        from app.research.openworker_bridge import fetch_url

        result = fetch_url(url, max_chars=40_000)
        if result.get("text") and not result.get("error"):
            return str(result["text"])
    except Exception:
        pass

    # 2) agent-reach WebChannel (Jina)
    try:
        from app.research.agent_reach_bridge import read_url as reach_read

        return reach_read(url, specialist_only=False)
    except Exception:
        return _fetch_jina_direct(url, timeout_s=timeout_s)


def _fetch_jina_direct(url: str, *, timeout_s: float) -> str:
    host = urlparse(url).netloc.lower()
    if not host or host in {"localhost", "127.0.0.1"}:
        raise ValueError("refusing local URL fetch")
    jina = f"https://r.jina.ai/{url}"
    req = urllib.request.Request(
        jina,
        headers={"User-Agent": _UA, "Accept": "text/plain"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = resp.read(_MAX_BYTES + 1)
    if len(body) > _MAX_BYTES:
        body = body[:_MAX_BYTES]
    return body.decode("utf-8", errors="replace")
