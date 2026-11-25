"""Learning events + a weekly mix recommendation (auditable, not silent)."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db.models import GtmEngineState, GtmLearningEvent
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)


def record(
    workspace_id: str,
    kind: str,
    *,
    contact_id: str | None = None,
    channel: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    with db()() as session:
        session.add(
            GtmLearningEvent(
                id=new_id("gtmln"),
                workspace_id=workspace_id,
                contact_id=contact_id,
                kind=kind,
                channel=channel,
                payload_json=json.dumps(payload or {}),
                created_at=utcnow(),
            )
        )
        session.commit()
    # Mirror execution events into the append-only, thesis-aware outcome ledger.
    # Learning failures must never make an already-completed send look failed.
    try:
        from app.gtm.localization.experiments import record_outcome

        data = payload or {}
        record_outcome(
            workspace_id,
            kind,
            contact_id=contact_id,
            channel=channel,
            variant=str(data.get("variant")) if data.get("variant") else None,
            value=float(data["value"]) if data.get("value") is not None else None,
            currency=str(data.get("currency")) if data.get("currency") else None,
            metadata=data,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("thesis outcome mirror skipped: %s", exc)


def summarize(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmLearningEvent)
                .where(GtmLearningEvent.workspace_id == workspace_id)
                .order_by(GtmLearningEvent.created_at.desc())
                .limit(500)
            )
        )
        kinds = Counter(r.kind for r in rows)
        channels = Counter(r.channel for r in rows if r.channel)
        sent = kinds.get("sent", 0)
        replies = kinds.get("reply_human", 0) + kinds.get("reply_positive", 0)
        bounces = kinds.get("bounce", 0)
        positive = kinds.get("reply_positive", 0)
        recs: list[str] = []
        if sent >= 20 and bounces / max(sent, 1) > 0.03:
            recs.append("Pause guessed-email lane; bounce rate is high.")
        if sent >= 30 and replies / max(sent, 1) < 0.02:
            recs.append("Switch cold Day 1 copy to a tighter use-case opener.")
        if positive >= 3:
            recs.append("Raise week_index by 1 if mailbox health is green.")
        if not recs:
            recs.append("Keep current mix; sample is still small.")
        state = session.get(GtmEngineState, workspace_id)
        if state:
            state.last_learn_at = utcnow()
            session.commit()
        result = {
            "events": sum(kinds.values()),
            "kinds": dict(kinds),
            "channels": dict(channels),
            "reply_rate": round(replies / sent, 4) if sent else 0,
            "bounce_rate": round(bounces / sent, 4) if sent else 0,
            "recommendations": recs,
        }
    try:
        from app.gtm.localization.experiments import list_experiments, summarize_experiment

        experiments = list_experiments(workspace_id)
        current = next(
            (item for item in experiments if item["status"] in {"running", "paused"}),
            None,
        )
        result["experiment"] = (
            summarize_experiment(workspace_id, current["id"]) if current else None
        )
    except Exception as exc:  # noqa: BLE001
        log.info("segmented learning summary skipped: %s", exc)
        result["experiment"] = None
    return result
