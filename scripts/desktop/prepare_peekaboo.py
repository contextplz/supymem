"""Stage a pinned, signed Peekaboo CLI as Supymem's native UI driver.

The binary is a build artifact, never checked into Git. A public app uses its
bundled copy only; Homebrew is a local build input, not a beta-user dependency.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINNED_VERSION = "4.5.0"
PINNED_TEAM = "FWJYW4S8P8"


def main() -> None:
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise SystemExit("Peekaboo driver staging currently supports Apple Silicon macOS only.")
    source = Path(os.environ.get("PEEKABOO_BINARY") or shutil.which("peekaboo") or "")
    if not source.is_file():
        raise SystemExit("Install the official Peekaboo 4.5.0 CLI on this build machine.")
    version = subprocess.check_output([str(source), "--version"], text=True, timeout=10)
    if f"Peekaboo {PINNED_VERSION} " not in version:
        raise SystemExit("Peekaboo build input is not the pinned version.")
    signature = subprocess.run(["codesign", "-dv", "--verbose=2", str(source)],
                               capture_output=True, text=True, timeout=10)
    if signature.returncode != 0 or f"TeamIdentifier={PINNED_TEAM}" not in signature.stderr:
        raise SystemExit("Peekaboo build input lacks the pinned official Developer ID signature.")
    library = source.resolve().with_name("libswiftCompatibilitySpan.dylib")
    library_signature = subprocess.run(["codesign", "-dv", "--verbose=2", str(library)],
                                       capture_output=True, text=True, timeout=10)
    if not library.is_file() or library_signature.returncode != 0 or f"TeamIdentifier={PINNED_TEAM}" not in library_signature.stderr:
        raise SystemExit("Peekaboo build input lacks its signed Swift compatibility library.")
    target = ROOT / "src-tauri/binaries/peekaboo-aarch64-apple-darwin"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(0o755)
    shutil.copy2(source, target)
    staged_library = target.parent / library.name
    if staged_library.exists():
        staged_library.chmod(0o644)
    shutil.copy2(library, staged_library)
    target.chmod(0o755)
    staged_library.chmod(0o644)
    # Homebrew's downloaded provenance xattr and read-only dylib mode make
    # Tauri's bundle-wide xattr cleanup fail before it can sign the app.
    for staged in (target, staged_library):
        subprocess.run(["xattr", "-c", str(staged)], check=True, timeout=10)
    print(f"Staged signed Peekaboo {PINNED_VERSION} for the Apple Silicon app.")


if __name__ == "__main__":
    main()
