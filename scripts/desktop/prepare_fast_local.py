"""Prepare an incremental local app build without release or updater work."""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("Fast local desktop builds currently require macOS.")
    identity = os.environ.get("APPLE_SIGNING_IDENTITY", "").strip()
    macos = {"signingIdentity": "-", "hardenedRuntime": False}
    if identity:
        identities = subprocess.check_output(
            ["security", "find-identity", "-v", "-p", "codesigning"], text=True
        )
        if identity not in identities:
            raise SystemExit("APPLE_SIGNING_IDENTITY is not installed in this Mac's Keychain.")
        if not identity.startswith("Developer ID Application:"):
            raise SystemExit("Fast production dogfood builds require a Developer ID Application identity.")
        macos = {"signingIdentity": identity, "hardenedRuntime": True,
                 "entitlements": "entitlements.plist"}
    config = {"bundle": {"createUpdaterArtifacts": False, "macOS": macos}}
    (ROOT / "src-tauri/tauri.release.json").write_text(json.dumps(config))
    print("Prepared fast local app-only build; DMG, updater artifacts and notarization are disabled.")


if __name__ == "__main__":
    main()
