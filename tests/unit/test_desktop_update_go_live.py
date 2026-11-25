"""Realistic founder go-live updater checks: 0.1.0 vs 0.2.0, Later vs force.

These tests exercise updater policy using isolated fixtures. They do not
verify a public release, halt cloud jobs, or Apple-sign a DMG.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop_runtime.update_policy import (
    DEFAULT_UPDATE_ENDPOINT,
    can_defer,
    evaluate_manifest,
    generate_latest_manifest,
    parse_latest_manifest,
)
from llm_gateway.hosted import HostedUpdateRequired
from llm_gateway.update_gate import enforce_forced_update, persist_update_status

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "desktop_update"
ENDPOINT = DEFAULT_UPDATE_ENDPOINT


def _load(name: str) -> dict:
    return parse_latest_manifest((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_tauri_plugin_updater_wired_to_consistent_signed_channel():
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    updater = tauri["plugins"]["updater"]
    assert updater["endpoints"] == [ENDPOINT]
    assert DEFAULT_UPDATE_ENDPOINT == ENDPOINT
    assert updater["pubkey"].strip()
    cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    assert "tauri-plugin-updater" in cargo


def test_fixture_0_1_0_sees_optional_0_2_0_update():
    manifest = _load("latest_0.2.0_optional.json")
    result = evaluate_manifest("0.1.0", ENDPOINT, manifest)
    assert result["available"] is True
    assert result["version"] == "0.2.0"
    assert result["force"] is False
    assert result["can_defer"] is True
    assert can_defer(False) is True


def test_same_version_0_2_0_skips_even_when_manifest_is_0_2_0():
    optional = _load("latest_0.2.0_optional.json")
    forced = _load("latest_0.2.0_force.json")
    skip_opt = evaluate_manifest("0.2.0", ENDPOINT, optional)
    skip_force = evaluate_manifest("0.2.0", ENDPOINT, forced)
    assert skip_opt["available"] is False
    assert skip_force["available"] is False
    assert skip_force["force"] is False
    assert "No newer signed release" in (skip_opt["honesty"] or "")


def test_optional_policy_allows_later_button():
    manifest = _load("latest_0.2.0_optional.json")
    result = evaluate_manifest("0.1.0", ENDPOINT, manifest)
    assert result["can_defer"] is True
    assert result["force"] is False


def test_forced_policy_refuses_later_in_local_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.0")
    manifest = _load("latest_0.2.0_force.json")
    result = evaluate_manifest("0.1.0", ENDPOINT, manifest)
    assert result["available"] is True
    assert result["force"] is True
    assert result["can_defer"] is False
    persist_update_status(result)
    with pytest.raises(HostedUpdateRequired):
        enforce_forced_update(current_version="0.1.0")
    # Already on advertised version: local gate allows continuation.
    enforce_forced_update(current_version="0.2.0")


def test_optional_policy_does_not_trigger_local_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_APP_VERSION", "0.1.0")
    manifest = _load("latest_0.2.0_optional.json")
    persist_update_status(evaluate_manifest("0.1.0", ENDPOINT, manifest))
    enforce_forced_update(current_version="0.1.0")


def test_generate_latest_json_then_old_version_would_update(tmp_path):
    path = tmp_path / "latest.json"
    manifest = generate_latest_manifest(
        version="0.2.0",
        mandatory=False,
        body="Dry-run optional",
        public_base="http://127.0.0.1:8769",
    )
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    loaded = parse_latest_manifest(path.read_text(encoding="utf-8"))
    assert loaded["platforms"]["darwin-aarch64"]["url"].startswith(
        "http://127.0.0.1:8769/"
    )
    old = evaluate_manifest("0.1.0", ENDPOINT, loaded)
    same = evaluate_manifest("0.2.0", ENDPOINT, loaded)
    assert old["available"] is True
    assert old["can_defer"] is True
    assert same["available"] is False


def test_desktop_version_bump_plus_latest_json_dryrun(tmp_path):
    """Call the same dry-run the founder runs: bump in a temp tree, generate latest.json."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "desktop_update_dryrun", ROOT / "scripts" / "desktop_update_dryrun.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "generated"
    report = mod.run_dryrun(old_version="0.1.0", new_version="0.2.0", out_dir=out)
    assert report["ok"] is True
    assert report["simulated_old_version"] == "0.1.0"
    assert report["simulated_new_version"] == "0.2.0"
    assert report["old_sees_optional_update"]["available"] is True
    assert report["old_sees_optional_update"]["can_defer"] is True
    assert report["old_sees_forced_update"]["force"] is True
    assert report["same_version_skips_optional"]["available"] is False
    repo_ver = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text())["version"]
    assert repo_ver == report["repo_version"]
    assert "cannot Apple-sign" in report["apple_signing"]
