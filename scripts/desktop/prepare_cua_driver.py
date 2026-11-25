"""Stage the pinned official CUA Driver for Supymem's embedded Mac runtime.

The archive is verified before extraction; the build machine's PATH is never a
source for the distributed computer-use binary. CUA is MIT licensed. This
script only stages the driver and does not enable desktop control.
"""
from __future__ import annotations

import argparse
import json
import re
import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.33.3"
TEAM_ID = "YCK386LBJ7"
ARCHIVE_SHA256 = "53c3c004003dd85929ce03fddba4d999a7aa360f11b789bebf4a29d928782553"
ARCHIVE_URL = (
    "https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.33.3/"
    "cua-driver-rs-0.33.3-darwin-universal-binary.tar.gz"
)
BINARIES_SHA256 = {
    "cua-driver": "9baf0ffe7f7a6c5eb912fdc0be54557d701b5aab2a67a4524c41c93fe0955c5f",
    "cua-cursor-theme": "1ffa7ad652f6834d6e87eba60492e9f1352df823e18947c646aabc56a41ebc85",
}
MAX_BINARY_BYTES = 100_000_000  # The pinned universal driver is 72,288,464 bytes.
# This is a distinct source artifact, not the official driver's version.
REVIEWED_SOURCE_VERSION = "0.29.1"
OUTPUT = ROOT / "src-tauri/binaries/cua-driver-aarch64-apple-darwin"
CURSOR_OUTPUT = OUTPUT.with_name("cua-cursor-theme-aarch64-apple-darwin")


def _signed_by_vendor(path: Path) -> bool:
    verified = subprocess.run(["codesign", "--verify", "--strict", str(path)],
                              capture_output=True, text=True, timeout=15)
    result = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)],
                            capture_output=True, text=True, timeout=15)
    return verified.returncode == result.returncode == 0 and f"TeamIdentifier={TEAM_ID}" in result.stderr.splitlines()


def stage_official_archive(archive: bytes) -> None:
    """Verify both private candidates before publication; roll back a failed pair."""
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("cua_release_archive_mismatch")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=".cua-stage-", dir=OUTPUT.parent))
    clean = True
    backups, installed = [], []
    try:
        pairs = (("cua-driver", OUTPUT), ("cua-cursor-theme", CURSOR_OUTPUT))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
            members = package.getmembers()
            for name, _target in pairs:
                selected = [member for member in members if member.name == name]
                if len(selected) != 1 or not selected[0].isreg() or selected[0].size > MAX_BINARY_BYTES:
                    raise ValueError("cua_release_members_invalid")
                extracted = package.extractfile(selected[0])
                if extracted is None:
                    raise ValueError("cua_release_members_invalid")
                with extracted:
                    data = extracted.read(MAX_BINARY_BYTES + 1)
                if hashlib.sha256(data).hexdigest() != BINARIES_SHA256[name]:
                    raise ValueError("cua_release_artifact_mismatch")
                candidate = directory / name
                candidate.write_bytes(data)
                candidate.chmod(0o755)
        for name, _target in pairs:
            if not _signed_by_vendor(directory / name):
                raise ValueError("cua_release_signature_invalid")
        version = subprocess.check_output([str(directory / "cua-driver"), "--version"], text=True, timeout=10).strip()
        if not re.fullmatch(r"(?:cua-driver(?:-rs)?\s+)?" + re.escape(VERSION), version):
            raise ValueError("cua_release_version_invalid")
        # ponytail: build staging is single-writer. Both artifacts were admitted above;
        # retain the old pair until both replacements succeed.
        for _name, target in pairs:
            if target.is_symlink() or target.exists() and not target.is_file():
                raise ValueError("cua_release_destination_invalid")
        try:
            for name, target in pairs:
                if target.exists():
                    backup = directory / (name + ".previous")
                    backups.append((backup, target))
                    os.replace(target, backup)
            for name, target in pairs:
                installed.append(target)
                os.replace(directory / name, target)
        except BaseException:
            try:
                for target in installed:
                    target.unlink(missing_ok=True)
                for backup, target in backups:
                    if backup.exists():
                        os.replace(backup, target)
            except BaseException as error:
                clean = False  # Never destroy the only retained recovery bytes.
                raise RuntimeError(f"CUA pair rollback failed; recovery retained at {directory}") from error
            raise
    finally:
        if clean:
            shutil.rmtree(directory)


REVIEWED_SOURCE_PIN = Path(__file__).resolve().parent / "vendor/cua-modal-0.29.1/reviewed-build.json"


def stage_reviewed_source_build(directory: Path) -> None:
    # This committed ledger is intentionally absent while the fork is a source
    # candidate. Only root-reviewed native trials can establish its exact pin.
    if not REVIEWED_SOURCE_PIN.is_file():
        raise ValueError("cua_source_build_not_reviewed")
    pin = json.loads(REVIEWED_SOURCE_PIN.read_text())
    manifest_path = directory / "candidate.json"
    candidate_bytes = manifest_path.read_bytes()
    candidate = json.loads(candidate_bytes)
    source_pin = json.loads((REVIEWED_SOURCE_PIN.parent / "manifest.json").read_text())
    if (pin.get("schema") != "supymem_reviewed_cua_build_v1"
            or pin.get("candidate_manifest_sha256") != hashlib.sha256(candidate_bytes).hexdigest()
            or candidate.get("native_build") is not True
            or candidate.get("source_commit") != source_pin["source_commit"]
            or candidate.get("source_tree_sha256") != source_pin["source_tree_sha256"]
            or candidate.get("patch_sha256") != source_pin["patch_sha256"]
            or candidate.get("cargo_lock_sha256") != source_pin["cargo_lock_sha256"]
            or candidate.get("protocol") != source_pin["protocol"]
            or candidate.get("source_version") != REVIEWED_SOURCE_VERSION
            or not isinstance(pin.get("reviewed_native_trial_receipt_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", pin["reviewed_native_trial_receipt_sha256"])
            or hashlib.sha256((directory / "native-trial-receipt.json").read_bytes()).hexdigest()
                != pin["reviewed_native_trial_receipt_sha256"]):
        raise ValueError("cua_reviewed_source_binding_invalid")
    staged = []
    for name, destination in (("cua-driver", OUTPUT), ("cua-cursor-theme", CURSOR_OUTPUT)):
        artifact = directory / "target/release" / name
        data = artifact.read_bytes()
        expected = candidate.get("binaries_sha256", {}).get(name)
        if (hashlib.sha256(data).hexdigest() != expected
                or expected != pin.get("binaries_sha256", {}).get(name)):
            raise ValueError("cua_reviewed_source_artifact_mismatch")
        signature = subprocess.run(["codesign", "--verify", "--strict", str(artifact)],
                                   capture_output=True, text=True, timeout=15)
        identity = subprocess.run(["codesign", "-dv", "--verbose=2", str(artifact)],
                                  capture_output=True, text=True, timeout=15)
        if (signature.returncode or identity.returncode
                or "TeamIdentifier=Q9YFTMS7DP" not in identity.stderr):
            raise ValueError("cua_reviewed_source_signature_invalid")
        staged.append((data, destination))
    # Verify both artifacts before changing either packaged destination.
    for data, destination in staged:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o755)
    print("Staged exact reviewed source candidate; native acceptance belongs to its pinned review receipt.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-source-build", type=Path)
    args = parser.parse_args()
    if args.reviewed_source_build is not None:
        stage_reviewed_source_build(args.reviewed_source_build)
        return
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise SystemExit("CUA Driver staging currently supports Apple Silicon macOS only.")
    source = os.environ.get("CUA_DRIVER_ARCHIVE")
    if source:
        archive = Path(source).read_bytes()
    else:
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response:
            archive = response.read(60_000_000)
    stage_official_archive(archive)
    print(f"Staged official CUA Driver {VERSION} for Supymem on Apple Silicon.")


if __name__ == "__main__":
    main()
