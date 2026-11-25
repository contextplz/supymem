from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dv = _load("desktop_version_mod", "scripts/desktop_version.py")
PUBLISH_SCRIPT = "scripts/publish_desktop_downloads.py"
pub = _load("publish_desktop_downloads_mod", PUBLISH_SCRIPT) if (ROOT / PUBLISH_SCRIPT).is_file() else None
requires_publisher = pytest.mark.skipif(pub is None, reason="Hosted release tooling is not in the public export.")


def test_in_repo_desktop_versions_match():
    version = dv.assert_versions_match()
    assert version == dv.read_repo_version()
    assert len(Version(version).release) == 3
    assert json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["version"] == version
    info = plistlib.loads((ROOT / "src-tauri/Info.local.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == info["CFBundleVersion"] == version.partition("-")[0]
    assert info["SupymemReleaseVersion"] == version
    assert json.loads((ROOT / "apps/console/package.json").read_text())["version"] == version
    for path in ("services/company_os/pyproject.toml", "packages/desktop_runtime/pyproject.toml"):
        assert tomllib.loads((ROOT / path).read_text())["project"]["version"] == version
    from desktop_runtime import __version__

    assert __version__ == version
    assert re.search(r"^## (.+)$", (ROOT / "CHANGELOG.md").read_text(), re.M)[1] == version


def test_version_does_not_regress_from_tauri_history():
    # shortcut: shallow clones and source exports may only know this floor; use full history in CI.
    versions = [Version("1.0.0-rc.2")]
    if (ROOT / ".git").exists():
        history = subprocess.check_output(
            ["git", "log", "--full-history", "-m", "--format=", "-p", "--", dv.TAURI_CONF, "src-tauri/tauri.local.json"],
            cwd=ROOT, encoding="utf-8",
        )
        versions.extend(Version(value) for value in re.findall(r'^[+-][ \t]+' + dv.JSON_VERSION, history, re.M))
    current = dv.read_repo_version()
    highest = max(versions)
    assert Version(current) >= highest, f"{current} is below the desktop version floor {highest}"


@pytest.mark.parametrize("domain, new_version", [("logistics.example", "2.3.4"), ("cooking.example", "3.4.5-rc.2")])
def test_bump_updates_all_app_version_metadata(tmp_path, domain, new_version):
    root = tmp_path / domain
    for name in dv.VERSION_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    assert dv.set_repo_version(new_version, root) == new_version
    assert dv.assert_versions_match(root) == new_version
    info = plistlib.loads((root / "src-tauri/Info.local.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == info["CFBundleVersion"] == new_version.partition("-")[0]
    assert info["SupymemReleaseVersion"] == new_version
    console = root / "apps/console/package.json"
    assert json.loads(console.read_text())["version"] == new_version
    shutil.copyfile(ROOT / "apps/console/package.json", console)
    with pytest.raises(SystemExit, match="apps/console/package.json"):
        dv.assert_versions_match(root)


@requires_publisher
@pytest.mark.parametrize("filename", ["App.tar.gz", "App archive#1.tar.gz"])
def test_publish_downloads_rewrites_urls_without_delete(tmp_path, monkeypatch, filename):
    src = tmp_path / "assets"
    src.mkdir()
    artifact = src / filename
    artifact.write_bytes(b"artifact")
    public_key, signature = _sign(artifact.read_bytes())
    config = tmp_path / "src-tauri"
    config.mkdir()
    (config / "tauri.conf.json").write_text(json.dumps({"version": "0.1.1", "plugins": {"updater": {"pubkey": public_key}}}))
    monkeypatch.setattr(pub, "ROOT", tmp_path)
    (src / "latest.json").write_text(
        json.dumps(
            {
                "version": "0.1.1",
                "notes": '<!--cos-update {"mandatory":false}-->\nOptional',
                "platforms": {
                    "darwin-aarch64": {
                        "signature": signature,
                        "url": "https://example.com/releases/" + quote(filename, safe=""),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    out = pub.publish(
        source_dir=src,
        bucket="selfhost-prod-frontend-123456789012",
        prefix="downloads",
        public_base="https://supymem.com/downloads",
        dry_run=True,
    )
    rewritten = json.loads(out.read_text())
    assert rewritten["platforms"]["darwin-aarch64"]["url"] == (
        f"https://supymem.com/downloads/0.1.1/{hashlib.sha256(b'artifact').hexdigest()}/{quote(filename, safe='')}"
    )
    src_text = (ROOT / "scripts" / "publish_desktop_downloads.py").read_text(encoding="utf-8")
    assert "s3 sync" not in src_text
    assert re.search(r"aws\s+s3\s+\S+[^\n]*--delete", src_text) is None
    assert '["aws", "s3", "cp"' in src_text


def test_updater_endpoint_is_https_and_matches_runtime():
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    endpoints = tauri["plugins"]["updater"]["endpoints"]
    from desktop_runtime.update_policy import DEFAULT_UPDATE_ENDPOINT

    assert endpoints == [DEFAULT_UPDATE_ENDPOINT]
    assert all(endpoint.startswith("https://") for endpoint in endpoints)
    assert not tauri["plugins"]["updater"].get("dangerousInsecureTransportProtocol")
    pubkey = tauri["plugins"]["updater"]["pubkey"]
    assert pubkey.strip(), "updater pubkey must live in tauri.conf.json"
    assert "PRIVATE" not in pubkey.upper()


@requires_publisher
def test_release_workflow_notarizes_draft_without_publishing_and_manual_workflow_publishes():
    text = (ROOT / ".github/workflows/desktop-hosted-release.yml").read_text(encoding="utf-8")
    publish = (ROOT / ".github/workflows/desktop-publish.yml").read_text(encoding="utf-8")
    assert "s3 rb" not in text
    assert "s3 sync" not in text
    assert re.search(r"aws\s+s3\s+\S+[^\n]*--delete", text) is None
    assert "TAURI_SIGNING_PRIVATE_KEY" in text
    assert "APPLE_CERTIFICATE" in text
    assert "APPLE_ID" in text
    assert "APPLE_TEAM_ID" in text
    assert "- name: Build unsigned review desktop" in text
    assert "- name: Compile signed candidate without signing credentials" in text
    unsigned = text.split("- name: Build unsigned review desktop", 1)[1].split(
        "- name: Compile signed candidate without signing credentials", 1
    )[0]
    signed = text.split("- name: Bundle and sign the compiled candidate", 1)[1].split(
        "- name: Attach signed candidate to the draft release", 1
    )[0]
    assert "includeUpdaterJson: false" in unsigned
    assert "APPLE_CERTIFICATE" not in unsigned
    assert "TAURI_SIGNING_PRIVATE_KEY" not in unsigned
    assert "tauri bundle --target" in signed
    assert "APPLE_CERTIFICATE" not in signed
    assert "TAURI_SIGNING_PRIVATE_KEY" in signed
    assert "--draft-assets" in text and "gh release create" in text and "--draft" in text
    # Signing/notarization creates private draft assets only. A separate
    # explicitly dispatched workflow verifies and moves the public pointer.
    assert "publish-downloads:" not in text
    assert "publish_desktop_downloads.py" not in text
    assert "create-invalidation" not in text
    assert "on:\n  workflow_dispatch:" in publish
    assert "publish-downloads:" in publish
    assert "Verify and publish immutable artifacts, then latest.json" in publish
    assert "scripts/publish_desktop_downloads.py" in publish
    assert "--publish-approved-release" in publish
    assert '--expected-version "$RELEASE_VERSION"' in publish
    assert re.search(r"aws-actions/configure-aws-credentials@[0-9a-f]{40}", publish)
    assert "create-invalidation" in publish
    assert "runs-on: macos-15" in publish.split("publish-downloads:", 1)[1]
    assert "signed_release:" in text
    assert "channel:" in text
    assert "SUPYMEM_DESKTOP_CHANNEL" in text
    assert "Reject signed public staging updater" in text
    assert "python scripts/desktop/prepare_release.py" in text
    assert "xcrun notarytool submit" in text
    assert "xcrun stapler validate" in text
    freeze = text.split("- name: Sign embedded native sidecar libraries after compilation", 1)[1].split(
        "- name: Bundle and sign the compiled candidate", 1
    )[0]
    assert "APPLE_SIGNING_IDENTITY:" in freeze
    assert "secrets.APPLE_SIGNING_IDENTITY" in freeze
    assert "inputs.signed_release" in freeze
    verify = text.split("- name: Verify packaged Mac sidecar and signature", 1)[1].split(
        "- name: Upload build artifacts for review", 1
    )[0]
    assert 'xcrun stapler validate "${dmgs[0]}"' in verify
    assert 'xcrun stapler validate "$app"' not in verify
    assert 'gh release upload "$RELEASE_TAG" "$dmg" --clobber' in text
    assert '--pattern "*.dmg"' in publish
    publisher = (ROOT / "scripts" / "publish_desktop_downloads.py").read_text(encoding="utf-8")
    assert "verify_macos_dmg" in publisher
    app_verify = publisher.split("def _verify_macos_app", 1)[1].split("def verify_macos_artifact", 1)[0]
    assert 'stapler", "validate' not in app_verify
    assert 'f"s3://{bucket}/{prefix}/Supymem.dmg"' in publisher
    # Founder control: mandatory defaults off (optional nag, not always-force).
    assert "default: false" in text.split("mandatory:", 1)[1][:400]


def _sign(payload: bytes) -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.generate()
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = b"testkey1"
    public = "untrusted comment: unit test\n" + base64.b64encode(b"Ed" + key_id + raw_public).decode() + "\n"
    signature = private.sign(hashlib.blake2b(payload, digest_size=64).digest())
    comment = "timestamp:0 file:test"
    global_signature = private.sign(signature + comment.encode())
    sig = "untrusted comment: unit test\n" + base64.b64encode(b"ED" + key_id + signature).decode() + "\ntrusted comment: " + comment + "\n" + base64.b64encode(global_signature).decode() + "\n"
    return base64.b64encode(public.encode()).decode(), base64.b64encode(sig.encode()).decode()


@requires_publisher
def test_updater_signature_rejects_tampering_and_wrong_key(tmp_path):
    artifact = tmp_path / "app.tar.gz"
    artifact.write_bytes(b"signed artifact")
    key, signature = _sign(artifact.read_bytes())
    pub.verify_updater_signature(artifact, signature, key)
    wrong, _ = _sign(b"different")
    with pytest.raises(ValueError, match="verification failed"):
        pub.verify_updater_signature(artifact, signature, wrong)
    artifact.write_bytes(b"tampered artifact")
    with pytest.raises(ValueError, match="verification failed"):
        pub.verify_updater_signature(artifact, signature, key)


@requires_publisher
def test_publish_rejects_unapproved_live_release_before_any_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(pub, "_s3_cp", lambda *args, **kwargs: pytest.fail("must not upload"))
    with pytest.raises(ValueError, match="approved release"):
        pub.publish(source_dir=tmp_path, bucket="verified-bucket", prefix="downloads",
                    public_base="https://supymem.com/downloads", dry_run=False)


@requires_publisher
def test_manifest_rejects_unsigned_fixture_and_missing_artifact(tmp_path):
    with pytest.raises(ValueError, match="fixtures"):
        pub.validate_manifest(tmp_path, {"version": "0.1.31", "test_fixture": True}, live=False)
    manifest = {"version": "0.1.31", "platforms": {"darwin-aarch64": {"url": "https://example.com/missing.app.tar.gz", "signature": "fixture"}}}
    with pytest.raises(ValueError, match="Missing"):
        pub.validate_manifest(tmp_path, manifest, live=False)


@requires_publisher
def test_tauri_arm64_app_alias_is_collapsed_only_when_identical():
    entry = {"url": "https://example.com/Supymem.app.tar.gz", "signature": "signed"}
    manifest = {
        "version": "0.1.39",
        "platforms": {
            "darwin-aarch64": dict(entry),
            "darwin-aarch64-app": dict(entry),
        },
    }
    normalized = pub.normalize_tauri_platform_aliases(manifest)
    assert set(normalized["platforms"]) == {"darwin-aarch64"}
    assert set(manifest["platforms"]) == {"darwin-aarch64", "darwin-aarch64-app"}

    manifest["platforms"]["darwin-aarch64-app"]["signature"] = "different"
    with pytest.raises(ValueError, match="same signed artifact"):
        pub.normalize_tauri_platform_aliases(manifest)


def test_unsigned_generator_cannot_target_public_domain():
    from desktop_runtime.update_policy import generate_latest_manifest

    with pytest.raises(ValueError, match="loopback"):
        generate_latest_manifest(version="0.2.0", public_base="https://supymem.com/downloads")
    with pytest.raises(ValueError, match="loopback"):
        generate_latest_manifest(version="0.2.0", public_base="https://localhost.attacker.example")
    fixture = generate_latest_manifest(version="0.2.0")
    assert fixture["test_fixture"] is True


@pytest.fixture
def mac_release(tmp_path, monkeypatch):
    """Real files/tar/minisign; only macOS certificate/notary/mount commands are offline."""
    if pub is None:
        pytest.skip("Hosted release tooling is not in the public export.")
    app = tmp_path / "updater" / "Supymem.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    (contents / "_CodeSignature").mkdir()
    info = {"CFBundleIdentifier": "com.companyos.app", "CFBundleShortVersionString": "0.1.84",
            "CFBundleExecutable": "company-os-desktop"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    for name in ("company-os-desktop", "companyos-runtime"):
        (contents / "MacOS" / name).write_bytes(("same build " + name).encode())
        (contents / "MacOS" / name).chmod(0o755)
    (contents / "Resources" / "policy.json").write_bytes(b"reviewed profile")
    (contents / "_CodeSignature" / "CodeResources").write_bytes(b"same sealed resources")
    (contents / "Resources" / "policy-link").symlink_to("policy.json")
    dmg_app = tmp_path / "dmg-source" / "Supymem.app"
    shutil.copytree(app, dmg_app, symlinks=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    artifact = assets / "Supymem.app.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(app, arcname=app.name)
    public_key, signature = _sign(artifact.read_bytes())
    (assets / "latest.json").write_text(json.dumps({"version": "0.1.84", "platforms": {
        "darwin-aarch64": {"url": "https://example.test/Supymem.app.tar.gz", "signature": signature}}}))
    dmg = assets / "Supymem_0.1.84_aarch64.dmg"
    dmg.write_bytes(b"offline outer DMG container with its staple")
    config = tmp_path / "src-tauri"
    config.mkdir()
    (config / "tauri.conf.json").write_text(json.dumps({"version": "0.1.84", "plugins": {"updater": {"pubkey": public_key}}}))
    monkeypatch.setattr(pub, "ROOT", tmp_path)
    monkeypatch.setattr(pub.platform, "system", lambda: "Darwin")
    proof = {"dmg_team": pub.PUBLIC_MAC_TEAM_ID, "dmg_flags": "0x10000(runtime)", "commands": [], "uploads": []}
    def run(args, **_kwargs):
        proof["commands"].append(args)
        if args[:2] == ["hdiutil", "attach"]:
            mount = Path(args[args.index("-mountpoint") + 1])
            shutil.copytree(dmg_app, mount / "Supymem.app", symlinks=True)
        mounted = "mount" in Path(args[-1]).parts
        team = proof["dmg_team"] if mounted else pub.PUBLIC_MAC_TEAM_ID
        flags = proof["dmg_flags"] if mounted else "0x10000(runtime)"
        details = ("Executable=/offline/runtime-name/Supymem.app\n"
                   "Authority=Developer ID Application: Supymem\n"
                   "TeamIdentifier=" + team + "\n"
                   "CodeDirectory v=20500 size=123 flags=" + flags + " hashes=1+5\n")
        return SimpleNamespace(returncode=0, stderr=details)
    def arch(args, **_kwargs):
        assert args[:2] == ["lipo", "-archs"]
        return "arm64\n"
    monkeypatch.setattr(pub.subprocess, "run", run)
    monkeypatch.setattr(pub.subprocess, "check_output", arch)
    monkeypatch.setattr(pub, "_s3_cp", lambda src, dest, **kwargs: proof["uploads"].append((src.name, dest, kwargs)))
    return app, dmg_app, assets, artifact, dmg, proof


def _publish_mac_fixture(assets):
    return pub.publish(source_dir=assets, bucket="verified-bucket", prefix="downloads",
        public_base="https://supymem.com/downloads", dry_run=False, release_approved=True, expected_version="0.1.84")


def test_same_signed_app_publishes_only_after_independent_dmg_verification(mac_release):
    _app, _dmg_app, assets, _artifact, _dmg, proof = mac_release
    _publish_mac_fixture(assets)
    assert len(proof["uploads"]) == 4
    assert proof["uploads"][-1][1].endswith("/downloads/latest.json")
    assert any(args[:3] == ["xcrun", "stapler", "validate"] for args in proof["commands"])
    assert any(args[:4] == ["spctl", "-a", "-t", "open"] for args in proof["commands"])
    assert any(args[:2] == ["hdiutil", "detach"] for args in proof["commands"])
    assert len([args for args in proof["commands"] if args[:2] == ["codesign", "--verify"]]) == 2


@pytest.mark.parametrize("foreign", ["bundle", "team", "hardened"])
def test_foreign_same_version_app_cannot_publish(mac_release, foreign):
    _app, dmg_app, assets, _artifact, _dmg, proof = mac_release
    if foreign == "bundle":
        path = dmg_app / "Contents/Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["CFBundleIdentifier"] = "com.foreign.app"
        path.write_bytes(plistlib.dumps(info))
    elif foreign == "team":
        proof["dmg_team"] = "OTHERTEAM1"
    else:
        proof["dmg_flags"] = "0x0(none)"  # A path containing 'runtime' must not grant hardened runtime.
    with pytest.raises(ValueError):
        _publish_mac_fixture(assets)
    assert proof["uploads"] == []


@pytest.mark.parametrize("part", ["main", "runtime", "resource", "signature", "plist", "link", "extra", "missing", "execute"])
def test_dmg_payload_mismatch_cannot_move_any_public_pointer(mac_release, part):
    _app, dmg_app, assets, _artifact, _dmg, proof = mac_release
    paths = {"main": "MacOS/company-os-desktop", "runtime": "MacOS/companyos-runtime",
             "resource": "Resources/policy.json", "signature": "_CodeSignature/CodeResources",
             "extra": "Resources/extra.json"}
    contents = dmg_app / "Contents"
    if part in paths:
        (contents / paths[part]).write_bytes(b"another independently signed build")
    elif part == "plist":
        info = plistlib.loads((contents / "Info.plist").read_bytes())
        info["CFBundleDisplayName"] = "Another same-version build"
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    elif part == "link":
        path = contents / "Resources/policy-link"
        path.unlink()
        path.symlink_to("../Info.plist")
    elif part == "execute":
        (contents / "Resources/policy.json").chmod(0o755)
    else:
        (contents / "Resources/policy.json").unlink()
    with pytest.raises(ValueError, match="differs from the signed updater app"):
        _publish_mac_fixture(assets)
    assert proof["uploads"] == []
    assert any(args[:2] == ["hdiutil", "detach"] for args in proof["commands"])


@pytest.mark.parametrize("name", ["company-os-desktop", "companyos-runtime"])
def test_nonexecutable_installer_main_or_runtime_cannot_publish(mac_release, name):
    _app, dmg_app, assets, _artifact, _dmg, proof = mac_release
    (dmg_app / "Contents/MacOS" / name).chmod(0o644)
    with pytest.raises(ValueError, match="executable main and runtime"):
        _publish_mac_fixture(assets)
    assert proof["uploads"] == []
    assert any(args[:2] == ["hdiutil", "detach"] for args in proof["commands"])


def test_container_read_write_mode_normalization_does_not_change_app_payload(mac_release):
    _app, dmg_app, assets, _artifact, _dmg, proof = mac_release
    (dmg_app / "Contents/Resources/policy.json").chmod(0o600)
    _publish_mac_fixture(assets)
    assert len(proof["uploads"]) == 4


def test_updater_signature_still_precedes_platform_and_upload_checks(mac_release):
    _app, _dmg_app, assets, artifact, _dmg, proof = mac_release
    artifact.write_bytes(artifact.read_bytes() + b"tampered after signing")
    with pytest.raises(ValueError, match="signature verification failed"):
        _publish_mac_fixture(assets)
    assert proof["commands"] == proof["uploads"] == []


@pytest.mark.parametrize("command", ["codesign", "xcrun", "spctl"])
def test_platform_verification_failure_cannot_publish(mac_release, monkeypatch, command):
    _app, _dmg_app, assets, _artifact, _dmg, proof = mac_release
    run = pub.subprocess.run
    def refuse(args, **kwargs):
        if args[0] == command:
            raise pub.subprocess.CalledProcessError(1, args)
        return run(args, **kwargs)
    monkeypatch.setattr(pub.subprocess, "run", refuse)
    with pytest.raises(pub.subprocess.CalledProcessError):
        _publish_mac_fixture(assets)
    assert proof["uploads"] == []
