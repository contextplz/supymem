#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mode=${1:---web}
case "$mode" in --web|--desktop|--build) ;; *) printf '%s\n' 'Run ./start, ./start --web or ./start --build from your checkout.' >&2; exit 1 ;; esac
if [[ -z "${SUPYMEM_PYTHON:-}" ]]; then
  if [[ "$mode" == --desktop ]]; then exec ./start; else exec ./start "$mode"; fi
fi
set -m
child=''
stop_child() {
  trap '' INT TERM
  if [[ -n "$child" ]]; then
    kill -TERM -- "-$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
}
trap 'stop_child; exit 130' INT
trap 'stop_child; exit 143' TERM
trap 'printf "%s\n" "Your setup stopped. Fix the reported dependency error, then rerun ./start. Your saved work is retained." >&2' ERR
run() {
  "$@" &
  child=$!
  wait "$child"
  child=''
}
old_venv=''
if [[ -e .venv || -L .venv ]] && { [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1; }; then
  old_venv=".venv-old-$(date +%Y%m%d-%H%M%S)"
  while [[ -e "$old_venv" || -L "$old_venv" ]]; do old_venv="$old_venv-$$"; done
  mv .venv "$old_venv"
fi
if [[ ! -x .venv/bin/python ]]; then
  printf '%s\n' 'Creating your private Python environment (about 10 seconds).'
  run "$SUPYMEM_PYTHON" -m venv .venv
fi
.venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "Your selected Python is not Python 3.12. Install Python 3.12, then rerun ./start. Your saved work is retained.")'
if [[ -n "$old_venv" ]]; then
  printf 'Your previous Python environment is saved in %s. Setup continues with Python 3.12.\n' "$old_venv"
fi

IFS=: read -r -a node_paths <<< "$PATH"
node_paths+=(
  "${NVM_DIR:-${HOME:-}/.nvm}/versions/node/"*/bin
  "${FNM_DIR:-${HOME:-}/.local/share/fnm}/node-versions/"*/installation/bin
  "${HOME:-}/.fnm/node-versions/"*/installation/bin
  "${HOME:-}/Library/Application Support/fnm/node-versions/"*/installation/bin
  "${VOLTA_HOME:-${HOME:-}/.volta}/bin"
  "${VOLTA_HOME:-${HOME:-}/.volta}/tools/image/node/"*/bin
  "${ASDF_DATA_DIR:-${HOME:-}/.asdf}/shims"
  "${ASDF_DATA_DIR:-${HOME:-}/.asdf}/installs/nodejs/"*/bin
  /opt/homebrew/opt/node@*/bin /usr/local/opt/node@*/bin
)
if command -v brew >/dev/null 2>&1; then
  brew_node_prefix=$(brew --prefix node@22 2>/dev/null || true)
  if [[ -n "$brew_node_prefix" ]]; then node_paths+=("$brew_node_prefix/bin"); fi
fi
node_binary=''
for directory in "${node_paths[@]}"; do
  for name in node22 node nodejs; do
    candidate="${directory:-.}/$name"
    if [[ -x "$candidate" ]] && version=$("$candidate" -v 2>/dev/null) && [[ "$version" =~ ^v([0-9]+)\.[0-9]+\.[0-9]+$ ]] && (( BASH_REMATCH[1] >= 22 )); then
      node_binary="$(cd "${directory:-.}" && pwd)/$name"
      break 2
    fi
  done
done
if [[ -z "$node_binary" ]]; then
  printf '%s\n' 'You need Node 22 or newer. Install Node 22 from nodejs.org, add its bin directory to PATH, then rerun ./start.' >&2
  exit 1
fi
if [[ ! "$node_binary" -ef .venv/bin/node ]]; then ln -sf "$node_binary" .venv/bin/node; fi
export PATH="$PWD/.venv/bin:$PATH:$(dirname "$node_binary")"
if [[ "$(COREPACK_ENABLE_NETWORK=0 COREPACK_ENABLE_AUTO_PIN=0 pnpm --version 2>/dev/null || true)" == 10.11.0 ]]; then
  node_command=(pnpm)
elif [[ "$(COREPACK_ENABLE_NETWORK=0 COREPACK_ENABLE_AUTO_PIN=0 corepack pnpm --version 2>/dev/null || true)" == 10.11.0 ]]; then
  node_command=(corepack pnpm)
else
  printf '%s\n' 'Run ./start to install pnpm 10.11.0 through Corepack, then continue automatically.' >&2
  exit 1
fi
inputs=(services/company_os/requirements.runtime.lock services/company_os/requirements.build.lock packages/desktop_runtime/requirements.local.lock services/company_os/pyproject.toml packages/*/pyproject.toml)
signature=$(cat "${inputs[@]}" | cksum)
if [[ ! -f .venv/.local-dependencies ]] || [[ "$(cat .venv/.local-dependencies)" != "$signature" ]]; then
  printf '%s\n' 'Installing your locked Python packages (first run: 2 to 5 minutes).'
  run .venv/bin/python -m pip install --require-hashes -r services/company_os/requirements.runtime.lock
  run .venv/bin/python -m pip install --require-hashes -r services/company_os/requirements.build.lock
  run .venv/bin/python -m pip install --require-hashes -r packages/desktop_runtime/requirements.local.lock
  editable=(
    -e packages/core -e packages/coding_agents -e packages/huginn_watch \
    -e packages/llm_gateway -e packages/guardrails -e packages/connectors \
    -e packages/desktop_runtime -e services/company_os
  )
  if [[ -f packages/harness/pyproject.toml ]]; then editable+=(-e packages/harness); fi
  run .venv/bin/python -m pip install --no-deps --no-build-isolation "${editable[@]}"
  printf '%s\n' "$signature" > .venv/.local-dependencies
fi
directory=apps/console
signature=$(cat "$directory/package.json" "$directory/pnpm-lock.yaml" | cksum)
if [[ ! -f "$directory/node_modules/.local-dependencies" ]] || [[ "$(cat "$directory/node_modules/.local-dependencies")" != "$signature" ]]; then
  printf '%s\n' 'Installing your locked interface packages (first run: 1 to 3 minutes).'
  run "${node_command[@]}" --dir "$directory" install --frozen-lockfile --node-linker=hoisted
  printf '%s\n' "$signature" > "$directory/node_modules/.local-dependencies"
fi
if [[ "$mode" != --web ]]; then
  printf '%s\n' 'Preparing your desktop payload (first run: 1 to 3 minutes; unchanged payloads are reused).'
  run node scripts/desktop/prepare_local_stack.cjs
fi
if [[ "$mode" == --web ]]; then
  printf '%s\n' 'Starting your browser interface (about 10 seconds).'
else
  printf '%s\n' 'Starting your app (first desktop compile: 5 to 15 minutes; later starts take seconds).'
fi
trap - INT TERM ERR
set +m

PYTHONPATH="$PWD/packages:$PWD/services/company_os${PYTHONPATH:+:$PYTHONPATH}" exec .venv/bin/python - "$mode" "${node_command[@]}" <<'PYTHON'
import os
import errno
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from coding_agents.env import load_dotenv_file


def local_environment(root):
    from app.db.session import resolve_source_database_url
    from scripts.desktop_version import read_repo_version

    backend = {**load_dotenv_file(root / ".env"), **os.environ}
    backend.setdefault("OPENSSL_CONF", str(root / "scripts/desktop/openssl.cnf"))
    backend.update(SUPYMEM_MODE="local", APP_ENV="local", SUPYMEM_APP_VERSION=read_repo_version())
    data = Path(backend.get("COMPANY_OS_DATA_DIR") or backend.get("SUPYMEM_DATA_DIR") or root / "data/runtime/local").expanduser().resolve()
    backend["COMPANY_OS_DB_URL"] = resolve_source_database_url(backend, data)
    ports = {}
    for name, default in (("CORE_PORT", "3300"), ("CONSOLE_PORT", "3100"), ("WORKER_HEALTH_PORT", "3301")):
        action = f"Set {name} to a free port from 1 to 65535 in your checkout .env or terminal environment, then rerun ./start --web."
        try:
            port = int(backend.get(name) or default)
        except ValueError:
            raise ValueError(action) from None
        if not 1 <= port <= 65535:
            raise ValueError(action)
        if port in ports:
            raise ValueError(f"{name} and {ports[port]} both use port {port}. {action}")
        # shortcut: this releases the socket before launch; pass bound sockets to the children if concurrent launches race.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
                listener.listen()
            except OSError:
                raise ValueError(f"Your app cannot listen on port {port}. {action}") from None
        backend[name] = str(port)
        ports[port] = name
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    backend["COMPANY_OS_DATA_DIR"] = str(data)
    backend["COMPANY_OS_WORKING_MEMORY_PATH"] = backend.get("COMPANY_OS_WORKING_MEMORY_PATH") or str(data / "working_memory.sqlite3")
    backend["CORE_URL"] = f"http://127.0.0.1:{backend['CORE_PORT']}"
    backend["COMPANY_OS_URL"] = backend["CORE_URL"]
    backend["CONSOLE_ORIGIN"] = f"http://127.0.0.1:{backend['CONSOLE_PORT']}"
    backend["PYTHONPATH"] = f"{root / 'packages'}:{root / 'services/company_os'}"
    try:
        bridge_port = int(backend.get("BRIDGE_PORT") or 3500)
        if not 1 <= bridge_port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("Set BRIDGE_PORT to a port from 1 to 65535 in your checkout .env or terminal environment, then rerun ./start --web.") from None
    # shortcut: the port is released before spawn, pass a bound socket to the bridge if concurrent launches race.
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", bridge_port if bridge_port not in ports else 0))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            listener.bind(("127.0.0.1", 0))
        backend["BRIDGE_PORT"] = str(listener.getsockname()[1])
    backend.update(BRIDGE_AUTH_OWNER="native", BRIDGE_MODE="http", BRIDGE_TOKEN=secrets.token_hex(32),
                   BRIDGE_NATIVE_TOKEN=secrets.token_hex(32))
    console = {name: backend[name] for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "NODE_USE_SYSTEM_CA", "OPENSSL_CONF") if name in backend}
    # The console signs the owner in from the per-install secret in the data folder, so it needs the same folder and origin.
    console.update(SUPYMEM_MODE="local", CORE_URL=backend["CORE_URL"], NEXT_PUBLIC_GATEWAY_URL=backend["CONSOLE_ORIGIN"],
                   COMPANY_OS_DATA_DIR=backend["COMPANY_OS_DATA_DIR"], CONSOLE_ORIGIN=backend["CONSOLE_ORIGIN"],
                   CONSOLE_PORT=backend["CONSOLE_PORT"])
    return backend, console


def stop_children(children):
    roots = {child.pid for child in children}
    groups = set()
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True)
        parents = [tuple(map(int, row.split())) for row in result.stdout.splitlines() if len(row.split()) == 2]
        descendants = roots.copy()
        while True:
            found = {pid for pid, parent in parents if parent in descendants} - descendants
            if not found:
                break
            descendants.update(found)
        for pid in descendants - roots:
            try:
                groups.add(os.getpgid(pid))
            except ProcessLookupError:
                pass
        groups -= roots | {os.getpgrp()}
    except (OSError, subprocess.CalledProcessError):
        print("Your process list could not be read. Restore /bin/ps on macOS or run sudo apt-get install procps on Ubuntu, then rerun ./start.", file=sys.stderr)
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


def supervise(children, service=True):
    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            for child in children:
                status = child.poll()
                if status is not None:
                    if service:
                        print("A local service stopped. Check its output, then run ./start --web again.", file=sys.stderr)
                    return (status or 1) if service else status
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_children(children)


def main():
    root = Path.cwd()
    mode = sys.argv[1]
    if mode != "--web":
        command = ["cargo", "tauri",
                   "build" if mode == "--build" else "dev", "--config", "tauri.local.json", "--features", "local-stack"]
        if mode != "--build":
            command.append("--no-dev-server-wait")
        else:
            command.extend(["--bundles", "app,dmg" if sys.platform == "darwin" else os.environ.get("SUPYMEM_LINUX_BUNDLES", "appimage")])
        environment = {**os.environ, "SUPYMEM_MODE": "local", "APP_ENV": "local"}
        if mode == "--build" and sys.platform == "linux":
            environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        try:
            child = subprocess.Popen(command, env=environment, cwd=root / "src-tauri", start_new_session=True)
        except OSError:
            print("Your desktop could not start. Run ./start to repair your Rust and Tauri installation, then retry.", file=sys.stderr)
            return 1
        return supervise([child], service=False)
    try:
        backend, console = local_environment(root)
    except (ValueError, OSError) as error:
        print(str(error) if isinstance(error, ValueError) else "Set COMPANY_OS_DATA_DIR to a folder you can write to in your checkout .env, then rerun ./start --web.", file=sys.stderr)
        return 1
    if subprocess.run([sys.executable, "-m", "desktop_runtime", "--role", "core", "--maintenance"], env=backend, check=False).returncode:
        print("Your saved data could not be prepared. Follow the recovery steps above, then rerun ./start --web.", file=sys.stderr)
        return 1
    if subprocess.run([sys.executable, "-c", "from app.state import db; db()"], env=backend, check=False).returncode:
        print("Your database could not start. Check that the SQLite file in COMPANY_OS_DATA_DIR is writable, then rerun ./start --web.", file=sys.stderr)
        return 1
    commands = [
        ([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", backend["CORE_PORT"], "--no-proxy-headers"], {**backend, "APP_ROLE": "api"}, root),
        ([sys.executable, "-m", "app.worker.main"], {**backend, "APP_ROLE": "worker"}, root),
        ([sys.executable, "-m", "desktop_runtime", "--role", "bridge", "--host", "127.0.0.1", "--port", backend["BRIDGE_PORT"]], {**backend, "APP_ROLE": "api"}, root),
        ([*sys.argv[2:], "exec", "next", "dev", "--hostname", "127.0.0.1", "--port", backend["CONSOLE_PORT"]], console, root / "apps/console"),
    ]
    children = []
    try:
        for command, environment, directory in commands:
            children.append(subprocess.Popen(command, env=environment, cwd=directory, start_new_session=True))
    except (OSError, KeyboardInterrupt):
        stop_children(children)
        print("A local service could not start. Check your Python and Node installation, then restart.", file=sys.stderr)
        return 1
    print(f"Open http://127.0.0.1:{backend['CONSOLE_PORT']}/setup to connect your AI service. Press Ctrl+C to stop.")
    return supervise(children)


if __name__ == "__main__":
    raise SystemExit(main())
PYTHON
