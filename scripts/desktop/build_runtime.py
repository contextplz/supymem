"""Freeze and install a native bridge sidecar without copying development stubs."""
from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_digest() -> str:
    """Fingerprint every source or dependency input frozen into the sidecar."""
    inputs = [Path(__file__), ROOT / "services/company_os/requirements.runtime.lock"]
    for package in ("desktop_runtime", "coding_agents", "core"):
        base = ROOT / "packages" / package
        inputs.append(base / "pyproject.toml")
        inputs.extend(path for path in base.rglob("*")
                      if path.is_file() and path.suffix in {".py", ".spec", ".plist"})
    digest = hashlib.sha256()
    for path in sorted(set(inputs)):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def host_triple() -> str:
    system, machine = platform.system(), platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64" if machine in {"x86_64", "amd64"} else machine
    suffix = {"Darwin": "apple-darwin", "Linux": "unknown-linux-gnu", "Windows": "pc-windows-msvc"}.get(system)
    if suffix is None or arch not in {"aarch64", "x86_64"}:
        raise SystemExit(f"Unsupported build host: {system}/{machine}")
    return f"{arch}-{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=host_triple())
    parser.add_argument("--if-stale", action="store_true")
    args = parser.parse_args()
    if args.target != host_triple():
        raise SystemExit(f"Freeze on a native {args.target} Python runner; this runner is {host_triple()}.")
    suffix = ".exe" if platform.system() == "Windows" else ""
    target = ROOT / "src-tauri" / "binaries" / f"companyos-runtime-{args.target}{suffix}"
    fingerprint = target.with_name(target.name + ".inputs.sha256")
    current_digest = source_digest()
    if (args.if_stale and target.is_file() and fingerprint.is_file()
            and fingerprint.read_text().strip() == current_digest):
        print(f"Native bridge inputs unchanged: {target}")
        return
    output = ROOT / "dist" / "desktop-runtime" / args.target
    work = ROOT / "build" / "desktop-runtime" / args.target
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(output), "--workpath", str(work),
                    str(ROOT / "packages/desktop_runtime/companyos-runtime.spec")], cwd=ROOT, check=True)
    source = output / f"companyos-runtime{suffix}"
    if not source.is_file() or source.read_bytes()[:2] == b"#!":
        raise SystemExit("Freeze did not produce a native executable.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        raise SystemExit(f"Sidecar path is a directory from an old build: {target}; remove that build artifact first.")
    shutil.copy2(source, target)
    target.chmod(0o755)
    fingerprint.write_text(current_digest + "\n")
    print(f"Native bridge: {target}")


if __name__ == "__main__":
    main()
