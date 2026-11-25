"""Stock, Resolve, desktop, browser, and ads probes — fail closed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from app.marketing.adapters import openclaw_bridge
from app.core.local_mode import is_local


def _stock_key(provider: str) -> str:
    if is_local():
        from app.core.local_secrets import get_secret

        return (get_secret("images", provider + "_api_key") or "").strip()
    return (os.environ.get(provider.upper() + "_API_KEY") or "").strip()


def pexels_available() -> dict[str, Any]:
    key = _stock_key("pexels")
    return {"ok": bool(key), "configured": bool(key)}


def pixabay_available() -> dict[str, Any]:
    key = _stock_key("pixabay")
    return {"ok": bool(key), "configured": bool(key)}


def fetch_stock_still(query: str, dest: Path) -> dict[str, Any]:
    key = _stock_key("pexels")
    if not key:
        return {
            "ok": False,
            "status": "blocked",
            "error": "pexels_key_missing",
            "block_reason": "Add your Pexels key in Keys and connections, then request your still again." if is_local() else "Set PEXELS_API_KEY to pull licensed B-roll stills.",
        }
    url = f"https://api.pexels.com/v1/search?query={query}&per_page=1&orientation=portrait"
    req = Request(url, headers={"Authorization": key})
    try:
        with urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        photos = body.get("photos") or []
        if not photos:
            return {"ok": False, "error": "pexels_empty"}
        src = (photos[0].get("src") or {}).get("large2x") or (
            photos[0].get("src") or {}
        ).get("large")
        if not src:
            return {"ok": False, "error": "pexels_no_src"}
        with urlopen(src, timeout=30) as img:
            dest.write_bytes(img.read())
        return {
            "ok": True,
            "path": str(dest),
            "photographer": photos[0].get("photographer"),
            "license_url": photos[0].get("url"),
            "source": "pexels",
        }
    except Exception as exc:
        return {"ok": False, "error": f"pexels:{exc}"[:200]}


def resolve_available() -> dict[str, Any]:
    app = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
    api = Path(
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    )
    studio_hint = app.exists()
    return {
        "ok": False,
        "installed": studio_hint,
        "scripting_api": api.exists(),
        "external_scripting": False,
        "note": (
            "Resolve Free cannot be driven from an outside process. "
            "Studio + Preferences ▸ General ▸ External scripting = Local is required. "
            "ffmpeg covers 15-30s clips without Resolve."
        ),
    }


def peekaboo_bin() -> str | None:
    which = shutil.which("peekaboo")
    if which:
        return which
    vendor = (
        Path(__file__).resolve().parents[5]
        / "vendor"
        / "openclaw"
        / "skills"
        / "peekaboo"
    )
    # Binary is not vendored; skill docs only.
    return None if not vendor.exists() else None


def cua_available() -> dict[str, Any]:
    path = shutil.which("cua-driver")
    return {"ok": bool(path), "path": path}


def peekaboo_available() -> dict[str, Any]:
    path = shutil.which("peekaboo")
    return {"ok": bool(path), "path": path}


def desktop_probe() -> dict[str, Any]:
    cua = cua_available()
    peek = peekaboo_available()
    if cua.get("ok"):
        try:
            proc = subprocess.run(
                [cua["path"], "doctor"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            return {
                "ok": proc.returncode == 0,
                "engine": "cua-driver",
                "output": (proc.stdout or proc.stderr or "")[:400],
            }
        except Exception as exc:
            return {"ok": False, "engine": "cua-driver", "error": str(exc)[:160]}
    if peek.get("ok"):
        try:
            proc = subprocess.run(
                [peek["path"], "--help"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            return {
                "ok": proc.returncode == 0,
                "engine": "peekaboo",
                "output": (proc.stdout or proc.stderr or "")[:400],
            }
        except Exception as exc:
            return {"ok": False, "engine": "peekaboo", "error": str(exc)[:160]}
    return {
        "ok": False,
        "needs_you": True,
        "block_reason": (
            "No desktop driver. Install CUA Driver (https://github.com/trycua/cua) "
            "or Peekaboo, grant Accessibility + Screen Recording, then resume. "
            "DaVinci/Photoshop stay on their scripting APIs, we will not click them."
        ),
    }


def browser_probe(*, attach: bool = False) -> dict[str, Any]:
    if not openclaw_bridge.browser_available():
        return {
            "ok": False,
            "needs_you": True,
            "block_reason": "Playwright is not installed in this environment.",
        }
    cdp = os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222").rstrip("/")
    attached = False
    if attach:
        try:
            with urlopen(f"{cdp}/json/version", timeout=1.2) as resp:
                attached = resp.status == 200
        except Exception:
            attached = False
        if not attached:
            return {
                "ok": False,
                "needs_you": True,
                "block_reason": (
                    "Chrome remote debugging is off. In Chrome open chrome://inspect "
                    "#remote-debugging, then resume. We never point automation at "
                    "the Default profile directory."
                ),
                "cdp": cdp,
            }
    return {
        "ok": True,
        "playwright": True,
        "cdp_attached": attached,
        "cdp": cdp if attach else None,
    }


def google_ads_available() -> dict[str, Any]:
    from app.marketing.adapters import ads_google

    return ads_google.available()


def meta_ads_available() -> dict[str, Any]:
    from app.marketing.adapters import ads_meta

    return ads_meta.available()


def create_paused_ad(
    *,
    platform: str,
    name: str,
    daily_budget: str | None = None,
    campaign_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a PAUSED provider campaign and, when specified, its ad hierarchy."""
    platform = (platform or "").lower()
    if platform in {"google", "google_ads"}:
        from app.marketing.adapters import ads_google

        return ads_google.create_paused_campaign(
            name=name, daily_budget=daily_budget, campaign_spec=campaign_spec
        )
    if platform in {"meta", "facebook", "facebook_ads", "instagram"}:
        from app.marketing.adapters import ads_meta

        return ads_meta.create_paused_campaign(
            name=name, daily_budget=daily_budget, campaign_spec=campaign_spec
        )
    return {
        "ok": False,
        "status": "failed",
        "error": f"unsupported_ads_platform:{platform}",
    }


def enable_ad(
    *,
    platform: str,
    resource_id: str,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Enable always hits spend_money floor unless approval_id is present."""
    from app.approvals.floor import requires_human

    floor = requires_human("spend_money")
    platform = (platform or "").lower()
    if floor is not None and not approval_id:
        return {
            "ok": False,
            "status": "blocked",
            "error": "spend_money_required",
            "block_reason": (
                "NeedsYou: enabling ads spends money (floor: spend_money). "
                "Approve in the console, then resume with approval_id set."
            ),
            "result": {
                "floor": floor.key,
                "platform": platform,
                "resource_id": resource_id,
            },
        }
    # Even with approval_id we still refuse silent browser enable — mutate via API only.
    # Real ENABLE mutate is intentionally not auto-wired until founder confirms spend;
    # returning blocked with clear next step avoids fake "enabled".
    if platform in {"google", "google_ads"}:
        from app.marketing.adapters import ads_google

        out = ads_google.enable_campaign(resource_name=resource_id)
        if approval_id:
            out.setdefault("result", {})["approval_id"] = approval_id
            out["block_reason"] = (
                (out.get("block_reason") or "")
                + " Approval recorded; wire live ENABLE mutate after MCC test."
            )[:400]
        return out
    if platform in {"meta", "facebook", "facebook_ads", "instagram"}:
        from app.marketing.adapters import ads_meta

        out = ads_meta.enable_campaign(campaign_id=resource_id)
        if approval_id:
            out.setdefault("result", {})["approval_id"] = approval_id
            out["block_reason"] = (
                (out.get("block_reason") or "")
                + " Approval recorded; wire live ENABLE mutate after BM test."
            )[:400]
        return out
    return {
        "ok": False,
        "status": "failed",
        "error": f"unsupported_ads_platform:{platform}",
    }
