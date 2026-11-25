"""Local-first image generation: compositor → ComfyUI → paid/free APIs.

Never drives ChatGPT/Claude/Gemini *web UIs*. Never loads non-commercial weights.
Never invents a person — founder photos must be uploaded.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from app.marketing.media import compositor, store as media_store
from app.marketing.media.store import refuse_blocked_weights
from app.core.local_mode import is_local

log = logging.getLogger(__name__)


def _comfy_base() -> str:
    if is_local():
        from app.core.local_secrets import read_settings

        return (read_settings("images")["values"].get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
    return os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")


def comfy_available() -> dict[str, Any]:
    url = f"{_comfy_base()}/system_stats"
    try:
        with urlopen(url, timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "url": _comfy_base(), "stats": body}
    except Exception as exc:
        return {"ok": False, "url": _comfy_base(), "error": str(exc)[:160]}


def _bfl_key() -> str:
    if is_local():
        from app.core.local_secrets import get_secret

        return (get_secret("images", "bfl_api_key") or "").strip()
    return (os.environ.get("BFL_API_KEY") or os.environ.get("BFL_API_TOKEN") or "").strip()


def _gemini_key() -> str:
    if is_local():
        from app.core.local_secrets import get_secret

        return (get_secret("images", "gemini_api_key") or "").strip()
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
        or ""
    ).strip()


def _pollinations_enabled() -> bool:
    if is_local():
        return False
    raw = os.environ.get("POLLINATIONS_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def image_setup_message() -> str:
    if _bfl_key():
        return "Your draft is ready without an image. Generate one in Media Studio, then add it when you publish."
    return ("Your draft is ready without an image. Upload your product screenshot or photo in Media Studio, "
            "then add it when you publish. For generated images, add your BFL image key in Keys and connections.")


def _photographic_prompt(headline: str, scene: str) -> str:
    bits = [
        scene.strip() or headline.strip(),
        "documentary photograph, 35mm, natural window light, Portra 400,",
        "visible skin texture only if a real person is in the source photo,",
        "slight film grain, uncluttered, no text, no watermark, no logo redraw,",
        "hands out of frame, not CGI, not illustrated",
    ]
    return " ".join(b for b in bits if b)


def _try_bfl(prompt: str, width: int, height: int) -> dict[str, Any]:
    key = _bfl_key()
    if not key:
        return {"ok": False, "error": "bfl_key_missing"}
    payload = json.dumps(
        {
            "prompt": prompt,
            "width": width,
            "height": height,
            "safety_tolerance": 2,
        }
    ).encode("utf-8")
    req = Request(
        "https://api.bfl.ai/v1/flux-schnell",
        data=payload,
        headers={"Content-Type": "application/json", "x-key": key},
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        return {"ok": True, "body": body, "engine": "bfl_flux_schnell"}
    except Exception as exc:
        return {"ok": False, "error": f"bfl:{exc}"[:200]}


def _try_pollinations(prompt: str, dest, width: int, height: int) -> dict[str, Any]:
    if not _pollinations_enabled():
        return {"ok": False, "error": "pollinations_disabled"}
    from urllib.parse import quote

    url = (
        "https://image.pollinations.ai/prompt/"
        f"{quote(prompt[:400])}"
        f"?width={width}&height={height}&nologo=true&model=flux"
    )
    req = Request(url, headers={"User-Agent": "company-os-media/1.0"})
    try:
        with urlopen(req, timeout=60) as resp:
            data = resp.read()
        if not data or len(data) < 800:
            return {"ok": False, "error": "pollinations_empty"}
        dest.write_bytes(data)
        return {"ok": True, "engine": "pollinations_flux", "path": str(dest)}
    except URLError as exc:
        return {"ok": False, "error": f"pollinations:{exc}"[:200]}
    except Exception as exc:
        return {"ok": False, "error": f"pollinations:{exc}"[:200]}


def generate_image(
    *,
    workspace_id: str,
    headline: str,
    scene: str = "",
    want_person: bool = False,
    width: int = 1080,
    height: int = 1350,
    accent: str = "#6B5A4A",
    allow_remote: bool = False,
) -> dict[str, Any]:
    blocked = refuse_blocked_weights(f"{headline} {scene}")
    if blocked:
        return {"ok": False, "status": "failed", "error": blocked}

    # Local compositor on real uploaded pixels. No invented desks, no invented faces.
    composed = compositor.compose_still(
        workspace_id=workspace_id,
        headline=headline,
        caption=scene[:80] if scene else None,
        width=width,
        height=height,
        accent=accent,
        want_person=want_person,
    )
    if composed.get("ok"):
        return _persist(workspace_id, composed, headline)

    if want_person or not allow_remote:
        return {
            "ok": False,
            "status": composed.get("status") or "failed",
            "error": composed.get("error") or "image_failed",
            "block_reason": composed.get("block_reason") if composed.get("error") == "founder_photos_required"
                or not is_local() else image_setup_message(),
            "result": composed,
        }

    # Optional scene generation only when explicitly allowed and no person is requested.
    prompt = _photographic_prompt(headline, scene)
    dest = media_store.media_dir(workspace_id, "stills") / f"api_scene_{os.urandom(4).hex()}.jpg"

    comfy = comfy_available()
    if comfy.get("ok"):
        # ComfyUI is present but we do not ship a FLUX graph in-repo. Record NeedsYou
        # with the live URL so the founder can drop in an Apache-licensed workflow.
        composed["comfy"] = comfy
        composed.setdefault("meta_notes", []).append(
            "comfyui_online_no_workflow: add an Apache FLUX schnell graph to use it"
        )

    if _bfl_key():
        bfl = _try_bfl(prompt, width, height)
        if bfl.get("ok") and isinstance(bfl.get("body"), dict):
            sample = (bfl["body"].get("sample") or bfl["body"].get("result") or "")
            if isinstance(sample, str) and sample.startswith("http"):
                try:
                    with urlopen(sample, timeout=45) as resp:
                        dest.write_bytes(resp.read())
                    composed = {
                        "ok": True,
                        "path": str(dest),
                        "width": width,
                        "height": height,
                        "mime": "image/jpeg",
                        "source": "bfl",
                        "source_kind": "api_scene",
                        "review_status": "pending",
                        "license": {"engine": "bfl_flux_schnell", "commercial": True},
                    }
                    return _persist(workspace_id, composed, headline)
                except Exception as exc:
                    log.warning("bfl download failed: %s", exc)

    poll = _try_pollinations(prompt, dest, width, height)
    if poll.get("ok"):
        composed = {
            "ok": True,
            "path": str(dest),
            "width": width,
            "height": height,
            "mime": "image/jpeg",
            "source": "pollinations",
            "source_kind": "api_scene",
            "review_status": "pending",
            "license": {
                "engine": "pollinations",
                "commercial": False,
                "note": "prototype only, review before any paid placement",
            },
        }
        return _persist(workspace_id, composed, headline)

    # Last resort is still not a fake person or a drawn desk.
    return {
        "ok": False,
        "status": composed.get("status") or "blocked",
        "error": composed.get("error") or "image_failed",
        "block_reason": image_setup_message() if is_local() else composed.get("block_reason")
        or "No real product screenshot in the brand kit, and no licensed image API is configured.",
        "result": composed,
    }


def _persist(workspace_id: str, composed: dict[str, Any], headline: str) -> dict[str, Any]:
    if not composed.get("ok"):
        return {
            "ok": False,
            "status": composed.get("status") or "failed",
            "error": composed.get("error") or "compose_failed",
            "block_reason": composed.get("block_reason"),
            "result": composed,
        }
    path = str(composed["path"])
    size = compositor.jpeg_size(path)
    width = size[0] if size else composed.get("width")
    height = size[1] if size else composed.get("height")
    asset = media_store.insert_asset(
        workspace_id=workspace_id,
        kind="generated_image",
        path=path,
        source=str(composed.get("source") or "compositor"),
        mime=str(composed.get("mime") or "image/jpeg"),
        title=headline[:120] or "still",
        width=width,
        height=height,
        license_info=composed.get("license") or {},
        meta={k: v for k, v in composed.items() if k not in {"path", "ok"}},
        review_status=str(composed.get("review_status") or "pending"),
    )
    return {
        "ok": True,
        "status": "done",
        "asset": asset,
        "result": {"asset_id": asset["id"], **composed},
        "proof": {
            "mode": "local",
            "visual_path": path,
            "asset_id": asset["id"],
            "source": composed.get("source"),
        },
    }
