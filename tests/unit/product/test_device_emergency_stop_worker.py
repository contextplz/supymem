"""Emergency Stop on the Mac runtime: local halt first, Core sync later, idempotent."""
import time

import pytest
from types import SimpleNamespace

from desktop_runtime.device_worker import DeviceWorker


class Gateway:
    order_id, venture_id = "dwo_one", "v_one"
    claim_attempted = False

    def __init__(self, failures=0):
        self.failures, self.calls = failures, []

    def emergency_stop(self, source):
        self.calls.append(source)
        if len(self.calls) <= self.failures:
            raise RuntimeError("core unreachable")
        return {"state": "cancelled"}


def worker(gateway, saves):
    w = DeviceWorker(gateway, saves.append, activity_current=lambda: True)
    w.STOP_SYNC_DELAYS = (0, 0, 0)
    return w


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_cancel_stops_the_owned_coding_process_tree_without_network(title):
    owner = worker(Gateway(), [])
    calls = []
    owner._product_coding = SimpleNamespace(tracker=SimpleNamespace(stop_all=lambda **kwargs: calls.append(title) or True))
    owner.cancel()
    assert owner.cancelled.is_set()
    assert calls == [title]
    assert not owner.gateway.calls


@pytest.mark.parametrize("title", ["Freight invoice reconciliation", "Garden watering journal"])
def test_cancel_kills_real_owned_descendants_in_a_temporary_workspace(tmp_path, title):
    import os
    import subprocess
    import sys
    import psutil
    from desktop_runtime.owned_processes import OwnedProcesses

    tracker = OwnedProcesses()
    instruction = ("import signal,subprocess,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(child.pid,flush=True);child.wait()")
    process = psutil.Popen([sys.executable, "-c", instruction], cwd=tmp_path, env={"PATH": os.defpath},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        tracker.register(process)
        descendant = psutil.Process(int(process.stdout.readline()))
        tracker.capture()
        owner = worker(Gateway(), [])
        owner.last["title"] = title
        owner._product_coding = SimpleNamespace(tracker=tracker)
        owner.cancel()
        process.wait(timeout=3)
        assert not descendant.is_running()
        assert tracker.stopped(timeout=.5)
    finally:
        tracker.stop_all(timeout=.5)
        process.stdout.close()


def test_stop_halts_locally_before_core_answers_and_records_a_receipt():
    saves, gateway = [], Gateway()
    w = worker(gateway, saves)
    result = w.emergency_stop("shortcut")
    assert w.cancelled.is_set() and w.stopped_by_user
    assert result["stopped_by_user"] is True
    assert saves[0]["stopped_by_user"] and saves[0]["stop_source"] == "shortcut"
    assert wait_for(lambda: w.last["core_stop_sync"] == "done")
    assert gateway.calls == ["shortcut"]


def test_stop_works_with_core_unreachable_and_retries_later():
    gateway = Gateway(failures=2)
    w = worker(gateway, [])
    w.emergency_stop("menu")
    assert w.cancelled.is_set()  # local stop never waits for Core
    assert wait_for(lambda: w.last["core_stop_sync"] == "done")
    assert len(gateway.calls) == 3


def test_core_never_reachable_leaves_the_stop_pending_but_effective():
    gateway = Gateway(failures=99)
    w = worker(gateway, [])
    w.emergency_stop("cursor")
    assert wait_for(lambda: len(gateway.calls) == 3)
    assert w.cancelled.is_set() and w.last["core_stop_sync"] == "pending"


def test_second_press_is_idempotent():
    gateway = Gateway()
    w = worker(gateway, [])
    w.emergency_stop("app")
    first = w.last["stopped_at"]
    w.emergency_stop("chat")
    assert wait_for(lambda: w.last["core_stop_sync"] == "done")
    assert w.last["stopped_at"] == first and w.last["stop_source"] == "app"
    assert gateway.calls == ["app"]


def test_journal_failure_cannot_delay_the_stop():
    def broken(_):
        raise OSError("disk")
    w = DeviceWorker(Gateway(), broken, activity_current=lambda: True)
    w.STOP_SYNC_DELAYS = (0,)
    w.emergency_stop("app")
    assert w.cancelled.is_set()


def test_bridge_stop_with_nothing_running_is_a_signed_out_safe_noop(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from desktop_runtime.bridge import create_bridge_app
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TOKEN", "t" * 40)
    monkeypatch.setenv("GATEWAY_URL", "https://gateway.test")
    monkeypatch.setenv("BRIDGE_AUTH_OWNER", "native")
    monkeypatch.setenv("BRIDGE_NATIVE_TOKEN", "native-fixture-token-" * 3)
    headers = {"x-native-token": "native-fixture-token-" * 3}
    client = TestClient(create_bridge_app())
    assert client.post("/native/emergency-stop", json={"source": "menu"}).status_code in {401, 403}
    assert client.get("/native/emergency-stop/status", headers=headers).json()["active"] is False
    assert client.post("/native/emergency-stop", headers=headers, json={"source": "menu"}).json() == {
        "stopped": False, "active": False}
