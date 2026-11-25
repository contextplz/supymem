#!/usr/bin/env python3
"""Simulate a desktop version bump + latest.json so an older build would update.

This is the founder/CI check for the updater **JSON contract** and policy
(optional Later vs forced). It does **not** Apple-sign or notarize a DMG.
Linux CI cannot produce a notarized Mac installer; unsigned local fixtures
are enough to prove 0.1.0 sees 0.2.0 and the same version skips.

Usage (from repo root):

    python scripts/desktop_update_dryrun.py
    make desktop-update-test
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "packages" / "desktop_runtime") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "desktop_runtime"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from update_policy import (  # noqa: E402
    DEFAULT_UPDATE_ENDPOINT,
    apply_policy_to_manifest,
    evaluate_manifest,
    generate_latest_manifest,
)

import desktop_version as dv  # noqa: E402

LIVE_ENDPOINT = DEFAULT_UPDATE_ENDPOINT


def assert_tauri_updater_endpoint() -> str:
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    endpoints = tauri.get("plugins", {}).get("updater", {}).get("endpoints") or []
    if LIVE_ENDPOINT not in endpoints:
        raise SystemExit(
            f"tauri-plugin-updater must point at {LIVE_ENDPOINT}, got {endpoints!r}"
        )
    pubkey = str(tauri.get("plugins", {}).get("updater", {}).get("pubkey") or "").strip()
    if not pubkey:
        raise SystemExit("plugins.updater.pubkey is empty in src-tauri/tauri.conf.json")
    if "PRIVATE" in pubkey.upper():
        raise SystemExit("private updater key must not live in tauri.conf.json")
    return LIVE_ENDPOINT


def bump_in_temp(new_version: str) -> str:
    """Copy desktop version files, run desktop_version.py bump, return version."""
    with tempfile.TemporaryDirectory(prefix="cos-desktop-bump-") as directory:
        root = Path(directory)
        for name in dv.VERSION_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        bumped = dv.set_repo_version(new_version, root)
        matched = dv.assert_versions_match(root)
        if bumped != matched:
            raise SystemExit(f"bump wrote {bumped} but check saw {matched}")
        return bumped


def run_dryrun(*, old_version: str, new_version: str, out_dir: Path | None = None) -> dict:
    endpoint = assert_tauri_updater_endpoint()
    if endpoint != DEFAULT_UPDATE_ENDPOINT:
        raise SystemExit(
            f"Python DEFAULT_UPDATE_ENDPOINT {DEFAULT_UPDATE_ENDPOINT} "
            f"must match tauri.conf {endpoint}"
        )
    repo_version = dv.read_repo_version()
    bumped = bump_in_temp(new_version)
    dest = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="cos-latest-"))
    dest.mkdir(parents=True, exist_ok=True)
    optional_path = dest / "latest_optional.json"
    force_path = dest / "latest_force.json"
    optional = generate_latest_manifest(
        version=bumped,
        mandatory=False,
        body=f"Optional dry-run {bumped}",
        public_base="http://127.0.0.1:8769",
    )
    optional_path.write_text(json.dumps(optional, indent=2) + "\n", encoding="utf-8")
    forced = apply_policy_to_manifest(optional, mandatory=True)
    force_path.write_text(json.dumps(forced, indent=2) + "\n", encoding="utf-8")

    old_optional = evaluate_manifest(old_version, endpoint, optional)
    old_forced = evaluate_manifest(old_version, endpoint, forced)
    same_optional = evaluate_manifest(bumped, endpoint, optional)
    same_forced = evaluate_manifest(bumped, endpoint, forced)

    report = {
        "ok": True,
        "apple_signing": (
            "This dry-run cannot Apple-sign or notarize a DMG. "
            "The updater JSON contract and policy path were verified instead. "
            "Fixture output stays local and cannot be published."
        ),
        "live_endpoint": endpoint,
        "repo_version": repo_version,
        "simulated_old_version": old_version,
        "simulated_new_version": bumped,
        "optional_manifest": str(optional_path),
        "forced_manifest": str(force_path),
        "old_sees_optional_update": old_optional,
        "old_sees_forced_update": old_forced,
        "same_version_skips_optional": same_optional,
        "same_version_skips_forced": same_forced,
        "later_button": {
            "optional_can_defer": old_optional.get("can_defer"),
            "forced_can_defer": old_forced.get("can_defer"),
            "ui": "optional → Later; forced → Update now only in the desktop UI",
        },
    }

    failures: list[str] = []
    if not old_optional.get("available") or old_optional.get("force") or not old_optional.get(
        "can_defer"
    ):
        failures.append("0.1.0 vs optional 0.2.0 must be available, not forced, Later allowed")
    if not old_forced.get("available") or not old_forced.get("force") or old_forced.get("can_defer"):
        failures.append("0.1.0 vs forced 0.2.0 must be available, forced, Later refused")
    if same_optional.get("available") or same_forced.get("available"):
        failures.append("current version equal to latest.json must skip even when mandatory")
    if old_optional.get("version") != bumped:
        failures.append(f"optional advertised version should be {bumped}")
    if failures:
        report["ok"] = False
        report["failures"] = failures
        raise SystemExit(json.dumps(report, indent=2))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulate desktop bump + latest.json update")
    parser.add_argument("--old-version", default="0.1.0")
    parser.add_argument("--new-version", default="0.2.0")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args(argv)
    report = run_dryrun(
        old_version=args.old_version,
        new_version=args.new_version,
        out_dir=Path(args.out_dir) if args.out_dir else None,
    )
    print(json.dumps(report, indent=2))
    print("desktop_update_dryrun: PASS")
    print(
        "Apple notarization: not performed by this dry-run. "
        "JSON contract verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
