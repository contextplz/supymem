"""Release secrets appear only after Cargo finishes; synthetic metadata only."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/desktop-release.yml"
SIGNING = {"TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
           "APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_SIGNING_IDENTITY",
           "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID", "APPLE_API_KEY", "APPLE_API_ISSUER", "APPLE_API_KEY_PATH"}


def test_cargo_cannot_inherit_release_signing_secrets_or_imported_identity():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["build"]
    assert not SIGNING.intersection(job.get("env", {}))
    steps = job["steps"]
    compile_index = next(i for i, step in enumerate(steps)
                         if step.get("name") == "Build local installers without release credentials")
    assert "tauri build --features local-stack --config tauri.local.json" in steps[compile_index]["run"]
    for step in steps[:compile_index + 1]:
        assert not SIGNING.intersection(step.get("env", {})), step.get("name")
        assert "--signed" not in step.get("run", "")
        assert "security import" not in step.get("run", "")
    for step in steps[compile_index + 1:]:
        assert not re.search(r"\bcargo\s+(?:build|test|run)|\btauri\s+build\b", step.get("run", ""))
        assert not step.get("uses", "").startswith("tauri-apps/tauri-action@")
    bundle = next(step for step in steps if "sign-mac" in step.get("run", ""))
    assert "APPLE_CERTIFICATE" in bundle["env"]
    assert "TAURI_SIGNING_PRIVATE_KEY" not in bundle["env"]
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    assert not config["build"].get("beforeBundleCommand"), "A signing-time hook could compile with secrets"


def test_release_action_is_immutable_and_candidate_remains_a_draft():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build"]["steps"]
    actions = [step["uses"] for step in steps if step.get("uses", "").startswith("tauri-apps/tauri-action@")]
    assert not actions, "Local packaging uses the locked CLI, not a mutable third-party release action"
    workflow = yaml.safe_load((ROOT / ".github/workflows/desktop-local-release.yml").read_text())
    upload = next(step for step in workflow["jobs"]["publish"]["steps"] if "gh release create" in step.get("run", ""))
    assert "gh release create" in upload["run"] and "--draft" in upload["run"]
    assert '--target "$GITHUB_SHA"' in upload["run"] and "--verify-tag" in upload["run"]
    assert "gh release edit" not in upload["run"] and "gh release upload" not in upload["run"]
    assert yaml.safe_load(WORKFLOW.read_text())["jobs"]["draft"]["needs"] == "build"
    assert "publish_desktop_downloads.py" not in WORKFLOW.read_text()


def _prepare():
    spec = importlib.util.spec_from_file_location("desktop_release_security", ROOT / "scripts/desktop/prepare_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signed_draft_manifest_preserves_signature_notes_and_exact_archive(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    archive_dir = bundle / "macos"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "Supymem candidate.app.tar.gz"
    archive.write_bytes(b"synthetic archive")
    signature = "synthetic-updater-signature"
    archive.with_name(archive.name + ".sig").write_text(signature + "\n")
    (bundle / "candidate.dmg").write_bytes(b"synthetic dmg")
    notes = "<!--cos-update {\"mandatory\":false}-->\nReview candidate\n"
    monkeypatch.setenv("GITHUB_REPOSITORY", "contextplz/company_os_mvp")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("RELEASE_NOTES", notes)
    _prepare().prepare_draft_assets("0.1.86", bundle)
    manifest = json.loads((bundle / "latest.json").read_text())
    assert manifest["version"] == "0.1.86" and manifest["notes"] == notes
    assert manifest["platforms"] == {"darwin-aarch64": {
        "signature": signature,
        "url": "https://github.com/contextplz/company_os_mvp/releases/download/companyos-v0.1.86/Supymem%20candidate.app.tar.gz",
    }}
    assert (bundle / "release-notes.txt").read_text() == notes
    assert archive.read_bytes() == b"synthetic archive"


def test_signed_draft_refuses_missing_signature_without_metadata(tmp_path, monkeypatch):
    (tmp_path / "candidate.app.tar.gz").write_bytes(b"synthetic archive")
    (tmp_path / "candidate.dmg").write_bytes(b"synthetic dmg")
    monkeypatch.setenv("GITHUB_REPOSITORY", "contextplz/company_os_mvp")
    with pytest.raises(SystemExit, match="signature are missing"):
        _prepare().prepare_draft_assets("0.1.86", tmp_path)
    assert not (tmp_path / "latest.json").exists()


def test_signed_draft_refuses_empty_signature(tmp_path):
    (tmp_path / "candidate.app.tar.gz").write_bytes(b"synthetic archive")
    (tmp_path / "candidate.app.tar.gz.sig").write_text("\n")
    (tmp_path / "candidate.dmg").write_bytes(b"synthetic dmg")
    with pytest.raises(SystemExit, match="signature is empty"):
        _prepare().prepare_draft_assets("0.1.86", tmp_path)
    assert not (tmp_path / "latest.json").exists()


def test_signed_draft_refuses_ambiguous_archives(tmp_path):
    for name in ("first", "second"):
        (tmp_path / f"{name}.app.tar.gz").write_bytes(b"synthetic archive")
    (tmp_path / "candidate.dmg").write_bytes(b"synthetic dmg")
    with pytest.raises(SystemExit, match="exactly one"):
        _prepare().prepare_draft_assets("0.1.86", tmp_path)
    assert not (tmp_path / "latest.json").exists()
