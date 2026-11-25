"""Entry point: python -m desktop_runtime --role {warden,core,gateway,bridge} --port N

Imports the ASGI app object directly (not as a uvicorn import string) so PyInstaller
can freeze the graph correctly (doc 90 §3.2).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import ExitStack
from pathlib import Path

REPO_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
ROLES = ("warden", "core", "gateway", "bridge", "worker")
DEFAULT_PORTS = {
    "warden": 3400,
    "core": 3300,
    "gateway": 3200,
    "bridge": 3500,
    "worker": 3301,
}


def _ensure_service_on_path(role: str) -> Path:
    if role in {"warden", "gateway"} and os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local":
        raise SystemExit("Start your local app with ./start instead.")
    mapping = {
        "warden": REPO_ROOT / "services" / "warden",
        "core": REPO_ROOT / "services" / "company_os",
        "gateway": REPO_ROOT / "services" / "gateway",
        "bridge": REPO_ROOT / "services" / "company_os",
        "worker": REPO_ROOT / "services" / "company_os",
    }
    service_dir = mapping[role]
    if not service_dir.is_dir():
        if role in {"warden", "gateway"}:
            raise SystemExit("Start your local app with ./start instead.")
        raise SystemExit(f"service directory missing for role={role}: {service_dir}")
    # Prefer the role's own app package; packages/ is already installed editable in venv.
    sys.path.insert(0, str(service_dir))
    return service_dir


def _load_asgi_app(role: str):
    _ensure_service_on_path(role)
    if role == "warden":
        from app.main import app as asgi_app  # type: ignore

        return asgi_app
    if role == "core":
        from app.main import app as asgi_app  # type: ignore

        return asgi_app
    if role == "gateway":
        from app.main import app as asgi_app  # type: ignore

        return asgi_app
    if role == "bridge":
        from desktop_runtime.bridge import create_bridge_app

        return create_bridge_app()
    raise SystemExit(f"unknown role: {role}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="companyos-runtime")
    p.add_argument("--role", required=True, choices=ROLES)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--maintenance", action="store_true")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("COMPANY_OS_DATA_DIR"),
        help="Overrides COMPANY_OS_DATA_DIR for this process",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    port = args.port or DEFAULT_PORTS[args.role]
    if args.data_dir:
        os.environ["COMPANY_OS_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())

    if args.maintenance:
        if args.role != "core":
            raise SystemExit("Use data maintenance only with your local app, then retry.")
        _ensure_service_on_path("core")
        from app.core.local_mode import is_local

        if not is_local():
            raise SystemExit("Use data maintenance only with your local app, then retry.")
        from app.cli_local import LocalMaintenanceError
        from desktop_runtime.maintenance import run

        try:
            run()
        except LocalMaintenanceError as error:
            raise SystemExit(str(error)) from None
        return

    if args.role == "worker":
        _ensure_service_on_path(args.role)
        from app.core.local_mode import is_local

        if not is_local():
            raise SystemExit("Your background tasks need local mode. Start your app in local mode, then retry.")
        os.environ["WORKER_HEALTH_PORT"] = str(port)
        from app.worker.main import main as run_worker

        run_worker([])
        return

    # Bridge role can also run as a long-lived websocket client without HTTP.
    if args.role == "bridge" and os.environ.get("BRIDGE_MODE", "http") == "client":
        from desktop_runtime.bridge import run_bridge_client

        asyncio.run(run_bridge_client())
        return

    with ExitStack() as lifetime:
        if args.role == "core":
            _ensure_service_on_path("core")
            from app.core.local_mode import is_local

            if is_local():
                from app.cli_local import LocalMaintenanceError
                from app.config import data_root
                from desktop_runtime.maintenance import instance_lock

                try:
                    lifetime.enter_context(instance_lock(data_root()))
                except LocalMaintenanceError as error:
                    raise SystemExit(str(error)) from None
        asgi_app = _load_asgi_app(args.role)
        import uvicorn

        # Pass the app object, not a string — required for frozen binaries (doc 90 §3.2).
        local_options = {"proxy_headers": False} if os.environ.get("SUPYMEM_MODE", "").strip().lower() == "local" else {}
        uvicorn.run(asgi_app, host=args.host, port=port, workers=1, log_level="info", **local_options)


if __name__ == "__main__":
    main()
