"""Deterministic execution router — API/code before Hermes before human."""

from __future__ import annotations

from typing import Any

from app.marketing.workorder import workorder_from_job


def route(
    job: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pick the executor lane for a job. Never uses an LLM."""
    order = workorder_from_job(job, plan=plan)
    tools = order.get("tools_allowed") or []
    kind = order["kind"]

    payload = (order.get("inputs") or {}).get("payload") or {}

    if kind == "create_content":
        lane = "code"
        adapter = "content_engine"
    elif kind == "check_result":
        # Live URL proof via OpenClaw/Playwright when allowed; else DB proof only.
        if (
            payload.get("verify_browser", True)
            and "hermes_browser" in tools
        ):
            lane = "hermes"
            adapter = "hermes_browser"
        else:
            lane = "code"
            adapter = "content_engine"
    elif kind == "create_visual":
        lane = "code"
        adapter = "visual_renderer"
    elif kind == "publish_content":
        # Prefer Composio/API. Hermes browser only if explicitly forced
        # (and even then the adapter refuses LinkedIn GUI publish).
        if payload.get("force_hermes") and "hermes_browser" in tools:
            lane = "hermes"
            adapter = "hermes_browser"
        else:
            lane = "api"
            adapter = "content_engine"
    elif kind in {"find_threads", "research_topic"} and "hermes_research" in tools:
        lane = "hermes"
        adapter = "hermes_research"
    elif kind in {"find_threads", "draft_reply", "publish_reply", "research_topic"}:
        lane = "code"
        adapter = "community"
    elif kind.startswith("community."):
        lane = "code"
        adapter = "community_department"
    elif kind == "generate_image":
        lane = "code"
        adapter = "image_studio"
    elif kind == "generate_video":
        lane = "code"
        adapter = "video_studio"
    elif kind == "operate_browser":
        lane = "browser"
        adapter = "browser_operator"
    elif kind == "operate_app":
        lane = "gui"
        adapter = "desktop_operator"
    elif kind == "create_ad":
        platform = str(payload.get("platform") or job.get("platform") or "").lower()
        lane = "api"
        if platform in {"google", "google_ads"}:
            adapter = "ads_google"
        elif platform in {"meta", "facebook", "facebook_ads", "instagram"}:
            adapter = "ads_meta"
        else:
            adapter = "ads"
    elif kind == "enable_ad":
        # Adapter returns blocked + spend_money NeedsYou (never auto-enable).
        lane = "api"
        platform = str(payload.get("platform") or job.get("platform") or "").lower()
        if platform in {"google", "google_ads"}:
            adapter = "ads_google"
        elif platform in {"meta", "facebook", "facebook_ads", "instagram"}:
            adapter = "ads_meta"
        else:
            adapter = "ads"
    elif kind in {"enqueue_outreach", "send_email_touch"}:
        lane = "code"
        adapter = "gtm_outreach"
    else:
        lane = "human"
        adapter = "needs_you"

    return {
        "lane": lane,  # code | api | hermes | human
        "adapter": adapter,
        "workorder": order,
        "reason": f"{kind}→{lane}/{adapter}",
    }
