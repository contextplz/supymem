"""Owned local Event delivery without GUI or network, including lost acknowledgements."""
import threading
from types import SimpleNamespace
import pytest
from desktop_runtime.device_resume import ResumeMonitor


class Gateway:
    path = "/api/ventures/ven_owned/device-work-orders/dwo_owned"
    execution_id, review_digest, device_id = "dex_owned", "a" * 64, "dev_owned"
    def __init__(self):
        self.signal = None
        self.authority = True
        self.acks = 0
        self.fail_ack = False
        self.requests = []
    def current(self):
        if not self.authority:
            raise PermissionError("revoked")
    def request(self, method, path, body):
        self.requests.append((method, path, body))
        if path.endswith("/human-pause"):
            return {"request_id": "nyr_owned", "pause_token": body["pause_token"], "state": "waiting"}
        if path.endswith("/human-resume"):
            return {"signal": self.signal}
        if path.endswith("/ack"):
            self.acks += 1
            if self.fail_ack:
                raise RuntimeError("lost ack")
            return {"state": "signal_accepted", "request_id": body["request_id"]}
        if path.endswith("/clear"):
            return {"state": "pause_loop_exited"}
        raise AssertionError("unexpected path")


def fixture():
    gateway = Gateway()
    pause = {"kind": "pause", "pause_kind": "captcha"}
    worker = SimpleNamespace(gateway=gateway, cancelled=threading.Event(), closed=False,
        last={"state": "needs_you", "pause_kind": "captcha", "steps": [pause]}, delivered=0)
    def continue_owned():
        worker.delivered += 1
        return worker.last["state"] == "needs_you"
    worker.continue_founder_handoff = continue_owned
    monitor = ResumeMonitor(worker)
    monitor.tick()
    gateway.signal = {"request_id": monitor.request_id, "pause_token": monitor.token,
        "execution_id": gateway.execution_id, "review_digest": gateway.review_digest, "device_id": gateway.device_id}
    return worker, gateway, monitor


def test_lost_ack_redelivery_does_not_set_owned_event_twice():
    worker, gateway, monitor = fixture()
    gateway.fail_ack = True
    with pytest.raises(RuntimeError):
        monitor.tick()
    assert worker.delivered == 1
    gateway.fail_ack = False
    monitor.tick()
    monitor.tick()
    assert worker.delivered == 1 and gateway.acks == 3


@pytest.mark.parametrize("field", ["request_id", "pause_token", "execution_id", "review_digest", "device_id"])
def test_foreign_signal_never_reaches_continue(field):
    worker, gateway, monitor = fixture()
    gateway.signal[field] = "different"
    with pytest.raises(PermissionError):
        monitor.tick()
    assert worker.delivered == 0


def test_cancel_and_revoked_authority_never_deliver():
    worker, gateway, monitor = fixture()
    worker.cancelled.set()
    monitor.tick()
    assert worker.delivered == 0
    worker.cancelled.clear()
    gateway.authority = False
    with pytest.raises(PermissionError):
        monitor.tick()
    assert worker.delivered == 0


def test_new_pause_and_auto_gate_exit_cannot_replay_previous_signal():
    worker, gateway, monitor = fixture()
    old_token = monitor.token
    worker.last = {**worker.last, "state": "acting", "resumed_after": "needs_you"}
    monitor.tick()
    assert not monitor.cleared  # local phase alone does not prove the human gate cleared
    worker.last = {"state": "needs_you", "pause_kind": "captcha", "steps": [*worker.last["steps"], {"kind": "pause", "pause_kind": "captcha"}]}
    with pytest.raises(PermissionError):
        monitor.tick()
    assert monitor.token != old_token and worker.delivered == 0


def test_pause_changes_during_authority_read_never_deliver_old_signal():
    worker, gateway, monitor = fixture()
    original = gateway.current
    calls = 0
    def current():
        nonlocal calls
        original()
        calls += 1
        if calls == 2:
            worker.last["steps"].append({"kind": "pause", "pause_kind": "login"})
    gateway.current = current
    monitor.tick()
    assert worker.delivered == 0


def test_optional_interruptible_transport_pins_stop_and_execution_deadline():
    worker, gateway, monitor = fixture()
    worker.deadline = 1234.5
    requests = []
    original = gateway.request
    def cancellable(method, path, body, *, stop, deadline):
        assert stop is worker.cancelled and deadline == worker.deadline
        requests.append((method, path))
        if path.endswith("/authority"):
            return {"current": True}
        return original(method, path, body)
    def current(*, request):
        assert request("GET", gateway.path + "/authority", None)["current"]
    gateway.cancellable_request = cancellable
    gateway.current = current
    monitor.tick()
    assert worker.delivered == 1
    assert ("GET", gateway.path + "/human-resume") in requests
    assert ("POST", gateway.path + "/human-resume/ack") in requests
