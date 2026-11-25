"""Two exact owned foreground fixtures for future background coexistence trials.

No GUI starts on import, construction, or prepare(). start() is an explicit
screen action, requiring the same exclusive GUI lease as the desktop suite.
Insertion is source-owned programmatic fixture text, not real user typing.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

from cu_input_decoy import snapshot_valid

SEED = "Owned user-window coexistence fixture."
ACTIONS = frozenset({"select", "insert_fixture"})
STATUSES = frozenset({"starting", "ready", "selection_pending", "selected",
                      "fixture_inserted", "refused_not_selected"})
FIELDS = frozenset({"instance_id", "pid", "received_input_events", "samples", "updated_at",
    "monotonic_at", "is_active", "active_app_pid", "body_sha256", "pointer_sha256",
    "fixture_insertions", "command_sequence", "command_status"})


def body_hash(insertions):
    if type(insertions) is not int or not 0 <= insertions <= 200:
        raise ValueError("qa_fixture_insertion_count_invalid")
    return hashlib.sha256((SEED + "q" * insertions).encode()).hexdigest()


def public_state(value, instance, pid, *, now=None):
    if (not snapshot_valid(value, instance, pid, now=now)
            or set(value) != FIELDS
            or type(value.get("fixture_insertions")) is not int
            or not 0 <= value["fixture_insertions"] <= 200
            or type(value.get("command_sequence")) is not int
            or not 0 <= value["command_sequence"] <= 200
            or type(value.get("command_status")) is not str
            or value.get("command_status") not in STATUSES
            or type(value.get("is_active")) is not bool
            or type(value.get("active_app_pid")) is not int or value["active_app_pid"] <= 0
            or type(value.get("monotonic_at")) not in (int, float)
            or not math.isfinite(value["monotonic_at"]) or value["monotonic_at"] <= 0
            or type(value.get("pointer_sha256")) is not str
            or len(value["pointer_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in value["pointer_sha256"])):
        return None
    return {key: value[key] for key in FIELDS}


class OwnedUserWindows:
    def __init__(self, root):
        self.root = Path(tempfile.mkdtemp(prefix="owned-user-windows-", dir=root))
        self.root.chmod(0o700)
        self.root_identity = self._identity()
        self.instances = [uuid.uuid4().hex, uuid.uuid4().hex]
        self.processes = []
        self.sequences = [0, 0]
        self.receipts = []
        self.prepared = False
        self.closed = False
        self.cleanup_verified = None
        self.overflow = False

    def _identity(self):
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("qa_fixture_directory_custody_unknown")
        return info.st_dev, info.st_ino

    def _live(self):
        try:
            same = self._identity() == self.root_identity
        except OSError:
            same = False
        if self.closed or not same:
            raise ValueError("qa_fixture_directory_custody_changed")

    def _path(self, slot, name):
        if type(slot) is not int or slot not in {0, 1}:
            raise ValueError("qa_fixture_slot_invalid")
        return self.root / self.instances[slot] / name

    def prepare(self):
        self._live()
        if self.prepared:
            raise ValueError("qa_fixture_already_prepared")
        binary = self.root / "fixture-binary"
        source = Path(__file__).with_name("cu_owned_user_window.swift")
        if source.is_symlink():
            raise ValueError("qa_fixture_source_custody_unknown")
        result = subprocess.run(["swiftc", str(source), "-o", str(binary)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise RuntimeError("qa_fixture_compile_failed")
        for slot, instance in enumerate(self.instances):
            app = self._path(slot, "Supymem owned user fixture.app")
            executable = app / "Contents" / "MacOS" / "cu-owned-user-window"
            executable.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
            shutil.copyfile(binary, executable)
            executable.chmod(0o700)
            info = {"CFBundleIdentifier": "com.supymem.qa.userwindow." + instance,
                    "CFBundleName": "Supymem owned user fixture",
                    "CFBundleExecutable": executable.name, "CFBundlePackageType": "APPL",
                    "NSPrincipalClass": "UserWindowApplication"}
            (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        self.prepared = True
        return {"fixture_count": 2, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "input_method": "source_owned_programmatic_fixture_insert", "gui_started": False}

    def start(self):
        self._live()
        if not self.prepared or self.processes:
            raise ValueError("qa_fixture_start_not_admitted")
        try:
            for slot in (0, 1):
                binary = self._path(slot, "Supymem owned user fixture.app") / "Contents" / "MacOS" / "cu-owned-user-window"
                process = subprocess.Popen([str(binary), str(self._path(slot, "state.json")),
                    str(self._path(slot, "control.json")), self.instances[slot], str(slot)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077)
                self.processes.append(process)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(self.read(slot) is not None for slot in (0, 1)):
                    return
                if any(process.poll() is not None for process in self.processes):
                    break
                time.sleep(.05)
            raise RuntimeError("qa_fixture_start_unconfirmed")
        except BaseException:
            self.close()
            raise

    def read(self, slot):
        self._live()
        if len(self.processes) <= slot:
            return None
        fd = None
        try:
            fd = os.open(self._path(slot, "state.json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 8192):
                return None
            data = os.read(fd, 8193)
            if len(data) > 8192:
                return None
            value = json.loads(data)
            return public_state(value, self.instances[slot], self.processes[slot].pid)
        except (OSError, ValueError):
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def issue(self, slot, action, *, sample=lambda: None, timeout=2):
        self._live()
        if (type(action) is not str or action not in ACTIONS or len(self.processes) != 2
                or not self.prepared or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 5):
            raise ValueError("qa_fixture_command_not_admitted")
        if len(self.receipts) >= 200:
            self.overflow = True
            raise ValueError("qa_fixture_receipt_limit")
        self._path(slot, "control.json")  # Validate before indexing or any effect.
        self.sequences[slot] += 1
        sequence = self.sequences[slot]
        command = {"instance_id": self.instances[slot], "sequence": sequence,
                   "action": action, "issued_at": time.time()}
        receipt = {"instance_id": self.instances[slot], "target_pid": self.processes[slot].pid,
                   "sequence": sequence, "action": action, "at_monotonic": time.monotonic(),
                   "outcome": "unconfirmed"}
        self.receipts.append(receipt)
        temporary = self._path(slot, "control-" + uuid.uuid4().hex + ".json")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            data = json.dumps(command, separators=(",", ":")).encode()
            if os.write(fd, data) != len(data):
                raise RuntimeError("qa_fixture_command_write_unconfirmed")
        finally:
            os.close(fd)
        os.replace(temporary, self._path(slot, "control.json"))
        deadline = time.monotonic() + timeout
        expected = "selected" if action == "select" else "fixture_inserted"
        while time.monotonic() < deadline:
            sample()
            state = self.read(slot)
            if state is not None and state["command_sequence"] == sequence:
                if state["command_status"] == expected:
                    receipt.update(outcome="confirmed", ended_at_monotonic=time.monotonic())
                    return state
                if state["command_status"] == "refused_not_selected":
                    receipt.update(outcome="refused", ended_at_monotonic=time.monotonic())
                    raise RuntimeError("qa_fixture_command_refused")
            if self.processes[slot].poll() is not None:
                break
            time.sleep(.025)
        receipt["ended_at_monotonic"] = time.monotonic()
        raise RuntimeError("qa_fixture_command_unconfirmed")  # Do not replay.

    def close(self):
        if self.closed:
            if self.cleanup_verified is not True:
                raise RuntimeError("qa_owned_user_fixture_cleanup_unconfirmed")
            return
        uncertain = False
        for process in self.processes:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                uncertain = True
        self.closed = True
        self.cleanup_verified = not uncertain
        if uncertain:
            raise RuntimeError("qa_owned_user_fixture_cleanup_unconfirmed")
