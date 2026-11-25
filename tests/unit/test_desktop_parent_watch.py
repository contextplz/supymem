import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

requires_frozen_runtime = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "packages/desktop_runtime/frozen_main.py").is_file(),
    reason="Hosted frozen runtime is not in the public local release",
)


@requires_frozen_runtime
def test_sidecar_exits_when_parent_pid_is_gone(monkeypatch):
    from desktop_runtime.frozen_main import exit_when_parent_dies

    dead = subprocess.Popen(["true"])
    dead.wait()
    monkeypatch.setenv("SUPYMEM_PARENT_PID", str(dead.pid))
    fired = threading.Event()
    exit_when_parent_dies(poll_s=0.01, exit=lambda _code: fired.set())
    assert fired.wait(2)


@requires_frozen_runtime
def test_no_watch_without_parent_pid(monkeypatch):
    from desktop_runtime.frozen_main import exit_when_parent_dies

    monkeypatch.delenv("SUPYMEM_PARENT_PID", raising=False)
    before = threading.active_count()
    exit_when_parent_dies()
    assert threading.active_count() == before


@pytest.mark.parametrize("domain", ["fleet-tools.invalid", "recipe-club.invalid"])
def test_local_console_exits_when_your_app_parent_stops(tmp_path, domain):
    server = tmp_path / "server.cjs"
    server.write_text(f"console.log({json.dumps(domain)}); setInterval(() => {{}}, 1000);\n")
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    console = subprocess.Popen(["node", str(Path(__file__).resolve().parents[2] / "scripts/desktop/local_console.cjs"), str(server)],
                               env={"PATH": os.defpath, "SUPYMEM_PARENT_PID": str(parent.pid)},
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert console.stdout.readline().strip() == domain
        parent.terminate()
        parent.wait(timeout=5)
        assert console.wait(timeout=6) == 0
    finally:
        for process in (console, parent):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("domain", ["fleet-tools.invalid", "recipe-club.invalid"])
def test_windows_parent_probe_never_sends_a_termination_signal(monkeypatch, domain):
    import ctypes

    from desktop_runtime import parent_watch

    monkeypatch.setattr(parent_watch.sys, "platform", "win32")
    monkeypatch.setattr(parent_watch.os, "kill", lambda *_args: pytest.fail("Your app parent must never receive a signal."))
    kernel = SimpleNamespace(OpenProcess=Mock(return_value=42), CloseHandle=Mock(return_value=1),
                             GetExitCodeProcess=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    for code, alive in [(259, True), (0, False)]:
        kernel.GetExitCodeProcess.side_effect = lambda _handle, pointer, exit_code=code: setattr(pointer._obj, "value", exit_code) or 1
        assert parent_watch.parent_alive(len(domain)) is alive
    assert kernel.CloseHandle.call_count == 2
    kernel.OpenProcess.return_value = None
    assert not parent_watch.parent_alive(len(domain))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    assert parent_watch.parent_alive(len(domain))
