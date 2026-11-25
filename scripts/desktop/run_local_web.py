"""Supervise the Windows source browser stack without desktop build tools."""

import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import psutil
from dotenv import dotenv_values

from desktop_runtime.owned_processes import OwnedProcesses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_version import read_repo_version


class SourceSetupError(ValueError):
    pass


def database_url(backend, data):
    from app.db import session

    if hasattr(session, "resolve_source_database_url"):
        try:
            return session.resolve_source_database_url(backend, data)
        except ValueError as error:
            raise SourceSetupError(str(error).replace("./start --web", ".\\start.ps1 --web")) from None
    # shortcut: reject inherited database settings on older checkouts, remove this when the shared validator is present.
    for name in ("COMPANY_OS_DB_URL", "DATABASE_URL", "DB_URL", "RDS_PROXY_ENDPOINT"):
        if (backend.get(name) or "").strip():
            raise SourceSetupError(f"Your browser app uses SQLite. Remove {name} from your checkout's .env and terminal environment, then rerun .\\start.ps1 --web.")
    legacy, desktop = data / "local.db", data / "database.sqlite3"
    selected = desktop if desktop.exists() and not legacy.exists() else legacy
    if (legacy.exists() and desktop.exists()) or not selected.resolve().is_relative_to(data):
        raise SourceSetupError("Your data folder has conflicting database files. Set COMPANY_OS_DATA_DIR in your checkout's .env to a folder with one SQLite file, then rerun .\\start.ps1 --web.")
    return f"sqlite:///{selected.as_posix()}"


def local_environment(root):
    env_path = root / ".env"
    try:
        with env_path.open(encoding="utf-8-sig") as source:
            values = dotenv_values(stream=source, interpolate=False)
    except FileNotFoundError:
        values = {}
    except UnicodeError:
        raise SourceSetupError(f"Save {env_path} as UTF-8, then rerun .\\start.ps1 --web.") from None
    except OSError:
        raise SourceSetupError(f"Check that your account can read {env_path}, then rerun .\\start.ps1 --web.") from None
    backend = {**{name: value for name, value in values.items() if value is not None}, **os.environ}
    backend.setdefault("OPENSSL_CONF", str(root / "scripts/desktop/openssl.cnf"))
    host = "127.0.0.1"
    with ExitStack() as sockets:
        for name, default in (("CORE_PORT", "3300"), ("CONSOLE_PORT", "3100"),
                              ("WORKER_HEALTH_PORT", "3301"), ("BRIDGE_PORT", "3500")):
            try:
                port = int(backend.get(name) or default)
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                raise SourceSetupError(f"Set {name} to a port from 1 to 65535 in your checkout's .env, then rerun .\\start.ps1 --web.") from None
            listener = sockets.enter_context(socket.socket())
            try:
                listener.bind((host, port))
                listener.listen()
            except OSError:
                if name != "BRIDGE_PORT" or backend.get(name):
                    raise SourceSetupError(f"Port {port} is unavailable. Set {name} to a different port in your checkout's .env, then rerun .\\start.ps1 --web.") from None
                listener.bind((host, 0))
            backend[name] = str(listener.getsockname()[1])

    data = Path(backend.get("COMPANY_OS_DATA_DIR") or backend.get("SUPYMEM_DATA_DIR") or root / "data/runtime/local").expanduser().resolve()
    origin = f"http://{host}:{backend['CONSOLE_PORT']}"
    core = f"http://{host}:{backend['CORE_PORT']}"
    backend.update(SUPYMEM_MODE="local", APP_ENV="local", SUPYMEM_APP_VERSION=read_repo_version(), COMPANY_OS_DATA_DIR=str(data),
                   CORE_URL=core, COMPANY_OS_URL=core, CONSOLE_ORIGIN=origin,
                   PYTHONPATH=os.pathsep.join((str(root / "packages"), str(root / "services/company_os"))),
                   BRIDGE_AUTH_OWNER="native", BRIDGE_MODE="http", BRIDGE_TOKEN=secrets.token_hex(32),
                   BRIDGE_NATIVE_TOKEN=secrets.token_hex(32))
    backend["COMPANY_OS_DB_URL"] = database_url(backend, data)
    backend["COMPANY_OS_WORKING_MEMORY_PATH"] = backend.get("COMPANY_OS_WORKING_MEMORY_PATH") or str(data / "working_memory.sqlite3")
    console = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA",
               "LOCALAPPDATA", "TEMP", "TMP", "NODE_USE_SYSTEM_CA") if name in os.environ}
    console.update(SUPYMEM_MODE="local", CORE_URL=core, CONSOLE_ORIGIN=origin, NEXT_PUBLIC_GATEWAY_URL=origin,
                   COMPANY_OS_DATA_DIR=str(data), CONSOLE_PORT=backend["CONSOLE_PORT"], OPENSSL_CONF=backend["OPENSSL_CONF"])
    return backend, console


def run(root, node):
    tracker = OwnedProcesses()
    children = []
    try:
        backend, console = local_environment(root)
        if subprocess.run([sys.executable, "-m", "desktop_runtime", "--role", "core", "--maintenance"],
                          cwd=root, env=backend).returncode:
            print("Your saved data could not be prepared. Follow the recovery steps above, then rerun .\\start.ps1 --web.", file=sys.stderr)
            return 1
        initialize = ("from app.config import data_root\nfrom app.state import db\n"
                      "from desktop_runtime.maintenance import instance_lock\nwith instance_lock(data_root()): db()")
        if subprocess.run([sys.executable, "-c", initialize], cwd=root, env=backend).returncode:
            print("Your database could not start. Check your data folder and database settings in your checkout's .env, then rerun .\\start.ps1 --web.", file=sys.stderr)
            return 1
        for role, name in (("core", "CORE_PORT"), ("worker", "WORKER_HEALTH_PORT"), ("bridge", "BRIDGE_PORT")):
            command = [sys.executable, "-m", "desktop_runtime", "--role", role, "--host", "127.0.0.1", "--port", backend[name]]
            child = psutil.Popen(command, cwd=root, env={**backend, "APP_ROLE": "worker" if role == "worker" else "api"})
            children.append(child)
            tracker.register(child)
        child = psutil.Popen([node, "node_modules/next/dist/bin/next", "dev", "--hostname", "127.0.0.1", "--port", console["CONSOLE_PORT"]],
                             cwd=root / "apps/console", env=console)
        children.append(child)
        tracker.register(child)
        print(f"Open {console['CONSOLE_ORIGIN']}/setup when your interface is ready. Press Ctrl+C to stop.", flush=True)
        while True:
            for child in children:
                status = child.poll()
                if status is not None:
                    print("A local service stopped. Fix the service error above, then rerun .\\start.ps1 --web.", file=sys.stderr)
                    return status or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except SourceSetupError as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("Your local services could not start. Check your Python and Node installation and your checkout's .env, then rerun .\\start.ps1 --web.", file=sys.stderr)
        return 1
    finally:
        if children and not tracker.stop_all():
            print("Your local services may still be running. Close this terminal before starting again.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run(Path(__file__).resolve().parents[2], sys.argv[1]))
