"""Build/run an owned QA-only AppKit recipient event counter. No event tap/TCC."""
from __future__ import annotations
import json
import plistlib
import re
import subprocess
import time
import uuid
from pathlib import Path


def snapshot_valid(value, instance, pid, *, now=None):
    now = time.time() if now is None else now
    return (isinstance(value, dict) and value.get("instance_id") == instance and value.get("pid") == pid
            and type(value.get("received_input_events")) is int and value["received_input_events"] >= 0
            and type(value.get("samples")) is int and value["samples"] > 0
            and isinstance(value.get("body_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["body_sha256"]) is not None
            and type(value.get("updated_at")) in (int, float) and 0 <= now - value["updated_at"] <= 2)


def evidence(before, after, instance, pid, *, now=None):
    if not snapshot_valid(before, instance, pid, now=before.get("updated_at") if isinstance(before, dict) else now):
        return None
    if not snapshot_valid(after, instance, pid, now=now) or after["samples"] <= before["samples"]:
        return None
    return (before["received_input_events"] == after["received_input_events"] == 0
            and before["body_sha256"] == after["body_sha256"])


def public_snapshot(value, instance, pid):
    if not snapshot_valid(value, instance, pid):
        return None
    result = {key: value[key] for key in ("instance_id", "pid", "received_input_events",
                                           "samples", "updated_at", "body_sha256")}
    for key in ("active_app_pid", "monotonic_at", "is_active"):
        expected = bool if key == "is_active" else int if key == "active_app_pid" else (int, float)
        if type(value.get(key)) in (expected if isinstance(expected, tuple) else (expected,)):
            result[key] = value[key]
    return result


class InputDecoy:
    def __init__(self, root, *, frontmost=None):
        self.root = Path(root)
        self.instance = uuid.uuid4().hex
        self.app = self.root / f"decoy-{self.instance}" / "Supymem QA Decoy.app"
        self.state = self.app.parent / "state.json"
        self.process = None
        self.frontmost = frontmost

    def start(self):
        binary = self.app / "Contents" / "MacOS" / "cu-input-decoy"
        binary.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        info = {"CFBundleIdentifier": f"com.supymem.qa.decoy.{self.instance}", "CFBundleName": "Supymem QA Decoy",
                "CFBundleExecutable": binary.name, "CFBundlePackageType": "APPL",
                "NSPrincipalClass": "DecoyApplication"}
        (self.app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        source = Path(__file__).with_suffix(".swift")
        result = subprocess.run(["swiftc", str(source), "-o", str(binary)], capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("qa_decoy_compile_failed")
        self.process = subprocess.Popen([str(binary), str(self.state), self.instance],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077)
        try:
            return self.wait_for_focus(timeout=10)
        except RuntimeError:
            self.close()
            raise

    def wait_for_focus(self, *, timeout=3):
        from desktop_runtime.cu_ai_engine import frontmost_info
        read_front = self.frontmost or frontmost_info
        deadline = time.monotonic() + timeout
        consecutive, last_sample, valid_seen = 0, None, False
        while time.monotonic() < deadline:
            snap = self.read()
            valid = snapshot_valid(snap, self.instance, self.process.pid)
            valid_seen = valid_seen or valid
            if valid and snap["samples"] != last_sample:
                last_sample = snap["samples"]
                own = (snap["received_input_events"] == 0 and snap.get("is_active") is True and snap.get("active_app_pid") == self.process.pid
                       and read_front()[1] == self.process.pid)
                consecutive = consecutive + 1 if own else 0
                if consecutive >= 3:
                    return snap
            elif not valid:
                consecutive = 0
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError("qa_decoy_focus_unverified" if valid_seen else "qa_decoy_monitor_unavailable")

    def read(self):
        try:
            return json.loads(self.state.read_text())
        except (OSError, ValueError):
            return None

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
