"""Start/attach a dedicated Chrome over CDP without stealing focus.

Why a dedicated instance: since Chrome 136, --remote-debugging-port is ignored on
Chrome's default user-data-dir, so the user's everyday Chrome cannot be attached
(and their live profile is never exposed on a port). Instead we run a second Chrome
on 127.0.0.1 with a Supymem-managed profile *copy* (cookies/sign-ins copied once;
the macOS "Chrome Safe Storage" keychain key is per-user so copied cookies decrypt).
Trade-offs: the copy is a snapshot (re-copy to refresh sign-ins); no saved passwords,
history or extensions are copied; the copy holds live session cookies, so it lives
under the app data dir with 0700 perms.
"""
from __future__ import annotations

import json
import errno
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from desktop_runtime.cu_cleanup import darwin_group_members
from urllib.parse import urlparse

CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/154.0.0.0 Safari/537.36")
CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
# Only what keeps a session signed in; deliberately no "Login Data" (saved passwords).
COPY_ITEMS = ("Cookies", "Network/Cookies", "Preferences", "Secure Preferences",
              "Local Storage", "Session Storage", "IndexedDB")


def find_profile_dir(email: str, chrome_root: Path = CHROME_ROOT) -> str | None:
    """Profile directory name (e.g. 'Profile 1') whose signed-in account is `email`."""
    try:
        cache = json.loads((chrome_root / "Local State").read_text())["profile"]["info_cache"]
    except (OSError, KeyError, ValueError):
        return None
    for name, info in cache.items():
        if str(info.get("user_name", "")).lower() == email.lower():
            return name
    return None


def prepare_profile_copy(email: str, dest: Path, chrome_root: Path = CHROME_ROOT) -> Path:
    """Snapshot the signed-in profile's session state into dest (a user-data-dir)."""
    name = find_profile_dir(email, chrome_root)
    if not name:
        raise LookupError(f"no Chrome profile signed in as {email}")
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    shutil.copy2(chrome_root / "Local State", dest / "Local State")
    for item in COPY_ITEMS:
        src = chrome_root / name / item
        if not src.exists():
            continue
        out = dest / "Default" / item
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, out, dirs_exist_ok=True)
        else:
            shutil.copy2(src, out)
    return dest


_SINGLETON = ("SingletonLock", "SingletonSocket", "SingletonCookie")
SEEDED_MARKER = ".supymem_seeded"


def persistent_profile_dir(data_dir: Path, account_id: str) -> Path:
    """Per-account Supymem Chrome profile under the app data dir (0700); survives sessions so sign-ins persist."""
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account_id):
        raise ValueError("browser_profile_account_invalid")
    path = Path(data_dir) / "profiles" / account_id
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for part in (path.parent, path):
        os.chmod(part, 0o700)
    return path


def release_stale_singleton(profile: Path) -> None:
    """Drop Chrome's singleton files left by a crashed run; refuse while their owner pid is alive or unknowable."""
    lock = profile / "SingletonLock"
    if lock.is_symlink():  # target is "<host>-<pid>"
        try:
            pid = int(os.readlink(lock).rsplit("-", 1)[1])
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except (OSError, ValueError, IndexError):
            raise RuntimeError("browser_profile_in_use") from None
        else:
            raise RuntimeError("browser_profile_in_use")
    for name in _SINGLETON:
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ChromeSession:
    """Owns one dedicated Chrome process; exposes its CDP endpoint."""

    def __init__(self, user_data_dir: Path, *, headless: bool = True, port: int | None = None,
                 chrome_bin: str = CHROME_BIN):
        self.user_data_dir, self.headless, self.chrome_bin = Path(user_data_dir), headless, chrome_bin
        self.port, self.user_agent = port or _free_port(), USER_AGENT
        self.proc: subprocess.Popen | None = None
        self._group = None
        self._owned_process = self._owned_group = None
        self._group_probe = {}
        self._group_permission_unconfirmed = False
        self._group_gone = False
        self.closed = False
        self.cleanup_trace = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def args(self) -> list[str]:
        a = [self.chrome_bin, f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={self.user_data_dir}", "--no-first-run", "--lang=en-US", "--no-default-browser-check",
             "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
             "--disable-backgrounding-occluded-windows", "--disable-blink-features=AutomationControlled",
             "--disable-features=BackForwardCache", "--window-size=1280,900"]
        if self.headless:  # default headless UA says "HeadlessChrome", which search engines challenge
            a.append("--user-agent=" + self.user_agent)
        a.append("about:blank")
        # headless never shows a window; otherwise start minimized so it opens behind the user's work
        a.insert(1, "--headless=new" if self.headless else "--start-minimized")
        return a

    # Readiness is polled (success returns at once; Popen exit fails at once), so the budget only
    # bounds a truly dead launch. 15 s was shorter than a cold Chrome start on a CPU-starved
    # 2-vCPU CI runner, which surfaced as a flaky browser_start_timeout.
    def start(self, timeout: float = 45.0) -> str:
        if self.proc is not None or self.closed:
            raise RuntimeError("browser_lifecycle_invalid")
        if self._listener_alive():
            raise RuntimeError("browser_endpoint_in_use")
        profile = self.user_data_dir.resolve()
        if profile == CHROME_ROOT.resolve() or CHROME_ROOT.resolve() in profile.parents:
            raise RuntimeError("browser_profile_not_owned")
        if any((profile / name).exists() or (profile / name).is_symlink()
               for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")):
            raise RuntimeError("browser_profile_in_use")
        profile.mkdir(parents=True, exist_ok=True)
        profile.chmod(0o700)
        # Direct ownership in both modes. Headful/minimized launch may still
        # activate on macOS and must not be advertised as background verified.
        self.proc = subprocess.Popen(self.args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, umask=0o077)
        self._group = self.proc.pid
        self._owned_process, self._owned_group = self.proc, self._group
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError("browser_start_exited")
                try:
                    value = self._read_version(min(1, max(.01, deadline - time.monotonic())))
                    endpoint = urlparse(value.get("webSocketDebuggerUrl", ""))
                    if (endpoint.scheme == "ws" and endpoint.hostname == "127.0.0.1"
                            and endpoint.port == self.port and endpoint.path.startswith("/devtools/browser/")):
                        return self.endpoint
                except (OSError, ValueError, TypeError):
                    pass
                time.sleep(.05)
            raise RuntimeError("browser_start_timeout")
        except BaseException:
            try:
                self.stop()
            except Exception:
                raise RuntimeError("browser_start_cleanup_unconfirmed") from None
            raise

    def reveal(self) -> bool:
        """Raise this session's own window. Only ever call from the founder's explicit click (never the harness)."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.endpoint + "/json/list", timeout=2) as response:
            pages = [t for t in json.loads(response.read(1 << 20)) if t.get("type") == "page"]
        if not pages:
            return False
        with opener.open(f"{self.endpoint}/json/activate/{pages[0]['id']}", timeout=2):
            return True

    def _read_version(self, timeout):
        # Never proxy loopback CDP or read an unbounded response into a receipt.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.endpoint + "/json/version", timeout=timeout) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("browser_version_response_invalid")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("browser_version_response_invalid")
        return value

    def _cleanup_stage(self, stage, fn, *args):
        from desktop_runtime.cu_cleanup import public_cleanup_error
        at = time.monotonic()
        row = {"stage": stage, "at_monotonic": at, "owned_group": self._group}
        try:
            result = fn(*args)
            row["outcome"] = "completed" if result is None else "confirmed" if result else "unconfirmed"
            return result
        except Exception as error:
            row.update(outcome="error", **public_cleanup_error(error))
            raise
        finally:
            row.update(duration_ms=min(60000, max(0, (time.monotonic() - at) * 1000)), group_retired=self._group_gone)
            if stage == 'group_wait':
                row.update(self._group_probe)
            self.cleanup_trace.append(row)
            self.cleanup_trace = self.cleanup_trace[-32:]

    def stop(self) -> None:
        if self.closed or self._group is None:
            return  # Knowing a port or borrowed process is never kill authority.
        try:
            if not self._group_gone:
                self._cleanup_stage("group_signal", self._signal_group, signal.SIGTERM)
                self._cleanup_stage("parent_wait", self._reap_parent)
                if not self._cleanup_stage("group_wait", self._wait_group, 2):
                    self._cleanup_stage("group_signal", self._signal_group, signal.SIGKILL)
                    self._cleanup_stage("parent_wait", self._reap_parent)
                    if not self._cleanup_stage("group_wait", self._wait_group, 2):
                        raise RuntimeError("browser_close_unconfirmed")
                self._group_gone = True  # Never signal a retired/reused group on retry.
            if self.proc.poll() is None or self._cleanup_stage("listener_probe", self._listener_alive):
                raise RuntimeError("browser_close_unconfirmed")
        except Exception:
            error = RuntimeError("browser_close_unconfirmed")
            error.cleanup_trace = list(self.cleanup_trace)
            raise error from None
        self.closed = True

    def _signal_group(self, sig):
        if self._group_gone or self._group_permission_unconfirmed:
            return  # EPERM quarantines signal authority; read-only proof may retry.
        try:
            os.killpg(self._group, sig)
        except ProcessLookupError:
            self._group_gone = True  # Retire immediately; never probe or signal a reused group.
        except PermissionError as error:
            if error.errno == errno.EPERM:
                self._group_permission_unconfirmed = True
            raise  # Signal denial is not proof of empty membership.

    def _group_alive(self):
        self._group_probe = {}
        if self._group_gone:
            return False
        if self._group_permission_unconfirmed:
            return self._prove_owned_group_membership()
        try:
            os.killpg(self._group, 0)
            return True
        except ProcessLookupError:
            self._group_gone = True
            return False
        except PermissionError as error:
            if error.errno != errno.EPERM:
                self._group_probe = {'group_probe_reason': 'probe_permission_unavailable'}
                raise
            self._group_permission_unconfirmed = True
            return self._prove_owned_group_membership()

    def _prove_owned_group_membership(self):
        if (self.proc is None or self.proc is not self._owned_process
                or self._group != self._owned_group or self.proc.pid != self._owned_group):
            self._group_probe = {'group_probe_reason': 'ownership_unconfirmed'}
            raise RuntimeError('browser_group_unconfirmed')
        if self.proc.poll() is None:
            self._group_probe = {'group_probe_reason': 'parent_not_exited'}
            raise RuntimeError('browser_group_unconfirmed')
        try:
            members = darwin_group_members(self._group)
        except Exception:
            self._group_probe = {'group_probe_reason': 'kernel_inventory_unavailable'}
            raise
        self._group_probe = {'group_probe_reason': 'kernel_group_nonempty' if members else 'kernel_group_empty',
                             'group_member_count': min(len(members), 4096)}
        if members:
            return True  # Read-only wait; no further signal authority.
        try:
            listener = self._listener_alive()
        except Exception:
            self._group_probe['group_probe_reason'] = 'listener_unknown'
            raise
        if listener is not False:
            self._group_probe['group_probe_reason'] = 'listener_present'
            raise RuntimeError('browser_group_unconfirmed')
        self._group_gone = True
        return False

    def _wait_group(self, timeout):
        if self._group_gone:
            return True
        deadline = time.monotonic() + timeout
        while True:
            if not self._group_alive():
                return True
            if time.monotonic() >= deadline:
                if self._group_permission_unconfirmed:
                    raise RuntimeError('browser_group_unconfirmed')
                return False
            time.sleep(.05)

    def _reap_parent(self):
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # Still owned; group escalation follows.

    def _listener_alive(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                return True
        except OSError as error:
            if error.errno == errno.ECONNREFUSED:
                return False
            raise RuntimeError("browser_listener_unknown") from None
