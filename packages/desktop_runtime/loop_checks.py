"""Fixed offline Node checks and loopback probes for the Vercel product profile."""
from __future__ import annotations

from core.deployment import infrastructure
import hashlib
import atexit
from base64 import b64encode
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import psutil
from core.static_qa_contracts import STATIC_MEDIA
from desktop_runtime.owned_coding import OwnedCoding, _SECRET, _trusted_git, workspace_files
from desktop_runtime.hosted import _is_local as is_local
from desktop_runtime.local_http import LoopbackHTTPRelay
from desktop_runtime.loop_diagnose import scrub
from desktop_runtime.owned_processes import OwnedProcesses

RECIPES = {"web_app_vercel_checks_v1", "web_app_static_checks_v1", "static_html_checks_v1"}
PRIVATE = {"README.md", "DEPLOY.md", "AGENTS.md", "LICENSE", "LICENSE.md", ".gitignore", ".vercelignore",
    ".supymem-venture", "CODING_CARD.md", "dev-server.mjs"}
MAX_OUTPUT = 256 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def http_journey(plan: dict | None) -> list[dict]:
    # shortcut: JSON in the approved spec, upgrade when the plan schema owns HTTP checks.
    blocks = re.findall(r"```http-journey\s*\n(.*?)\n```", (plan or {}).get("spec", ""), re.S)
    if not blocks:
        return []
    try:
        if len(blocks) != 1 or len(blocks[0].encode()) > 8000:
            raise ValueError()
        steps = json.loads(blocks[0])
        if not isinstance(steps, list) or not 1 <= len(steps) <= 6:
            raise ValueError()
        for step in steps:
            if (not isinstance(step, dict) or set(step) - {"method", "path", "status", "body", "expected_json"}
                    or not {"method", "path", "status", "expected_json"}.issubset(step)
                    or step["method"] not in {"GET", "POST"}
                    or not isinstance(step["path"], str)
                    or not re.fullmatch(r"/api/[A-Za-z0-9_-]+(?:\?[A-Za-z0-9_.~%=&+-]{1,500})?", step["path"])
                    or re.search(r"%(?![a-fA-F0-9]{2})", step["path"])
                    or type(step["status"]) is not int or not 200 <= step["status"] <= 299
                    or not isinstance(step["expected_json"], (dict, list))
                    or "body" in step and (step["method"] != "POST" or not isinstance(step["body"], (dict, list)))):
                raise ValueError()
        if all(step["path"].split("?", 1)[0] == "/api/health" for step in steps):
            raise ValueError()
        if _SECRET.search(blocks[0].encode()):
            raise ValueError()
        return steps
    except (ValueError, TypeError):
        raise ValueError("coding_http_contract_invalid") from None


def verify_http_journey(request, steps, *, authorize=lambda: True, cancelled=lambda: False,
        deadline=None) -> dict:
    probes, failures = [], []
    for index, step in enumerate(steps):
        if cancelled() or not authorize() or deadline is not None and time.monotonic() >= deadline:
            failures.append(_failure("stopped", "Your app check stopped before it finished."))
            break
        status, body = None, b""
        try:
            status, body = request(step)
            if len(body) > MAX_OUTPUT or status != step["status"]:
                raise ValueError()
            actual = json.loads(body)
            if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(step["expected_json"], sort_keys=True, allow_nan=False):
                raise ValueError()
        except (ValueError, TypeError, OSError, URLError):
            failures.append(_failure("http_journey", "Your app did not return the result in your approved plan.", test=step["path"]))
        probes.append({"index": index, "path": step["path"], "status": status,
            "body_sha256": hashlib.sha256(body).hexdigest(), "matched": not failures})
        if failures:
            break
    return {"http_journey": probes, "failures": failures,
        "functional_status": "verified" if steps and not failures else "failed" if failures else "unavailable"}


def _http_request(opener, base_url, step, timeout=1):
    body = json.dumps(step["body"], allow_nan=False).encode() if "body" in step else None
    request = Request(base_url + step["path"], data=body, method=step["method"],
        headers={"Content-Type": "application/json"} if body is not None else {})
    with opener.open(request, timeout=timeout) as response:
        return response.status, response.read(MAX_OUTPUT + 1)


def node_binary() -> str | None:
    bundled = Path(sys.executable).with_name("node.exe" if sys.platform == "win32" else "node")
    if is_local() and bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which("node") or next((str(path) for path in
        (Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node")) if path.is_file() and os.access(path, os.X_OK)), None)


def preflight() -> bool:
    if not node_binary():
        return False
    if sys.platform == "linux":
        from desktop_runtime.linux_checks import available
        return available()
    return sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def checks_next_step() -> str:
    if sys.platform == "win32":
        return "Windows cannot safely check a runnable web app yet. Ask Charlie for a static page in chat, then open Build activity to see the result."
    if not node_binary():
        return "Install Node on your laptop, then open Build activity and retry your build."
    if sys.platform == "linux":
        if not Path("/usr/bin/python3").is_file():
            return "Install Python 3 with your Linux package manager, then open Build activity and retry your build."
        return "Install bubblewrap with your Linux package manager, allow user namespaces, then open Build activity and retry your build."
    return "Use the official Node 22 runtime on your Mac and make sure sandbox-exec is available, then open Build activity and retry your build."


class _MacRelay(LoopbackHTTPRelay):
    def __init__(self, port):
        self.directory = tempfile.TemporaryDirectory(prefix="supymem-check-", dir="/tmp")
        self.path = Path(self.directory.name).resolve() / "server.sock"
        try:
            super().__init__(port, self._connect)
        except BaseException:
            self.directory.cleanup()
            raise

    def _connect(self):
        client = socket.socket(socket.AF_UNIX)
        try:
            client.settimeout(1)
            client.connect(str(self.path))
            return client
        except BaseException:
            client.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self.directory.cleanup()


def _mac_server_preload(listener):
    # The checked server keeps its PORT contract, but only the trusted relay binds TCP.
    source = f'''import {{Server}} from "node:net";
const listen = Server.prototype.listen;
Server.prototype.listen = function(first, ...args) {{
    const options = first && typeof first === "object" ? first : null;
    const port = options ? options.port : first;
    const host = options ? options.host : typeof args[0] === "string" ? args[0] : undefined;
    if (String(port) !== process.env.PORT || (host && host !== "127.0.0.1")) {{
        return listen.call(this, first, ...args);
    }}
    const callback = args.at(-1);
    return listen.call(this, {json.dumps(str(listener))}, ...(typeof callback === "function" ? [callback] : []));
}};
'''
    return "data:text/javascript;base64," + b64encode(source.encode()).decode("ascii")


def _launch(workspace, temporary, node, argv, env, output, *, readonly=False):
    if sys.platform == "darwin":
        node = str(Path(node).resolve(strict=True))
        relay = _MacRelay(int(env["PORT"])) if "PORT" in env else None
        try:
            profile = sandbox_profile(workspace, temporary, node, listener=relay.path if relay else None, readonly=readonly)
            preload = ["--import", _mac_server_preload(relay.path)] if relay else []
            process = psutil.Popen(["/usr/bin/sandbox-exec", "-p", profile, node, *preload, *argv],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
            if relay is not None:
                process.check_relay = relay
            return process
        except BaseException:
            if relay is not None:
                relay.close()
            raise
    if sys.platform != "linux":
        raise ValueError("coding_checks_unavailable")
    from desktop_runtime.linux_checks import backend, command, LoopbackRelay
    tool = backend()
    if not tool:
        raise ValueError("coding_checks_unavailable")
    if readonly:
        temporary = temporary / "scratch"
        temporary.mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(temporary), "TMPDIR": str(temporary),
        "OPENSSL_CONF": "/dev/null", **{key: env[key] for key in ("PORT", "HOST") if key in env}}
    parent, child = socket.socketpair() if "PORT" in env else (None, None)
    try:
        node = str(Path(node).resolve())
        args = command(tool, workspace, temporary, node=node, readonly=readonly)
        descriptor = 0 if child else -1
        process = psutil.Popen([*args, str(descriptor), node, *argv], cwd=workspace, env=env,
            stdin=child if child else subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
        if parent:
            try:
                process.check_relay = LoopbackRelay(parent, int(env["PORT"]))
            except Exception:
                _stop(process, None)
                raise
        return process
    except Exception:
        if parent:
            parent.close()
        raise
    finally:
        if child:
            child.close()


def sandbox_profile(workspace: Path, temporary: Path, node: str, *, listener: Path | None = None, readonly=False) -> str:
    quote = lambda value: json.dumps(str(value))
    workspace, temporary = workspace.resolve(), temporary.resolve()
    roots = [workspace, temporary, Path("/usr/lib"), Path("/System/Library"), Path("/Library/Apple/System/Library"),
        Path("/System/Volumes/Preboot/Cryptexes/OS/usr/lib"), Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld")]
    files = [Path(node).resolve(), Path("/dev/null"), Path("/dev/random"), Path("/dev/urandom"), Path("/dev/zero")]
    env_pattern = quote(r"^" + re.escape(str(workspace)) + r"/(.*/)?\.env[^/]*(/.*)?$")
    rules = ["(version 1)", "(deny default)", '(import "dyld-support.sb")', "(allow process-fork)", "(allow process-exec)",
        "(allow signal (target same-sandbox))", "(allow sysctl-read)", "(allow file-read-metadata)",
        "(allow file-read* " + " ".join([*(f"(subpath {quote(root)})" for root in roots),
            *(f"(literal {quote(path)})" for path in files)]) + ")",
        f"(allow file-write* (subpath {quote(workspace)}) (subpath {quote(temporary)}) (literal \"/dev/null\"))",
        f'(deny file-read* (regex {env_pattern}))', "(deny network*)"]
    if listener is not None:
        path = quote(listener.resolve())
        rules.extend([f"(allow network-bind network-inbound (local unix-socket (path-literal {path})))",
            f"(allow file-write* (literal {path}))"])
    if readonly:
        rules.append(f"(deny file-write* (subpath {quote(workspace)}))")
    return "\n".join(rules)


def _failure(kind: str, message: str, file="", test="", line=None) -> dict:
    return {"kind": kind, "test": test, "file": file, "line": line, "message": scrub(message)}


def source_manifest(workspace: Path) -> dict:
    files = []
    for name, data in sorted(workspace_files(workspace).items()):
        if name in PRIVATE or name.startswith(("review/", "tests/", "docs/", ".vercel/", "artifacts/")):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or any(part.startswith(".") or part.casefold() in {"node_modules", "vendor"} for part in Path(name).parts) or _SECRET.search(data):
            raise ValueError("coding_secret_detected" if _SECRET.search(data) else "coding_file_policy_invalid")
        if name == "package.json":
            try:
                if json.loads(data) != {"type": "module"}:
                    raise ValueError("coding_package_policy_invalid")
            except (ValueError, UnicodeDecodeError):
                raise ValueError("coding_package_policy_invalid") from None
        elif not (name == "Dockerfile" and is_local()) and Path(name).suffix.lower() not in STATIC_MEDIA and not (name.startswith("api/") and name.endswith(".js")):
            raise ValueError("coding_file_policy_invalid")
        files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return {"files": files, "file_manifest_sha256": hashlib.sha256(json.dumps(files,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def parse_tap(output: bytes, workspace: Path) -> dict:
    text = output.decode("utf-8", "replace")
    failures = []
    for match in re.finditer(r"(?m)^\s*not ok \d+ - (.+)$", text):
        part = text[match.end():match.end() + 2000]
        location = re.search(r"location:\s*['\"]?(.+?):(\d+)(?::\d+)?['\"]?\s*$", part, re.M)
        message = re.search(r"error:\s*(.+)", part)
        failures.append(_failure("test", scrub(message[1] if message else match[1], workspace),
            scrub(location[1], workspace) if location else "", scrub(match[1], workspace), int(location[2]) if location else None))
    passes = re.findall(r"(?m)^# pass (\d+)\s*$", text)
    fails = re.findall(r"(?m)^# fail (\d+)\s*$", text)
    return {"failures": failures, "tap_summary": {"pass": int(passes[-1]) if passes else 0,
        "fail": int(fails[-1]) if fails else len(failures)},
        "test_names": [scrub(name, workspace, 400) for name in re.findall(r"(?m)^\s*ok \d+ - (.+)$", text)]}


def _stop(process, tracker):
    if relay := getattr(process, "check_relay", None):
        relay.close()
    if tracker is not None:
        tracker.stop_all(timeout=.5)
    OwnedCoding._stop_process_group(process)
    try:
        process.wait(timeout=3)
    except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=3)


def _run_tests(workspace, tracker, authorize, cancelled, deadline, node) -> dict:
    tests = sorted(str(path.relative_to(workspace)) for path in (workspace / "tests").rglob("*")
        if path.is_file() and path.suffix in {".js", ".mjs", ".cjs"})
    if not tests:
        return {"failures": [_failure("test", "Write a named node:test check under tests/.")], "tap_summary": {"pass": 0, "fail": 0}}
    with tempfile.TemporaryDirectory(prefix="supymem-checks-") as temp, tempfile.TemporaryFile() as output:
        env = OwnedCoding._env()
        # Ignore ambient OpenSSL configuration; offline Node checks need no
        # external policy or credential-bearing config file.
        env.update(TMPDIR=temp, OPENSSL_CONF="/dev/null")
        process = _launch(workspace, Path(temp), node, ["--test", "--test-reporter=tap", *tests], env, output)
        failure, next_authority = None, 0
        try:
            tracker.register(process)
            tracker.watch_workspace(workspace)
            while process.poll() is None:
                now = time.monotonic()
                if cancelled() or now >= deadline:
                    failure = _failure("stopped" if cancelled() else "timeout", "Local checks stopped at their time limit.")
                    break
                if now >= next_authority:
                    if not authorize():
                        failure = _failure("stopped", "The permission to run these checks ended.")
                        break
                    next_authority = now + 5
                if os.fstat(output.fileno()).st_size > MAX_OUTPUT:
                    failure = _failure("output_bounds", "The test output exceeded its limit.")
                    break
                time.sleep(.1)
        finally:
            _stop(process, tracker)
        output.seek(0)
        data = output.read(MAX_OUTPUT + 1)
        result = parse_tap(data[:MAX_OUTPUT], workspace)
        if len(data) > MAX_OUTPUT:
            failure = _failure("output_bounds", "The test output exceeded its limit.")
        if failure:
            result["failures"].append(failure)
        elif process.returncode != 0 and not result["failures"]:
            result["failures"].append(_failure("build_error", checks_next_step() if sys.platform == "darwin"
                else "The local tests could not complete. Open Build activity and retry your build."))
        elif result["tap_summary"]["pass"] == 0 or not result["test_names"]:
            result["failures"].append(_failure("build_error", "Write a named node:test check before continuing."))
        return result


def _probe_server(workspace, endpoints, tracker, authorize, cancelled, deadline, node, *, journey=None) -> dict:
    failures, probes = [], []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    functional = {"functional_status": "unavailable", "http_journey": []}
    with tempfile.TemporaryDirectory(prefix="supymem-server-") as temp, tempfile.TemporaryFile() as output:
        env = OwnedCoding._env()
        env.update(TMPDIR=temp, PORT=str(port), HOST="127.0.0.1", OPENSSL_CONF="/dev/null")
        process = _launch(workspace, Path(temp), node, ["dev-server.mjs"], env, output)
        try:
            tracker.register(process)
            tracker.watch_workspace(workspace)
            ready_until = min(deadline, time.monotonic() + 8)
            for endpoint in ["/", *endpoints]:
                status, body = None, b""
                while time.monotonic() < ready_until:
                    if cancelled() or not authorize() or process.poll() is not None:
                        break
                    if os.fstat(output.fileno()).st_size > MAX_OUTPUT:
                        failures.append(_failure("output_bounds", "The local server output exceeded its limit."))
                        break
                    if (relay := getattr(process, "check_relay", None)) is not None and not relay.listening():
                        time.sleep(.1)
                        continue
                    try:
                        with opener.open(f"http://127.0.0.1:{port}{endpoint}", timeout=min(1, max(.05, deadline - time.monotonic()))) as response:
                            status, body = response.status, response.read(MAX_OUTPUT + 1)
                        break
                    except HTTPError as error:
                        status = error.code
                        break
                    except (URLError, OSError):
                        time.sleep(.1)
                if status is None:
                    failures.append(_failure("server", "Your app check did not start. Open Build activity and retry your build.", "dev-server.mjs"))
                    break
                probes.append({"path": endpoint, "status": status})
                if len(body) > MAX_OUTPUT or (endpoint in {"/", "/api/health"} and status != 200) or status >= 500 or status == 404:
                    failures.append(_failure("http", "A local page or API check failed.", test=endpoint))
                elif endpoint == "/" and not re.search(rb"<title\b[^>]*>\s*[^<]+</title>", body, re.I):
                    failures.append(_failure("html", "The home page needs a readable title.", "index.html"))
            if cancelled() or not authorize() or time.monotonic() >= deadline:
                failures.append(_failure("stopped", "The permission to run these checks ended."))
            if not failures:
                functional = verify_http_journey(lambda step: _http_request(opener, f"http://127.0.0.1:{port}", step,
                    timeout=min(1, max(.05, deadline - time.monotonic()))), journey or [],
                    authorize=authorize, cancelled=cancelled, deadline=deadline)
                failures.extend(functional.pop("failures"))
        finally:
            _stop(process, tracker)
    availability = [probe for probe in probes if probe["path"] in {"/", "/api/health"}]
    protected = len(availability) == 2 and all(probe["status"] in {401, 403} for probe in availability)
    available = len(availability) == 2 and all(probe["status"] == 200 for probe in availability)
    return {"failures": failures, "http_probes": probes, **functional,
        "availability_status": "protected" if protected else "available" if available else "unavailable"}


class _PreviewProcesses(OwnedProcesses):
    def capture(self):
        for identity, root in self._values():
            try:
                if abs(root.create_time() - identity[1]) >= .001:
                    continue
                for child in root.children(recursive=True):
                    with self._lock:
                        self.observed[(child.pid, child.create_time())] = child
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                self.complete = False
        self._capture_workspace_processes()

    def _capture_workspace_processes(self):
        workspaces, group = getattr(self, "workspaces", set()), getattr(self, "group", None)
        if not workspaces and group is None:
            return
        if group is not None:
            try:
                if (getattr(self, "group_created_at", None) is None
                        or psutil.Process(group).create_time() != self.group_created_at):
                    self.complete, group = False, None
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                self.complete, group = False, None
        try:
            for process in psutil.process_iter():
                try:
                    if process.pid == self.owner.pid:
                        continue
                    owned = group is not None and os.getpgid(process.pid) == group
                    if not owned and workspaces and process.uids().real == os.getuid():
                        cwd = Path(process.cwd()).resolve()
                        owned = any(cwd == root or root in cwd.parents for root in workspaces)
                    if owned:
                        with self._lock:
                            self.observed[(process.pid, process.create_time())] = process
                except (psutil.NoSuchProcess, ProcessLookupError):
                    continue
                except (psutil.AccessDenied, OSError):
                    self.complete = False
        except (psutil.Error, OSError):
            self.complete = False

    def stopped(self, timeout=3.0):
        if not super().stopped(timeout=timeout):
            return False
        try:
            os.killpg(self.group, 0)
        except ProcessLookupError:
            return True
        except OSError:
            self.complete = False
        return False


class OwnedProductPreview:
    def __init__(self, *, workspace: Path, build: dict, closed: dict):
        self.workspace, self.build, self.closed = workspace, build, closed
        self.tracker = None
        self.process, self.resources = None, ExitStack()
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self.receipt = {"status": "unavailable", "ready": False,
            "source_commit": build.get("source_commit"), "file_manifest_sha256": build.get("file_manifest_sha256")}

    def _source_current(self):
        return (_trusted_git(self.workspace, "rev-parse", "HEAD") == self.build["source_commit"]
            and not any(line != "!! .tmp/" for line in _trusted_git(self.workspace, "status", "--porcelain=v1",
                "--untracked-files=all", "--ignored=matching").splitlines())
            and source_manifest(self.workspace)["file_manifest_sha256"] == self.build["file_manifest_sha256"])

    def _listening(self):
        if relay := getattr(self.process, "check_relay", None):
            return relay.listening()
        return any(connection.status == psutil.CONN_LISTEN and connection.laddr.ip == "127.0.0.1"
            and connection.laddr.port == self.port for connection in self.process.net_connections(kind="tcp"))

    def start(self) -> dict:
        from core.local_diagnostics import write_error
        with self._lock:
            if self.process is not None or self._stopped.is_set():
                return dict(self.receipt)
            try:
                if (self.closed.get("processes_stopped") is not True or self.closed.get("task_verified") is not True
                        or self.closed.get("product_build") != self.build
                        or self.build.get("outcome") != "pass" or self.build.get("task_verified") is not True
                        or self.build.get("artifact_verified") is not True
                        or not re.fullmatch(r"[a-f0-9]{40}", str(self.build.get("source_commit", "")))
                        or not re.fullmatch(r"[a-f0-9]{64}", str(self.build.get("file_manifest_sha256", "")))
                        or not OwnedProcesses.receipt_stopped(self.closed.get("inventory"))):
                    write_error(None, code="coding_preview_stop_unconfirmed")
                    return dict(self.receipt)
                if not preflight():
                    write_error(None, code="coding_checks_unavailable")
                    return dict(self.receipt)
                if not self._source_current():
                    write_error(None, code="coding_source_changed")
                    self.receipt.update(status="expired", ready=False)
                    return dict(self.receipt)
                preview_root = self.workspace / ".tmp" if is_local() else None
                if preview_root is not None:
                    if preview_root.is_symlink():
                        raise ValueError("coding_changed_path_invalid")
                    preview_root.mkdir(mode=0o700, exist_ok=True)
                # Run inside the venture, but never serve its review files or repository metadata.
                temporary = Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix="supymem-preview-", dir=preview_root)))
                snapshot = temporary / "app"
                snapshot.mkdir(mode=0o700)
                rows = source_manifest(self.workspace)["files"]
                server = _trusted_git(self.workspace, "show", self.build["source_commit"] + ":dev-server.mjs", binary_output=True)
                if _SECRET.search(server):
                    raise ValueError("coding_secret_detected")
                rows = [*rows, {"path": "dev-server.mjs", "bytes": len(server), "sha256": hashlib.sha256(server).hexdigest()}]
                for row in rows:
                    blob = _trusted_git(self.workspace, "show", f"{self.build['source_commit']}:{row['path']}", binary_output=True)
                    if len(blob) != row["bytes"] or hashlib.sha256(blob).hexdigest() != row["sha256"] or _SECRET.search(blob):
                        raise ValueError("coding_source_changed")
                    target = snapshot / row["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob)
                if not self._source_current():
                    raise ValueError("coding_source_changed")
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    self.port = listener.getsockname()[1]
                self.output = self.resources.enter_context(tempfile.TemporaryFile())
                node = node_binary()
                env = OwnedCoding._env()
                env.update(TMPDIR=str(temporary), PORT=str(self.port), HOST="127.0.0.1", OPENSSL_CONF="/dev/null")
                self.tracker = _PreviewProcesses()
                launcher = self.resources.enter_context(ThreadPoolExecutor(max_workers=1))
                self.process = launcher.submit(_launch, snapshot, temporary, node, ["dev-server.mjs"],
                    env, self.output, readonly=True).result()
                if relay := getattr(self.process, "check_relay", None):
                    self.resources.callback(relay.close)
                self.tracker.group = self.process.pid
                self.tracker.group_created_at = self.process.create_time()
                atexit.register(self.stop)
                # Linux launches in a private PID namespace, even for a child that detaches.
                if sys.platform != "linux":
                    self.tracker.watch_workspace(temporary)
                self.tracker.register(self.process)
                opener = build_opener(ProxyHandler({}), _NoRedirect())
                url, ready_until = f"http://127.0.0.1:{self.port}", time.monotonic() + 8
                while time.monotonic() < ready_until:
                    if self.process.poll() is not None or os.fstat(self.output.fileno()).st_size > MAX_OUTPUT:
                        raise ValueError("coding_preview_failed")
                    try:
                        if self._listening():
                            status, body = _http_request(opener, url, {"method": "GET", "path": "/"})
                            health, health_body = _http_request(opener, url, {"method": "GET", "path": "/api/health"})
                            if status != 200 or health != 200 or max(len(body), len(health_body)) > MAX_OUTPUT or not re.search(rb"<title\b[^>]*>\s*[^<]+</title>", body, re.I):
                                raise ValueError("coding_preview_failed")
                            if self.process.poll() is None and self._source_current():
                                break
                    except (URLError, OSError):
                        pass
                    time.sleep(.1)
                else:
                    raise ValueError("coding_preview_failed")
                self.receipt.update(status="ready", ready=True, url=url, expires_at=time.time() + 600,
                    server_sha256=hashlib.sha256(server).hexdigest(), stop_available=True)
                threading.Thread(target=self._monitor, daemon=True, name=infrastructure('supymem-product-preview')).start()
            except (ValueError, OSError, psutil.Error, RuntimeError) as error:
                self.stop(status="failed", error=error)
            return dict(self.receipt)

    def _monitor(self):
        while not self._stopped.wait(1):
            try:
                if time.time() >= self.receipt["expires_at"] or not self._source_current():
                    self.stop(status="expired")
                elif self.process.poll() is not None or not self._listening() or os.fstat(self.output.fileno()).st_size > MAX_OUTPUT:
                    self.stop(status="failed")
            except (ValueError, OSError, psutil.Error) as error:
                self.stop(status="failed", error=error)

    def status(self) -> dict:
        with self._lock:
            if self.receipt["ready"]:
                try:
                    if time.time() >= self.receipt["expires_at"] or not self._source_current():
                        return self.stop(status="expired")
                    if self.process.poll() is not None or not self._listening():
                        return self.stop(status="failed")
                except (ValueError, OSError, psutil.Error) as error:
                    return self.stop(status="failed", error=error)
            return dict(self.receipt)

    def stop(self, *, status="stopped", error=None) -> dict:
        with self._lock:
            stopped = True
            if self.process is not None:
                try:
                    if sys.platform == "linux" and (relay := getattr(self.process, "check_relay", None)):
                        # EOF lets the namespace helper reap Node before forced cleanup can orphan it.
                        try:
                            relay.close()
                            self.process.wait(timeout=3)
                        except (OSError, psutil.TimeoutExpired, subprocess.TimeoutExpired):
                            pass
                    self.tracker.stop_all(timeout=.5)
                    if self.process.poll() is None:
                        self.process.kill()
                    self.process.wait(timeout=3)
                    stopped = self.tracker.stopped()
                except (OSError, psutil.Error, subprocess.TimeoutExpired) as cleanup_error:
                    error = cleanup_error
                    stopped = False
            if (status == "failed" or not stopped) and self.receipt.get("status") != "failed":
                from core.local_diagnostics import write_error
                write_error(error, code="coding_preview_failed")
            self.receipt.update(status=status if stopped else "failed", ready=False, stop_available=not stopped,
                processes_stopped=stopped)
            self.receipt.pop("url", None)
            if stopped:
                self._stopped.set()
                self.resources.close()
                atexit.unregister(self.stop)
            return dict(self.receipt)


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.missing_alt = [], False
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img" and "alt" not in values:
            self.missing_alt = True
        for key in ("src", "href", "action", "poster"):
            if values.get(key):
                self.refs.append((key, values[key]))


def _local_assets(workspace, files):
    failures = []
    for name, data in files.items():
        if name.startswith(("review/", "tests/", "docs/", ".vercel/", "artifacts/")):
            continue
        if Path(name).suffix not in {".html", ".css"}:
            continue
        text = data.decode("utf-8", "replace")
        if re.search(r"url\(\s*['\"]?(?:https?:)?//|@import\s+['\"](?:https?:)?//", text, re.I):
            failures.append(_failure("html", "Use local styles and image assets.", name))
        if not name.endswith(".html"):
            continue
        parsed = _HTML()
        parsed.feed(text)
        if parsed.missing_alt:
            failures.append(_failure("html", "An image needs alternative text.", name))
        for key, ref in parsed.refs:
            try:
                url = urlsplit(ref)
                if url.scheme or url.netloc:
                    if key == "href" and url.scheme in {"mailto", "tel"}:
                        continue
                    failures.append(_failure("html", "Use a local link or asset.", name))
                elif url.path:
                    target = unquote(url.path).lstrip("/") if url.path.startswith("/") else str(Path(name).parent / unquote(url.path))
                    if target.startswith("api/"):
                        target += ".js"
                    elif url.path.endswith("/"):
                        target = str(Path(target) / "index.html")
                    if target not in files:
                        failures.append(_failure("html", "A local link does not resolve.", name))
            except ValueError:
                failures.append(_failure("html", "A local link is invalid.", name))
    return failures


def run(recipe: str, workspace: Path, *, contract=None, tracker=None, authorize=lambda: True,
        cancelled=lambda: False, deadline=None, plan=None, tasks=None, partial=False) -> dict:
    if recipe not in RECIPES:
        raise ValueError("coding_check_recipe_unsupported")
    workspace = workspace.resolve()
    failures, result = [], {"tap_summary": {"pass": 0, "fail": 0}, "http_probes": []}
    try:
        files = workspace_files(workspace)
        source_manifest(workspace)
        if any(_SECRET.search(data) for data in files.values()):
            raise ValueError("coding_secret_detected")
    except (ValueError, OSError):
        return {"passed": False, "failures": [_failure("file_policy", "A source file violates the reviewed file policy.")], **result}
    failures.extend(_local_assets(workspace, files))
    if "index.html" not in files and not partial:
        failures.append(_failure("html", "Create an index.html page.", "index.html"))
    if recipe == "static_html_checks_v1":
        for required in (contract or {}).get("required_text", []):
            if str(required).casefold() not in files.get("index.html", b"").decode("utf-8", "replace").casefold():
                failures.append(_failure("html", "Required page text is missing.", "index.html"))
        return {"passed": not failures, "failures": failures, **result}
    if recipe == "web_app_vercel_checks_v1" and not partial:
        if files.get("package.json") is None or "dev-server.mjs" not in files or "api/health.js" not in files:
            failures.append(_failure("file_policy", "Create the reviewed package, local server and health API."))
    if not preflight():
        failures.append(_failure("checks_unavailable", checks_next_step()))
    if not authorize() or cancelled():
        failures.append(_failure("stopped", "The permission to run these checks ended."))
    cap = min(deadline or time.monotonic() + 120, time.monotonic() + 120)
    if not failures:
        result.update(_run_tests(workspace, tracker, authorize, cancelled, cap, node_binary()))
        failures.extend(result.pop("failures"))
        if not failures:
            for task in tasks or []:
                for check in task["acceptance"]:
                    if check["test_file"] not in files or scrub(check["test_name"], limit=400) not in result["test_names"]:
                        failures.append(_failure("test", "A reviewed acceptance test is missing or did not pass.",
                            check["test_file"], check["test_name"]))
        if not failures and recipe == "web_app_vercel_checks_v1" and (not partial or {"index.html", "dev-server.mjs", "api/health.js", "package.json"}.issubset(files)):
            endpoints = sorted(set(re.findall(r"/api/[A-Za-z0-9_-]+", (plan or {}).get("spec", ""))) | {"/api/health"})
            for endpoint in endpoints:
                if endpoint.lstrip("/") + ".js" not in files:
                    failures.append(_failure("api", "An API endpoint in your plan is missing.", test=endpoint))
            if not failures:
                try:
                    probe = _probe_server(workspace, endpoints, tracker, authorize, cancelled, cap, node_binary(),
                        journey=http_journey(plan))
                    failures.extend(probe.pop("failures"))
                    result.update(probe)
                except ValueError:
                    failures.append(_failure("http_journey", "Your approved plan needs a valid HTTP app check."))
    return {"passed": not failures, "failures": failures, **result}
