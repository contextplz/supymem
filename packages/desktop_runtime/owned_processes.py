"""Track this executor's descendants without targeting app names or global PIDs."""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

import psutil


class OwnedProcesses:
    def __init__(self):
        self.owner = psutil.Process(os.getpid())
        self.baseline = {(child.pid, child.create_time()) for child in self.owner.children(recursive=True)}
        self.observed = {}
        self.complete = True
        self.started_at = time.time()
        self.workspaces = set()
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch = None

    def register(self, child):
        """Register a process object created directly by this executor."""
        try:
            if self.owner.pid not in {parent.pid for parent in child.parents()}:
                raise ValueError("owned_process_parent_mismatch")
            identity = child.pid, child.create_time()
            if identity in self.baseline:
                raise ValueError("owned_process_preexisting")
            with self._lock:
                self.observed[identity] = child
            self._start_watch()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self.complete = False
            raise ValueError("owned_process_identity_unconfirmed") from None

    def watch_workspace(self, workspace):
        """Track only this reviewed folder, including ordinary detached dev servers."""
        self.workspaces.add(Path(workspace).resolve())
        self._start_watch()

    def _start_watch(self):
        with self._lock:
            if self._watch is not None and self._watch.is_alive():
                return
            self._watch_stop.clear()
            self._watch = threading.Thread(target=self._monitor, daemon=True,
                name="supymem-owned-processes")
            self._watch.start()

    def _monitor(self):
        while not self._watch_stop.wait(.1):
            self.capture()

    def _values(self):
        with self._lock:
            return list(self.observed.items())

    def capture(self):
        parents = self._values()
        if parents:
            children = []
            for (pid, created), parent in parents:
                try:
                    if parent.create_time() == created:
                        children.extend(parent.children(recursive=True))
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied:
                    self.complete = False
        else:
            try:
                children = self.owner.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.complete = False
                return
        for child in children:
            try:
                identity = child.pid, child.create_time()
                if identity not in self.baseline:
                    with self._lock:
                        self.observed[identity] = child
            except psutil.NoSuchProcess:
                # A vanished child cannot keep writing; other live children are
                # still inventoried instead of invalidating every normal race.
                continue
            except psutil.AccessDenied:
                self.complete = False

    def _capture_workspace_processes(self):
        if not self.workspaces:
            return
        windows = os.name == "nt"
        for process in psutil.process_iter(["pid", "create_time", "username" if windows else "uids"]):
            try:
                if process.pid == self.owner.pid:
                    continue
                identity = process.pid, process.create_time()
                if windows:
                    if process.username() != self.owner.username():
                        continue
                elif process.uids().real != os.getuid():
                    continue
                if identity in self.baseline or identity[1] < self.started_at - .1:
                    continue
                cwd = Path(process.cwd()).resolve()
                if any(cwd == workspace or workspace in cwd.parents for workspace in self.workspaces):
                    if windows and identity not in self.observed:
                        # shortcut: a Windows folder cannot prove detached ancestry, use job containment before adopting unknown processes.
                        self.complete = False
                        continue
                    with self._lock:
                        self.observed[identity] = process
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

    def quiesce(self, timeout=1.0):
        """Freeze owned descendants before stopping their parent can orphan them.

        New-session tool children are discovered while their ancestry still
        exists. Re-enumerate after each suspension until no live descendant
        remains unfrozen; incomplete access never earns a stopped receipt.
        """
        deadline, frozen = time.monotonic() + timeout, set()
        while True:
            self.capture()
            pending = [(identity, process) for identity, process in self._values()
                       if identity not in frozen]
            if not pending:
                return self.complete
            if time.monotonic() >= deadline or len(self.observed) > 512:
                self.complete = False
                return False
            for identity, process in pending:
                try:
                    if abs(process.create_time() - identity[1]) < .001:
                        process.suspend()
                    frozen.add(identity)
                except psutil.NoSuchProcess:
                    frozen.add(identity)
                except psutil.AccessDenied:
                    self.complete = False
                    return False

    def stopped(self, timeout=3.0):
        self.capture()
        if not self.observed or not self.complete:
            return False
        try:
            _, alive = psutil.wait_procs([process for _, process in self._values()], timeout=timeout)
            return not alive
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return False

    @staticmethod
    def receipt_stopped(inventory):
        """Verify that every exact recorded PID identity is gone or reused."""
        if (not isinstance(inventory, dict) or inventory.get("inventory_complete") is not True
                or not isinstance(inventory.get("processes"), list)
                or not inventory["processes"]):
            return False
        for identity in inventory["processes"]:
            if (not isinstance(identity, dict) or type(identity.get("pid")) is not int
                    or not isinstance(identity.get("created_at"), (int, float))):
                return False
            try:
                process = psutil.Process(identity["pid"])
                if abs(process.create_time() - float(identity["created_at"])) < .001:
                    return False
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                return False
        return True

    def stop_all(self, timeout=2.0):
        """Stop captured descendants, including children that created a new session."""
        self._capture_workspace_processes()
        self.quiesce()

        def alive():
            values = []
            for (pid, created), process in self._values():
                try:
                    if abs(process.create_time() - created) < .001 and process.is_running():
                        values.append(process)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return values

        def signal_processes(processes, *, force=False):
            for process in processes:
                try:
                    if (process.pid, process.create_time()) not in self.observed:
                        continue
                    if force: process.kill()
                    else: process.terminate()
                except (ProcessLookupError, psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        remaining = alive()
        signal_processes(remaining)
        deadline = time.monotonic() + timeout
        while remaining and time.monotonic() < deadline:
            time.sleep(.05)
            self._capture_workspace_processes()
            remaining = alive()
        if remaining:
            signal_processes(remaining, force=True)
        result = self.stopped(timeout=1.0)
        self.close()
        return result

    def close(self):
        self._watch_stop.set()
        if self._watch is not None and self._watch is not threading.current_thread():
            self._watch.join(timeout=.5)

    def receipt(self):
        # Identity only, never process environment, command line or file paths.
        return {"inventory_complete": self.complete,
                "processes": [{"pid": pid, "created_at": created}
                              for (pid, created), _ in sorted(self._values(), key=lambda item: item[0])]}
