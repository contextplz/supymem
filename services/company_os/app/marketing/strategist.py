"""Marketing strategist — turns a brief into an executable plan.

Deterministic by default so we do not burn tokens re-deciding strategy every
morning. The LLM is not required for v1; themes come from the brief's product,
ICP pains, and differentiators.
"""

from __future__ import annotations

from typing import Any

from app.content import brief as brief_store
from app.marketing import compiler, store
from app.marketing.schema import default_constraints, empty_strategy


def _themes_from_brief(brief: dict[str, Any]) -> list[str]:
    themes: list[str] = []
    product = brief.get("product") or {}
    for d in product.get("differentiators") or []:
        themes.append(str(d))
    for seg in (brief.get("icp") or {}).get("segments") or []:
        for pain in seg.get("pains") or []:
            themes.append(str(pain))
        for objection in seg.get("objections") or []:
            themes.append(f"objection: {objection}")
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in themes:
        key = t.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t.strip())
    return out[:12]


def build_strategy_from_brief(brief: dict[str, Any]) -> dict[str, Any]:
    strategy = empty_strategy()
    cadence = brief.get("cadence") or {}
    voice = brief.get("voice") or {}
    product = brief.get("product") or {}

    strategy["voice"] = str(voice.get("tone") or "founder")
    strategy["themes"] = _themes_from_brief(brief)
    strategy["product_name"] = str(product.get("name") or "")
    strategy["one_liner"] = str(product.get("one_liner") or "")

    platforms: dict[str, Any] = {}
    li = cadence.get("linkedin") or {}
    if isinstance(li, dict) and int(li.get("per_week") or 0) > 0:
        platforms["linkedin"] = {
            "per_week": int(li.get("per_week") or 4),
            "format": "text_plus_card",
            "enabled": True,
        }
    x = cadence.get("x") or {}
    if isinstance(x, dict) and int(x.get("per_day") or 0) > 0:
        platforms["x"] = {
            "per_day": int(x.get("per_day") or 2),
            "format": "text",
            "enabled": True,
        }
    reddit = cadence.get("reddit") or {}
    if isinstance(reddit, dict) and int(reddit.get("per_week") or 0) > 0:
        platforms["reddit"] = {
            "per_week": int(reddit.get("per_week") or 1),
            "format": "text",
            "enabled": True,
            "mode": "find_and_draft",
        }
    # Brief template defaults always include linkedin.
    if not platforms:
        platforms["linkedin"] = {
            "per_week": 4,
            "format": "text_plus_card",
            "enabled": True,
        }
    strategy["content"]["platforms"] = platforms
    strategy["content"]["value_to_promo"] = int(cadence.get("value_to_promo") or 4)

    watering: list[str] = []
    for seg in (brief.get("icp") or {}).get("segments") or []:
        for hole in seg.get("watering_holes") or []:
            watering.append(str(hole))
    strategy["community"]["watering_holes"] = watering[:20]

    visual = brief.get("visual_language") if isinstance(brief.get("visual_language"), dict) else {}
    if visual:
        strategy["content"]["visuals"]["brand"] = {
            "colors": visual.get("colors") or ["#0B1F17", "#E8F0EC", "#1B4D3E"],
            "wordmark": visual.get("wordmark") or product.get("name") or "Company OS",
            "accent": visual.get("accent") or "#1B4D3E",
        }
    else:
        strategy["content"]["visuals"]["brand"] = {
            "colors": ["#0B1F17", "#E8F0EC", "#1B4D3E"],
            "wordmark": product.get("name") or "Company OS",
            "accent": "#1B4D3E",
        }
    return strategy


def create_plan_from_brief(
    *,
    workspace_id: str,
    horizon_days: int = 30,
    compile_days: int = 7,
    activate: bool = True,
) -> dict[str, Any]:
    """Create (and optionally activate) a plan, then compile the first week of jobs."""
    brief = brief_store.active(workspace_id)
    if brief is None:
        raise ValueError("no_brief")

    strategy = build_strategy_from_brief(brief)
    strategy["horizon_days"] = horizon_days
    try:
        from app.marketing import learning

        strategy = learning.apply_to_strategy(strategy, learning.summarize(workspace_id))
    except Exception:
        pass
    # Opt-in outbound: MARKETING_OUTBOUND_ENABLED=true (and Gmail ready) turns on
    # enqueue_outreach / send_email_touch compilation.
    try:
        import os

        from app.gtm.supymem import pipeline as gtm_pipeline

        want = os.environ.get("MARKETING_OUTBOUND_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        status = gtm_pipeline.enrich_status()
        if want and status.get("gmail", {}).get("ready"):
            strategy.setdefault("outbound", {})["enabled"] = True
            strategy["outbound"]["note"] = (
                "Enabled via MARKETING_OUTBOUND_ENABLED, CHANNELS_DRY_RUN gates live sends."
            )
    except Exception:
        pass
    constraints = default_constraints()

    plan = store.save_plan(
        workspace_id=workspace_id,
        strategy=strategy,
        constraints=constraints,
        brief_id=str(brief.get("id") or "") or None,
        horizon_days=horizon_days,
        source="strategist",
        activate=activate,
    )
    jobs = compiler.compile_plan(
        workspace_id=workspace_id,
        plan=plan,
        days=compile_days,
    )
    return {
        "ok": True,
        "plan": plan,
        "jobs_created": len(jobs),
        "jobs": jobs[:20],
        "job_counts": store.job_counts(workspace_id, plan_id=plan["id"]),
    }


def activate_plan(*, workspace_id: str, plan_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.db.models import MarketingPlan
    from app.state import db
    from core.clock import utcnow

    now = utcnow()
    factory = db()
    with factory() as session:
        target = session.get(MarketingPlan, plan_id)
        if target is None or target.workspace_id != workspace_id:
            raise ValueError("plan_not_found")
        for existing in session.scalars(
            select(MarketingPlan).where(
                MarketingPlan.workspace_id == workspace_id,
                MarketingPlan.status == "active",
            )
        ):
            if existing.id != plan_id:
                existing.status = "archived"
                existing.updated_at = now
        target.status = "active"
        target.updated_at = now
        session.commit()
    return store.get_plan(workspace_id, plan_id) or {}
