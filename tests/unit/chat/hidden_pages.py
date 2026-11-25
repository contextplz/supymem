"""Python mirror of the console's chatFirstHidden(), read from apps/console/src (no app import)."""
from __future__ import annotations

import re
from pathlib import Path

_VISIBILITY = Path(__file__).resolve().parents[3] / "apps/console/src/lib/chatVisibility.ts"
_SEGMENTS = re.findall(r'"([a-z]+)"', _VISIBILITY.read_text().split("CHAT_FIRST_HIDDEN_SEGMENTS = [", 1)[1].split("]", 1)[0])


def hidden(href: str) -> bool:
    """Python mirror of chatFirstHidden() plus the outreach/tasks aliases, read from the console source."""
    path = re.split(r"[?#]", href, maxsplit=1)[0]
    match = re.match(r"^/v/[^/]+/([^/]+)", path)
    return (path in {"/gtm", "/connections", "/tasks", "/execution", "/ops/outreach"} or path.startswith(("/gtm/", "/ops/outreach/"))
            or bool(re.fullmatch(r"/v/[^/]+/idea/?", path))
            or bool(match and match[1] in {*_SEGMENTS, "gtm"}))


def hidden_hrefs(reply) -> list[str]:
    return [a["href"] for a in (reply or {}).get("actions") or []
            if isinstance(a, dict) and isinstance(a.get("href"), str) and not a.get("kind") and hidden(a["href"])]
