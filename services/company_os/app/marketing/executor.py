"""Execution Manager — WorkOrders through the router, then adapters, then proof.

Flow:
  STRATEGY → PLAN → WORKORDERS → ROUTER → ADAPTER → VERIFY → LEARNING

Hermes adapters (hermes_research / hermes_browser) are operator fallbacks only —
not the brain. Auth/CAPTCHA failures become ``blocked`` (NeedsYou), never fake ok.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.core.local_mode import is_local
from app.community.adapter import CommunityDepartmentAdapter
from app.marketing import store
from app.marketing.adapters.community_adapter import CommunityAdapter
from app.marketing.adapters.content_adapter import ContentAdapter
from app.marketing.adapters.gtm_adapter import GtmOutreachAdapter
from app.marketing.adapters.hermes_adapter import (
    HermesBrowserAdapter,
    HermesResearchAdapter,
)
from app.marketing.adapters.media_adapters import (
    AdsAdapter,
    AdsGoogleAdapter,
    AdsMetaAdapter,
    BrowserOperatorAdapter,
    DesktopOperatorAdapter,
    ImageStudioAdapter,
    VideoStudioAdapter,
)
from app.marketing.adapters.visual_adapter import VisualAdapter
from app.marketing.router import route
from app.marketing.verify import is_block_error

log = logging.getLogger(__name__)

_ADAPTERS = (
    ContentAdapter(),
    VisualAdapter(),
    CommunityAdapter(),  # Reddit watering-hole find/draft
    CommunityDepartmentAdapter(),  # Owned Discord / thesis (community.*)
    HermesResearchAdapter(),
    HermesBrowserAdapter(),
    ImageStudioAdapter(),
    VideoStudioAdapter(),
    BrowserOperatorAdapter(),
    DesktopOperatorAdapter(),
    AdsGoogleAdapter(),
    AdsMetaAdapter(),
    AdsAdapter(),
    GtmOutreachAdapter(),
)


def execution_enabled() -> bool:
    if is_local():
        from app.content.agent.policy import agent_enabled

        return agent_enabled()
    # Planning and drafts are useful by default; connector side effects are an
    # explicit deployment decision and remain disabled after a branch merge.
    raw = os.environ.get("MARKETING_EXECUTION_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _adapter_by_name(name: str):
    for adapter in _ADAPTERS:
        if adapter.name == name:
            return adapter
    return None


def _adapter_for_kind(kind: str):
    for adapter in _ADAPTERS:
        if adapter.supports(kind):
            return adapter
    return None


async def run_tick(*, workspace_id: str, limit: int = 5) -> dict[str, Any]:
    """Execute up to ``limit`` due jobs for the workspace."""
    summary: dict[str, Any] = {
        "workspace_id": workspace_id,
        "enabled": execution_enabled(),
        "processed": [],
        "done": 0,
        "failed": 0,
        "blocked": 0,
    }
    if not execution_enabled():
        summary["reason"] = "marketing_execution_disabled"
        if is_local():
            from app.content.agent.policy import content_blocked_message

            summary["next_step"] = content_blocked_message()
        return summary

    plan = store.active_plan(workspace_id)
    if plan is None:
        summary["reason"] = "no_active_plan"
        return summary
    summary["plan_id"] = plan["id"]

    due = store.due_jobs(workspace_id, limit=limit)
    summary["due"] = len(due)

    for job in due:
        decision = route(job, plan=plan)
        summary.setdefault("routes", []).append(
            {"id": job["id"], "reason": decision["reason"]}
        )
        if decision["lane"] == "human":
            store.mark_job(
                job["id"],
                status="blocked",
                bump_attempt=True,
                block_reason="router_needs_you",
                error_message="router_needs_you",
                executor="needs_you",
                result={"needs_you": True, "workorder": decision["workorder"]},
            )
            summary["blocked"] += 1
            summary["processed"].append(
                {"id": job["id"], "status": "blocked", "kind": job["kind"]}
            )
            continue

        adapter = _adapter_by_name(decision["adapter"]) or _adapter_for_kind(job["kind"])
        store.mark_job(job["id"], status="running", bump_attempt=True)
        if adapter is None:
            store.mark_job(
                job["id"],
                status="failed",
                error_message=f"no_adapter:{job['kind']}",
                result={"error": "no_adapter", "route": decision},
            )
            summary["failed"] += 1
            summary["processed"].append(
                {"id": job["id"], "status": "failed", "kind": job["kind"]}
            )
            continue
        try:
            outcome = await adapter.run(job)
        except Exception as exc:
            log.exception("marketing job %s failed", job["id"])
            msg = str(exc)[:300]
            status = "blocked" if is_block_error(msg) else "failed"
            store.mark_job(
                job["id"],
                status=status,
                error_message=msg,
                block_reason=msg if status == "blocked" else None,
                executor=adapter.name,
                result={"error": msg, "route": decision},
            )
            summary[status] = summary.get(status, 0) + 1
            summary["processed"].append(
                {"id": job["id"], "status": status, "kind": job["kind"], "error": msg}
            )
            try:
                from app.marketing import learning

                learning.record_from_job(
                    {**job, "error_message": msg, "block_reason": msg if status == "blocked" else None},
                    status=status,
                )
            except Exception:
                pass
            continue

        status = str(outcome.get("status") or ("done" if outcome.get("ok") else "failed"))
        if status not in {"done", "failed", "blocked"}:
            status = "failed"
        marked = store.mark_job(
            job["id"],
            status=status,
            result={**(outcome.get("result") or {}), "route": decision["reason"]},
            proof=outcome.get("proof") or {},
            error_message=outcome.get("error"),
            block_reason=outcome.get("block_reason"),
            executor=adapter.name,
        )
        summary[status] = summary.get(status, 0) + 1
        summary["processed"].append(
            {
                "id": job["id"],
                "status": status,
                "kind": job["kind"],
                "executor": adapter.name,
                "lane": decision["lane"],
                "proof": outcome.get("proof") or {},
            }
        )
        try:
            from app.marketing import learning

            learning.record_from_job(marked or job, status=status)
        except Exception:
            pass

    summary["job_counts"] = store.job_counts(workspace_id, plan_id=plan["id"])
    try:
        from app.marketing import learning

        summary["learning"] = learning.summarize(workspace_id)
    except Exception:
        pass
    return summary


def resume_blocked(workspace_id: str, job_id: str) -> dict[str, Any]:
    """Founder cleared NeedsYou — re-queue a blocked job."""
    from datetime import timedelta

    from core.clock import utcnow

    from app.db.models import MarketingJob
    from app.state import db

    now = utcnow()
    factory = db()
    with factory() as session:
        row = session.get(MarketingJob, job_id)
        if row is None or row.workspace_id != workspace_id:
            raise ValueError("blocked_job_not_found")
        if row.status != "blocked":
            raise ValueError("job_not_blocked")
        row.status = "pending"
        row.scheduled_for = now - timedelta(seconds=5)
        row.block_reason = None
        row.error_message = None
        row.finished_at = None
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return store.serialize_job(row)


def status(workspace_id: str) -> dict[str, Any]:
    plan = store.active_plan(workspace_id)
    browser_ok = False
    try:
        from app.marketing.adapters import openclaw_bridge

        browser_ok = openclaw_bridge.browser_available()
    except Exception:
        browser_ok = False
    learning_summary: dict[str, Any] = {}
    try:
        from app.marketing import learning

        learning_summary = learning.summarize(workspace_id)
    except Exception:
        learning_summary = {}

    def _flag(name: str, *, default: str = "true") -> bool:
        raw = os.environ.get(name, default)
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    from app.content.platforms import dry_run_enabled as content_dry_run

    return {
        "enabled": execution_enabled(),
        "plan": plan,
        "job_counts": store.job_counts(workspace_id, plan_id=plan["id"] if plan else None),
        "adapters": [a.name for a in _ADAPTERS],
        "loop": "strategy → plan → workorders → router → adapter → verify → learning",
        "doc": "122_MARKETING_MEDIA_MACHINE.md",
        "note": (
            "API/script first. image_studio = real-asset compositor (local-first). "
            "video_studio = ffmpeg Ken Burns. browser_operator = Playwright isolate. "
            "desktop_operator = CUA/Peekaboo probe (NeedsYou until granted). "
            "ads = official APIs, always PAUSED; enable_ad requires spend_money. "
            "outbound = gtm.supymem wrap. Never ChatGPT web UIs or Ads Manager GUI."
        ),
        "openclaw_browser": browser_ok,
        "learning": learning_summary,
        "flags": {
            "CONTENT_DRY_RUN": bool(content_dry_run()),
            "CHANNELS_DRY_RUN": _flag("CHANNELS_DRY_RUN", default="true"),
            "MARKETING_OUTBOUND_ENABLED": _flag("MARKETING_OUTBOUND_ENABLED", default="false"),
            "MARKETING_EXECUTION_ENABLED": execution_enabled(),
        },
    }
