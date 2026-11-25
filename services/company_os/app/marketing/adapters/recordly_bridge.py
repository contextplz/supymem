"""Recordly sidecar — AGPL-3 desktop recorder, never imported in-process.

Upstream: https://github.com/webadderallorg/Recordly (AGPL-3.0).
Company OS does **not** copy or import Recordly source (same rule as Postiz).
We clone it under ``vendor/recordly`` for study, detect an installed GUI, and
ingest MP4/GIF the founder exports. Scripted walkthroughs use our Playwright +
ffmpeg path in ``app.marketing.media.demo_recorder``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
VENDOR_DIR = REPO_ROOT / "vendor" / "recordly"
UPSTREAM = "https://github.com/webadderallorg/Recordly"
LICENSE = "AGPL-3.0"


def vendor_root() -> Path:
    override = (os.environ.get("RECORDLY_VENDOR") or "").strip()
    return Path(override) if override else VENDOR_DIR


def _macos_app() -> Path | None:
    candidates = [
        Path("/Applications/Recordly.app"),
        Path.home() / "Applications" / "Recordly.app",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return None


def probe() -> dict[str, Any]:
    root = vendor_root()
    license_file = root / "LICENSE.md"
    pkg = root / "package.json"
    app = _macos_app()
    return {
        "ok": bool(pkg.is_file() or app),
        "license": LICENSE,
        "in_process": False,
        "upstream": UPSTREAM,
        "vendor_cloned": pkg.is_file(),
        "vendor_path": str(root) if pkg.is_file() else None,
        "license_file": str(license_file) if license_file.is_file() else None,
        "app_installed": str(app) if app else None,
        "cli": False,
        "note": (
            "Recordly is an Electron GUI (Screen Studio-style). There is no headless "
            "CLI. Launch the app, record a window, export MP4, then ingest. Do not "
            "import vendor/recordly into Company OS — AGPL network clause."
        ),
    }


def launch_gui() -> dict[str, Any]:
    """Open the Recordly desktop app. Human records; we do not drive the UI."""
    info = probe()
    app = _macos_app()
    if app is not None:
        try:
            proc = subprocess.run(
                ["open", "-a", str(app)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            return {
                "ok": False,
                "status": "blocked",
                "error": f"recordly_launch:{exc}"[:200],
                "block_reason": "Could not open Recordly.app. Open it from Applications yourself.",
                "result": {**info, "needs_you": True},
            }
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "open_failed")[:240]
            return {
                "ok": False,
                "status": "blocked",
                "error": err,
                "block_reason": "macOS refused to open Recordly. Grant Screen Recording, then retry.",
                "result": {**info, "needs_you": True},
            }
        return {
            "ok": True,
            "status": "done",
            "result": {
                **info,
                "launched": True,
                "needs_you": True,
                "next": (
                    "Record the product window, polish in Recordly (zooms, frame, "
                    "cursor), Export MP4, then ingest via Media Studio or "
                    "`make record-demo INGEST=/path/to/demo.mp4`."
                ),
            },
            "proof": {"mode": "recordly_gui", "app": str(app)},
        }

    which = shutil.which("recordly")
    if which:
        try:
            subprocess.Popen([which], start_new_session=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "status": "blocked",
                "error": str(exc)[:200],
                "block_reason": "recordly binary found but failed to start.",
                "result": {**info, "needs_you": True},
            }
        return {
            "ok": True,
            "status": "done",
            "result": {**info, "launched": True, "binary": which, "needs_you": True},
            "proof": {"mode": "recordly_gui", "binary": which},
        }

    if info.get("vendor_cloned"):
        block = (
            "Recordly source is cloned at vendor/recordly (AGPL-3) but the desktop "
            "app is not installed. Download a release from "
            "https://github.com/webadderallorg/Recordly/releases or, on this Mac: "
            "cd vendor/recordly && npm install && npm run dev. Then record and export."
        )
    else:
        block = (
            "Install Recordly (https://github.com/webadderallorg/Recordly/releases) "
            "or clone with `make sync-vendors`. Scripted demos still work without it: "
            "`make record-demo URL=http://127.0.0.1:3100/content`."
        )
    return {
        "ok": False,
        "status": "blocked",
        "error": "recordly_gui_missing",
        "block_reason": block,
        "result": {**info, "needs_you": True},
    }
