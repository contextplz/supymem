"""Marketing learning loop — auditable outcomes that feed the next plan.

Mirrors GTM learning: record events, summarize, recommend cadence/theme tweaks.
Does not silently mutate strategy; recommendations are applied only when the
strategist rebuilds a plan.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.db.models import MarketingJob, MarketingLearningEvent
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)


def record(
    workspace_id: str,
    kind: str,
    *,
    platform: str | None = None,
    job_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        with db()() as session:
            session.add(
                MarketingLearningEvent(
                    id=new_id("mkln"),
                    workspace_id=workspace_id,
                    job_id=job_id,
                    kind=kind,
                    platform=platform,
                    payload_json=json.dumps(payload or {}, default=str),
                    created_at=utcnow(),
                )
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 — learning must never break execution
        log.info("marketing learning record skipped: %s", exc)


def record_from_job(job: dict[str, Any], *, status: str) -> None:
    kind = f"job_{status}"
    if status == "done":
        kind = f"done_{job.get('kind') or 'unknown'}"
    elif status == "blocked":
        kind = "blocked"
    elif status == "failed":
        kind = "failed"
    record(
        str(job.get("workspace_id") or ""),
        kind,
        platform=job.get("platform"),
        job_id=job.get("id"),
        payload={
            "job_kind": job.get("kind"),
            "error": job.get("error_message"),
            "block_reason": job.get("block_reason"),
            "proof_mode": (job.get("proof") or {}).get("mode"),
        },
    )


def summarize(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        events = list(
            session.scalars(
                select(MarketingLearningEvent)
                .where(MarketingLearningEvent.workspace_id == workspace_id)
                .order_by(MarketingLearningEvent.created_at.desc())
                .limit(500)
            )
        )
        jobs = list(
            session.scalars(
                select(MarketingJob)
                .where(MarketingJob.workspace_id == workspace_id)
                .order_by(MarketingJob.updated_at.desc())
                .limit(300)
            )
        )

    kinds = Counter(e.kind for e in events)
    platforms = Counter(e.platform for e in events if e.platform)
    job_status = Counter(j.status for j in jobs)
    published = sum(1 for j in jobs if j.kind == "publish_content" and j.status == "done")
    blocked = job_status.get("blocked", 0)
    failed = job_status.get("failed", 0)
    linkedin_done = sum(
        1
        for j in jobs
        if j.platform == "linkedin" and j.kind == "publish_content" and j.status == "done"
    )
    outbound_ticks = kinds.get("done_send_email_touch", 0) + kinds.get(
        "done_enqueue_outreach", 0
    )

    recs: list[str] = []
    if published >= 8 and blocked / max(published + blocked, 1) > 0.25:
        recs.append("Reconnect LinkedIn/Composio, block rate is high relative to publishes.")
    if linkedin_done >= 6:
        recs.append("Keep LinkedIn cadence; proof chain is healthy.")
    elif published == 0 and job_status.get("pending", 0) > 0:
        recs.append("Run marketing tick with CONTENT_DRY_RUN=true until Composio is connected.")
    if failed >= 5:
        recs.append("Inspect failed create_visual / publish jobs before raising cadence.")
    if outbound_ticks == 0:
        recs.append("Enable strategy.outbound when Gmail + Hunter are ready for cold mail.")
    if not recs:
        recs.append("Keep current mix; sample is still small.")

    return {
        "events": sum(kinds.values()),
        "kinds": dict(kinds),
        "platforms": dict(platforms),
        "job_status": dict(job_status),
        "published": published,
        "linkedin_published": linkedin_done,
        "outbound_ticks": outbound_ticks,
        "recommendations": recs,
    }


def apply_to_strategy(strategy: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied strategy with learning annotations (non-destructive)."""
    out = dict(strategy)
    learning = {
        "recommendations": list(summary.get("recommendations") or []),
        "published": summary.get("published"),
        "linkedin_published": summary.get("linkedin_published"),
        "as_of": utcnow().isoformat(),
    }
    out["learning"] = learning
    # Soft cadence nudge: if LinkedIn is healthy, keep per_week; if blocked heavy, lower.
    content = dict(out.get("content") or {})
    platforms = dict(content.get("platforms") or {})
    li = dict(platforms.get("linkedin") or {})
    if li:
        published = int(summary.get("linkedin_published") or 0)
        blocked = int((summary.get("job_status") or {}).get("blocked") or 0)
        if published >= 4 and blocked == 0:
            li["per_week"] = max(int(li.get("per_week") or 4), 4)
        elif blocked >= 3:
            li["per_week"] = min(int(li.get("per_week") or 4), 2)
            learning["cadence_nudge"] = "linkedin_per_week_reduced"
        platforms["linkedin"] = li
        content["platforms"] = platforms
        out["content"] = content
    return out
