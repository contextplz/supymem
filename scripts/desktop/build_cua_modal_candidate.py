"""Build an unactivated candidate from exact reviewed upstream sources.

No binary download, installation, TCC request, daemon launch, or automatic staging.
Native Cargo builds may obtain their pinned compiler/dependencies.
Supply the Rust workspace exported from the pinned official commit. Full native
builds require at least 4 GiB free (1 GiB for strictly admitted failed-cache reuse);
pure scope tests need no desktop session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import stat
from pathlib import Path

LEGACY_CACHE_REF = "46813be11eb87ca704b7e9fdc8a5612911e76a7e"
LEGACY_BUILD_SCRIPT_SHA256 = '831da9386247351975b2ed14bb2b230e45dd613a2cd895104b2774d000392818'
BUILT_CACHE_REF = "ac6edd1e8bf7e84f0c64d339012ac6caaead69e7"
BUILT_CACHE_SCRIPT_SHA256 = "d6d301ff522391034b6dab2eced065f23022931aa5b2662aa9153edc18c24e56"
BUILT_CACHE_CANDIDATE_SHA256 = "6eef8ca853ce9a421cb5b217ed1469a6024c054b96dd0072e2d70db03f4f6c33"
BUILT_CACHE_PREPARED_SHA256 = "f4e2ebf2ddde332178b35b0eb7d4440fd0e8a59487a22676316e49ca18fa4624"
BUILT_CACHE_COMPILER_SHA256 = "e794146d99fad54562fdcbd4a7c8b22b0240d2b0312ff6d879d67939b3131850"
BUILT_CACHE_TEAM = "Q9YFTMS7DP"
BUILT_CACHE_UNSIGNED = {
    "cua-driver": "658552e08f4b5935faf7473ed3d013e80c47e2c23fb6f50ba24a3d7c1b4ec404",
    "cua-cursor-theme": "0d77813cdb1107e1f5291d0a5944430a600cc443db15924aab15ac718e97c86c",
}
BUILT_CACHE_SIGNED = {
    "cua-driver": "2e296a71926f5afd2ff0e1ef820272084c2d5e0663c4c9e448e93f73f08dcb42",
    "cua-cursor-theme": "e97f2c0559ebbd0fbe32ccdfe843e765ab4cd1cd865d9ad5d7e3bebe7830c048",
}
BUILT_CACHE_PROFILE = {"profile": "release", "debug": "0", "incremental": "0", "jobs": 2}
BUILD_PROFILE = {"profile": "release", "debug": "0", "incremental": "0", "jobs": 2}

PIN_DIR = Path(__file__).resolve().parent / "vendor/cua-modal-0.29.1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError("cua_source_symlink_refused")
        if path.is_file():
            rows.append(f"{relative.as_posix()}\0{digest(path.read_bytes())}\n")
    return digest("".join(rows).encode())


def prepare_source(source: Path, output: Path) -> tuple[Path, dict]:
    pin = json.loads((PIN_DIR / "manifest.json").read_text())
    patch = PIN_DIR / "exact-modal.patch"
    if (digest(patch.read_bytes()) != pin["patch_sha256"]
            or digest((PIN_DIR / "LICENSE.upstream.md").read_bytes()) != pin["license_sha256"]):
        raise ValueError("cua_patch_pin_mismatch")
    if tree_digest(source) != pin["source_tree_sha256"]:
        raise ValueError("cua_source_pin_mismatch")
    if output.exists() or output.is_symlink():
        raise ValueError("cua_candidate_output_must_be_fresh")
    output.mkdir(parents=True, mode=0o700)
    workspace = output / "source"
    shutil.copytree(source, workspace)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=workspace, check=True, timeout=30)
    subprocess.run(["git", "apply", str(patch)], cwd=workspace, check=True, timeout=30)
    if digest((workspace / "Cargo.lock").read_bytes()) != pin["cargo_lock_sha256"]:
        raise ValueError("cua_source_lock_changed")
    shutil.copyfile(PIN_DIR / "LICENSE.upstream.md", output / "LICENSE.upstream.md")
    prepared = {"schema": "supymem_cua_source_prepared_v1", **pin,
                "prepared_tree_sha256": tree_digest(workspace), "build_profile": BUILD_PROFILE,
                "native_build": False, "acceptance": "pending_root_native_trials"}
    (output / "source-prepared.json").write_text(json.dumps(prepared, indent=2) + "\n")
    return workspace, pin


def private_owned_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("cua_cache_scope_refused")
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("cua_cache_not_private")
    for path in root.rglob("*"):
        if path.lstat().st_uid != os.getuid():
            raise ValueError("cua_cache_owner_refused")
        if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("cua_cache_external_symlink_refused")


def compiler_identity(workspace: Path) -> str:
    return subprocess.run(["rustc", "-vV"], cwd=workspace, check=True,
                          capture_output=True, text=True, timeout=30).stdout


def cache_idle(cache: Path) -> None:
    # Read-only process inspection. Neither arguments nor private paths are emitted.
    result = subprocess.run(["/bin/ps", "-axo", "comm=,args="], check=True,
                            capture_output=True, text=True, timeout=10)
    for line in result.stdout.splitlines():
        command = line.split(None, 1)[0] if line.split() else ""
        if Path(command).name in {"cargo", "rustc", "swiftc", "clang", "cc", "ld"}:
            # Unknown compiler custody is also denied; this lane is deliberately serial.
            raise ValueError("cua_cache_compiler_busy")


def validate_resume_cache(pristine: Path, workspace: Path, output: Path, cache: Path) -> Path:
    """Only failed exact-197 caches; Cargo still validates every dependency fingerprint.

    Legacy caches have no pre-build receipt. Reconstruct their complete source from
    the committed old pin, rather than trusting a newly asserted receipt. Release
    parameters come from the independently pinned old build script; incompatible
    Cargo fingerprints cannot be reused under the explicitly identical new flags.
    """
    private_owned_tree(cache)
    cache_idle(cache)
    target = cache / "target"
    if not target.is_dir() or target.is_symlink():
        raise ValueError("cua_cache_target_missing")
    if (cache / "candidate.json").exists() or (cache / "reviewed-build.json").exists():
        raise ValueError("cua_cache_accepted_receipt_refused")
    if any((target / "release" / name).exists() for name in ("cua-driver", "cua-cursor-theme")):
        raise ValueError("cua_cache_final_artifact_refused")
    repo = Path(__file__).resolve().parents[2]
    prefix = "scripts/desktop/vendor/cua-modal-0.29.1/"
    def old_file(name: str) -> bytes:
        return subprocess.run(["git", "show", f"{LEGACY_CACHE_REF}:{prefix}{name}"], cwd=repo,
                              check=True, capture_output=True, timeout=30).stdout
    old_pin = json.loads(old_file("manifest.json"))
    old_patch = old_file("exact-modal.patch")
    if digest(old_patch) != old_pin["patch_sha256"] or tree_digest(pristine) != old_pin["source_tree_sha256"]:
        raise ValueError("cua_cache_legacy_pin_mismatch")
    old_script = subprocess.run(["git", "show", f"{LEGACY_CACHE_REF}:scripts/desktop/build_cua_modal_candidate.py"],
                                cwd=repo, check=True, capture_output=True, timeout=30).stdout
    # This is an exact reviewed legacy build contract, not an arbitrary old ref.
    if digest(old_script) != LEGACY_BUILD_SCRIPT_SHA256:
        raise ValueError("cua_cache_legacy_profile_mismatch")
    proof = output / "legacy-source-proof"
    shutil.copytree(pristine, proof)
    patch = output / "legacy-proof.patch"
    patch.write_bytes(old_patch)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=proof, check=True, timeout=30)
    subprocess.run(["git", "apply", str(patch)], cwd=proof, check=True, timeout=30)
    expected = tree_digest(proof)
    if tree_digest(cache / "source") != expected:
        raise ValueError("cua_cache_prepared_source_mismatch")
    lock = digest((cache / "source/Cargo.lock").read_bytes())
    if lock != old_pin["cargo_lock_sha256"] or lock != digest((workspace / "Cargo.lock").read_bytes()):
        raise ValueError("cua_cache_lock_mismatch")
    compiler = compiler_identity(workspace)
    info = json.loads((target / ".rustc_info.json").read_text())
    if not any(item.get("success") is True and item.get("stdout") == compiler
               for item in info.get("outputs", {}).values()):
        raise ValueError("cua_cache_compiler_mismatch")
    receipt = {"schema": "supymem_cua_cache_admission_v1", "legacy_ref": LEGACY_CACHE_REF,
               "legacy_patch_sha256": old_pin["patch_sha256"], "legacy_prepared_tree_sha256": expected,
               "cargo_lock_sha256": lock, "compiler_sha256": digest(compiler.encode()),
               "build_profile": BUILD_PROFILE, "target_path_sha256": digest(str(target.resolve()).encode()),
               "acceptance_inherited": False}
    (output / "cache-admission.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return target




def _dependency_metadata_bytes(path: Path) -> bytes:
    try:
        if path.is_symlink() or path.stat().st_size > 262144:
            raise ValueError("cua_dependency_cache_attestation_invalid")
        return path.read_bytes()
    except OSError:
        raise ValueError("cua_dependency_cache_attestation_missing") from None


def _dependency_metadata(path: Path) -> dict:
    try:
        value = json.loads(_dependency_metadata_bytes(path))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("cua_dependency_cache_attestation_invalid") from None
    if type(value) is not dict:
        raise ValueError("cua_dependency_cache_attestation_invalid")
    return value


def _built_cache_files() -> tuple[dict, bytes]:
    """Independent committed baseline; editable cache receipts are not authority."""
    repo = Path(__file__).resolve().parents[2]
    def read(name: str) -> bytes:
        return subprocess.run(["git", "show", f"{BUILT_CACHE_REF}:{name}"], cwd=repo,
                              check=True, capture_output=True, timeout=30).stdout
    prefix = "scripts/desktop/vendor/cua-modal-0.29.1/"
    pin = json.loads(read(prefix + "manifest.json"))
    patch = read(prefix + "exact-modal.patch")
    if (digest(patch) != pin["patch_sha256"]
            or digest(read("scripts/desktop/build_cua_modal_candidate.py")) != BUILT_CACHE_SCRIPT_SHA256
            or digest(read(prefix + "LICENSE.upstream.md")) != pin["license_sha256"]
            or BUILD_PROFILE != BUILT_CACHE_PROFILE):
        raise ValueError("cua_dependency_cache_trusted_pin_mismatch")
    return pin, patch


def _workspace_packages(workspace: Path) -> list[str]:
    # New cache lane only; normal/legacy builds retain their older Python support.
    try:
        import tomllib
    except ModuleNotFoundError:
        raise ValueError("cua_dependency_cache_python311_required") from None
    members = tomllib.loads((workspace / "Cargo.toml").read_text())["workspace"]["members"]
    names = []
    for member in members:
        path = workspace / member
        if (not isinstance(member, str) or not path.resolve().is_relative_to(workspace.resolve())
                or path.is_symlink()):
            raise ValueError("cua_dependency_cache_workspace_refused")
        name = tomllib.loads((path / "Cargo.toml").read_text())["package"]["name"]
        if not isinstance(name, str) or not name or name.startswith("-") or name in names:
            raise ValueError("cua_dependency_cache_workspace_refused")
        names.append(name)
    if not names:
        raise ValueError("cua_dependency_cache_workspace_refused")
    return names



def _dependency_cache_idle(cache: Path) -> None:
    cache_idle(cache)
    # Separate comm-only rows preserve spaces in an absolute executable path.
    result = subprocess.run(["/bin/ps", "-axo", "comm="], check=True,
                            capture_output=True, text=True, timeout=10)
    for line in result.stdout.splitlines():
        executable = Path(line.strip())
        if executable.name in {"cua-driver", "cua-cursor-theme"}:
            try:
                if not executable.is_absolute():
                    raise OSError("unknown native executable custody")
                resolved = executable.resolve(strict=True)
            except OSError:
                raise ValueError("cua_dependency_cache_native_busy") from None
            if resolved.is_relative_to(cache.resolve()):
                raise ValueError("cua_dependency_cache_native_busy")


def _dependency_cache_facts(workspace: Path, output: Path, cache: Path, *, preserved: Path | None = None) -> dict:
    private_owned_tree(cache)
    _dependency_cache_idle(cache)
    target = cache / "target"
    if not target.is_dir() or target.is_symlink():
        raise ValueError("cua_dependency_cache_target_missing")
    if cache.resolve() == output.resolve() or output.resolve().is_relative_to(cache.resolve()):
        raise ValueError("cua_dependency_cache_output_refused")
    if any((cache / name).exists() for name in ("reviewed-build.json", "native-trial-receipt.json")):
        raise ValueError("cua_dependency_cache_activated_refused")
    pin, _ = _built_cache_files()
    proof = output / "dependency-source-proof"
    if (tree_digest(proof) != BUILT_CACHE_PREPARED_SHA256
            or tree_digest(cache / "source") != BUILT_CACHE_PREPARED_SHA256):
        raise ValueError("cua_dependency_cache_source_mismatch")
    prepared = _dependency_metadata(cache / "source-prepared.json")
    native = _dependency_metadata(cache / "native-prepared.json")
    unsigned_bytes = _dependency_metadata_bytes(cache / "candidate.json")
    candidate = json.loads(unsigned_bytes)
    signed_bytes = _dependency_metadata_bytes(cache / "signed-qa-artifacts.json")
    signed = json.loads(signed_bytes)
    fresh = _dependency_metadata(output / "source-prepared.json")
    current = _dependency_metadata(PIN_DIR / "manifest.json")
    if (digest(unsigned_bytes) != BUILT_CACHE_CANDIDATE_SHA256
            or candidate.get("schema") != "supymem_cua_candidate_build_v1"
            or candidate.get("native_build") is not True
            or candidate.get("acceptance") != "pending_root_native_trials"
            or candidate.get("binaries_sha256") != BUILT_CACHE_UNSIGNED
            or any(candidate.get(k) != pin[k] or prepared.get(k) != pin[k]
                   for k in ("source_commit", "source_tree_sha256", "patch_sha256", "cargo_lock_sha256", "protocol", "source_version"))
            or prepared.get("license_sha256") != pin["license_sha256"]
            or prepared.get("prepared_tree_sha256") != BUILT_CACHE_PREPARED_SHA256
            or prepared.get("build_profile") != BUILT_CACHE_PROFILE
            or prepared.get("native_build") is not False
            or prepared.get("acceptance") != "pending_root_native_trials"
            or native.get("prepared_tree_sha256") != BUILT_CACHE_PREPARED_SHA256
            or native.get("build_profile") != BUILT_CACHE_PROFILE
            or native.get("acceptance") != "pending_root_native_trials"
            or native.get("cache_only") is not False
            or signed.get("schema") != "supymem_signed_qa_candidate_v1"
            or signed.get("installed_acceptance") is not False
            or signed.get("activation_allowed") is not False
            or signed.get("unsigned_candidate_manifest_sha256") != BUILT_CACHE_CANDIDATE_SHA256):
        raise ValueError("cua_dependency_cache_attestation_mismatch")
    if (any(fresh.get(k) != current[k] for k in current)
            or fresh.get("build_profile") != BUILD_PROFILE
            or fresh.get("native_build") is not False
            or fresh.get("acceptance") != "pending_root_native_trials"
            or tree_digest(workspace) != fresh.get("prepared_tree_sha256")
            or any(current[k] != pin[k] for k in ("source_commit", "source_tree_sha256", "cargo_lock_sha256", "license_sha256"))
            or digest((cache / "LICENSE.upstream.md").read_bytes()) != pin["license_sha256"]
            or digest((cache / "source/Cargo.lock").read_bytes()) != pin["cargo_lock_sha256"]
            or digest((workspace / "Cargo.lock").read_bytes()) != pin["cargo_lock_sha256"]):
        raise ValueError("cua_dependency_cache_lock_or_source_mismatch")
    compiler = compiler_identity(workspace)
    info = _dependency_metadata(target / ".rustc_info.json")
    if (digest(compiler.encode()) != BUILT_CACHE_COMPILER_SHA256
            or native.get("compiler_sha256") != BUILT_CACHE_COMPILER_SHA256
            or not any(item.get("success") is True and item.get("stdout") == compiler
                       for item in info.get("outputs", {}).values())):
        raise ValueError("cua_dependency_cache_compiler_mismatch")
    if preserved is not None:
        private_owned_tree(preserved)
        if preserved.resolve() != (cache / "preserved-signed-root357").resolve():
            raise ValueError("cua_chained_cache_preservation_refused")
        for name in ("candidate.json", "source-prepared.json", "native-prepared.json", "signed-qa-artifacts.json"):
            if _dependency_metadata_bytes(preserved / name) != _dependency_metadata_bytes(cache / name):
                raise ValueError("cua_chained_cache_preservation_mismatch")
    artifacts = signed.get("artifacts")
    if type(artifacts) is not dict or set(artifacts) != set(BUILT_CACHE_SIGNED):
        raise ValueError("cua_dependency_cache_signature_mismatch")
    for name, signed_hash in BUILT_CACHE_SIGNED.items():
        artifact = preserved / name if preserved is not None else target / "release" / name
        record = artifacts[name]
        if (type(record) is not dict or record.get("sha256") != signed_hash
                or record.get("unsigned_sha256") != BUILT_CACHE_UNSIGNED[name]
                or record.get("strict_verified") is not True or record.get("team_id") != BUILT_CACHE_TEAM
                or artifact.is_symlink() or digest(artifact.read_bytes()) != signed_hash):
            raise ValueError("cua_dependency_cache_signature_mismatch")
        verified = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(artifact)],
                                  capture_output=True, timeout=15)
        identity = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", str(artifact)],
                                 capture_output=True, text=True, timeout=15)
        if (verified.returncode != 0 or identity.returncode != 0
                or f"TeamIdentifier={BUILT_CACHE_TEAM}" not in identity.stderr.splitlines()):
            raise ValueError("cua_dependency_cache_signature_mismatch")
    return {"schema": "supymem_cua_built_dependency_cache_admission_v1", "trusted_ref": BUILT_CACHE_REF,
            "trusted_builder_sha256": BUILT_CACHE_SCRIPT_SHA256,
            "old_prepared_tree_sha256": BUILT_CACHE_PREPARED_SHA256,
            "unsigned_candidate_manifest_sha256": BUILT_CACHE_CANDIDATE_SHA256,
            "signed_qa_receipt_sha256": digest(signed_bytes),
            "unsigned_binaries_sha256": BUILT_CACHE_UNSIGNED, "signed_binaries_sha256": BUILT_CACHE_SIGNED,
            "compiler_sha256": BUILT_CACHE_COMPILER_SHA256, "cargo_lock_sha256": pin["cargo_lock_sha256"],
            "license_sha256": pin["license_sha256"], "build_profile": BUILD_PROFILE,
            "current_prepared_tree_sha256": fresh["prepared_tree_sha256"],
            "current_patch_sha256": current["patch_sha256"],
            "target_path_sha256": digest(str(target.resolve()).encode()),
            "first_party_packages_to_clean": _workspace_packages(workspace),
            "reuse_only_dependencies": True, "acceptance_inherited": False,
            "activation_allowed": False, "installed_acceptance": False}


def validate_dependency_cache(pristine: Path, workspace: Path, output: Path, cache: Path) -> Path:
    """Only the independently pinned signed-but-unactivated root357 build."""
    private_owned_tree(cache)
    _dependency_cache_idle(cache)
    pin, patch = _built_cache_files()
    if tree_digest(pristine) != pin["source_tree_sha256"]:
        raise ValueError("cua_dependency_cache_pristine_mismatch")
    proof = output / "dependency-source-proof"
    if proof.exists() or proof.is_symlink():
        raise ValueError("cua_dependency_cache_proof_must_be_fresh")
    shutil.copytree(pristine, proof)
    patch_path = output / "dependency-proof.patch"
    patch_path.write_bytes(patch)
    subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=proof, check=True, timeout=30)
    subprocess.run(["git", "apply", str(patch_path)], cwd=proof, check=True, timeout=30)
    receipt = _dependency_cache_facts(workspace, output, cache)
    (output / "dependency-cache-admission.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return cache / "target"


def _build_with_dependency_cache(workspace: Path, output: Path, target: Path) -> dict:
    cache = target.parent
    private_owned_tree(cache)
    _dependency_cache_idle(cache)
    lock = cache / ".candidate-build.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("cua_cache_build_locked") from None
    identity = os.fstat(fd)
    try:
        os.write(fd, str(os.getpid()).encode())
        receipt = _dependency_metadata(output / "dependency-cache-admission.json")
        if receipt.get("schema") == "supymem_cua_chained_dependency_cache_admission_v1":
            from cua_chained_cache import facts
            current = facts(globals(), workspace, output, cache, Path(receipt["chain_output"]))
        else:
            current = _dependency_cache_facts(workspace, output, cache)
        if current != receipt:
            raise ValueError("cua_dependency_cache_admission_changed")
        result = _native_build(workspace, output, cache_target=target, dependency_only=True,
                               clean_before_headroom=receipt.get("schema") == "supymem_cua_chained_dependency_cache_admission_v1")
        if tree_digest(workspace) != receipt["current_prepared_tree_sha256"]:
            raise ValueError("cua_dependency_cache_current_source_changed")
        return result
    finally:
        os.close(fd)
        current = lock.lstat()
        if current.st_dev == identity.st_dev and current.st_ino == identity.st_ino:
            lock.unlink()


def scope_test(workspace: Path, output: Path) -> None:
    # Execute the actual vendor pure-decision module; no mock implementation.
    wrapper = output / "scope-tests.rs"
    module = workspace / "crates/platform-macos/src/ax/window_scope.rs"
    wrapper.write_text('mod windows { #[derive(Debug, Clone)] pub enum WindowOwner { '
        'SamePid, Unknown, ForeignPid { owner_pid: i32, owner_app_name: String } } }\n'
        f'#[path = {json.dumps(str(module))}] mod window_scope;\n')
    for source, binary in [(wrapper, output / "scope-tests"),
            (workspace / "crates/platform-macos/src/ax/modal_scope.rs", output / "admission-tests"),
            (workspace / "crates/platform-macos/src/ax/press_readback.rs", output / "press-readback-tests"),
            (workspace / "crates/platform-macos/src/ax/press_diagnostic.rs", output / "press-diagnostic-tests"),
            (workspace / "crates/platform-macos/src/ax/ancestry_scope.rs", output / "ancestry-scope-tests"),
            (workspace / "crates/platform-macos/src/ax/control_state.rs", output / "control-state-tests"),
            (workspace / "crates/platform-macos/src/ax/action_error.rs", output / "action-error-tests")]:
        subprocess.run(["rustc", "--edition", "2021", "--test", str(source), "-o", str(binary)],
                       check=True, timeout=90)
        subprocess.run([str(binary)], check=True, timeout=30)


def native_build(workspace: Path, output: Path, *, cache_target: Path | None = None,
                 dependency_cache_target: Path | None = None) -> dict:
    if dependency_cache_target is not None:
        if cache_target is not None:
            raise ValueError("cua_cache_modes_mutually_exclusive")
        return _build_with_dependency_cache(workspace, output, dependency_cache_target)
    if cache_target is None:
        return _native_build(workspace, output)
    compiler = compiler_identity(workspace)
    admission = json.loads((output / "cache-admission.json").read_text())
    if (admission.get("compiler_sha256") != digest(compiler.encode())
            or admission.get("target_path_sha256") != digest(str(cache_target.resolve()).encode())
            or admission.get("build_profile") != BUILD_PROFILE
            or admission.get("acceptance_inherited") is not False):
        raise ValueError("cua_cache_admission_changed")
    private_owned_tree(cache_target.parent)
    cache_idle(cache_target.parent)
    lock = cache_target.parent / ".candidate-build.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("cua_cache_build_locked") from None
    identity = os.fstat(fd)
    try:
        os.write(fd, str(os.getpid()).encode())
        return _native_build(workspace, output, cache_target=cache_target)
    finally:
        os.close(fd)
        current = lock.lstat()
        if current.st_dev == identity.st_dev and current.st_ino == identity.st_ino:
            lock.unlink()


def _native_build(workspace: Path, output: Path, *, cache_target: Path | None = None,
                  dependency_only: bool = False, clean_before_headroom: bool = False) -> dict:
    if clean_before_headroom and not dependency_only:
        raise ValueError("cua_chained_cache_cleanup_scope_refused")
    if not clean_before_headroom and shutil.disk_usage(output).free < (1 if cache_target is not None else 4) * 1024**3:
        raise ValueError("cua_native_build_disk_headroom_required")
    target = cache_target if cache_target is not None else output / "target"
    compiler = compiler_identity(workspace)
    if cache_target is not None:
        cache_idle(cache_target.parent)
    inherited = {key: value for key, value in os.environ.items()
                 if not key.startswith("CARGO_PROFILE_RELEASE_")
                 and key not in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"}}
    env = {**inherited, "CARGO_TARGET_DIR": str(target), "CARGO_INCREMENTAL": "0",
           "CARGO_PROFILE_RELEASE_DEBUG": "0"}
    (output / "native-prepared.json").write_text(json.dumps({"build_profile": BUILD_PROFILE,
        "compiler_sha256": digest(compiler.encode()), "prepared_tree_sha256": tree_digest(workspace),
        "cache_only": cache_target is not None, "acceptance": "pending_root_native_trials"}) + "\n")
    if cache_target is not None:
        # Never inherit first-party binaries/test executables as accepted output.
        packages = _workspace_packages(workspace) if dependency_only else ["platform-macos", "cua-driver", "cursor-theme-cli"]
        clean = ["cargo", "clean", "--release"]
        for package in packages:
            clean.extend(["-p", package])
        subprocess.run(clean, cwd=workspace, env=env, check=True, timeout=120)
    # Dependency-only admission is rechecked under its exclusive lock before
    # cleaning. Cleaning may recover headroom, but no compilation starts below
    # the unchanged one-GiB floor, including after a successful clean.
    if clean_before_headroom and shutil.disk_usage(output).free < 1024**3:
        raise ValueError("cua_native_build_disk_headroom_required")
    if clean_before_headroom:
        scope_test(workspace, output)
    # Filtered tests exercise pure/schema contracts, never the desktop E2E lane.
    # Share the production release profile/target instead of compiling two profiles.
    for selector in ["ax::window_scope", "ax::modal_scope", "ax::press_readback", "ax::press_diagnostic", "tools::get_window_state::tests",
                     "tools::press_key::tests::modal_transition_evidence_is_truthful",
                     "tools::hotkey::tests::scoped_key_receipt_requires_explicit_modal_readback",
                     "ax::ancestry_scope", "ax::exact_target::tests", "ax::control_state",
                     "ax::bindings::tests::binary_value_", "ax::action_error",
                     "tools::click::tests::typed_ax_error_receipt_is_unconfirmed_and_private"]:
        subprocess.run(["cargo", "test", "--release", "--locked", "-p", "platform-macos", "--lib", selector, "--jobs", "2"],
                       cwd=workspace, env=env, check=True, timeout=1800)
    # The public canonical projection is a different seam from native AX tests.
    # Its closed diagnostic and non-promotion gates must run on the actual crates.
    for package, selector in [("cua-driver-contract", "outputs::tests"),
                              ("cua-driver-core", "runtime_isolation_tests::canonical_"),
                              ("platform-macos", "ax::action_receipt"),
                              ("cua-driver-core", "owned_modal::tests"),
                              ("cua-driver-core", "session_manifest::tests::reviewed_modal_")]:
        subprocess.run(["cargo", "test", "--release", "--locked", "-p", package, "--lib", selector, "--jobs", "2"],
                       cwd=workspace, env=env, check=True, timeout=1800)
    subprocess.run(["cargo", "build", "--release", "--locked", "-p", "cua-driver", "-p", "cursor-theme-cli",
                    "--jobs", "2"], cwd=workspace, env=env, check=True, timeout=3600)
    if cache_target is not None:
        destination = output / "target/release"
        destination.mkdir(parents=True)
        for name in ("cua-driver", "cua-cursor-theme"):
            shutil.copy2(target / "release" / name, destination / name)
    return {name: digest((output / "target/release" / name).read_bytes())
            for name in ("cua-driver", "cua-cursor-theme")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-build", action="store_true")
    caches = parser.add_mutually_exclusive_group()
    caches.add_argument("--resume-cache", type=Path, help="Failed exact-197 private cache; never inherits acceptance")
    caches.add_argument("--dependency-cache", type=Path, help="Exact signed root357 QA build dependencies only")
    parser.add_argument("--chained-output", type=Path, help="Pinned signed700 output; requires --dependency-cache and --native-build")
    args = parser.parse_args()
    if args.chained_output and (not args.dependency_cache or not args.native_build):
        raise ValueError("cua_chained_cache_requires_dependency_build")
    if args.dependency_cache and not args.native_build:
        raise ValueError("cua_dependency_cache_requires_native_build")
    if args.chained_output:
        from cua_chained_cache import disjoint
        disjoint(args.output.absolute(),args.dependency_cache.absolute(),args.chained_output.absolute())
    workspace, pin = prepare_source(args.source_tree.resolve(), args.output.absolute())
    cache_target = validate_resume_cache(args.source_tree.resolve(), workspace, args.output,
        args.resume_cache.absolute()) if args.resume_cache else None
    if args.chained_output:
        from cua_chained_cache import validate
        dependency_target = validate(globals(), args.source_tree.resolve(), workspace, args.output,
                                     args.dependency_cache.absolute(), args.chained_output.absolute())
    else:
        dependency_target = validate_dependency_cache(args.source_tree.resolve(), workspace, args.output,
            args.dependency_cache.absolute()) if args.dependency_cache else None
    if not args.chained_output:
        scope_test(workspace, args.output)
    binaries = native_build(workspace, args.output, cache_target=cache_target,
        dependency_cache_target=dependency_target) if args.native_build else {}
    receipt = {"schema": "supymem_cua_candidate_build_v1", "source_commit": pin["source_commit"],
               "source_tree_sha256": pin["source_tree_sha256"], "patch_sha256": pin["patch_sha256"],
               "protocol": pin["protocol"], "source_version": pin["source_version"],
               "cargo_lock_sha256": pin["cargo_lock_sha256"], "native_build": bool(binaries),
               "binaries_sha256": binaries, "acceptance": "pending_root_native_trials"}
    (args.output / "candidate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("Candidate source prepared; activation remains disabled.")


if __name__ == "__main__":
    main()
