"""Bake and evaluate desktop update policy.

Tauri's ``latest.json`` schema is fixed. Company OS policy lives in ``notes``:

    <!--cos-update {"mandatory":true,"min_version":"0.1.0"}-->

- ``mandatory: true`` → force (Update now only).
- current < ``min_version`` → also force.
- otherwise optional (Install now / Later; remind next launch).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

POLICY_MARKER_START = "<!--cos-update"
POLICY_MARKER_END = "-->"
DEFAULT_UPDATE_ENDPOINT = "https://supymem.com/downloads/latest.json"
UPDATE_ENDPOINT_ENV = "COMPANY_OS_UPDATE_ENDPOINT"

_MARKER_RE = re.compile(
    re.escape(POLICY_MARKER_START) + r"\s*(\{.*?\})\s*" + re.escape(POLICY_MARKER_END),
    re.DOTALL,
)


def parse_update_policy(notes: str) -> dict[str, Any]:
    match = _MARKER_RE.search(notes or "")
    if not match:
        return {"mandatory": False, "min_version": None}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"mandatory": False, "min_version": None}
    if not isinstance(data, dict):
        return {"mandatory": False, "min_version": None}
    min_version = data.get("min_version")
    if min_version is not None:
        min_version = str(min_version).strip() or None
    return {
        "mandatory": bool(data.get("mandatory", False)),
        "min_version": min_version,
    }


def parse_dotted_version(raw: str) -> tuple[int, int, int]:
    s = (raw or "").strip().lstrip("vV")
    parts = s.split(".")

    def _num(idx: int) -> int:
        if idx >= len(parts):
            return 0
        digits = "".join(ch for ch in parts[idx] if ch.isdigit())
        return int(digits) if digits else 0

    return _num(0), _num(1), _num(2)


def version_lt(current: str, other: str) -> bool:
    return parse_dotted_version(current) < parse_dotted_version(other)


def version_gt(left: str, right: str) -> bool:
    return parse_dotted_version(left) > parse_dotted_version(right)


def should_force(current: str, policy: dict[str, Any]) -> bool:
    if policy.get("mandatory"):
        return True
    min_version = policy.get("min_version")
    if min_version:
        return version_lt(current, str(min_version))
    return False


def can_defer(force: bool) -> bool:
    return not force


def endpoint_is_local(endpoint: str) -> bool:
    return urlparse(endpoint or "").hostname in {"127.0.0.1", "localhost", "::1"}


def honesty_for_http(status: int | None, endpoint: str) -> str:
    local = endpoint_is_local(endpoint)
    if status in (403, 404):
        if local:
            return (
                f"No signed update on this channel (HTTP {status} at {endpoint}). "
                "This is a local test endpoint — Nothing was installed."
            )
        return (
            f"No signed update on this channel (HTTP {status} at {endpoint}). "
            "Common for a local or unsigned build, or a draft GitHub release. "
            "Nothing was installed."
        )
    if status is not None:
        return f"Update check failed (HTTP {status} at {endpoint}). Nothing was installed."
    return f"Could not reach the update channel ({endpoint}). Nothing was installed."


def honesty_up_to_date(current: str, endpoint: str) -> str:
    if endpoint_is_local(endpoint):
        return f"You're on {current}. The local fixture has no newer signed release."
    return f"You're on {current}. No newer signed release."


def parse_latest_manifest(body: str) -> dict[str, Any]:
    data = json.loads(body)
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError("latest.json is not valid updater JSON")
    return data


def strip_policy_prefix(notes: str) -> str:
    idx = notes.find(POLICY_MARKER_END)
    if idx == -1:
        return notes
    return notes[idx + len(POLICY_MARKER_END) :].strip()


def evaluate_manifest(current_version: str, endpoint: str, manifest: dict[str, Any]) -> dict[str, Any]:
    notes = str(manifest.get("notes") or "")
    policy = parse_update_policy(notes)
    remote = str(manifest.get("version") or "").strip()
    if not version_gt(remote, current_version):
        return {
            "available": False,
            "current_version": current_version,
            "version": None,
            "notes": None,
            "mandatory": False,
            "min_version": None,
            "force": False,
            "can_defer": True,
            "honesty": honesty_up_to_date(current_version, endpoint),
            "endpoint": endpoint,
        }
    force = should_force(current_version, policy)
    return {
        "available": True,
        "current_version": current_version,
        "version": remote,
        "notes": strip_policy_prefix(notes) or None,
        "mandatory": bool(policy.get("mandatory")),
        "min_version": policy.get("min_version"),
        "force": force,
        "can_defer": can_defer(force),
        "honesty": None,
        "endpoint": endpoint,
    }


def apply_policy_to_manifest(
    manifest: dict[str, Any], *, mandatory: bool, min_version: str | None = None
) -> dict[str, Any]:
    """Flip forced vs optional on an existing latest.json without changing artifacts."""
    notes = str(manifest.get("notes") or "")
    body = strip_policy_prefix(notes) if POLICY_MARKER_START in notes else notes
    out = dict(manifest)
    out["notes"] = bake_notes(mandatory, min_version, body)
    return out


def rewrite_platform_urls(manifest: dict[str, Any], *, public_base: str) -> dict[str, Any]:
    """Point platform artifact URLs at CloudFront downloads/ (same filenames)."""
    base = public_base.rstrip("/")
    out = dict(manifest)
    platforms = out.get("platforms")
    if not isinstance(platforms, dict):
        return out
    rewritten: dict[str, Any] = {}
    for name, spec in platforms.items():
        if not isinstance(spec, dict):
            rewritten[name] = spec
            continue
        item = dict(spec)
        url = str(item.get("url") or "")
        filename = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        if filename:
            item["url"] = f"{base}/{filename}"
        rewritten[name] = item
    out["platforms"] = rewritten
    return out


def bake_notes(mandatory: bool, min_version: str | None = None, body: str = "") -> str:
    policy: dict[str, Any] = {"mandatory": bool(mandatory)}
    if min_version and str(min_version).strip():
        policy["min_version"] = str(min_version).strip()
    prefix = f"{POLICY_MARKER_START} {json.dumps(policy, separators=(',', ':'))}{POLICY_MARKER_END}"
    rest = (body or "").strip()
    return f"{prefix}\n{rest}" if rest else prefix


def generate_latest_manifest(
    *,
    version: str,
    mandatory: bool = False,
    min_version: str | None = None,
    body: str = "",
    public_base: str = "http://127.0.0.1:8769",
    signature: str = "unsigned-local-fixture",
    artifact_name: str = "CompanyOS.app.tar.gz",
    extra_platforms: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a Tauri updater ``latest.json`` (JSON contract live clients fetch).

    This does **not** Apple-sign a DMG. ``signature`` is a fixture string unless
    the Tauri signer filled it in during ``desktop-release``.
    """
    ver = (version or "").strip().lstrip("vV")
    fixture = signature == "unsigned-local-fixture"
    if fixture and not endpoint_is_local(public_base):
        raise ValueError("Unsigned updater fixtures require a loopback artifact URL")
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        raise ValueError(f"version must look like 0.2.0, not {version!r}")
    notes = bake_notes(mandatory, min_version, body or f"Company OS v{ver}")
    url_base = public_base.rstrip("/")
    platforms: dict[str, Any] = {
        "darwin-aarch64": {"signature": signature, "url": f"{url_base}/{artifact_name}"},
        "darwin-x86_64": {"signature": signature, "url": f"{url_base}/{artifact_name}"},
        "linux-x86_64": {"signature": signature, "url": f"{url_base}/{artifact_name}"},
        "windows-x86_64": {"signature": signature, "url": f"{url_base}/{artifact_name}"},
    }
    if extra_platforms:
        platforms.update(extra_platforms)
    return {
        "version": ver,
        "test_fixture": fixture,
        "notes": notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": platforms,
    }


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Company OS desktop update policy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bake = sub.add_parser("bake", help="Wrap release notes with the policy marker")
    bake.add_argument("--mandatory", default="false")
    bake.add_argument("--min-version", default="")
    bake.add_argument("--version", default="")
    bake.add_argument("--body", default="")
    gen = sub.add_parser(
        "generate-latest",
        help="Write a Tauri latest.json (unsigned fixture OK; not a notarized DMG)",
    )
    gen.add_argument("--version", required=True)
    gen.add_argument("--out", required=True)
    gen.add_argument("--mandatory", default="false")
    gen.add_argument("--min-version", default="")
    gen.add_argument("--body", default="")
    gen.add_argument("--public-base", default="http://127.0.0.1:8769")
    gen.add_argument("--signature", default="unsigned-local-fixture")
    gen.add_argument("--artifact-name", default="CompanyOS.app.tar.gz")
    ev = sub.add_parser(
        "evaluate",
        help="Compare a current app version against a latest.json (update vs skip)",
    )
    ev.add_argument("--current", required=True, help="Installed app version, e.g. 0.1.0")
    ev.add_argument("--latest-json", required=True)
    ev.add_argument("--endpoint", default=DEFAULT_UPDATE_ENDPOINT)
    mode = sub.add_parser("set-mode", help="Flip forced vs optional on an existing latest.json")
    mode.add_argument("--latest-json", required=True)
    mode.add_argument("--mandatory", default="false")
    mode.add_argument("--min-version", default="")
    args = parser.parse_args(argv)
    if args.cmd == "bake":
        heading = f"Supymem v{args.version}" if args.version else "Supymem"
        body = args.body or heading
        notes = bake_notes(_parse_bool(args.mandatory), args.min_version, body)
        print(notes)
        github_out = os.environ.get("GITHUB_OUTPUT")
        if github_out:
            with open(github_out, "a", encoding="utf-8") as fh:
                fh.write("notes<<COS_NOTES\n")
                fh.write(notes)
                fh.write("\nCOS_NOTES\n")
        return 0
    if args.cmd == "set-mode":
        path = Path(args.latest_json)
        manifest = parse_latest_manifest(path.read_text(encoding="utf-8"))
        updated = apply_policy_to_manifest(
            manifest,
            mandatory=_parse_bool(args.mandatory),
            min_version=args.min_version or None,
        )
        path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        print(updated["notes"])
        return 0
    if args.cmd == "generate-latest":
        manifest = generate_latest_manifest(
            version=args.version,
            mandatory=_parse_bool(args.mandatory),
            min_version=args.min_version or None,
            body=args.body,
            public_base=args.public_base,
            signature=args.signature,
            artifact_name=args.artifact_name,
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(str(out.resolve()))
        print(json.dumps({"version": manifest["version"], "notes": manifest["notes"]}, indent=2))
        return 0
    if args.cmd == "evaluate":
        path = Path(args.latest_json)
        manifest = parse_latest_manifest(path.read_text(encoding="utf-8"))
        result = evaluate_manifest(args.current, args.endpoint, manifest)
        print(json.dumps(result, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
