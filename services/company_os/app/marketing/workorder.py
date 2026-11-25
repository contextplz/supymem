"""Creative WorkOrder shape — explicit executable jobs, not chat instructions."""

from __future__ import annotations

from typing import Any

from app.marketing.schema import JOB_KINDS, default_constraints


def workorder_from_job(job: dict[str, Any], *, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Lift a marketing job into a Creative WorkOrder view."""
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    strategy = (plan or {}).get("strategy") or {}
    constraints = (plan or {}).get("constraints") or default_constraints()
    kind = str(job.get("kind") or "")
    platform = job.get("platform") or payload.get("platform")

    tools = _tools_for(kind, platform, constraints)
    from app.gtm.relationships.denylist import linkedin_host_blocked

    payload_url = str(payload.get("url") or "")
    if linkedin_host_blocked(payload_url) or (
        kind == "operate_browser" and linkedin_host_blocked(str(platform or ""))
    ):
        tools = [t for t in tools if t not in {"hermes_browser", "browser_operator"}]
    return {
        "id": job.get("id"),
        "kind": kind,
        "platform": platform,
        "title": job.get("title"),
        "objective": payload.get("objective")
        or _default_objective(kind, platform, payload),
        "inputs": {
            "payload": payload,
            "depends_on": job.get("depends_on") or [],
            "themes": payload.get("themes")
            or (strategy.get("themes") or [])[:6],
        },
        "expected_output": _expected_output(kind),
        "tools_allowed": tools,
        "constraints": {
            **constraints,
            "require_visual": kind in {"create_visual", "publish_content"}
            and platform == "linkedin"
            and bool((strategy.get("content") or {}).get("visuals", {}).get("enabled", True)),
        },
        "success_criteria": _success_criteria(kind, platform),
        "verification_method": _verification(kind),
        "approval_required": bool(constraints.get("approval_required", False)),
        "retry_policy": {"max": 2, "on_block": "needs_you"},
        "deadline": job.get("scheduled_for"),
        "status": job.get("status"),
        "plan_id": job.get("plan_id"),
        "workspace_id": job.get("workspace_id"),
    }


def _tools_for(kind: str, platform: Any, constraints: dict[str, Any]) -> list[str]:
    allowed = list(constraints.get("executors_allowed") or [])
    if kind == "create_content":
        return ["content_engine", "director"]
    if kind == "create_visual":
        return ["visual_renderer"]
    if kind == "publish_content":
        tools = ["composio", "content_engine"]
        if "hermes" in allowed or "hermes_browser" in allowed:
            tools.append("hermes_browser")
        return tools
    if kind in {"find_threads", "research_topic"}:
        return ["community", "hermes_research"]
    if kind in {"draft_reply", "publish_reply"}:
        return ["community"]
    if kind == "check_result":
        tools = ["content_engine", "composio"]
        if "hermes_browser" in allowed or "hermes" in allowed:
            tools.append("hermes_browser")
        return tools
    if kind == "generate_image":
        return ["image_studio"]
    if kind == "generate_video":
        return ["video_studio", "ffmpeg"]
    if kind == "operate_browser":
        return ["browser_operator", "hermes_browser"]
    if kind == "operate_app":
        return ["desktop_operator"]
    if kind == "create_ad":
        platform = str(platform or "").lower()
        if platform in {"google", "google_ads"}:
            return ["ads_google", "ads"]
        if platform in {"meta", "facebook", "facebook_ads", "instagram"}:
            return ["ads_meta", "ads"]
        return ["ads"]
    if kind == "enable_ad":
        return ["ads", "spend_money"]
    if kind in {"enqueue_outreach", "send_email_touch"}:
        return ["gtm_outreach"]
    if kind == "create_relationship_asset":
        return ["content_engine"]
    if kind == "review_asset":
        return ["content_engine"]
    if kind in {"capture_interview", "propose_pilot"}:
        return ["gtm_outreach"]
    return allowed or ["content_engine"]


def _default_objective(kind: str, platform: Any, payload: dict[str, Any] | None = None) -> str:
    if kind == "create_content":
        return f"Draft {platform or 'social'} post from the brief"
    if kind == "create_visual":
        return "Render branded card for the post (no image model)"
    if kind == "publish_content":
        return f"Publish to {platform} with verification proof"
    if kind == "check_result":
        return "Open the live URL and screenshot proof (OpenClaw/Playwright)"
    if kind == "research_topic":
        return "Gather evidence for today's narrative"
    if kind == "generate_image":
        return "Compose a photographic still from real assets (never a fake person)"
    if kind == "generate_video":
        if str((payload or {}).get("engine") or "").lower() in {"product_demo", "recordly", "demo"}:
            return "Record a product URL walkthrough (Playwright) or ingest a Recordly MP4"
        return "Stitch a 15-30s clip with ffmpeg (no generative video)"
    if kind == "operate_browser":
        return "Drive an isolated browser or attach to Chrome via CDP"
    if kind == "operate_app":
        return "Script the local app or NeedsYou for GUI control"
    if kind == "create_ad":
        return "Create a PAUSED campaign via the official ads API"
    if kind == "enable_ad":
        return "Enable a PAUSED campaign only after spend_money approval"
    if kind == "enqueue_outreach":
        return "Enroll contacts into GTM cold-mail Day 1 via supymem"
    if kind == "send_email_touch":
        return "Drain the GTM send queue (dry-run unless approved)"
    return kind


def _expected_output(kind: str) -> dict[str, Any]:
    if kind == "create_content":
        return {"content_post_id": "string", "body": "string"}
    if kind == "create_visual":
        return {"visual_path": "png path", "or": "skipped"}
    if kind == "publish_content":
        return {"external_id": "required when live", "external_url": "required when live"}
    return {}


def _success_criteria(kind: str, platform: Any) -> list[str]:
    if kind == "create_visual":
        return ["visual_path_or_explicit_skip", "template_allowlisted", "no_image_model"]
    if kind == "publish_content":
        out = ["external_id_present_or_simulated", "no_fake_live_ok"]
        if platform == "linkedin":
            out.append("image_attached_when_visual_path_exists")
        return out
    if kind == "create_content":
        return ["content_post_id", "lint_and_critic_passed_or_refused"]
    return ["proof_recorded"]


def _verification(kind: str) -> str:
    if kind == "publish_content":
        return "external_id_and_url"
    if kind == "check_result":
        return "browser_screenshot_or_db_proof"
    if kind == "create_visual":
        return "visual_path_or_skipped"
    if kind == "create_content":
        return "content_post_id"
    return "result_json"


def validate_kind(kind: str) -> bool:
    return kind in JOB_KINDS
