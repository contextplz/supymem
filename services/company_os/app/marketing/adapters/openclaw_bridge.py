"""OpenClaw-shaped browser operator — Playwright verify, not silent LinkedIn spam.

OpenClaw (vendor/openclaw) is the reference PC/browser runtime (Playwright +
optional computer-use). Company OS does **not** embed the OpenClaw gateway.
We reuse the same contract: isolated browser actions for verification when
Composio/API already published.

Publish still goes through Composio. This module proves the post exists.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[5]
VERIFY_DIR = REPO_ROOT / "data" / "content_verify"


def browser_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except Exception:
        return False


def _verify_sync(
    url: str,
    *,
    workspace_id: str,
    post_id: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    """Sync Playwright body — must not run on the asyncio event-loop thread."""
    out_dir = VERIFY_DIR / workspace_id
    out_dir.mkdir(parents=True, exist_ok=True)
    shot = out_dir / f"{post_id or new_id('vrf')}.png"

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": f"playwright_import:{exc}",
            "error": str(exc)[:200],
            "result": {"needs_you": True},
            "proof": {},
        }

    title = ""
    final_url = url
    body_snip = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            title = page.title() or ""
            final_url = page.url
            body_snip = (page.inner_text("body") or "")[:500].lower()
            page.screenshot(path=str(shot), full_page=False)
            browser.close()
    except Exception as exc:
        log.exception("openclaw verify failed")
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": f"browser_error:{exc}"[:400],
            "error": str(exc)[:200],
            "result": {"needs_you": True},
            "proof": {},
        }

    challenge = any(
        t in body_snip or t in title.lower()
        for t in ("captcha", "checkpoint", "sign in", "log in", "challenge", "verify you")
    )
    if challenge:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "human_verification_required",
            "error": "human_verification_required",
            "result": {
                "needs_you": True,
                "title": title,
                "final_url": final_url,
                "screenshot": str(shot) if shot.exists() else None,
            },
            "proof": {
                "mode": "live",
                "timestamp": utcnow().isoformat(),
                "screenshot_path": str(shot) if shot.exists() else None,
                "final_url": final_url,
            },
        }

    return {
        "ok": True,
        "status": "done",
        "result": {
            "title": title,
            "final_url": final_url,
            "screenshot": str(shot) if shot.exists() else None,
            "operator": "openclaw_playwright",
        },
        "proof": {
            "mode": "live",
            "timestamp": utcnow().isoformat(),
            "external_url": final_url,
            "screenshot_path": str(shot) if shot.exists() else None,
            "content_post_id": post_id,
            "executor": "openclaw_playwright",
        },
    }


def verify_public_url(
    url: str,
    *,
    workspace_id: str,
    post_id: str | None = None,
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    """Open ``url`` in Chromium, capture a screenshot, return proof.

    Safe to call from sync code. From async code prefer
    :func:`verify_public_url_async` (or this will still work if no loop is
    running on the calling thread).
    """
    if not url or url.startswith("dryrun://"):
        return {
            "ok": False,
            "status": "failed",
            "error": "no_live_url",
            "result": {},
            "proof": {},
        }
    if not browser_available():
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "playwright_not_installed",
            "error": "playwright_not_installed",
            "result": {
                "needs_you": True,
                "install": "pip install playwright && playwright install chromium",
            },
            "proof": {},
        }
    return _verify_sync(
        url, workspace_id=workspace_id, post_id=post_id, timeout_ms=timeout_ms
    )


async def verify_public_url_async(
    url: str,
    *,
    workspace_id: str,
    post_id: str | None = None,
    timeout_ms: int = 45000,
) -> dict[str, Any]:
    """Async wrapper — runs sync Playwright off the event loop."""
    if not url or url.startswith("dryrun://"):
        return {
            "ok": False,
            "status": "failed",
            "error": "no_live_url",
            "result": {},
            "proof": {},
        }
    if not browser_available():
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "playwright_not_installed",
            "error": "playwright_not_installed",
            "result": {
                "needs_you": True,
                "install": "pip install playwright && playwright install chromium",
            },
            "proof": {},
        }
    return await asyncio.to_thread(
        _verify_sync,
        url,
        workspace_id=workspace_id,
        post_id=post_id,
        timeout_ms=timeout_ms,
    )
