"""Prepare a packaged, local-only Mac UI review app with its own app identity.

Tauri dev uses the Next dev URL for every relative window, which makes bundled
setup/coach documents resolve to 404s. A packaged app serves those documents
from frontendDist and gives macOS/CUA a real .app bundle to select. The marker
in Info.local-review.plist is rejected by release collection and publication.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("Local desktop UI review builds require macOS.")
    if os.environ.get("CI"):
        raise SystemExit("Local desktop UI review builds cannot run in CI.")
    base = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
    if base.get("identifier") != "com.companyos.app" or base.get("productName") != "Supymem":
        raise SystemExit("Local UI review requires the existing Supymem app identity.")
    for name in ("index.html", "setup.html", "coach.html", "update.html", "offline.html"):
        if not (ROOT / "src-tauri/splashscreen" / name).is_file():
            raise SystemExit(f"Bundled UI document missing: {name}")
    identity = os.environ.get("APPLE_SIGNING_IDENTITY", "").strip()
    macos: dict[str, object] = {
        "signingIdentity": "-", "hardenedRuntime": False,
        "infoPlist": "Info.local-review.plist",
    }
    if identity:
        identities = subprocess.check_output(
            ["security", "find-identity", "-v", "-p", "codesigning"], text=True
        )
        if not identity.startswith("Developer ID Application:") or identity not in identities:
            raise SystemExit("APPLE_SIGNING_IDENTITY must name an installed Developer ID Application identity.")
        macos.update(signingIdentity=identity, hardenedRuntime=True,
                     entitlements="entitlements.plist")
    override = {
        "productName": "Supymem Local Review",
        "identifier": "com.companyos.localreview",
        "app": {"windows": [{**window, **({"title": "Supymem Local Review"} if window["label"] == "main" else {})}
                            for window in base["app"]["windows"]]},
        "bundle": {"createUpdaterArtifacts": False, "macOS": macos},
        "plugins": {"updater": {"endpoints": []}},
    }
    path = ROOT / "src-tauri/tauri.local-review.json"
    path.write_text(json.dumps(override))
    print("Prepared Supymem Local Review.app; distinct local identity, bundled setup/coach, no DMG or updater.")


if __name__ == "__main__":
    main()
