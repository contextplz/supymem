"""Launch the built app's real supervisor without creating a window."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def installed_resources(executable: Path, resources: Path | None) -> Path:
    candidates = [resources] if resources else [executable.parent, executable.parent.parent / "Resources"]
    if not resources:
        library = executable.parent.parent / "lib"
        if library.is_dir():
            candidates.extend(library.iterdir())
    for candidate in candidates:
        path = candidate.resolve()
        source_tree = any(parent.name == "src-tauri" and path.is_relative_to(parent / "resources") for parent in path.parents)
        if (not path.is_relative_to(ROOT) and not source_tree
                and (path / "console/apps/console/server.js").is_file()
                and (path / "local/services/company_os/app/core/local_mode.py").is_file()):
            return path
    raise ValueError("Your installed resources were not found. Extract your installer and pass its resource folder, not src-tauri/resources, then rerun.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("resources", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        resources = installed_resources(args.executable.resolve(), args.resources)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
    environment = {name: value for name, value in os.environ.items() if name in {
        "PATH", "HOME", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    }}
    temporary = Path(tempfile.mkdtemp(prefix="desktop-smoke-"))
    try:
        for domain in ("fleet-tools.invalid", "recipe-club.invalid"):
            data = temporary / domain
            for phase in ("first-run", "warm-start"):
                started = time.perf_counter()
                subprocess.run([str(args.executable.resolve()), "--local-stack-smoke", str(resources), str(data)],
                               env=environment, check=True, timeout=180)
                elapsed = time.perf_counter() - started
                if not (data / "database.sqlite3").is_file():
                    raise SystemExit("Your database was not created in its data folder. Check startup, then retry.")
                print(f"PASS {phase} {domain} launch-through-stop={elapsed:.3f}s", flush=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise SystemExit(f"Your smoke test stopped. Open the logs under {temporary}, then rebuild and retry.") from None
    else:
        shutil.rmtree(temporary)
    print("SUMMARY PASS=4 FAIL=0 SKIP=0; owned processes stopped and temporary data removed.")


if __name__ == "__main__":
    main()
