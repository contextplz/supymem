"""Bundled local API, worker and native bridge entry point."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["core", "worker", "bridge"], required=True)
    parser.add_argument("--host", choices=["127.0.0.1"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--maintenance", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not args.data_dir.is_absolute():
        parser.error("Choose a valid port and an absolute data folder, then rebuild your app.")
    root = Path(__file__).resolve().parent
    site = Path(sys.prefix) / ("Lib/site-packages" if os.name == "nt" else
                              f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    sys.path[:0] = [str(root / "services/company_os"), str(root / "packages"), str(site)]
    from desktop_runtime.parent_watch import exit_when_parent_dies

    exit_when_parent_dies()
    os.environ.update(SUPYMEM_MODE="local", APP_ENV="local", COMPANY_OS_DATA_DIR=str(args.data_dir),
                      APP_ROLE="worker" if args.role == "worker" else "api")
    from desktop_runtime.crash_report import install_exception_hooks

    install_exception_hooks()
    if not args.maintenance:
        from app.core.local_env_import import import_local_env

        import_local_env()
    from desktop_runtime.__main__ import main as run_runtime

    run_runtime()


if __name__ == "__main__":
    main()
