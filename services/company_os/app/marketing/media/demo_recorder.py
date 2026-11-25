"""Product demo recorder — Playwright walkthrough + ffmpeg polish.

This is the automatable path. Recordly (AGPL Electron GUI) is the optional
human polish lane via ``recordly_bridge``. We never import Recordly source.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.ids import new_id

from app.gtm.relationships.denylist import linkedin_host_blocked
from app.marketing.adapters import recordly_bridge
from app.marketing.media import store as media_store
from app.marketing.media.video import ffmpeg_available, ffmpeg_bin, ffmpeg_install_message

REPO_ROOT = Path(__file__).resolve().parents[5]

DEFAULT_URL = "http://127.0.0.1:3100/content"
DEFAULT_STEPS: list[dict[str, Any]] = [
    {"action": "wait", "ms": 1800},
    {"action": "scroll", "y": 420},
    {"action": "wait", "ms": 900},
    {"action": "scroll", "y": 0},
    {"action": "wait", "ms": 700},
]


def default_demo_url() -> str:
    return (os.environ.get("RECORD_DEMO_URL") or DEFAULT_URL).strip() or DEFAULT_URL


def validate_demo_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return "url_required"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in {"http", "https"}:
        return "url_scheme_blocked"
    if not parsed.hostname:
        return "url_host_required"
    if linkedin_host_blocked(raw):
        return "linkedin_host_blocked"
    return None


def ingest_allowed(path: Path) -> bool:
    resolved = path.resolve()
    extra = (os.environ.get("RECORD_DEMO_INGEST_ROOT") or "").strip()
    roots = [
        media_store.MEDIA_ROOT.resolve(),
        (REPO_ROOT / "data").resolve(),
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Movies",
    ]
    if extra:
        roots.append(Path(extra).expanduser().resolve())
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _headed(payload: dict[str, Any]) -> bool:
    if payload.get("headed") is True:
        return True
    raw = (os.environ.get("RECORD_DEMO_HEADED") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def capture_walkthrough(
    *,
    url: str,
    dest_dir: Path,
    steps: list[dict[str, Any]] | None = None,
    headed: bool = False,
    timeout_ms: int = 25000,
) -> dict[str, Any]:
    """Record an isolated Chromium session. Mock this in CI — no headed browser."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {
            "ok": False,
            "error": "playwright_missing",
            "block_reason": f"Install Playwright Chromium, then retry. ({exc})"[:240],
        }

    dest_dir.mkdir(parents=True, exist_ok=True)
    video_path: Path | None = None
    title = ""
    final_url = url
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                record_video_dir=str(dest_dir),
                record_video_size={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            title = page.title() or ""
            final_url = page.url
            for step in steps or DEFAULT_STEPS:
                _apply_step(page, step)
            page.wait_for_timeout(400)
            video = page.video
            page.close()
            context.close()
            browser.close()
            if video is not None:
                raw = Path(video.path())
                if raw.is_file():
                    video_path = raw
    except (OSError, RuntimeError, TimeoutError, PlaywrightError) as exc:
        return {
            "ok": False,
            "error": f"capture_failed:{exc}"[:240],
            "block_reason": (
                "Could not record the URL. Is the app running (console :3100)? "
                f"{exc}"
            )[:400],
        }

    if video_path is None or not video_path.is_file():
        return {
            "ok": False,
            "error": "no_video_file",
            "block_reason": "Playwright did not write a recording. Retry headed on your Mac.",
        }
    return {
        "ok": True,
        "path": str(video_path),
        "title": title,
        "final_url": final_url,
    }


def _apply_step(page: Any, step: dict[str, Any]) -> None:
    action = str(step.get("action") or "wait").strip().lower()
    required = bool(step.get("required"))
    try:
        if action == "goto":
            target = str(step.get("url") or "").strip()
            if target and not validate_demo_url(target):
                page.goto(target, wait_until="domcontentloaded", timeout=20000)
        elif action == "wait":
            page.wait_for_timeout(max(50, min(int(step.get("ms") or 800), 8000)))
        elif action == "scroll":
            y = int(step.get("y") or 400)
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(400)
        elif action in {"click", "hover"}:
            selector = str(step.get("selector") or "").strip()
            if not selector:
                return
            loc = page.locator(selector).first
            if action == "hover":
                loc.hover(timeout=4000)
            else:
                loc.click(timeout=4000)
            page.wait_for_timeout(350)
        elif action == "press":
            page.keyboard.press(str(step.get("key") or "Escape"))
        elif action == "type":
            selector = str(step.get("selector") or "").strip()
            text = str(step.get("text") or "")
            if selector and text:
                page.locator(selector).first.fill(text, timeout=4000)
        else:
            page.wait_for_timeout(200)
    except Exception:
        if required:
            raise


def encode_mp4(
    src: Path,
    dest: Path,
    *,
    seconds: int | None = None,
    width: int = 1920,
    height: int = 1080,
) -> dict[str, Any]:
    ff = ffmpeg_available()
    if not ff.get("ok"):
        return {
            "ok": False,
            "error": "ffmpeg_missing",
            "block_reason": ff.get("next_step") or ffmpeg_install_message(),
            "result": ff,
        }
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30"
    )
    cmd = [
        str(ffmpeg_bin()),
        "-y",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
    ]
    if seconds:
        cmd.extend(["-t", str(max(4, min(int(seconds), 90)))])
    cmd.append(str(dest))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"ffmpeg_exec:{exc}"[:200]}
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 500:
        err = (proc.stderr or proc.stdout or "ffmpeg_failed")[-400:]
        return {"ok": False, "error": f"ffmpeg:{err}"[:300]}
    return {"ok": True, "path": str(dest)}


def encode_short_clip(src: Path, dest: Path, *, seconds: int = 15) -> dict[str, Any]:
    return encode_mp4(src, dest, seconds=seconds, width=1080, height=1920)


def ingest_export(
    *,
    workspace_id: str,
    src: Path,
    title: str,
    source: str = "recordly_export",
) -> dict[str, Any]:
    if not src.is_file():
        return {
            "ok": False,
            "status": "blocked",
            "error": "ingest_missing",
            "block_reason": f"No file at {src}",
        }
    if not ingest_allowed(src):
        return {
            "ok": False,
            "status": "blocked",
            "error": "ingest_path_rejected",
            "block_reason": (
                "Ingest only from Downloads, Desktop, Movies, or data/marketing_media. "
                "Set RECORD_DEMO_INGEST_ROOT to add a folder."
            ),
        }
    suffix = src.suffix.lower()
    if suffix not in {".mp4", ".webm", ".mov", ".gif", ".m4v"}:
        return {
            "ok": False,
            "status": "blocked",
            "error": "ingest_type_rejected",
            "block_reason": "Need an MP4, WebM, MOV, or GIF export.",
        }
    work = media_store.media_dir(workspace_id, "demo")
    dest = work / f"demo_{new_id('dem')}{suffix}"
    shutil.copy2(src, dest)
    encoded = dest
    if suffix != ".mp4" and suffix != ".gif":
        mp4 = work / f"demo_{new_id('dem')}.mp4"
        out = encode_mp4(dest, mp4)
        if out.get("ok"):
            encoded = Path(str(out["path"]))
    mime = "image/gif" if encoded.suffix.lower() == ".gif" else "video/mp4"
    asset = media_store.insert_asset(
        workspace_id=workspace_id,
        kind="generated_video",
        path=str(encoded),
        source=source,
        mime=mime,
        title=title[:120],
        license_info={
            "engine": source,
            "recordly_license": "AGPL-3.0",
            "generative_video": False,
        },
        meta={"ingest": str(src), "note": "pending_review_before_publish"},
        review_status="pending",
    )
    return {
        "ok": True,
        "status": "done",
        "asset": asset,
        "result": {"asset_id": asset["id"], "path": str(encoded), "engine": source},
        "proof": {"mode": "local", "visual_path": str(encoded), "asset_id": asset["id"]},
    }


def record_product_demo(
    *,
    workspace_id: str,
    url: str | None = None,
    headline: str = "Product demo",
    steps: list[dict[str, Any]] | None = None,
    seconds: int = 20,
    headed: bool | None = None,
    ingest_path: str | None = None,
    launch_recordly: bool = False,
    short_clip: bool = True,
) -> dict[str, Any]:
    """Capture or ingest a demo. Publishing stays on the usual content dry-run gates."""
    if ingest_path:
        return ingest_export(
            workspace_id=workspace_id,
            src=Path(ingest_path).expanduser(),
            title=headline or Path(ingest_path).stem,
            source="recordly_export",
        )
    if launch_recordly:
        launched = recordly_bridge.launch_gui()
        launched.setdefault("result", {})
        launched["result"]["recordly"] = recordly_bridge.probe()
        return launched

    target = (url or default_demo_url()).strip()
    bad = validate_demo_url(target)
    if bad:
        return {
            "ok": False,
            "status": "blocked",
            "error": bad,
            "block_reason": (
                "linkedin_host_blocked"
                if bad == "linkedin_host_blocked"
                else "Give an http(s) URL (console, First Look, or apps/marketing)."
            ),
        }

    ff = ffmpeg_available()
    if not ff.get("ok"):
        return {
            "ok": False,
            "status": "blocked",
            "error": "ffmpeg_missing",
            "block_reason": ff.get("next_step") or ffmpeg_install_message(),
            "result": ff,
        }

    work = media_store.media_dir(workspace_id, "demo")
    capture_dir = work / f"raw_{new_id('cap')}"
    use_headed = _headed({"headed": headed}) if headed is not None else _headed({})
    captured = capture_walkthrough(
        url=target,
        dest_dir=capture_dir,
        steps=steps,
        headed=use_headed,
    )
    if not captured.get("ok"):
        return {
            "ok": False,
            "status": "blocked",
            "error": captured.get("error"),
            "block_reason": captured.get("block_reason"),
            "result": {**captured, "recordly": recordly_bridge.probe()},
        }

    src = Path(str(captured["path"]))
    dest = work / f"demo_{new_id('dem')}_{max(8, min(int(seconds or 20), 90))}s.mp4"
    encoded = encode_mp4(src, dest, seconds=max(8, min(int(seconds or 20), 90)))
    if not encoded.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "error": encoded.get("error"),
            "block_reason": encoded.get("block_reason"),
            "result": encoded,
        }

    short_path = None
    if short_clip:
        short_dest = work / f"demo_{new_id('dem')}_9x16.mp4"
        short = encode_short_clip(Path(str(encoded["path"])), short_dest, seconds=min(15, int(seconds or 15)))
        if short.get("ok"):
            short_path = str(short["path"])
            media_store.insert_asset(
                workspace_id=workspace_id,
                kind="generated_video",
                path=short_path,
                source="product_demo_short",
                mime="video/mp4",
                title=f"{headline[:100]} · 9:16",
                width=1080,
                height=1920,
                duration_ms=min(15, int(seconds or 15)) * 1000,
                license_info={"engine": "playwright_ffmpeg", "generative_video": False},
                meta={"url": target, "parent": str(dest)},
                review_status="pending",
            )

    asset = media_store.insert_asset(
        workspace_id=workspace_id,
        kind="generated_video",
        path=str(dest),
        source="product_demo_playwright",
        mime="video/mp4",
        title=(headline or captured.get("title") or "Product demo")[:120],
        width=1920,
        height=1080,
        duration_ms=max(8, min(int(seconds or 20), 90)) * 1000,
        license_info={"engine": "playwright_ffmpeg", "generative_video": False},
        meta={
            "url": target,
            "final_url": captured.get("final_url"),
            "page_title": captured.get("title"),
            "headed": use_headed,
            "short_clip": short_path,
            "recordly": recordly_bridge.probe(),
            "quality": "raw_walkthrough_plus_ffmpeg_crop",
        },
        review_status="pending",
    )
    return {
        "ok": True,
        "status": "done",
        "asset": asset,
        "result": {
            "asset_id": asset["id"],
            "path": str(dest),
            "short_clip": short_path,
            "url": target,
            "engine": "playwright_ffmpeg",
            "quality": "raw_walkthrough_plus_ffmpeg_crop",
            "recordly": recordly_bridge.probe(),
            "publish_note": "Asset is pending review. CONTENT_DRY_RUN still gates public posts.",
        },
        "proof": {"mode": "local", "visual_path": str(dest), "asset_id": asset["id"]},
    }


def steps_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = payload.get("steps")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_STEPS
    if isinstance(raw, list) and raw:
        return [s for s in raw if isinstance(s, dict)]
    return None
