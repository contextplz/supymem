"""Capability probe for the Media Studio GUI."""

from __future__ import annotations

from typing import Any

from app.marketing.adapters import openclaw_bridge, recordly_bridge
from app.marketing.media.images import _bfl_key, _gemini_key, _pollinations_enabled, comfy_available
from app.marketing.media.probes import (
    cua_available,
    google_ads_available,
    meta_ads_available,
    peekaboo_available,
    pexels_available,
    pixabay_available,
    resolve_available,
)
from app.marketing.media.video import ffmpeg_available


def capabilities(*, hosted: bool | None = None) -> dict[str, Any]:
    from app.worker.handlers import hosted_mode
    if hosted is None:
        hosted = hosted_mode()
    if hosted:
        blocked = {"ok": False, "note": "Unavailable on the hosted Media API."}
        # Do not probe server desktop, loopback services or global credentials.
        return {"local_first": False, "execution_location": "hosted_server",
            "legacy_execution_available": False,
            "unavailable_reason": "Recording, generation, browser and desktop actions need an isolated hosted execution service. Uploads and saved assets are available; use Ads for reviewed campaign proposals.",
            "image": {key: dict(blocked) for key in ("compositor", "comfyui", "bfl", "gemini_api", "pollinations")},
            "video": {key: dict(blocked) for key in ("ffmpeg", "resolve", "recordly", "product_demo")},
            "stock": {}, "browser": {"playwright": dict(blocked)},
            "desktop": {"cua": dict(blocked), "peekaboo": dict(blocked)},
            "ads": {"google": dict(blocked), "meta": dict(blocked), "always_paused": True}}
    comfy = comfy_available()
    return {
        "local_first": True,
        "execution_location": "local_backend",
        "legacy_execution_available": True,
        "image": {
            "compositor": True,
            "comfyui": comfy,
            "bfl": {"ok": bool(_bfl_key())},
            "gemini_api": {"ok": bool(_gemini_key())},
            "pollinations": {"ok": _pollinations_enabled(), "note": "prototype scenes only, never people"},
            "chatgpt_web": {"ok": False, "refused": "tos"},
        },
        "video": {
            "ffmpeg": ffmpeg_available(),
            "resolve": resolve_available(),
            "generative_t2v": {"ok": False, "refused": "ai_look"},
            "recordly": recordly_bridge.probe(),
            "product_demo": {
                "ok": bool(ffmpeg_available().get("ok") and openclaw_bridge.browser_available()),
                "note": "Playwright walkthrough + ffmpeg. Recordly GUI is optional polish.",
            },
        },
        "stock": {"pexels": pexels_available(), "pixabay": pixabay_available()},
        "browser": {
            "playwright": openclaw_bridge.browser_available(),
            "note": "Attach via chrome://inspect, never the Default profile dir.",
        },
        "desktop": {
            "cua": cua_available(),
            "peekaboo": peekaboo_available(),
            "note": "Needs Accessibility + Screen Recording. Scripting first for Resolve/Photoshop.",
        },
        "ads": {
            "google": google_ads_available(),
            "meta": meta_ads_available(),
            "always_paused": True,
        },
    }
