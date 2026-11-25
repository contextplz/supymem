"""Boolean-only status from the founder's installed CLI, never auth files."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import dropwhile
from pathlib import Path
from typing import NamedTuple

import psutil

from core.artifact_policy import directory_fd, read_file, replace_file
from desktop_runtime.owned_coding import (OwnedCoding, _CLAUDE_MIN_VERSION, _CODEX_LOOP_MIN_VERSION,
    _PROVIDER_BINARIES, _SYSTEM_PATH, _executable, _provider_fallbacks, _safe_location, _windows_git_identity, executable_identity,
    coding_connection_message, is_local, launch_identity, opencode_cli_environment, opencode_supports_builds,
    provider_which, verified_launch)
from desktop_runtime.paths import data_root
from desktop_runtime.pipe_reader import PipeReader


_TOOLS = {"cursor", "opencode"}
_BREW_PACKAGES = {"codex": "codex", "claude": "claude-code", "cursor": "cursor-cli"}
_NATIVE_PACKAGES = {"claude": "claude", "cursor": "cursor-agent"}
_NPM_PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code"}
_STATUS_PROVIDERS = ("claude", "codex", "cursor", "opencode")
_STATUS_RULE_VERSION = 2
_STATUS_LOCK = threading.Lock()
_STATUS_POOL = ThreadPoolExecutor(max_workers=1)
_STATUS_KEY = None
_STATUS_FUTURE = None
_STATUS_CHECKED_AT = 0.0
_STATUS_VALUES = None
_PROBE_TIMEOUT_SECONDS = 5
_PROBE_LOCK = threading.Lock()
_PROBES = set()
_PROBES_CLOSED = False


def start_probes() -> None:
    global _PROBES_CLOSED
    with _PROBE_LOCK:
        _PROBES_CLOSED = False


def _stop_probe(process) -> None:
    try:
        if sys.platform == "win32":
            # shortcut: only reachable descendants can be found here; use a job object if probes can detach children.
            for child in reversed(process.children(recursive=True)):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, psutil.NoSuchProcess):
        pass


def stop_probes() -> None:
    global _PROBES_CLOSED
    with _PROBE_LOCK:
        _PROBES_CLOSED = True
        for process in _PROBES:
            _stop_probe(process)


atexit.register(stop_probes)


def tool_binary(provider: str, path_env: str | None = None, *, launcher: bool = False) -> str | None:
    if provider not in _TOOLS:
        return None
    names = ("cursor-agent", "agent") if provider == "cursor" else ("opencode",)
    for name in names:
        if binary := provider_which(name, path_env, launcher=launcher):
            return binary
        if path_env is not None:
            continue
        for directory in (Path.home() / ".local/bin", Path.home() / ".cursor/bin",
                          Path.home() / ".npm-global/bin", Path.home() / ".yarn/bin",
                          Path.home() / "node_modules/.bin",
                          Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            if binary := _executable(directory / name, launcher=launcher):
                return binary
    if provider == "opencode" and path_env is None:
        return _executable(Path.home() / ".opencode/bin/opencode", launcher=launcher)
    return None


def provider_binary(provider: str, path_env: str | None = None) -> str | None:
    if provider in _TOOLS:
        return tool_binary(provider, path_env, launcher=True)
    if provider not in _PROVIDER_BINARIES:
        return None
    binary = provider_which(_PROVIDER_BINARIES[provider], path_env, launcher=True)
    if binary:
        return binary
    return next(filter(None, (_executable(path, launcher=True) for path in _provider_fallbacks(provider))), None)


def _pin_name(provider: str) -> str:
    if provider not in _PROVIDER_BINARIES and provider not in _TOOLS:
        raise ValueError("coding_provider_not_yet_supported")
    return f"coding-tools/{provider}.json"


class _Pin(NamedTuple):
    identity: tuple
    launcher: str | None
    install: tuple | None


def _read_pin(provider: str) -> _Pin | None:
    root, name = data_root(), _pin_name(provider)
    path = root / name
    if not path.exists() and not path.is_symlink():
        return None
    value = json.loads(read_file(root, name))
    if value is None:
        return None
    if isinstance(value, list):
        identity, launcher, install = value, None, None
    elif isinstance(value, dict) and value.keys() == {"identity", "launcher", "install"}:
        identity, launcher, install = value["identity"], value["launcher"], value["install"]
        if not isinstance(launcher, str) or not Path(launcher).is_absolute():
            raise ValueError("coding_provider_not_installed")
    else:
        raise ValueError("coding_provider_not_installed")
    def valid_identity(parts):
        return (isinstance(parts, list) and len(parts) == 6 and isinstance(parts[0], str)
                and all(type(item) is int for item in parts[1:]))

    lengths = (6, 8) if sys.platform == "win32" else (6, 7)
    if (not isinstance(identity, list) or not valid_identity(identity[:6]) or len(identity) not in lengths
            or not all(valid_identity(part) for part in identity[6:])):
        raise ValueError("coding_provider_not_installed")
    identity[6:] = [tuple(part) for part in identity[6:]]
    if install is not None and (not isinstance(install, list) or len(install) != 5 or not isinstance(install[0], str)
            or any(type(item) is not int for item in install[1:])):
        raise ValueError("coding_provider_not_installed")
    return _Pin(tuple(identity), launcher, tuple(install) if install is not None else None)


def provider_pin(provider: str) -> tuple | None:
    pin = _read_pin(provider)
    return pin.identity if pin is not None else None


def _trusted_install(provider: str, launcher: Path, target: Path) -> tuple | None:
    if os.name != "posix" or launcher.parent.name != "bin" or ".." in launcher.parts:
        return None
    prefix = launcher.parent.parent
    roots = []
    if package := _BREW_PACKAGES.get(provider):
        roots.extend(prefix / layout / package for layout in ("Caskroom", "Cellar"))
    if package := _NATIVE_PACKAGES.get(provider):
        roots.append(prefix / "share" / package / "versions")
    root = next((root for root in roots if root in target.parents), None)
    if root is None and (package := _NPM_PACKAGES.get(provider)) and prefix / "lib/node_modules" / package in target.parents:
        root = prefix
    if root is None:
        return None
    owners = {0, os.geteuid()}
    link = launcher.lstat()
    if not stat.S_ISLNK(link.st_mode) or link.st_uid not in owners:
        return None
    # shortcut: require a direct link to the target; check every hop if an updater starts using linked directories.
    destination = Path(os.readlink(launcher))
    if (".." in dropwhile(lambda part: part == "..", destination.parts)
            or Path(os.path.normpath(launcher.parent / destination)) != target):
        return None
    if not _safe_location(launcher):
        return None
    anchor = root.lstat()
    return str(root), link.st_uid, anchor.st_dev, anchor.st_ino, anchor.st_uid


def _pin_candidate(provider: str, discovered: str | None = None) -> tuple[_Pin | None, tuple | None]:
    pin = _read_pin(provider)
    if pin is None:
        return None, None
    launcher = pin.launcher or pin.identity[0]
    if pin.launcher is not None:
        selected = provider_binary(provider) if discovered is None else discovered
        if selected and selected != pin.launcher:
            raise ValueError
    if is_local() and not _safe_location(launcher):
        if sys.platform == "win32":
            raise ValueError("coding_provider_unsafe_location")
        raise ValueError
    runtime = pin.identity[6] if len(pin.identity) > 6 else None
    identity = launch_identity(launcher, runtime=runtime) if is_local() else executable_identity(launcher)
    if identity[6:] != pin.identity[6:]:
        raise ValueError
    if pin.install is not None and _trusted_install(provider, Path(launcher), Path(identity[0])) != pin.install:
        raise ValueError
    if identity != pin.identity and (pin.install is None or identity[0] == pin.identity[0]):
        raise ValueError
    return pin, identity


@contextmanager
def _pin_write_lock():
    root = data_root(create=True)
    if os.name != "posix":
        yield root
        return
    import fcntl

    with directory_fd(root) as parent:
        lock = os.open(".coding-tools.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("coding_provider_changed")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield root
        finally:
            os.close(lock)


def _verify_update(provider: str, pin: _Pin, identity: tuple, discovered: str | None = None) -> dict:
    status = _status(provider, expected=identity, include_version=True)
    if (status.get("available") is not True or not status.get("version")
            or _pin_candidate(provider, discovered) not in ((pin, identity), (pin._replace(identity=identity), identity))):
        raise ValueError
    return status


def build_provider_pin(provider: str, *, fixed: tuple | None = None) -> tuple[tuple | None, str | None]:
    try:
        pin, identity = _pin_candidate(provider)
        if fixed is not None and identity is not None and identity != fixed:
            raise ValueError
        if pin is None or identity == pin.identity:
            return identity, None
        status = _verify_update(provider, pin, identity)
        updated = pin._replace(identity=identity)
        with _pin_write_lock() as root:
            current = _pin_candidate(provider)
            if current == (updated, identity):
                return identity, None
            if current != (pin, identity):
                raise ValueError
            replace_file(root, _pin_name(provider), json.dumps(updated._asdict()).encode())
        return identity, status["version"]
    except (OSError, ValueError, RuntimeError) as error:
        if sys.platform == "win32" and str(error) == "coding_provider_unsafe_location":
            raise
        raise ValueError("coding_provider_changed") from None


def _approval_token(binary: str, identity: tuple) -> str:
    return hashlib.sha256(json.dumps((binary, identity)).encode()).hexdigest()


def pin_provider(provider: str, allow: bool = True, *, candidate: str | None = None) -> tuple | None:
    name = _pin_name(provider)
    if not allow:
        path = data_root() / name
        if path.parent.exists() or path.parent.is_symlink():
            with _pin_write_lock(), directory_fd(path.parent) as parent:
                if os.name == "nt":
                    (parent / path.name).unlink(missing_ok=True)
                else:
                    try:
                        os.unlink(path.name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        return None
    binary = provider_binary(provider)
    if not binary:
        raise ValueError("coding_provider_not_installed")
    local = is_local()
    if local and not _safe_location(binary):
        raise ValueError("coding_provider_unsafe_location")
    try:
        identity = launch_identity(binary) if local else executable_identity(binary)
        if candidate is not None and candidate != _approval_token(binary, identity):
            raise ValueError("coding_provider_changed")
        install = _trusted_install(provider, Path(binary), Path(identity[0]))
        field = "available" if provider in _PROVIDER_BINARIES else "subscription_login"
        status = _status(provider, binary, expected=identity)
        if sys.platform == "win32" and status.get("unsafe_location"):
            raise ValueError("coding_provider_unsafe_location")
        if status.get("build_blocked_message"):
            raise ValueError("coding_git_not_installed")
        if (status.get(field) is not True or executable_identity(binary) != identity[:6]
                or _trusted_install(provider, Path(binary), Path(identity[0])) != install):
            raise ValueError
        if sys.platform == "win32" and local and launch_identity(
                binary, runtime=identity[6] if len(identity) == 8 else None) != identity:
            raise ValueError("coding_provider_changed")
    except (OSError, ValueError, RuntimeError) as error:
        if sys.platform == "win32" and str(error) in {"coding_provider_unsafe_location", "coding_provider_changed", "coding_git_not_installed"}:
            raise
        raise ValueError("coding_sign_in_required") from None
    payload = json.dumps(_Pin(identity, binary, install)._asdict()).encode()
    with _pin_write_lock() as root:
        replace_file(root, name, payload)
    return identity


def _probe(binary: str, arguments: list[str], *, tracker=None, cwd: Path | None = None,
           expected: tuple | None = None, opencode=False) -> tuple[int, bytes]:
    with tempfile.TemporaryDirectory(prefix="supymem-probe-", dir="/tmp" if os.name != "nt" else None) as temporary:
        temporary = str(Path(temporary).resolve())
        env = OwnedCoding._env()
        if sys.platform == "win32":
            env["PATH"] = os.pathsep.join((str(Path(binary).parent), env["PATH"]))
        else:
            env["PATH"] = os.pathsep.join(dict.fromkeys((*_SYSTEM_PATH, str(Path(binary).parent))))
        env.update(TMPDIR=temporary, TMP=temporary, TEMP=temporary)
        if opencode:
            env = opencode_cli_environment(env, Path(temporary))
        return _run_probe(binary, arguments, tracker, cwd or Path(temporary), env, expected)


def _run_probe(binary, arguments, tracker, cwd, env, expected) -> tuple[int, bytes]:
    with _PROBE_LOCK:
        if _PROBES_CLOSED:
            raise ValueError("coding_status_unavailable")
        if sys.platform == "win32":
            identity = expected if expected is not None else launch_identity(binary)
        else:
            identity = executable_identity(binary)
            if expected is not None and identity != expected[:6]:
                raise ValueError("coding_status_unavailable")
        launcher = binary if sys.platform == "win32" else identity[0]
        with verified_launch([launcher, *arguments], expected or identity, env, cwd=cwd) as (argv, launch):
            process = psutil.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=sys.platform != "win32", **launch)
        _PROBES.add(process)
    output, deadline = bytearray(), time.monotonic() + _PROBE_TIMEOUT_SECONDS
    try:
        if tracker is not None:
            tracker.register(process)
        with PipeReader() as reader:
            reader.add(process.stdout, output)
            while reader.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("coding_status_unavailable")
                event = reader.read(min(.05, remaining))
                if event is not None:
                    _destination, chunk = event
                    if len(chunk) + len(output) > 8192:
                        raise ValueError("coding_status_unavailable")
                    output.extend(chunk)
        code = process.wait(timeout=max(.01, deadline - time.monotonic()))
        if _PROBES_CLOSED:
            raise ValueError("coding_status_unavailable")
        return code, bytes(output)
    finally:
        with _PROBE_LOCK:
            try:
                _stop_probe(process)
                process.wait(timeout=.5)
                _PROBES.discard(process)
            finally:
                process.stdout.close()


def opencode_logins(output: bytes) -> tuple[list[str], list[str]]:
    login = re.sub(rb"\x1b\[[0-9;]*m", b"", output).decode("utf-8", "strict")
    rows = re.findall(r"^[│┃●•\s]*([^\r\n]+?)\s+(oauth|api)[ \t]*$", login, re.MULTILINE)
    # shortcut: only built-in OAuth providers are admitted; extend when a new built-in login can be verified.
    known = {"openai": "openai", "anthropic": "anthropic", "github copilot": "github-copilot"}
    return [method for _, method in rows], [known[label] for name, method in rows
        if method == "oauth" and (label := name.strip().lower()) in known]


def _status(provider: str, binary: str | None = None, *, expected: tuple | None = None,
            include_version: bool = False) -> dict:
    status = {"name": provider, "installed": False, "signed_in": False,
              "subscription_login": False, "available": False}

    def unsafe_status():
        message = coding_connection_message("coding_provider_unsafe_location", provider)
        return {**status, "unsafe_location": True, **({"unsafe_location_message": message} if message else {})}

    def build_readiness(status):
        if sys.platform == "win32" and is_local() and status["available"]:
            try:
                _windows_git_identity()
            except ValueError as error:
                status.update(available=False, build_blocked_message=coding_connection_message(str(error), provider))
        return status

    try:
        if expected is None and is_local():
            changed = False
            try:
                pin, expected = _pin_candidate(provider, binary)
                if pin is not None and expected != pin.identity:
                    verified = _verify_update(provider, pin, expected, binary)
                    verified.pop("version")
                    return build_readiness(verified)
            except (OSError, ValueError, RuntimeError):
                expected, changed = None, True
            if expected is None:
                binary = provider_binary(provider) if binary is None else binary
                status.update(installed=bool(binary), changed=changed)
                if not binary:
                    return status
                if not _safe_location(binary):
                    return unsafe_status()
                if provider not in _PROVIDER_BINARIES:
                    return status
                try:
                    identity = launch_identity(binary)
                except (OSError, ValueError, RuntimeError):
                    return unsafe_status()
                return {**status, "candidate_path": identity[0], "candidate": _approval_token(binary, identity)}
        if expected is not None:
            binary = expected[0]
        elif binary is None:
            binary = provider_binary(provider)
        if not binary:
            return status
        status["installed"] = True
        expected = expected if expected is not None else (
            launch_identity(binary) if sys.platform == "win32" else executable_identity(binary))

        def probe(arguments):
            options = {"opencode": True} if provider == "opencode" else {}
            return _probe(binary, arguments, expected=expected, **options)

        # CLI output stays in memory only and never enters a receipt, log or API.
        code, output = probe(["--version"])
        patterns = {"claude": rb"^(\d+)\.(\d+)\.(\d+)\b",
                    "codex": rb"^codex-cli (\d+)\.(\d+)\.(\d+)\b",
                    "cursor": rb"^(?:cursor-agent )?(\d{4})\.(\d{2})\.(\d{2})\b",
                    "opencode": rb"^(?:opencode )?(\d+)\.(\d+)\.(\d+)[ \t]*\r?$"}
        pattern = patterns[provider]
        versions = re.findall(pattern, output, re.MULTILINE)
        if code != 0 or len(versions) != 1:
            return status
        if provider not in _TOOLS and tuple(map(int, versions[0])) < (
                _CLAUDE_MIN_VERSION if provider == "claude" else _CODEX_LOOP_MIN_VERSION):
            return status
        if provider in _TOOLS:
            arguments = ["status"] if provider == "cursor" else ["auth", "list"]
            code, output = probe(arguments)
            if code != 0:
                return status
            login = re.sub(rb"\x1b\[[0-9;]*m", b"", output).decode("utf-8", "strict").strip()
            if provider == "cursor":
                if login.startswith("{"):
                    parsed = json.loads(login)
                    if not isinstance(parsed, dict):
                        return status
                    status["signed_in"] = parsed.get("isAuthenticated") is True
                    status["subscription_login"] = (status["signed_in"]
                        and parsed.get("hasAccessToken") is True and parsed.get("hasRefreshToken") is True)
                else:
                    lines = [re.sub(r"^[✓✔●•\s]+", "", line) for line in login.splitlines() if line.strip()]
                    logins = [line for line in lines if re.fullmatch(r"Logged in as [^\s@]+@[^\s@]+", line)]
                    status["subscription_login"] = (len(logins) == 1
                        and all(line == "Cursor Agent Status" or line in logins for line in lines))
                    status["signed_in"] = status["subscription_login"]
            else:
                methods, oauth_providers = opencode_logins(output)
                status["signed_in"] = bool(methods)
                status["subscription_login"] = bool(methods) and all(method == "oauth" for method in methods)
        elif provider == "codex":
            code, output = probe(["login", "status"])
            login = output.strip()
            status["subscription_login"] = code == 0 and login == b"Logged in using ChatGPT"
            status["signed_in"] = status["subscription_login"] or (code == 0
                and b"\n" not in login and b"\r" not in login and login.startswith(b"Logged in using an API key"))
        else:
            code, output = probe(["auth", "status"])
            if code != 0:
                return status
            parsed = json.loads(output)
            if not isinstance(parsed, dict):
                return status
            status["signed_in"] = parsed.get("loggedIn") is True
            status["subscription_login"] = (status["signed_in"]
                                               and parsed.get("authMethod") == "claude.ai")
        status["available"] = (status["subscription_login"] and provider in _PROVIDER_BINARIES
            and (provider != "cursor" or cursor_supports_builds(binary, expected=expected))
            and (provider != "opencode" or opencode_supports_builds(tuple(map(int, versions[0])))
                and len(oauth_providers) == len(methods)))
        if status["available"] and include_version:
            status["version"] = b".".join(versions[0]).decode("ascii")
    except (OSError, ValueError, RuntimeError, psutil.TimeoutExpired, subprocess.TimeoutExpired) as error:
        if sys.platform == "win32" and str(error) == "coding_provider_unsafe_location":
            return unsafe_status()
    return status if include_version else build_readiness(status)


def cursor_supports_builds(binary: str, *, tracker=None, expected: tuple | None = None) -> bool:
    code, output = _probe(binary, ["--help"], tracker=tracker, expected=expected)
    options = re.findall(rb"(?m)^[ \t]*(?:-\w,[ \t]*)?--sandbox[ \t]+([^\r\n]+)", output)
    if code != 0 or len(options) != 1 or not re.search(rb"(?m)^[ \t]*--workspace[ \t]+<[^>\r\n]+>", output):
        return False
    # shortcut: help is prose, refuse unknown wording until Cursor exposes machine-readable options.
    description = options[0].strip()
    if re.fullmatch(rb"<mode>[ \t]+(?:Explicitly )?enable or disable sandbox mode\.?", description, re.I):
        return True
    choices = re.fullmatch(rb"<mode>[ \t]+(?:(?:Sandbox mode|Explicitly enable or disable sandbox mode)[ \t]+)?"
        rb"\(choices:[ \t]*([^)]*)\)(?:[ \t]+\(default:[ \t]*['\"]?(?:enabled|disabled)['\"]?\))?", description, re.I)
    return choices is not None and sorted(option.strip(b" \t'\"").lower()
        for option in choices[1].split(b",")) == [b"disabled", b"enabled"]


def _scan_statuses(path_env: str | None = None) -> list[dict]:
    """Check providers concurrently; detection does not authorize any task."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        if path_env is None:
            return list(pool.map(_status, _STATUS_PROVIDERS))
        def check(provider):
            return _status(provider, provider_binary(provider, path_env) or "")
        return list(pool.map(check, _STATUS_PROVIDERS))


def _display_identity(binary: str) -> tuple:
    try:
        return binary, Path(binary).lstat().st_ctime_ns, executable_identity(binary)
    except (OSError, ValueError, RuntimeError):
        return (binary,)


def statuses(*, refresh: bool = False, path_env: str | None = None) -> list[dict]:
    """Keep display verdicts in this process only; execution uses uncached _status."""
    # shortcut: display readiness can lag a 30-second refresh, use explicit refresh when checking a changed login.
    global _STATUS_KEY, _STATUS_FUTURE, _STATUS_CHECKED_AT, _STATUS_VALUES
    root = data_root()
    local = is_local()
    identities = []
    for provider in _STATUS_PROVIDERS:
        binaries = [provider_binary(provider, path_env)]
        try:
            pin = _read_pin(provider) if local else None
        except (OSError, ValueError, RuntimeError):
            pin = None
        if pin is not None:
            binaries.extend([pin.launcher or pin.identity[0], *(identity[0] for identity in pin.identity[6:])])
        identities.append((pin, tuple(_display_identity(binary) for binary in dict.fromkeys(binaries) if binary)))
    version = os.environ.get("SUPYMEM_APP_VERSION") or os.environ.get("SUPYMEM_VERSION")
    key = (version, _STATUS_RULE_VERSION, local, path_env, os.environ.get("PATH", ""),
           Path.home(), root, tuple(identities))
    with _STATUS_LOCK:
        if key != _STATUS_KEY:
            _STATUS_KEY, _STATUS_FUTURE, _STATUS_VALUES = key, None, None
        if _STATUS_FUTURE is not None and _STATUS_FUTURE.done():
            completed, _STATUS_FUTURE = _STATUS_FUTURE, None
            _STATUS_VALUES = completed.result()
            _STATUS_CHECKED_AT = time.monotonic()
        if refresh or _STATUS_VALUES is None or time.monotonic() - _STATUS_CHECKED_AT >= 30:
            if _STATUS_FUTURE is None:
                _STATUS_FUTURE = (_STATUS_POOL.submit(_scan_statuses) if path_env is None
                                  else _STATUS_POOL.submit(_scan_statuses, path_env))
        if _STATUS_VALUES is not None and not refresh:
            return [dict(value) for value in _STATUS_VALUES]
        pending = _STATUS_FUTURE
    values = pending.result()
    with _STATUS_LOCK:
        if _STATUS_KEY == key and _STATUS_FUTURE is pending:
            _STATUS_VALUES, _STATUS_FUTURE = values, None
            _STATUS_CHECKED_AT = time.monotonic()
    return [dict(value) for value in values]
