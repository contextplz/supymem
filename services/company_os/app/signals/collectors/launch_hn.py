"""Launch HN collector — Algolia HN Search API (clean-room; doc 115)."""

from __future__ import annotations

import hashlib
import html as ihtml
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from app.signals.collectors.base import RawSignal

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _clean_title(title: str) -> str:
    t = ihtml.unescape(title or "")
    t = re.sub(r"^Launch\s+HN:\s*", "", t, flags=re.I)
    # "Acme – does X" → Acme
    t = re.split(r"\s+[–—-]\s+", t, maxsplit=1)[0]
    return t.strip()[:80]


def _domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).hostname
    if not host:
        return None
    return host.removeprefix("www.").lower()


def parse_launch_hits(hits: list[dict[str, Any]], *, since: datetime) -> list[dict[str, Any]]:
    since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    out: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = hit.get("title") or ""
        if "launch hn" not in title.lower():
            continue
        ts = hit.get("created_at_i") or 0
        occurred = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if occurred < since_aware:
            continue
        url = hit.get("url")
        object_id = str(hit.get("objectID") or "")
        evidence = f"https://news.ycombinator.com/item?id={object_id}" if object_id else url
        text = ihtml.unescape(hit.get("story_text") or hit.get("comment_text") or "")
        emails = _EMAIL.findall(text)
        email = emails[0].lower() if emails else None
        out.append(
            {
                "name": _clean_title(title),
                "domain": _domain_of(url if isinstance(url, str) else None),
                "evidence_url": evidence,
                "occurred_at": occurred,
                "email": email,
                "object_id": object_id,
                "description": title,
            }
        )
    return out


class LaunchHnCollector:
    id = "launch_hn"
    cadence_s = 3 * 3600
    cost_per_run_usd = Decimal("0")

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    ALGOLIA,
                    params={"query": "Launch HN", "tags": "story", "hitsPerPage": 50},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []
        hits = data.get("hits") or []
        parsed = parse_launch_hits(hits, since=since)
        signals: list[RawSignal] = []
        for item in parsed:
            sid = hashlib.sha256(
                f"lhn|{item.get('object_id') or item['name']}".encode()
            ).hexdigest()[:24]
            signals.append(
                RawSignal(
                    id=sid,
                    person_key=hashlib.sha256(
                        (item.get("email") or item.get("domain") or item["name"]).encode()
                    ).hexdigest(),
                    collector_id=self.id,
                    occurred_at=item["occurred_at"].replace(tzinfo=None),
                    effect=1.0,
                    window=item["occurred_at"].date().isoformat(),
                    payload={
                        "kind": "launch_hn",
                        "company_name": item["name"],
                        "domain": item.get("domain"),
                        "evidence_url": item.get("evidence_url"),
                        "description": item.get("description"),
                        "contact_email": item.get("email"),
                        "email_confidence": "safe" if item.get("email") else "none",
                        "venture_id": venture_id,
                    },
                )
            )
        return signals
