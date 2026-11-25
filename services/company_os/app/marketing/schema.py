"""Canonical marketing plan + job shapes."""

from __future__ import annotations

from typing import Any

JOB_KINDS = frozenset(
    {
        "create_content",
        "create_visual",
        "publish_content",
        "find_threads",
        "draft_reply",
        "publish_reply",
        "research_topic",
        "check_result",
        "generate_image",
        "generate_video",
        "operate_app",
        "operate_browser",
        "create_ad",
        "enable_ad",
        "ingest_asset",
        "enqueue_outreach",
        "send_email_touch",
        "create_relationship_asset",
        "review_asset",
        "capture_interview",
        "propose_pilot",
    }
)

PLATFORMS = ("linkedin", "x", "reddit")

DEFAULT_TEMPLATES = (
    "insight_card",
    "stat_card",
    "quote_card",
    "before_after",
    "screenshot_frame",
)


def empty_strategy() -> dict[str, Any]:
    return {
        "horizon_days": 30,
        "voice": "founder",
        "themes": [],
        "goals": [
            "Stay visible to the ICP",
            "Teach, don't pitch",
            "Prove posts actually landed",
        ],
        "content": {
            "platforms": {},
            "formats": ["text", "branded_card"],
            "visuals": {
                "enabled": True,
                "templates": list(DEFAULT_TEMPLATES),
                "default_template": "insight_card",
            },
        },
        "outbound": {
            "enabled": False,
            "per_week": 3,
            "note": (
                "When enabled, compiler emits enqueue_outreach + send_email_touch "
                "jobs that wrap gtm.supymem.pipeline (CHANNELS_DRY_RUN gated)."
            ),
        },
        "community": {
            "enabled": True,
            "platforms": ["reddit"],
            "mode": "find_and_draft",  # human-send until commercial Reddit is clear
        },
    }


def default_constraints() -> dict[str, Any]:
    return {
        "require_proof": True,
        "fail_closed": True,
        "no_image_model": True,
        "linkedin_replies": "policy_gated",
        # LinkedIn posts prefer a branded card; missing render skips to text
        # only with an event — never blocks the whole pipeline.
        "missing_visual": "skip_text_only",
        "linkedin_prefer_image": True,
        "executors_allowed": [
            "content_engine",
            "composio",
            "visual_renderer",
            "hermes_research",
            "hermes_browser",
            "image_studio",
            "video_studio",
            "browser_operator",
            "desktop_operator",
            "ads",
            "ads_google",
            "ads_meta",
            "gtm_outreach",
        ],
    }


def proof_shape(
    *,
    external_id: str | None = None,
    external_url: str | None = None,
    account_id: str | None = None,
    mode: str = "simulated",
    timestamp: str | None = None,
    content_post_id: str | None = None,
    visual_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "external_id": external_id,
        "external_url": external_url,
        "account_id": account_id,
        "mode": mode,
        "timestamp": timestamp,
        "content_post_id": content_post_id,
        "visual_path": visual_path,
    }
    if extra:
        out.update(extra)
    return out
