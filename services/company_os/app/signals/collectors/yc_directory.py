"""YC directory collector — public yc-oss JSON (clean-room; doc 115).

Endpoint: https://yc-oss.github.io/api/companies/all.json
Does not scrape ycombinator.com founder pages (ToS / rate risk). Company +
batch signal only; people discovery is lead_researcher's job.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from app.signals.collectors.base import RawSignal

YC_ALL = "https://yc-oss.github.io/api/companies/all.json"
YC_CHANGES = "https://yc-oss.github.io/api/companies/changes/latest.json"


def _domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = urlparse(website if "://" in website else f"https://{website}").hostname
    if not host:
        return None
    return host.removeprefix("www.").lower()


def parse_yc_companies(rows: list[dict[str, Any]], *, since: datetime, limit: int = 200) -> list[dict[str, Any]]:
    """Pure helper — filter YC rows into signal payloads."""
    out: list[dict[str, Any]] = []
    since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or row.get("slug") or "").strip()
        if not name:
            continue
        batch = (row.get("batch") or row.get("batch_name") or "").strip() or None
        website = row.get("website") or row.get("url")
        domain = _domain_of(website if isinstance(website, str) else None)
        slug = (row.get("slug") or "").strip()
        evidence = (
            f"https://www.ycombinator.com/companies/{slug}" if slug else website
        )
        # Prefer recently listed if a launched/updated field exists; else take all.
        launched = row.get("launched_at") or row.get("updated_at") or row.get("created_at")
        if isinstance(launched, (int, float)):
            occurred = datetime.fromtimestamp(launched, tz=timezone.utc)
        elif isinstance(launched, str) and launched[:4].isdigit():
            try:
                occurred = datetime.fromisoformat(launched.replace("Z", "+00:00"))
            except ValueError:
                occurred = since_aware
        else:
            occurred = since_aware
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        if occurred < since_aware and launched is not None:
            continue
        out.append(
            {
                "name": name,
                "domain": domain,
                "yc_batch": batch,
                "evidence_url": evidence,
                "description": (row.get("one_liner") or row.get("long_description") or "")[:500],
                "occurred_at": occurred,
                "slug": slug,
            }
        )
        if len(out) >= limit:
            break
    return out


class YcDirectoryCollector:
    id = "yc_directory"
    cadence_s = 3 * 3600
    cost_per_run_usd = Decimal("0")

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(YC_ALL)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []
        rows = data if isinstance(data, list) else data.get("companies") or []
        parsed = parse_yc_companies(rows, since=since)
        signals: list[RawSignal] = []
        for item in parsed:
            sid = hashlib.sha256(
                f"yc|{item.get('slug') or item['name']}|{item.get('yc_batch')}".encode()
            ).hexdigest()[:24]
            signals.append(
                RawSignal(
                    id=sid,
                    person_key=hashlib.sha256(
                        (item.get("domain") or item["name"]).encode()
                    ).hexdigest(),
                    collector_id=self.id,
                    occurred_at=item["occurred_at"].replace(tzinfo=None),
                    effect=1.0,
                    window=item.get("yc_batch"),
                    payload={
                        "kind": "yc_admit",
                        "company_name": item["name"],
                        "domain": item.get("domain"),
                        "yc_batch": item.get("yc_batch"),
                        "evidence_url": item.get("evidence_url"),
                        "description": item.get("description"),
                        "venture_id": venture_id,
                    },
                )
            )
        return signals
