#!/usr/bin/env python3
"""Keep app version metadata in sync with the Tauri package version."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAURI_CONF = "src-tauri/tauri.conf.json"
JSON_VERSION = r'"version"\s*:\s*"([^"]+)"'
TOML_VERSION = r'^version\s*=\s*"([^"]+)"'
VERSION_FILES = {
    TAURI_CONF: JSON_VERSION,
    "src-tauri/tauri.local.json": JSON_VERSION,
    "src-tauri/Cargo.toml": TOML_VERSION,
    "src-tauri/Cargo.lock": r'\[\[package\]\]\nname = "company-os-desktop"\nversion = "([^"]+)"',
    "src-tauri/Info.local.plist": ("CFBundleShortVersionString", "CFBundleVersion", "SupymemReleaseVersion"),
    "apps/desktop/package.json": JSON_VERSION,
    "apps/console/package.json": JSON_VERSION,
    "services/company_os/pyproject.toml": TOML_VERSION,
    "packages/desktop_runtime/pyproject.toml": TOML_VERSION,
    "packages/desktop_runtime/__init__.py": r'^__version__\s*=\s*"([^"]+)"',
}


def _version_match(path: Path, pattern: str, text: str) -> re.Match[str]:
    match = re.search(pattern, text, flags=re.M)
    if not match:
        raise SystemExit(f"{path} is missing version")
    return match


def _version_fields(patterns: str | tuple[str, ...], version: str):
    if isinstance(patterns, str):
        yield patterns, version
    else:
        for key in patterns:
            value = version.partition("-")[0] if key.startswith("CFBundle") else version
            yield rf'<key>{key}</key>\s*<string>([^<]+)</string>', value


def read_repo_version(root: Path = ROOT) -> str:
    path = root / TAURI_CONF
    return _version_match(path, JSON_VERSION, path.read_text(encoding="utf-8"))[1]


def _validate_version(version: str) -> None:
    number = r"(?:0|[1-9][0-9]*)"
    if not re.fullmatch(rf"{number}\.{number}\.{number}(?:-rc\.{number})?", version):
        raise SystemExit(f"Use a version like 1.0.0 or 1.0.0-rc.2, not {version!r}.")


def set_repo_version(version: str, root: Path = ROOT) -> str:
    ver = version.strip().lstrip("vV")
    _validate_version(ver)
    updates = []
    for name, patterns in VERSION_FILES.items():
        path = root / name
        text = path.read_text(encoding="utf-8")
        for pattern, expected in _version_fields(patterns, ver):
            match = _version_match(path, pattern, text)
            text = text[:match.start(1)] + expected + text[match.end(1):]
        updates.append((path, text))
    for path, text in updates:
        path.write_text(text, encoding="utf-8")
    return ver


def assert_versions_match(root: Path = ROOT) -> str:
    version = read_repo_version(root)
    _validate_version(version)
    mismatches = []
    for name, patterns in VERSION_FILES.items():
        path = root / name
        text = path.read_text(encoding="utf-8")
        for pattern, expected in _version_fields(patterns, version):
            found = _version_match(path, pattern, text)[1]
            if found != expected:
                mismatches.append(f"{name}={found} (expected {expected})")
    if mismatches:
        raise SystemExit(f"Versions differ from {TAURI_CONF}={version}: {', '.join(mismatches)}")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supymem app version")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("print", help="Print src-tauri/tauri.conf.json version")
    sub.add_parser("check", help="Fail if the app version metadata diverges")
    bump = sub.add_parser("bump", help="Set the in-repo version (commit this)")
    bump.add_argument("version")
    args = parser.parse_args(argv)
    if args.cmd == "print":
        print(read_repo_version())
        return 0
    if args.cmd == "check":
        print(assert_versions_match())
        return 0
    if args.cmd == "bump":
        print(set_repo_version(args.version))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
