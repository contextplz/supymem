"""Signal sweep — run GTM collectors and persist into relationship store."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app.db.models import Venture
from app.gtm.relationships.store import ingest_signal
from app.signals.collectors.accelerator_json import AcceleratorJsonCollector
from app.signals.collectors.launch_hn import LaunchHnCollector
from app.signals.collectors.yc_directory import YcDirectoryCollector
from app.state import db
from core.clock import utcnow

log = logging.getLogger(__name__)

COLLECTORS = (
    YcDirectoryCollector(),
    LaunchHnCollector(),
    AcceleratorJsonCollector(),
)


def _workspace_for_venture(venture_id: str) -> str | None:
    with db()() as session:
        ven = session.get(Venture, venture_id)
        if ven and ven.workspace_id:
            return ven.workspace_id
    return None


async def run_signal_sweep(
    venture_id: str, *, since_hours: int = 72, limit_per_collector: int = 50
) -> dict[str, Any]:
    """Collect warm signals and upsert companies/contacts/signals."""
    workspace_id = _workspace_for_venture(venture_id) or venture_id
    since = utcnow() - timedelta(hours=since_hours)
    ingested = 0
    errors: list[str] = []
    for collector in COLLECTORS:
        try:
            raw = await collector.collect(venture_id, since)
        except Exception as exc:  # noqa: BLE001 — sweep must continue
            errors.append(f"{collector.id}:{exc.__class__.__name__}")
            continue
        with db()() as session:
            for signal in raw[:limit_per_collector]:
                payload = dict(signal.payload or {})
                kind = str(payload.get("kind") or collector.id)
                name = str(payload.get("company_name") or "").strip()
                if not name:
                    continue
                try:
                    row = ingest_signal(
                        session,
                        workspace_id=workspace_id,
                        kind=kind,
                        company_name=name,
                        domain=payload.get("domain") if isinstance(payload.get("domain"), str) else None,
                        evidence_url=payload.get("evidence_url")
                        if isinstance(payload.get("evidence_url"), str)
                        else None,
                        payload=payload,
                        yc_batch=payload.get("yc_batch")
                        if isinstance(payload.get("yc_batch"), str)
                        else None,
                        accelerator=payload.get("accelerator")
                        if isinstance(payload.get("accelerator"), str)
                        else None,
                        contact_email=payload.get("contact_email")
                        if isinstance(payload.get("contact_email"), str)
                        else None,
                        contact_first_name=payload.get("contact_first_name")
                        if isinstance(payload.get("contact_first_name"), str)
                        else None,
                        contact_role=payload.get("contact_role")
                        if isinstance(payload.get("contact_role"), str)
                        else None,
                        email_confidence=str(payload.get("email_confidence") or "none"),
                    )
                    if row:
                        ingested += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"ingest:{exc.__class__.__name__}")
            session.commit()
    log.info(
        "signal_sweep venture=%s workspace=%s ingested=%s errors=%s",
        venture_id,
        workspace_id,
        ingested,
        len(errors),
    )
    return {
        "venture_id": venture_id,
        "workspace_id": workspace_id,
        "ingested": ingested,
        "errors": errors,
    }
