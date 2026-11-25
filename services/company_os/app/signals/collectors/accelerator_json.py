"""Accelerator JSON collector (EF / Techstars / Antler via startups dataset).

Pinned CDN URL from doc 115. Clean-room; no OpenLeads import.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from app.signals.collectors.base import RawSignal

# Community-maintained startup lists. Pinning happens at the CDN layer.
EF_JSON = (
    "https://cdn.jsdelivr.net/gh/yigitmeteozcan/startups@main/data/by-source/ef.json"
)
KIND_BY_SOURCE = {
    "ef": "ef_admit",
    "entrepreneur first": "ef_admit",
    "techstars": "ef_admit",  # treated as accelerator admit for ICP scoring
    "antler": "ef_admit",
    "500": "ef_admit",
}


def _domain_of(website: str | None) -> str | None:
    if not website:
        return None
    host = urlparse(website if "://" in website else f"https://{website}").hostname
    if not host:
        return None
    return host.removeprefix("www.").lower()


def parse_accelerator_rows(
    rows: list[dict[str, Any]], *, since: datetime, limit: int = 100
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or row.get("company") or "").strip()
        if not name:
            continue
        source = (row.get("source") or row.get("accelerator") or "ef").strip().lower()
        kind = KIND_BY_SOURCE.get(source, "ef_admit")
        website = row.get("website") or row.get("url")
        domain = _domain_of(website if isinstance(website, str) else None)
        out.append(
            {
                "name": name,
                "domain": domain,
                "accelerator": source,
                "kind": kind,
                "evidence_url": website if isinstance(website, str) else None,
                "description": (row.get("description") or "")[:500],
                "occurred_at": since if since.tzinfo else since.replace(tzinfo=timezone.utc),
            }
        )
        if len(out) >= limit:
            break
    return out


class AcceleratorJsonCollector:
    id = "accelerator_json"
    cadence_s = 6 * 3600
    cost_per_run_usd = Decimal("0")

    async def collect(self, venture_id: str, since: datetime) -> list[RawSignal]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(EF_JSON)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []
        rows = data if isinstance(data, list) else data.get("companies") or data.get("items") or []
        parsed = parse_accelerator_rows(rows, since=since)
        signals: list[RawSignal] = []
        for item in parsed:
            sid = hashlib.sha256(
                f"acc|{item.get('accelerator')}|{item['name']}|{item.get('domain')}".encode()
            ).hexdigest()[:24]
            signals.append(
                RawSignal(
                    id=sid,
                    person_key=hashlib.sha256(
                        (item.get("domain") or item["name"]).encode()
                    ).hexdigest(),
                    collector_id=self.id,
                    occurred_at=item["occurred_at"].replace(tzinfo=None),
                    effect=0.95,
                    window=item.get("accelerator"),
                    payload={
                        "kind": item["kind"],
                        "company_name": item["name"],
                        "domain": item.get("domain"),
                        "accelerator": item.get("accelerator"),
                        "evidence_url": item.get("evidence_url"),
                        "description": item.get("description"),
                        "venture_id": venture_id,
                    },
                )
            )
        return signals
