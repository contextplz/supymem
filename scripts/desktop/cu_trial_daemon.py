"""QA-only direct-child ownership. Production CuaSession never owns its host daemon."""
from __future__ import annotations

import ctypes  # noqa: F401 - retained QA kernel-test injection seam
import errno
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import sys  # noqa: F401 - retained QA platform-test injection seam
import time
from pathlib import Path


def darwin_group_members(group):
    # The packaged helper uses only Python stdlib/libproc, no company/provider
    # dependencies. Keep this legacy QA diagnostic and injection boundary.
    from desktop_runtime.cu_cleanup import darwin_group_members as inventory
    try:
        return inventory(group)
    except Exception:
        raise RuntimeError('qa_group_inventory_unavailable') from None


class TrialDaemon:
    def __init__(self):
        self.directory = None
        self.process = None
        self.process_group = None
        self._group_gone = False
        self._owned_group_identity = None
        self.closed = False
        self.close_trace = []
        self._group_probe = {}

    def _close_stage(self, stage, operation, expected=None):
        start = time.monotonic()
        outcome = "error"
        diagnostic = {}
        try:
            value = operation()
            outcome = "completed" if expected is None else "confirmed" if value is expected else "unconfirmed"
            return value
        except BaseException as error:
            from desktop_runtime.cu_cleanup import public_cleanup_error
            diagnostic = public_cleanup_error(error)
            raise
        finally:
            self.close_trace.append({"stage": stage, "at_monotonic": start,
                "duration_ms": round(1000 * max(0, time.monotonic() - start), 1), "outcome": outcome,
                "owned_group": self.process_group, "group_retired": self._group_gone, **self._group_probe, **diagnostic})
            del self.close_trace[:-32]

    def start(self, binary: str, *, timeout: float = 8) -> str:
        if self.directory is not None or self.closed:
            raise RuntimeError("qa_daemon_lifecycle_invalid")
        self.directory = Path(tempfile.mkdtemp(prefix="supymem-cua-qa-", dir="/private/tmp"))
        self.directory.chmod(0o700)
        socket = self.directory / "driver.sock"
        try:
            self.process = subprocess.Popen(
                [binary, "serve", "--socket", str(socket), "--no-overlay"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077, start_new_session=True,
                env={**os.environ, "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"})
            # Popen created this session; independently confirm its native
            # identity while the direct child is live, never from a reused PID.
            if (os.getpgid(self.process.pid), os.getsid(self.process.pid)) != (self.process.pid, self.process.pid):
                raise RuntimeError("qa_daemon_group_identity_unconfirmed")
            self.process_group = self.process.pid
            self._owned_group_identity = (self.process.pid, self.process.pid, self.process.pid)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("qa_daemon_start_failed")
                if socket.is_socket():
                    return str(socket)  # CuaSession additionally verifies the MCP handshake.
                time.sleep(0.05)
            raise RuntimeError("qa_daemon_start_timeout")
        except BaseException:
            try:
                self.close()
            except Exception:
                raise RuntimeError("qa_daemon_start_cleanup_unconfirmed") from None
            raise

    def close(self):
        if self.closed:
            return
        process = self.process
        self.close_trace = []
        try:
            if self.process_group is not None and not self._group_gone:
                # serve can fork. Parent wait alone is not a daemon-death proof.
                # This group was created only by this owner's direct child;
                # borrowed/production sessions never supply a process group.
                if self._close_stage("group_wait", self._group_alive, False):
                    self._close_stage("group_wait", lambda: self._signal_group(signal.SIGTERM))
                self._close_stage("parent_wait", self._reap_parent, True)
                if not self._close_stage("group_wait", lambda: self._wait_group(2), True):
                    self._close_stage("group_wait", lambda: self._signal_group(signal.SIGKILL))
                    self._close_stage("parent_wait", self._reap_parent, True)
                    if not self._close_stage("group_wait", lambda: self._wait_group(2), True):
                        raise RuntimeError("qa_daemon_close_unconfirmed")
                self._group_gone = True  # Never signal a retired/reused group on retry.
            elif self.process_group is None and process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process is not None and not self._close_stage("parent_wait", lambda: process.poll() is not None, True):
                raise RuntimeError("qa_daemon_close_unconfirmed")
            if self._close_stage("listener_probe", self._listener_alive, False):
                raise RuntimeError("qa_daemon_close_unconfirmed")
        except Exception:
            # Keep the owned directory and child handle when death is unproved.
            raise RuntimeError("qa_daemon_close_unconfirmed") from None
        if self.directory is not None:
            self._close_stage("dir_cleanup", lambda: shutil.rmtree(self.directory))
        self.closed = True

    def _group_alive(self):
        self._group_probe = {}
        if self._group_gone:
            return False
        try:
            os.killpg(self.process_group, 0)
            return True
        except ProcessLookupError:
            self._group_gone = True
            return False
        except PermissionError as error:
            # Darwin can report EPERM during retirement. It proves neither
            # existence nor absence. Require independent exact membership and
            # the positively exited original direct child before retirement.
            if error.errno != errno.EPERM:
                self._group_probe = {"group_probe_reason": "probe_permission_unavailable"}
                raise
            if (self.process is None or self._owned_group_identity !=
                    (self.process.pid, self.process_group, self.process_group)):
                self._group_probe = {"group_probe_reason": "ownership_unconfirmed"}
                raise
            if self.process.poll() is None:
                self._group_probe = {"group_probe_reason": "parent_not_exited"}
                raise
            try:
                members = darwin_group_members(self.process_group)
            except Exception:
                self._group_probe = {"group_probe_reason": "kernel_inventory_unavailable"}
                raise
            self._group_probe = {"group_probe_reason": "kernel_group_nonempty" if members else "kernel_group_empty",
                                 "group_member_count": min(len(members), 4096)}
            if members and os.environ.get("CU_DAEMON_DEBUG"):
                shown = subprocess.run(["/bin/ps", "-o", "pid=,ppid=,stat=,comm=", "-p", ",".join(str(m) for m in members[:16])],
                                       capture_output=True, text=True).stdout
                print("DAEMON_GROUP_MEMBERS", self.process_group, shown, file=sys.stderr, flush=True)
            if members:
                raise  # Includes possible recycled/unowned group; never signal.
            self._group_gone = True
            return False

    def _signal_group(self, sig):
        if self._group_gone:
            return
        try:
            os.killpg(self.process_group, sig)
        except ProcessLookupError:
            self._group_gone = True  # Retire immediately; never probe a reused ID.

    def _listener_alive(self):
        if self.directory is None or not (self.directory / "driver.sock").exists():
            return False
        # A fork that escapes its original group must not leave an accepting
        # owned listener hidden by parent/group death. No protocol/tool request.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.directory / "driver.sock"))
            except OSError as error:
                if error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                    return False
                raise RuntimeError("qa_daemon_close_unconfirmed") from None
        return True

    def _wait_group(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if not self._group_alive():
                    return True
            except PermissionError as error:
                # EPERM with a kernel-confirmed non-empty owned group is a member still exiting (a zombie answers
                # EPERM to killpg): keep polling until the deadline instead of failing the whole close.
                if self._group_probe.get("group_probe_reason") != "kernel_group_nonempty" or error.errno != errno.EPERM:
                    raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _reap_parent(self):
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # Group escalation also owns this parent.
        return self.process.poll() is not None


def owned_caller_close(session, daemon=None):
    """Attempt both routes, including daemon teardown when the proxy close fails.

    Omitting daemon closes only the borrowed proxy; it never signals another
    process or removes another socket directory.
    """
    def close():
        from desktop_runtime.cu_cleanup import CleanupError
        codes = []
        try:
            session.close()
        except Exception:
            codes.append("qa_proxy_close_unconfirmed")
        try:
            if daemon is not None:
                daemon.close()
        except Exception:
            codes.append("qa_daemon_close_unconfirmed")
        from desktop_runtime.cu_cleanup import public_cleanup_trace
        close.cleanup_trace = public_cleanup_trace(getattr(daemon, "close_trace", None))
        if codes:
            error = CleanupError("qa_backend_close_unconfirmed", codes)
            error.cleanup_trace = close.cleanup_trace
            raise error from None
    close.cleanup_trace = []
    return close
