from types import SimpleNamespace

from desktop_runtime.device_worker import DeviceWorker
import pytest


@pytest.mark.parametrize("changing_readout, expected_effects", [(False, 4), (True, 5)])
def test_desktop_action_history_detects_only_stalled_cycles_with_changing_control_ids(
        monkeypatch, changing_readout, expected_effects):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Example", "com.example.Example", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, observations = [], [], []
    sequence = ["Advance", "Next", "Advance", "Next", "Advance"]

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                count = len(captures)
                # A fresh leading control changes both action targets' c IDs.
                elements = ([{"id": f"elem_extra_{count}", "role": "button", "label": "Other",
                             "enabled": True, "has_point": True, "actionable": True}]
                            if count % 2 == 0 else [])
                elements += [{"id": f"elem_{count}_{label}", "role": "button", "label": label,
                              "enabled": True, "has_point": True, "actionable": True}
                             for label in ("Advance", "Next")]
                elements.append({"id": f"readout_{count}", "role": "text", "label":
                                 f"Progress {count}" if changing_readout else "Progress 0",
                                 "readout": True, "enabled": True, "has_point": False,
                                 "actionable": False})
                return {"snapshot_id": f"ps1_{count}", "elements": elements}
            effects.append(arguments)
            return {"effect": "confirmed"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_generic", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_generic"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            observations.append(observation)
            label = sequence[len(observations) - 1]
            control = next(c for c in observation.controls if c.label == label)
            return Candidate("click", control.id, observation.digest())
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Advance the visible sequence", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background",
            "max_steps": 5, "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == expected_effects
    assert (next(c.id for c in observations[0].controls if c.label == "Advance")
            != next(c.id for c in observations[1].controls if c.label == "Advance"))
    assert "Dispatched effects so far" in observations[4].context
    assert "click 'Advance' ok" in observations[4].context
    assert "click 'Next' ok" in observations[4].context
    assert "elem_" not in observations[4].context
    assert worker.last["outcome"] == ("step_limit_reached" if changing_readout else "repeated_action_stopped")


def test_desktop_task_refuses_changed_process_before_claim(monkeypatch):
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False

        def read(self):
            return {"state": "approved", "manifest": {
                "applications": ["com.google.Chrome"], "origins": [],
                "actions": ["desktop.read", "desktop.interact"],
                "duration_seconds": 60, "instruction": "Search the web",
                "desktop_verification": {"operation": "desktop_goal_v1",
                    "application_bundle": "com.google.Chrome", "process_id": 101,
                    "process_start_identity": "123456", "window_id": 300,
                    "window_title": "Search", "delivery_mode": "background", "max_steps": 4,
                    "reviewed_text": ["pets"]}}}

        def claim(self):
            raise AssertionError("Changed app must never claim authority")

    monkeypatch.setattr(peekaboo_driver.PeekabooDriver, "running_apps",
        classmethod(lambda cls: [AppChoice("Google Chrome", "com.google.Chrome", 101, "999999")]))
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run()
    assert worker.last["state"] == "not_started"
    assert worker.last["outcome"] == "reviewed_window_changed_before_start"
    assert worker.last["failure_code"] == "desktop_app_identity_changed"


def test_desktop_task_refuses_changed_window_before_claim(monkeypatch):
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Calculator", "com.apple.calculator", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 301, "on_screen": True}]))

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None

        def claim(self):
            raise AssertionError("Changed window must never claim authority")

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read Calculator", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Calculator", "delivery_mode": "background",
            "max_steps": 4, "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert worker.last["state"] == "not_started"
    assert worker.last["outcome"] == "reviewed_window_changed_before_start"
    assert worker.last["failure_code"] == "desktop_window_identity_changed"
    assert worker.last["evidence"]["steps"] == []


def _off_space_setup(monkeypatch, mode):
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Calculator", "com.apple.calculator", 101, "123456")
    calls = []
    monkeypatch.setattr(peekaboo_driver, "_binary", lambda _value=None: "/bin/echo")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": False}]))
    monkeypatch.setattr(PeekabooDriver, "focus_reviewed_app", lambda self: calls.append("focus"))

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None

        def claim(self):
            calls.append("claim")
            raise RuntimeError("stop after claim")

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read Calculator", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Calculator", "delivery_mode": mode,
            "max_steps": 4, "reviewed_text": []}}
    return DeviceWorker(Gateway(), lambda _receipt: None), manifest, calls


def test_off_space_reviewed_window_is_not_brought_forward_before_claim(monkeypatch):
    worker, manifest, calls = _off_space_setup(monkeypatch, "foreground_fallback")
    worker.run_desktop({"state": "approved"}, manifest)
    assert calls == ["claim"]
    assert worker.last["outcome"] != "reviewed_window_changed_before_start"


def test_off_space_window_stays_refused_without_foreground_review(monkeypatch):
    worker, manifest, calls = _off_space_setup(monkeypatch, "background")
    worker.run_desktop({"state": "approved"}, manifest)
    assert calls == []
    assert worker.last["outcome"] == "reviewed_window_changed_before_start"


@pytest.mark.parametrize("refusal", ["capability", "claim", "authority"])
@pytest.mark.parametrize("cua", [False, True])
def test_native_start_checks_current_core_admission_before_native_effects(monkeypatch, refusal, cua):
    from desktop_runtime import cua_driver, peekaboo_driver
    from desktop_runtime.device_gateway import WorkOrderGateway, UpstreamRejected
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    worker, manifest, _ = _off_space_setup(monkeypatch, "foreground_fallback")
    calls = []
    app = AppChoice("Calculator", "com.apple.calculator", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    class Driver:
        def __init__(self, **_kwargs): calls.append("driver")
        def focus_reviewed_app(self): calls.append("focus")
        def __call__(self, _arguments, *, session_id):
            raise AssertionError("Refused authority must never capture or interact")
        def close(self): calls.append("close")
    monkeypatch.setattr(peekaboo_driver, "PeekabooDriver", Driver)
    monkeypatch.setattr(Driver, "running_apps", classmethod(lambda cls: [app]), raising=False)
    monkeypatch.setattr(Driver, "running_windows", classmethod(
        lambda cls, _app: [{"window_id": 300, "on_screen": False}]), raising=False)
    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)
    saved = {"id": "dwo_mac", "venture_id": "ven_ours", "device_id": "dev_ours",
             "state": "approved", "review_digest": "a" * 64}
    def request(method, path, body):
        if path.endswith("/capabilities"):
            calls.append("capability")
            return {"available": refusal != "capability"}
        if path.endswith("/claim"):
            calls.append("claim")
            if refusal == "claim":
                raise UpstreamRejected(409, "device_desktop_execution_unavailable")
            saved.update(state="claimed", execution_id="dex_test")
            return saved.copy()
        if path.endswith("/authority"):
            calls.append("authority")
            return {"current": False}
        if path.endswith("/stop-receipt"):
            calls.append("stop")
            return {"state": "stopped"}
        return saved.copy()
    worker.gateway = WorkOrderGateway(venture_id="ven_ours", order_id="dwo_mac",
                                     device_id="dev_ours", request=request)
    if cua:
        worker.cua_socket = "/private/tmp/supymem-cua-test/driver.sock"
        worker.cua_host_pid = 1234
    worker.run_desktop({"state": "approved"}, manifest)
    assert "focus" not in calls
    assert worker.last["task_verified"] is False
    if refusal != "authority":
        assert "driver" not in calls and "stop" not in calls
        assert worker.last["state"] == "not_started"
    else:
        assert calls.index("claim") < calls.index("driver")
        assert "close" in calls and "stop" in calls
    if refusal == "capability":
        assert worker.last["outcome"] == "execution_unavailable"


def test_reviewed_foreground_fallback_recaptures_sparse_background_window(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Example", "com.example.Example", 101, "123456")
    calls = []
    monkeypatch.setattr(peekaboo_driver, "_binary", lambda _value=None: "/bin/echo")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    def capture(self):
        calls.append("capture")
        assert self._allow_reviewed_foreground is True
        elements = ([{"id": "elem_1", "role": "button", "label": "Back", "enabled": True,
                     "has_point": True, "actionable": True},
                    {"id": "ocr_1", "role": "OCRText", "label": "Search", "enabled": True,
                     "has_point": True, "confidence": 0.5, "actionable": False}]
                    if calls.count("capture") == 1 else
                    [{"id": "elem_1", "role": "textField", "label": "Search", "enabled": True,
                      "has_point": True, "actionable": True}])
        return {"snapshot_id": "ps1_abc", "elements": elements}
    monkeypatch.setattr(PeekabooDriver, "capture", capture)
    monkeypatch.setattr(PeekabooDriver, "focus_reviewed_app", lambda self: calls.append("focus"))

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_mac"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            calls.append("decide")
            return Candidate("blocked", None, observation.digest())
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read the selected app", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Search", "delivery_mode": "foreground_fallback",
            "max_steps": 4, "reviewed_text": ["example.com"]}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert calls == ["capture", "focus", "capture", "decide"]
    assert worker.last["outcome"] == "attention_required"


def test_desktop_readout_without_controls_can_propose_done_but_is_not_verified(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Example", "com.example.Example", 101, "123456")
    monkeypatch.setattr(peekaboo_driver, "_binary", lambda _value=None: "/bin/echo")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    monkeypatch.setattr(PeekabooDriver, "capture", lambda self: {
        "snapshot_id": "ps1_abc", "elements": [{"id": "elem_1", "role": "StaticText",
            "label": "Result: 4", "enabled": True, "has_point": False,
            "readout": True, "actionable": False,
            "observed_url": "https://pets.example.org/listings"}]} )

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_mac"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            assert observation.context.find("Result: 4") >= 0
            assert observation.controls == []
            return Candidate("done", None, observation.digest(), True)
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read the result", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Result", "delivery_mode": "background",
            "max_steps": 4, "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert worker.last["outcome"] == "outcome_review_required"
    assert worker.last["task_verified"] is False
    assert worker.last["evidence"]["observed_urls"] == ["https://pets.example.org/listings"]


def test_embedded_cua_scroll_is_offered_and_admitted_for_any_reviewed_app(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Notes", "com.apple.Notes", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects = []

    class Driver:
        def __init__(self, **kwargs):
            assert kwargs["app"] == app and kwargs["window_id"] == 300
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": f"ps1_{len(effects) + 1}", "elements": []}
            effects.append(arguments)
            return {"effect": "returned_unverified"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        decisions = 0
        claim_attempted = False
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            self.decisions += 1
            if self.decisions == 1:
                scroll = next(c for c in observation.controls if c.label.endswith("down"))
                return Candidate("scroll", scroll.id, observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def admit_effect(self, _invocation, _observation, _digest): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read the next page of Notes", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Notes", "delivery_mode": "background", "max_steps": 2,
            "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert effects == [{"action": "scroll", "app": app.bundle_id,
                        "snapshot_id": "ps1_1", "direction": "down",
                        "delivery_mode": "background", "bring_to_front": False,
                        "raise_window": False}]
    assert worker.last["outcome"] == "outcome_review_required"
    assert worker.last["task_verified"] is False


def test_embedded_cua_explicit_refusal_records_no_dispatched_effect(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Chrome", "com.google.Chrome", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": "ps1_abc", "elements": [{"id": "elem_1",
                    "role": "AXButton", "label": "New Tab", "enabled": True,
                    "has_point": True, "actionable": True}]}
            raise cua_driver.CuaDriverUnavailable("desktop_pre_dispatch_cua_stale_element_token")
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        receipts = []
        claim_attempted = False
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest())
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): self.receipts.append(_args)
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Open a new tab", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Chrome", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": []}}
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert gateway.receipts[0][2] == "not_dispatched"
    assert worker.last["outcome"] == "pre_dispatch_rejected"
    assert worker.last["driver_failure_code"] == "desktop_pre_dispatch_cua_stale_element_token"
    assert worker.last["credits_reconciled"] is True


def test_embedded_cua_transport_detail_lands_in_receipt_evidence(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Chrome", "com.google.Chrome", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": "ps1_abc", "elements": [{"id": "elem_1",
                    "role": "AXButton", "label": "New Tab", "enabled": True,
                    "has_point": True, "actionable": True}]}
            raise cua_driver.CuaDriverUnavailable(
                "desktop_pre_dispatch_cua_stale_element_token", "click: ConnectionResetError: peer reset")
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        receipts = []
        claim_attempted = False
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest())
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): self.receipts.append(_args)
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Open a new tab", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Chrome", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": []}}
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert worker.last["driver_failure_detail"] == "desktop_driver_detail_private"
    assert worker.last["evidence"]["failure_detail"] == "desktop_driver_detail_private"
    assert "peer reset" not in str(worker.last)


@pytest.mark.parametrize("capture_code,remaining_window,expected_outcome", [
    ("desktop_cua_window_target_not_found", 301, "reviewed_window_closed_after_effect"),
    ("desktop_cua_observation_unpinned", 301, "reviewed_window_closed_after_effect"),
    ("desktop_cua_observation_unpinned", 300, "reviewed_window_unavailable"),
])
def test_embedded_cua_disappearing_dialog_stops_without_replaying_click(
        monkeypatch, capture_code, remaining_window, expected_outcome):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("TextEdit", "com.apple.TextEdit", 101, "123456")
    other_instance = AppChoice("TextEdit", "com.apple.TextEdit", 102, "123457")
    app_inventories = iter(([app], [other_instance, app]))
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(
        lambda cls: next(app_inventories)))
    inventories = [[{"window_id": 300, "on_screen": True}],
                   [{"window_id": remaining_window, "on_screen": True}]]
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: inventories.pop(0)))
    calls = []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            calls.append(arguments["action"])
            if arguments["action"] == "capture":
                if calls.count("capture") > 1:
                    raise cua_driver.CuaDriverUnavailable(capture_code)
                return {"snapshot_id": "ps1_first", "elements": [{"id": "elem_1",
                    "role": "AXButton", "label": "Cancel", "enabled": True,
                    "has_point": True, "actionable": True}]}
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_dialog", "ven_ours", None
        claim_attempted = False
        receipts = []
        def claim(self):
            self.execution_id = "dex_dialog"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest())
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): self.receipts.append(_args)
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Dismiss the file chooser", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "TextEdit", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": []}}
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)

    assert calls == ["capture", "click", "capture"]
    assert len(gateway.receipts) == 1
    assert worker.last["outcome"] == expected_outcome
    assert worker.last["task_verified"] is False
    assert worker.last["failure_code"] == capture_code
    assert worker.last["evidence"]["steps"][-1] == {
        "kind": "window_identity_after_capture_refusal", "after_effect": "Cancel",
        "same_process": True,
        "reviewed_window_present": remaining_window == 300,
        "other_window_count": int(remaining_window != 300)}
    assert worker.last["credits_reconciled"] is True


def test_embedded_cua_recaptures_after_lease_expires_during_admission(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("TextEdit", "com.apple.TextEdit", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, attempts = [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(len(captures) + 1)
                number = captures[-1]
                return {"snapshot_id": f"ps1_{number}", "elements": [{
                    "id": f"elem_{number}", "role": "AXButton", "label": "Cancel",
                    "enabled": True, "has_point": True, "actionable": True}]}
            attempts.append(arguments)
            if len(attempts) == 1:
                raise cua_driver.CuaDriverUnavailable("desktop_fresh_observation_required")
            return {"effect": "confirmed"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False
        def __init__(self):
            self.receipts, self.admissions, self.revisions = [], [], []
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            self.revisions.append(observation.revision)
            if len(self.revisions) == 3:
                return Candidate("done", None, observation.digest(), True)
            return Candidate("click", "c1", observation.digest())
        def admit_effect(self, invocation, observation, _digest):
            self.admissions.append((invocation, observation))
            return True
        def effect_receipt(self, *args): self.receipts.append(args)
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Dismiss the file chooser", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "TextEdit", "delivery_mode": "background", "max_steps": 3,
            "reviewed_text": []}}
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)

    assert gateway.revisions == ["ps1_1", "ps1_2", "ps1_3"]
    assert attempts[0]["snapshot_id"] == "ps1_1" and attempts[0]["element_id"] == "elem_1"
    assert attempts[1]["snapshot_id"] == "ps1_2" and attempts[1]["element_id"] == "elem_2"
    assert gateway.admissions[0][0] != gateway.admissions[1][0]
    assert gateway.receipts[0][2:] == ("not_dispatched", "desktop_fresh_observation_required")
    assert gateway.receipts[1][2] == "returned"
    assert worker.last["outcome"] == "outcome_review_required"


def test_embedded_cua_recovers_from_outside_window_control(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Chrome", "com.google.Chrome", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": False}]))
    effects = []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": f"ps1_{len(effects) + 1}", "elements": [
                    {"id": "elem_1", "role": "AXMenuItem", "label": "Open Location…",
                     "enabled": True, "has_point": True, "actionable": True},
                    {"id": "elem_2", "role": "AXTextField", "label": "Address and Search",
                     "enabled": True, "has_point": True, "actionable": True,
                     "text_entry": True}]}
            effects.append(arguments)
            if arguments.get("element_id") == "elem_1":
                raise cua_driver.CuaDriverUnavailable(
                    "desktop_pre_dispatch_cua_element_outside_target_window")
            return {"effect": "returned_unverified"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        receipts = []
        observations = []
        claim_attempted = False
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            self.observations.append([c.label for c in observation.controls])
            if len(self.observations) == 1:
                return Candidate("click", "c1", observation.digest())
            if len(self.observations) == 2:
                address = next(c for c in observation.controls if c.label == "Address and Search")
                return Candidate("click", address.id, observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): self.receipts.append(_args)
        def stop_receipt(self, *_args, **_kwargs): return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Open an address", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Chrome", "delivery_mode": "background", "max_steps": 3,
            "reviewed_text": []}}
    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert gateway.observations[0][0] == "Open Location…"
    assert "Open Location…" not in gateway.observations[1]
    assert [row[2] for row in gateway.receipts] == ["not_dispatched", "returned"]
    assert [row["kind"] for row in worker.last["evidence"]["steps"] if row["kind"] == "safe_refusal"] == ["safe_refusal"]
    assert worker.last["outcome"] == "outcome_review_required"


def test_browser_return_focuses_fresh_field_and_stops_repeated_type_cycle(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Chrome", "com.google.Chrome", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects = []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                elements = [{
                    "id": "elem_1", "role": "AXTextField", "label": "Address and Search",
                    "enabled": True, "has_point": True, "actionable": True,
                    "text_entry": True, "browser_address": True}]
                elements.extend({"id": f"elem_{index}", "role": "AXButton",
                                 "label": f"Page action {index}", "enabled": True,
                                 "has_point": True, "actionable": True}
                                for index in range(2, 101))
                return {"snapshot_id": f"ps1_{len(effects) + 1}", "elements": elements}
            if arguments["action"] == "wait":
                return {"waited_ms": arguments["milliseconds"]}
            effects.append(arguments)
            return {"effect": "returned_unverified"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        decisions = 0
        claim_attempted = False
        def claim(self):
            self.execution_id = "dex_mac"
            self.claim_attempted = True
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            self.decisions += 1
            if self.decisions == 1:
                return Candidate("type", "c1", observation.digest(), value_index=0)
            if self.decisions == 2:
                assert len(observation.controls) == 1
                target = next(c for c in observation.controls if c.operations == ["key"])
                return Candidate("key", target.id, observation.digest())
            return Candidate("type", "c1", observation.digest(), value_index=0)
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, *_args, **_kwargs): return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Go to pets.example.org", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Chrome", "delivery_mode": "background", "max_steps": 3,
            "reviewed_text": ["https://pets.example.org"]}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert [effect["action"] for effect in effects] == ["type", "key"]
    assert effects[1]["element_id"] == "elem_1"
    assert effects[1]["snapshot_id"] == "ps1_2"
    assert worker.last["outcome"] == "repeated_action_stopped"
    assert any(step["kind"] == "repeated_action_prevented"
               and step["operation"] == "type" for step in worker.last["evidence"]["steps"])


def test_desktop_consequential_control_stops_with_target_evidence(monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

    app = AppChoice("Example", "com.example.Example", 101, "123456")
    monkeypatch.setattr(peekaboo_driver, "_binary", lambda _value=None: "/bin/echo")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    monkeypatch.setattr(PeekabooDriver, "capture", lambda self: {
        "snapshot_id": "ps1_abc", "elements": [{"id": "elem_1", "role": "button",
            "label": "Delete", "enabled": True, "has_point": True, "actionable": True}]})
    monkeypatch.setattr(PeekabooDriver, "snapshot_ready_for_admission", lambda self, _snapshot: True)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_mac"

        def current(self):
            return {"state": "claimed"}

        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest())

        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Review the selected app", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background",
            "max_steps": 4, "reviewed_text": []}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert worker.last["outcome"] == "action_requires_review"
    assert worker.last["evidence"]["steps"][-1] == {
        "kind": "action_rejected", "reason": "consequential_control",
        "operation": "click", "target_label": "Delete"}
    assert not any(step["kind"] == "effect" for step in worker.last["evidence"]["steps"])


@pytest.mark.parametrize("visual_edit_id,expected_effects", [("c1", 1), ("none", 0)])
def test_delete_label_needs_visual_transient_edit_classification(
        tmp_path, monkeypatch, visual_edit_id, expected_effects):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects, reviews = [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": "ps1_abc", "screenshot_path": str(image_path),
                        "elements": [{"id": "elem_1", "role": "AXButton", "label": "Delete",
                                      "enabled": True, "has_point": True, "actionable": True}]}
            effects.append(arguments)
            return {"effect": "confirmed"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_edit", "ven_ours", None
        claim_attempted = False
        def claim(self): self.claim_attempted, self.execution_id = True, "dex_edit"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest())
        def review_desktop_image(self, invocation, snapshot_id, _encoded, observation=None):
            reviews.append(snapshot_id)
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "continue", "visible_facts": ["Input is visible"],
                               "next_step": "Clear input", "click_control_id": "c1",
                               "input_edit_control_id": visual_edit_id},
                    "proposal_invocation_id": invocation}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Clear the current input", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 1,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert reviews == ["ps1_abc"]
    assert len(effects) == expected_effects
    assert any(step["kind"] == "input_edit_requires_visual_review"
               for step in worker.last["evidence"]["steps"])
    if not expected_effects:
        assert worker.last["outcome"] == "action_requires_review"


def test_desktop_done_requests_approved_visual_review_without_granting_effect(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import peekaboo_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Calculator", "com.apple.Calculator", 101, "123456")
    monkeypatch.setattr(peekaboo_driver, "_binary", lambda _value=None: "/bin/echo")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    monkeypatch.setattr(PeekabooDriver, "capture", lambda self: {
        "snapshot_id": "ps1_abc", "screenshot_path": str(image_path),
        "elements": [{"id": "elem_1", "role": "StaticText", "label": "227", "enabled": True,
                      "has_point": False, "readout": True, "actionable": False}]})
    seen = []

    class Gateway:
        order_id, venture_id, execution_id = "dwo_mac", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_mac"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("done", None, observation.digest(), True)
        def review_desktop_image(self, _invocation, snapshot_id, encoded, observation=None):
            seen.append((snapshot_id, encoded))
            return {"authority": "observation_only", "snapshot_id": snapshot_id,
                    "image_sha256": "a" * 64, "report": {"verdict": "done",
                        "visible_facts": ["227"], "next_step": ""}}
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read the result", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Calculator", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert seen and seen[0][0] == "ps1_abc"
    assert worker.last["evidence"]["visual_review"]["visible_facts"] == ["227"]
    assert worker.last["outcome"] == "visual_result_review_required"
    assert worker.last["task_verified"] is False


def test_visual_review_can_select_observed_reversible_control_when_jev_blocks(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "dialog.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, decisions = [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": [{"id": "elem_1", "role": "AXButton", "label": "OK",
                                      "enabled": True, "has_point": True, "actionable": True},
                                     {"id": "elem_2", "role": "AXButton", "label": "Delete",
                                      "enabled": True, "has_point": True, "actionable": True}]}
            effects.append(arguments)
            return {"effect": "returned"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_dialog", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_dialog"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            decisions.append(observation)
            if len(decisions) == 1:
                return Candidate("blocked", None, observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def review_desktop_image(self, _invocation, snapshot_id, _encoded, observation=None):
            if len(decisions) == 1:
                assert observation.controls[0].label == "OK"
                assert observation.controls[1].label == "Delete"
            report = ({"verdict": "continue", "visible_facts": ["An error dialog is visible"],
                       "next_step": "Dismiss the dialog with OK", "click_control_id": "c1"}
                      if len(decisions) == 1 else
                      {"verdict": "done", "visible_facts": ["The result is visible"],
                       "next_step": "", "click_control_id": "none"})
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64, "report": report,
                    "proposal_invocation_id": _invocation if report.get("click_control_id") == "c1" else None}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Read the result", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == 1
    assert effects[0]["element_id"] == "elem_1"
    assert len(decisions) == 2
    assert any(step["kind"] == "visual_control_proposal" for step in worker.last["evidence"]["steps"])
    assert worker.last["outcome"] == "visual_result_review_required"


def test_sparse_ax_ocr_click_requires_fresh_visual_proposal(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "native-window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, reviews = [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": [{"id": "ocr_1", "role": "OCRText", "label": "7 + 8",
                                      "enabled": True, "has_point": True, "confidence": 0.99},
                                     {"id": "ocr_2", "role": "OCRText", "label": "Clear",
                                      "enabled": True, "has_point": True, "confidence": 0.99}]}
            effects.append(arguments)
            return {"effect": "confirmed"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_ocr", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_ocr"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            assert [c.label for c in observation.controls if "click" in c.operations] == ["7 + 8", "Clear"]
            # Jev sees OCR text but has no pixels; even its top choice must not
            # authorize a point click without the image-review proposal.
            return Candidate("click", "c2", observation.digest())
        def review_desktop_image(self, invocation, snapshot_id, _encoded, observation=None):
            reviews.append(snapshot_id)
            assert observation is not None
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "continue", "visible_facts": ["Clear is visible"],
                               "next_step": "Clear the display", "click_control_id": "c2"},
                    "proposal_invocation_id": invocation}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Clear the display", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 1,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(reviews) == 1
    assert [effect["element_id"] for effect in effects] == ["ocr_2"]
    assert any(step["kind"] == "ocr_click_requires_visual_review"
               for step in worker.last["evidence"]["steps"])


def test_desktop_stops_second_identical_blocked_decision_after_visual_continue(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, decisions, reviews, effects = [], [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": [{"id": f"elem_{len(captures)}", "role": "AXButton",
                                      "label": "Advance", "enabled": True, "has_point": True,
                                      "actionable": True},
                                     {"id": f"readout_{len(captures)}", "role": "StaticText",
                                      "label": "Still waiting", "enabled": True, "readout": True,
                                      "has_point": False, "actionable": False}]}
            effects.append(arguments)
            return {"effect": "returned"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_blocked", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_blocked"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            decisions.append(observation)
            return Candidate("blocked", None, observation.digest())
        def review_desktop_image(self, _invocation, snapshot_id, _encoded, observation=None):
            reviews.append(snapshot_id)
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "continue", "visible_facts": ["Still waiting"],
                               "next_step": "Advance", "click_control_id": "none"}}
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Advance", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 8,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)

    assert len(decisions) == 2
    assert len(reviews) == 1
    assert len(effects) == 0
    assert worker.last["outcome"] == "attention_required"
    assert worker.last["failure_code"] == "desktop_repeated_blocked_observation"
    assert worker.last["evidence"]["steps"][-1] == {
        "kind": "blocked_decision_stopped", "reason": "repeated_blocked_observation",
        "available_controls": 3, "editable_controls": 0, "visible_readouts": 1,
        "prior_visual_verdict": "continue"}


@pytest.mark.parametrize("proposal_source", ["jev", "vision"])
def test_ocr_display_is_readout_while_ax_button_remains_actionable(
        tmp_path, monkeypatch, proposal_source):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, observations = [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                return {"snapshot_id": f"ps1_{len(captures)}",
                        "screenshot_path": str(image_path),
                        "elements": [
                            {"id": "ocr_1", "role": "OCRText", "label": "7 + 8", "enabled": True,
                             "has_point": True, "confidence": 0.99, "actionable": False,
                             "readout": False},
                            {"id": "elem_2", "role": "AXButton", "label": "Clear", "enabled": True,
                             "has_point": True, "actionable": True, "readout": False}]}
            effects.append(arguments)
            return {"effect": "confirmed"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_readout", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_readout"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            observations.append(observation)
            assert "7 + 8" in observation.context
            assert [(c.id, c.label, c.operations) for c in observation.controls
                    if "click" in c.operations] == [("c1", "Clear", ["click"])]
            if len(observations) == 1:
                return Candidate("click" if proposal_source == "jev" else "blocked",
                                 "c1" if proposal_source == "jev" else None,
                                 observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def review_desktop_image(self, invocation, snapshot_id, _encoded, observation=None):
            assert proposal_source == "vision"
            assert observation is not None
            assert all(c.label != "7 + 8" for c in observation.controls)
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "continue", "visible_facts": ["Result display visible"],
                               "next_step": "Clear the input", "click_control_id": "c1"},
                    "proposal_invocation_id": invocation}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Clear the current input", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 2,
            "reviewed_text": [], "visual_review": proposal_source == "vision"}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert [effect["element_id"] for effect in effects] == ["elem_2"]
    assert len(observations) == 2
    assert worker.last["task_verified"] is False


def test_unverified_click_is_not_repeated_when_control_id_changes(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "clock.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Clock", "com.apple.clock", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects = []
    captures = []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                elements = [{"id": "elem_1", "role": "AXButton", "label": "Back",
                             "enabled": True, "has_point": True, "actionable": True}] if len(captures) > 1 else []
                elements.append({"id": f"elem_{len(elements) + 1}", "role": "AXButton",
                                 "label": "World Clock", "enabled": True,
                                 "has_point": True, "actionable": True})
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": elements}
            effects.append(arguments)
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_clock", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_clock"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            control = next(c for c in observation.controls if c.label == "World Clock")
            return Candidate("click", control.id, observation.digest())
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def review_desktop_image(self, _invocation, snapshot_id, _encoded, observation=None):
            assert snapshot_id == "ps1_2"
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "done", "visible_facts": ["World Clock tab visible"],
                               "next_step": ""}}
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Open World Clock", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Clock", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == 1
    assert effects[0]["element_id"] == "elem_1"
    assert worker.last["outcome"] == "visual_result_review_required"
    assert worker.last["task_verified"] is False
    assert any(step["kind"] == "visual_review" and step.get("after_effect") == "World Clock"
               for step in worker.last["evidence"]["steps"])


def test_each_uncertain_effect_is_visually_checked_before_next_action(tmp_path, monkeypatch):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, decisions, reviews = [], [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": [{"id": f"elem_{len(captures)}_{label}", "role": "AXRadioButton",
                                      "label": label, "enabled": True, "has_point": True,
                                      "actionable": True} for label in ("First", "Second")]}
            effects.append(arguments)
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_sequence", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_sequence"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            decisions.append(observation)
            label = ("First", "Second")[len(decisions) - 1]
            control = next(c for c in observation.controls if c.label == label)
            return Candidate("click", control.id, observation.digest())
        def review_desktop_image(self, _invocation, snapshot_id, _encoded, observation=None):
            reviews.append(observation)
            if len(reviews) == 1:
                report = {"verdict": "continue", "visible_facts": ["First is visibly selected"],
                          "next_step": "Select Second", "click_control_id": "none"}
            else:
                report = {"verdict": "done", "visible_facts": ["Second is visibly selected"],
                          "next_step": "", "click_control_id": "none"}
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64, "report": report}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Select First, then Second", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == 2
    assert len(reviews) == 2
    assert effects[0]["snapshot_id"] == "ps1_1"
    assert effects[1]["snapshot_id"] == "ps1_3"
    assert "First is visibly selected" in decisions[1].context
    assert "Select Second" in decisions[1].context
    assert worker.last["outcome"] == "visual_result_review_required"
    assert worker.last["task_verified"] is False


@pytest.mark.parametrize("initial_readout, final_readout, verified", [
    ("In progress", "Completed", True),
    ("In progress", "Still pending", False),
    ("Completed", "Completed", False),
])
def test_desktop_goal_requires_fresh_independent_post_action_readout(
        tmp_path, monkeypatch, initial_readout, final_readout, verified):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    captures, effects, decisions, stops = [], [], [], []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                captures.append(1)
                label = (initial_readout if len(captures) == 1 else
                         final_readout if len(captures) == 3 else "In progress")
                button = "First" if len(captures) == 1 else "Second"
                return {"snapshot_id": f"ps1_{len(captures)}", "screenshot_path": str(image_path),
                        "elements": [
                            {"id": "elem_button", "role": "AXButton", "label": button,
                             "enabled": True, "has_point": True, "actionable": True},
                            {"id": "elem_readout", "role": "AXStaticText", "label": label,
                             "enabled": True, "readout": True, "actionable": False}]}
            effects.append(arguments)
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_observed", "ven_ours", None
        claim_attempted = False
        def claim(self): self.claim_attempted, self.execution_id = True, "dex_observed"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            decisions.append(observation)
            if len(decisions) <= 2:
                return Candidate("click", "c1", observation.digest())
            return Candidate("done", None, observation.digest(), True)
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            stops.append(task_verified)
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Advance twice, then verify the exact visible result Completed",
        "desktop_verification": {"operation": "desktop_goal_v1",
            "application_bundle": app.bundle_id, "process_id": 101,
            "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 4,
            "reviewed_text": [], "expected_visible_text": "Completed"}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == 2
    assert worker.last["task_verified"] is verified
    assert worker.last["evidence"]["task_verified"] is verified
    assert stops == [verified]
    if verified:
        assert worker.last["outcome"] == "verified"
        assert any(step["kind"] == "independent_goal_observation"
                   and step["snapshot_id"] == "ps1_3" for step in worker.last["evidence"]["steps"])
    else:
        assert worker.last["outcome"] == "outcome_review_required"


@pytest.mark.parametrize("visual_proposes_point", [True, False])
def test_ax_point_without_press_requires_fresh_visual_proposal(
        tmp_path, monkeypatch, visual_proposes_point):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    from PIL import Image

    image_path = tmp_path / "window.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    app = AppChoice("Example", "com.example.app", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects = []

    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments["action"] == "capture":
                return {"snapshot_id": "ps1_point", "screenshot_path": str(image_path),
                        "elements": [{"id": "elem_1", "role": "AXButton", "label": "Next",
                                      "enabled": True, "has_point": True,
                                      "actionable": False, "point_candidate": True}]}
            effects.append(arguments)
            return {"effect": "unverifiable"}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass

    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)

    class Gateway:
        order_id, venture_id, execution_id = "dwo_point", "ven_ours", None
        claim_attempted = False
        def claim(self): self.claim_attempted, self.execution_id = True, "dex_point"
        def current(self): return {"state": "claimed"}
        def decide(self, _invocation, observation):
            return Candidate("click", "c1", observation.digest()) if not effects else Candidate(
                "done", None, observation.digest(), True)
        def review_desktop_image(self, invocation, snapshot_id, _image, observation=None):
            return {"snapshot_id": snapshot_id, "image_sha256": "a" * 64,
                    "report": {"verdict": "continue" if visual_proposes_point else "blocked",
                               "visible_facts": [], "next_step": "",
                               "click_control_id": "c1" if visual_proposes_point else "none"},
                    **({"proposal_invocation_id": invocation} if visual_proposes_point else {})}
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): return None
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_reason):
            assert task_verified is False
            return {"state": "stopped"}

    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Advance", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Example", "delivery_mode": "background", "max_steps": 1,
            "reviewed_text": [], "visual_review": True}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None,
                          cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_kwargs: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == int(visual_proposes_point)
    assert any(step["kind"] == "ax_point_click_requires_visual_review"
               for step in worker.last["evidence"]["steps"])


def test_readonly_inspection_lists_marked_workspace_without_writing(tmp_path, monkeypatch):
    from desktop_runtime import owned_inspect
    from desktop_runtime.owned_inspect import OwnedInspect

    workspace = tmp_path / "ventures" / "labhire-local"
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text("ven_ours\n")
    (workspace / "README.md").write_text("kept\n")
    nested = workspace / "src"
    nested.mkdir()
    (nested / "app.py").write_text("print(1)\n")
    before = (workspace / "README.md").read_bytes()
    monkeypatch.setattr(owned_inspect, "cursor_installed", lambda **_kwargs: True)

    result = OwnedInspect(venture_id="ven_ours", workspace_slug="labhire-local",
        root=tmp_path / "ventures").inventory()

    assert result["writes_performed"] is False
    assert result["cursor_edit_session"] is False
    assert result["workspace_marker_verified"] is True
    assert result["file_count"] == 2
    assert result["sample_paths"] == ["README.md", "src/app.py"]
    assert (workspace / "README.md").read_bytes() == before
    monkeypatch.setattr(owned_inspect, "cursor_installed", lambda **_kwargs: False)
    try:
        OwnedInspect(venture_id="ven_ours", workspace_slug="labhire-local",
            root=tmp_path / "ventures").inventory()
    except ValueError as error:
        assert str(error) == "coding_provider_not_installed"
    else:
        raise AssertionError("missing Cursor must not count as an inspection")


def test_reviewed_inspection_stops_with_verified_receipt(monkeypatch):
    from desktop_runtime import owned_inspect

    class Gateway:
        order_id, venture_id, execution_id = "dwo_inspect", "ven_ours", None
        claim_attempted = False

        def read(self):
            return {"state": "approved", "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.read"], "duration_seconds": 30,
                "inspection_verification": {
                    "operation": "workspace_inspect_v1", "provider": "cursor",
                    "workspace_slug": "labhire-local", "read_scope": "venture_workspace",
                    "write_scope": "none"}}}

        def claim(self):
            self.claim_attempted = True
            self.execution_id = "dex_inspect"

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert evidence_digest
            assert task_verified is True
            return {"state": "stopped"}

    monkeypatch.setattr(owned_inspect, "cursor_installed", lambda **_kwargs: True)
    monkeypatch.setattr(owned_inspect.OwnedInspect, "inventory", lambda self: {
        "operation": "workspace_inspect_v1", "provider": "cursor",
        "cursor_edit_session": False, "cursor_present": True, "file_count": 1,
        "inventory_truncated": False, "inventory_sha256": "abc", "sample_paths": ["README.md"],
        "git_present": False, "git_unchanged": True, "workspace_marker_verified": True,
        "writes_performed": False,
    })
    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run()
    assert worker.last["state"] == "stopped"
    assert worker.last["tool"] == "inspect"
    assert worker.last["task_verified"] is True
    assert worker.last["credits_reconciled"] is True


def test_reviewed_supymem_navigation_requires_exact_same_venture_route():
    import threading
    import time

    class Gateway:
        order_id, venture_id = "dwo_nav", "ven_ours"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["app.navigate"], "duration_seconds": 5,
                "app_navigation_verification": {
                    "operation": "open_venture_screen_v1", "route": "builds"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_nav"

        def current(self):
            return self.read()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            return {"state": "stopped"}

    for observed, verified in [
        ("https://app.supymem.com/v/ven_ours/builds", True),
        ("https://app.supymem.com/v/ven_other/builds", False),
    ]:
        worker = DeviceWorker(Gateway(), lambda _receipt: None)
        thread = threading.Thread(target=worker.run)
        thread.start()
        deadline = time.monotonic() + 2
        while worker.last["state"] != "awaiting_native_navigation" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.last["state"] == "awaiting_native_navigation"
        assert worker.submit_app_navigation_result(observed) is True
        assert worker.submit_app_navigation_result(observed) is False
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert worker.last["state"] == "stopped"
        assert worker.last["task_verified"] is verified
        assert worker.last["credits_reconciled"] is True


def test_codex_preflight_finds_signed_desktop_cli_outside_finder_path(monkeypatch, tmp_path):
    from desktop_runtime import agent_status
    from desktop_runtime.owned_coding import OwnedCoding
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    applications = tmp_path / "Applications"
    codex = applications / "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    fallback_builder = agent_status._provider_fallbacks
    monkeypatch.setattr(agent_status, "_provider_fallbacks",
        lambda provider: fallback_builder(
            provider, home=tmp_path, applications=applications))
    monkeypatch.setattr("app.coding_agents_consent.consents", lambda _subject: {"codex": False})
    monkeypatch.setattr("app.workspaces.resolve.local_person", lambda: SimpleNamespace(id="per_test"))
    coding = OwnedCoding(venture_id="ven_test", workspace_slug="test",
        provider="codex", tracker=None, authorize=lambda: True)
    with pytest.raises(ValueError, match="coding_tool_not_connected"):
        coding.preflight()
    _pin_worker_tool(monkeypatch)
    assert coding.preflight() == str(codex)


def test_browser_verification_requires_expected_locations_in_reviewed_order():
    manifest = {"browser_verification": {"expected_locations": [
        "https://venture.example/", "https://gateway.example/waitlist",
    ]}}
    assert DeviceWorker.verification_satisfied(manifest, [
        "https://venture.example/", "https://gateway.example/waitlist",
    ])
    assert not DeviceWorker.verification_satisfied(manifest, [
        "https://gateway.example/waitlist", "https://venture.example/",
    ])
    assert not DeviceWorker.verification_satisfied({}, [
        "https://venture.example/", "https://gateway.example/waitlist",
    ])


def test_strip_check_marker_removes_only_supymem_check():
    assert DeviceWorker.strip_check_marker(
        "https://example.test/path?a=1&supymem_check=1&b=2#frag"
    ) == "https://example.test/path?a=1&b=2#frag"
    assert DeviceWorker.strip_check_marker(
        "https://example.test/path?supymem_check=1"
    ) == "https://example.test/path"
    assert DeviceWorker.strip_check_marker(
        "https://example.test/path?x=1#h"
    ) == "https://example.test/path?x=1#h"
    assert DeviceWorker.strip_check_marker("https://example.test/") == "https://example.test/"


def test_browser_start_url_adds_supymem_check_to_first_expected():
    manifest = {
        "origins": ["https://venture.example"],
        "browser_verification": {"expected_locations": [
            "https://venture.example/", "https://gateway.example/waitlist",
        ]},
    }
    assert DeviceWorker.browser_start_url(manifest) == "https://venture.example/?supymem_check=1"
    assert DeviceWorker.browser_start_url(
        {"origins": ["https://venture.example"]}
    ) == "https://venture.example/?supymem_check=1"


def test_new_sidecar_strips_marker_before_exact_compare():
    """Core keeps expected unmarked; new sidecar may observe marked URLs."""
    expected = [
        "https://venture.example/",
        "https://gateway.example/waitlist?source=review",
    ]
    manifest = {"browser_verification": {"expected_locations": expected}}
    assert DeviceWorker.verification_satisfied(manifest, [
        "https://venture.example/?supymem_check=1",
        "https://gateway.example/waitlist?source=review&supymem_check=1",
    ])
    assert DeviceWorker.verification_satisfied(manifest, list(expected))
    assert not DeviceWorker.verification_satisfied(manifest, [
        "https://venture.example/?supymem_check=1",
        "https://gateway.example/waitlist?source=review&supymem_check=1&x=1",
    ])


def test_old_sidecar_exact_compare_still_verifies_unmarked_server_manifest():
    """Installed 0.1.48 apps: start at origin+'/', compare without stripping."""
    expected = [
        "https://venture.example/",
        "https://gateway.example/waitlist",
    ]
    origins = ["https://venture.example", "https://gateway.example"]
    start = origins[0] + "/"
    assert start == expected[0]
    observed = list(expected)
    position = 0
    for location in observed:
        if position < len(expected) and location == expected[position]:
            position += 1
    assert position == len(expected)
    assert DeviceWorker.verification_satisfied(
        {"browser_verification": {"expected_locations": expected}},
        observed,
    )


def test_exact_reviewed_evidence_completes_before_another_model_call(monkeypatch):
    expected = [
        "https://venture.example/",
        "https://gateway.example/waitlist",
    ]
    observed = [
        "https://venture.example/?supymem_check=1",
        "https://gateway.example/waitlist?supymem_check=1",
    ]
    started = []

    class Observation:
        def __init__(self, location):
            self.location, self.context, self.controls = location, "Visible page", []

        def digest(self):
            return "a" * 64

    class Candidate:
        operation, target = "click", "waitlist"

    class Gateway:
        order_id, execution_id, claim_attempted = "dwo_one", None, False

        def __init__(self):
            self.decisions = 0

        def read(self):
            return {"state": "approved", "manifest": {"applications": ["com.google.Chrome"],
                "origins": ["https://venture.example", "https://gateway.example"],
                "actions": ["browser.read", "browser.interact"], "duration_seconds": 60,
                "instruction": "Verify the reviewed pages", "browser_verification": {
                    "expected_locations": expected}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_one"

        def current(self):
            return self.read()

        def decide(self, invocation_id, observation):
            self.decisions += 1
            return Candidate()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            return {"state": "stopped"}

    class Browser:
        def __init__(self, **kwargs):
            self.session_id = "session_one"
            self.observations = iter(map(Observation, observed))

        def start(self, location):
            started.append(location)

        def observe(self):
            return next(self.observations)

        def verified_load(self, observation):
            self.last_load_evidence = {'qualified': True, 'observation_digest': observation.digest()}
            return True

        def click(self, candidate, invocation):
            pass

        def close(self):
            pass

    class Processes:
        def capture(self):
            pass

        def stop_all(self):
            return True

        def stopped(self):
            return True

        def receipt(self):
            return {"inventory_complete": True, "processes": []}

    monkeypatch.setattr("desktop_runtime.device_worker.OwnedBrowser", Browser)
    monkeypatch.setattr("desktop_runtime.device_worker.OwnedProcesses", Processes)
    gateway, saved = Gateway(), []
    worker = DeviceWorker(gateway, lambda receipt: saved.append(receipt.copy()))
    worker.run()

    assert started == ["https://venture.example/?supymem_check=1"]
    assert gateway.decisions == 1
    assert worker.last["state"] == "stopped"
    assert worker.last["task_verified"] is True
    assert worker.last["credits_reconciled"] is True
    assert worker.last["evidence"]["observed_locations"] == observed
    assert worker.last["evidence"]["outcome"] == "verified"


def test_fixed_terminal_status_uses_owned_workspace_and_stops(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))

    class Gateway:
        order_id, venture_id = "dwo_terminal", "ven_terminal"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["terminal.execute"], "duration_seconds": 60,
                "instruction": "Run the fixed status check", "terminal_verification": {
                    "operation": "git_status_v1", "workspace_slug": "terminal-fixture"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_terminal"

        def current(self):
            return self.read()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert task_verified is True
            return {"state": "stopped"}

    saved = []
    worker = DeviceWorker(Gateway(), lambda receipt: saved.append(receipt.copy()))
    worker.run()

    assert worker.last["state"] == "stopped"
    assert worker.last["tool"] == "terminal"
    assert worker.last["task_verified"] is True
    assert worker.last["credits_reconciled"] is True
    assert worker.last["evidence"]["outcome"] == "verified"
    assert worker.last["evidence"]["terminal"]["operation"] == "git_status_v1"
    assert worker.last["evidence"]["terminal"]["workspace_marker_verified"] is True
    assert worker.last["evidence"]["inventory"]["inventory_complete"] is True
    assert len(worker.last["evidence"]["inventory"]["processes"]) >= 1
    workspace = tmp_path / "data" / "workspaces" / "terminal-fixture"
    assert (workspace / ".supymem-venture").read_text().strip() == "ven_terminal"
    record = tmp_path / "data" / "ventures" / "ven_terminal" / "workspace.json"
    assert "terminal-fixture" in record.read_text()


def test_fixed_terminal_refuses_preexisting_unowned_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "Documents" / "Supymem" / "ventures" / "shared-name"
    workspace.mkdir(parents=True)
    (workspace / "private.txt").write_text("unrelated")

    from desktop_runtime.owned_processes import OwnedProcesses
    from desktop_runtime.owned_terminal import OwnedTerminal

    terminal = OwnedTerminal(venture_id="ven_one", workspace_slug="shared-name",
        tracker=OwnedProcesses(), authorize=lambda: True)
    import time

    import pytest
    with pytest.raises(ValueError, match="owner_unconfirmed"):
        terminal.git_status(time.monotonic() + 5)
    assert not (workspace / ".supymem-venture").exists()


def _coding_static_snapshot(task_id="tsk_coding"):
    return {"task_id": task_id, "artifact_profile": "static_landing_v1",
            "landing_expectations": {"contract": {"profile": "static_landing_v1",
                "product_name": "Reviewed page", "required_text": ["Reviewed page"], "actions": []}}}


def _pin_worker_tool(monkeypatch, provider="codex"):
    from pathlib import Path

    from desktop_runtime import agent_status
    from tests.coding_tool_fixtures import private_test_install

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    private_test_install(monkeypatch, Path(agent_status.provider_binary(provider)).parent)
    monkeypatch.setattr(agent_status, "_PROBES_CLOSED", False)
    responses = ({("--version",): b"codex-cli 0.138.0", ("login", "status"): b"Logged in using ChatGPT"}
        if provider == "codex" else {("--version",): b"2.1.284", ("auth", "status"):
            b'{"loggedIn":true,"authMethod":"claude.ai"}'})
    with monkeypatch.context() as probe:
        probe.setattr(agent_status, "_probe", lambda path, arguments, **kwargs: (0, responses[tuple(arguments)]))
        agent_status.pin_provider(provider)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("domain", ["Warehouse invoice checks", "Family meal plans"])
@pytest.mark.parametrize("failed", [False, True])
def test_coding_order_owns_process_and_records_independent_artifact_evidence(
        monkeypatch, tmp_path, provider, domain, failed):
    import json
    import shlex

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    tools = tmp_path / "bin"
    tools.mkdir()
    codex = tools / provider
    message = f"Checking {domain}" + (": password=fixture-private-value" if failed else "")
    events = ([{"type": "item.completed", "item": {"type": "agent_message", "text": message}},
               {"type": "item.completed", "item": {"type": "command_execution", "command": "node --test",
                    "exit_code": 0}}] if provider == "codex" else [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": message}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
            "input": {"command": "node --test"}}]}}])
    if failed:
        events.append({"type": "error", "message": f"I cannot finish {domain}."})
    stream = "".join("printf '%s\\n' " + shlex.quote(json.dumps(event)) + "\n" for event in events)
    codex.write_text("""#!/bin/sh
if [ "$1" = "--version" ]; then
  sleep 0.05
  echo "2.1.284 (fixture)"
  exit 0
fi
""" + stream + ("exit 1\n" if failed else """cat > product.py <<'EOF'
def ready():
    return True
EOF
""" + ("echo '{\"type\":\"turn.completed\"}'\n" if provider == "codex" else
       "echo '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false}'\n")))
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    _pin_worker_tool(monkeypatch, provider)

    class Gateway:
        order_id, venture_id = "dwo_coding", "ven_coding"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "workspace_id": "wsp_coding",
                    "actor_id": "person_coding", "device_id": "dev_coding",
                    "task_snapshot": _coding_static_snapshot(), "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": f"Implement the approved saved task for {domain}.", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": provider,
                    "workspace_slug": "coding-fixture", "read_scope": "device_filesystem",
                    "write_scope": "venture_workspace", "command_network": "disabled", "offline_profile": "static_html_v1"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_coding"

        def current(self):
            return self.read()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert task_verified is False
            return {"state": "stopped"}

    receipts = []
    worker = DeviceWorker(Gateway(), lambda receipt: receipts.append(receipt.copy()))
    worker.run()

    assert worker.last["state"] == "stopped"
    assert worker.last["tool"] == "coding"
    assert worker.last["credits_reconciled"] is True
    assert worker.last["task_verified"] is False
    activity = [event["text"] for event in worker.last.get("events", [])]
    assert any(domain in text for text in activity)
    assert any("node --test" in text for text in activity)
    assert f"Your coding tool exited with code {int(failed)}." in activity
    assert "fixture-private-value" not in json.dumps(receipts)
    assert any(receipt.get("events") and receipt["state"] == "running_coding" for receipt in receipts)
    if failed:
        assert worker.last["message"] == (f"I cannot finish {domain}. "
            "Open Settings > Keys and connections > Coding tools, check your connection, then retry from Build activity.")
        coding = worker.last["evidence"]["coding"]
        assert coding["failure_stage"] == "check"
        assert coding["failure_code"] == "coding_provider_failed"
        assert coding["exception_type"] == "ValueError"
        assert not coding.get("artifact_verified")
        assert worker.last["evidence"]["processes_stopped"] is True
        assert "source_receipt" not in worker.last
        return
    coding = worker.last["evidence"]["coding"]
    assert coding["provider"] == provider
    assert coding["artifact_verified"] is True
    assert coding["task_acceptance_verified"] is False
    assert coding["diff_check_passed"] is True
    assert [item["path"] for item in coding["changed_files"]] == ["product.py"]
    assert worker.last["evidence"]["processes_stopped"] is True
    assert worker.last["source_receipt"]["qa_verified"] is False
    assert worker.last["source_receipt"]["plan_task_id"] == "tsk_coding"
    assert worker.last["source_receipt"]["device_id"] == "dev_coding"
    assert worker.last["source_receipt"]["provider"] == provider


def test_coding_evidence_records_deleted_preexisting_untracked_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    import subprocess
    subprocess.run(["/usr/bin/git", "init", "--quiet"], cwd=workspace, check=True)
    removed = workspace / "removed.txt"
    removed.write_text("old draft")

    from desktop_runtime.owned_coding import OwnedCoding
    before = OwnedCoding._git_state(workspace)
    removed.unlink()
    (workspace / "replacement.txt").write_text("new draft")
    evidence = OwnedCoding._verify_changes(workspace, before)

    assert evidence["changed_files"] == [
        {"path": "removed.txt", "bytes": 0, "sha256": None, "deleted": True},
        {"path": "replacement.txt", "bytes": 9,
         "sha256": "4b91e7b6d3c4a150247a653368126bd8d42e24293fbae41f4fccb003be12c6d4",
         "deleted": False},
    ]


def test_coding_authority_rejection_before_spawn_reconciles_without_a_process(monkeypatch, tmp_path):
    """A claimed pre-spawn failure cannot strand the entire credit hold."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    tools = tmp_path / "bin"
    tools.mkdir()
    binary = tools / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    _pin_worker_tool(monkeypatch)

    class Gateway:
        order_id, venture_id = "dwo_pre_spawn", "ven_pre_spawn"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "task_snapshot": _coding_static_snapshot(), "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": "Implement the approved saved task.", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": "codex",
                    "workspace_slug": "pre-spawn-fixture", "read_scope": "device_filesystem",
                    "write_scope": "venture_workspace", "command_network": "disabled", "offline_profile": "static_html_v1"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_pre_spawn"

        def current(self):
            raise PermissionError("native_worker_upstream_rejected")

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert task_verified is False
            return {"state": "stopped"}

    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    worker.run()
    assert worker.last["state"] == "stopped"
    assert worker.last["credits_reconciled"] is True
    assert worker.last["task_verified"] is False
    assert worker.last["failure_code"] == "native_worker_upstream_rejected"
    assert worker.last["evidence"]["inventory"]["processes"] == []
    assert worker.last["evidence"]["processes_stopped"] is True


def test_unimplemented_coding_provider_is_rejected_before_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))

    class Gateway:
        order_id, venture_id = "dwo_unknown", "ven_unknown"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "task_snapshot": _coding_static_snapshot(), "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": "Implement the approved saved task.", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": "unknown",
                    "workspace_slug": "unknown-fixture", "read_scope": "device_filesystem",
                    "write_scope": "venture_workspace", "command_network": "disabled", "offline_profile": "static_html_v1"}}}

        def claim(self):
            raise AssertionError("unsupported provider must not claim")

    gateway = Gateway()
    worker = DeviceWorker(gateway, lambda _receipt: None)
    worker.run()
    assert gateway.claim_attempted is False
    assert worker.last["state"] == "not_started"
    assert worker.last["failure_code"] == "coding_provider_not_yet_supported"


def test_cancelled_coding_order_stops_provider_descendants_before_late_write(
        monkeypatch, tmp_path):
    import threading
    import time

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    tools = tmp_path / "bin"
    tools.mkdir()
    codex = tools / "codex"
    import sys
    child_script = "import pathlib,time; pathlib.Path('started.txt').write_text('started'); time.sleep(3); pathlib.Path('late.txt').write_text('late')"
    codex.write_text(f"#!{sys.executable}\n" +
        "import subprocess,sys,time\n" +
        "if '--version' in sys.argv: print('codex-fixture'); sys.exit(0)\n" +
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}],start_new_session=True)\n" +
        "time.sleep(20)\n")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    _pin_worker_tool(monkeypatch)

    class Gateway:
        order_id, venture_id = "dwo_cancel", "ven_cancel"
        execution_id, claim_attempted = None, False

        def read(self):
            return {"state": "approved", "task_snapshot": _coding_static_snapshot(), "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": "Implement the approved saved task.", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": "codex",
                    "workspace_slug": "cancel-fixture", "read_scope": "device_filesystem",
                    "write_scope": "venture_workspace", "command_network": "disabled", "offline_profile": "static_html_v1"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_cancel"

        def current(self):
            return self.read()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert task_verified is False
            return {"state": "stopped"}

    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    thread = threading.Thread(target=worker.run)
    thread.start()
    workspace = tmp_path / "data" / "workspaces" / "cancel-fixture"
    deadline = time.monotonic() + 5
    while not (workspace / "started.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (workspace / "started.txt").exists()
    # Stop before the periodic capture; the tool has its own session.
    worker.cancel()
    thread.join(timeout=8)
    assert not thread.is_alive()
    assert worker.last["state"] == "stopped"
    assert worker.last["evidence"]["outcome"] == "cancelled"
    assert worker.last["evidence"]["processes_stopped"] is True
    time.sleep(2.2)
    assert not (workspace / "late.txt").exists()


def test_coding_authority_refresh_failure_stops_provider_descendants(monkeypatch, tmp_path):
    import threading
    import time

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    tools = tmp_path / "bin"
    tools.mkdir()
    codex = tools / "codex"
    codex.write_text("""#!/bin/sh
if [ "$1" = "--version" ]; then echo codex-fixture; exit 0; fi
echo started > started.txt
(sleep 7; echo late > late.txt) &
sleep 20
""")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    _pin_worker_tool(monkeypatch)

    class Gateway:
        order_id, venture_id = "dwo_authority", "ven_authority"
        execution_id, claim_attempted = None, False
        authority_checks = 0

        def read(self):
            return {"state": "approved", "task_snapshot": _coding_static_snapshot(), "manifest": {
                "applications": ["com.companyos.app"], "origins": [],
                "actions": ["editor.edit", "terminal.execute"], "duration_seconds": 60,
                "instruction": "Implement the approved saved task.", "coding_verification": {
                    "operation": "workspace_edit_v1", "provider": "codex",
                    "workspace_slug": "authority-fixture", "read_scope": "device_filesystem",
                    "write_scope": "venture_workspace", "command_network": "disabled", "offline_profile": "static_html_v1"}}}

        def claim(self):
            self.claim_attempted, self.execution_id = True, "dex_authority"

        def current(self):
            self.authority_checks += 1
            if self.authority_checks > 1:
                raise ConnectionError("offline")
            return self.read()

        def stop_receipt(self, evidence_digest, processes_stopped, *, task_verified=False):
            assert processes_stopped is True
            assert task_verified is False
            return {"state": "stopped"}

    worker = DeviceWorker(Gateway(), lambda _receipt: None)
    thread = threading.Thread(target=worker.run)
    thread.start()
    workspace = tmp_path / "data" / "workspaces" / "authority-fixture"
    deadline = time.monotonic() + 8
    while thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert worker.last["state"] == "stopped"
    assert worker.last["evidence"]["processes_stopped"] is True
    time.sleep(2.2)
    assert not (workspace / "late.txt").exists()


def _claim_gateway(status, code):
    from desktop_runtime.device_gateway import UpstreamRejected, WorkOrderGateway

    order = {"id": "dwo_x", "venture_id": "ven_1", "device_id": "dev_1", "state": "approved",
             "review_digest": "a" * 64, "manifest": {}}

    def request(method, path, body):
        if method == "POST":
            raise UpstreamRejected(status, code)
        return {"available": True} if path.endswith("/capabilities") else order

    return WorkOrderGateway(venture_id="ven_1", order_id="dwo_x", device_id="dev_1", request=request)


def test_refused_claim_reports_a_plain_reason_and_no_uncertain_action():
    from desktop_runtime.device_gateway import ClaimRejected

    gateway = _claim_gateway(409, "device_order_expired")
    with pytest.raises(ClaimRejected):
        gateway.claim()
    # Core refused: nothing is claimed, so nothing may be treated as attempted.
    assert gateway.claim_attempted is False and gateway.claim_rejection == "device_order_expired"

    worker = DeviceWorker(gateway, lambda _receipt: None)

    def legacy_run():  # what every tool path did: an uncertain-action receipt
        worker.progress("not_started", outcome="execution_requires_reconciliation",
                        reason_code="cu_effect_unverified", failure_code="native_claim_rejected")
    worker._run = legacy_run
    worker.run()
    assert worker.last["state"] == "not_started"
    assert worker.last["outcome"] == "claim_rejected"
    assert worker.last["reason_code"] == "cu_claim_expired"


def test_server_error_on_claim_stays_unconfirmed():
    from desktop_runtime.device_gateway import UpstreamRejected

    gateway = _claim_gateway(503, "")
    with pytest.raises(UpstreamRejected):
        gateway.claim()
    assert gateway.claim_attempted is True and gateway.claim_rejection is None


@pytest.mark.parametrize("multiline", [False, True])
def test_textedit_document_refusal_never_offers_font_combobox_for_prose(monkeypatch, multiline):
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    app = AppChoice("TextEdit", "com.apple.TextEdit", 101, "123456")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, _app, **_kwargs: [{"window_id": 300, "on_screen": True}]))
    effects, observations = [], []
    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, args, *, session_id):
            if args["action"] == "capture":
                return {"snapshot_id": "ps1_document", "elements": [
                    {"id": "elem_doc", "role": "AXTextArea", "label": "Document text area",
                     "enabled": True, "has_point": True, "text_entry": True},
                    {"id": "elem_font", "role": "AXComboBox", "label": "Font size",
                     "enabled": True, "has_point": True, "text_entry": True}]}
            effects.append(args)
            raise cua_driver.CuaDriverUnavailable("desktop_pre_dispatch_cua_element_outside_target_window")
        def snapshot_ready_for_admission(self, _): return True
        def close(self): pass
    monkeypatch.setattr(cua_driver, "CuaWindowDriver", Driver)
    class Gateway:
        order_id, venture_id, execution_id = "dwo_document", "ven_ours", None
        claim_attempted = False
        def claim(self):
            self.claim_attempted = True
            self.execution_id = "dex_document"
        def current(self): return {"state": "claimed"}
        def decide(self, _, observation):
            observations.append(observation)
            assert all(not (c.label == "Font size" and "type" in c.operations) for c in observation.controls)
            document = next((c for c in observation.controls if "type" in c.operations), None)
            return Candidate("type", document.id, observation.digest(), value_index=0) if document else Candidate("blocked", None, observation.digest())
        def admit_effect(self, *_): return True
        def effect_receipt(self, *_): pass
        def stop_receipt(self, *_args, **_kwargs): return {"state": "stopped"}
    manifest = {"applications": [app.bundle_id], "origins": [],
        "actions": ["desktop.read", "desktop.interact"], "duration_seconds": 60,
        "instruction": "Write reviewed lines in a TextEdit document", "desktop_verification": {
            "operation": "desktop_goal_v1", "application_bundle": app.bundle_id,
            "process_id": 101, "process_start_identity": "123456", "window_id": 300,
            "window_title": "Untitled", "delivery_mode": "background", "max_steps": 5,
            "reviewed_text": ["Reviewed first\nReviewed second\nReviewed third" if multiline else "Reviewed document prose"],
            **({"document_text_index": 0} if multiline else {})}}
    worker = DeviceWorker(Gateway(), lambda _: None,
        cua_socket="/private/tmp/supymem-cua-test/driver.sock", cua_host_pid=1234)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **_: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert len(effects) == 1 and effects[0]["element_id"] == "elem_doc"
    assert effects[0]["text"] == manifest["desktop_verification"]["reviewed_text"][0]
    assert effects[0].get("text_entry_profile") == ("textedit_three_lines_v1" if multiline else None)
    assert len(observations) >= 2
    assert worker.last["task_verified"] is False


@pytest.mark.parametrize('proof,initially_satisfied', [('exact', False), ('exact', True), ('mismatch', False), ('wrong_window', False), ('stale', False), ('unreadable', False), ('compound', False), ('unregistered', False), ('non_typing_effect', False)])
def test_typing_only_document_goal_requires_fresh_exact_bound_ax_and_never_retypes(monkeypatch, proof, initially_satisfied):
    import hashlib
    from coding_agents.jev_decision import Candidate
    from desktop_runtime import cua_driver
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    app = AppChoice('TextEdit', 'com.apple.TextEdit', 101, '123456')
    monkeypatch.setattr(PeekabooDriver, 'running_apps', classmethod(lambda cls: [app]))
    monkeypatch.setattr(PeekabooDriver, 'running_windows', classmethod(lambda cls, *_args, **_kwargs: [{'window_id': 300, 'on_screen': True}]))
    expected = 'Supymem exact document test'
    captures, effects, decisions, stops = [], [], [], []
    class Driver:
        def __init__(self, **_kwargs): pass
        def __call__(self, arguments, *, session_id):
            if arguments['action'] == 'capture':
                captures.append(1)
                return {'snapshot_id': f'ps1_{len(captures)}', 'elements': [{'id': 'elem_document',
                    'role': 'AXTextArea', 'label': 'Document text area', 'enabled': True, 'has_point': True,
                    'text_entry': True, 'actionable': False}]}
            effects.append(arguments)
            return {'effect': 'confirmed'}
        def document_text_observation(self, snapshot_id, text):
            if proof == 'unreadable':
                return None
            return {'snapshot_id': 'ps1_stale' if proof == 'stale' else snapshot_id,
                'pid': 101, 'window_id': 999 if proof == 'wrong_window' else 300,
                'element_id': 'elem_document', 'element_index': 7,
                'expected_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'matches': proof in {'exact', 'compound', 'unregistered'} and (initially_satisfied or bool(effects))}
        def snapshot_ready_for_admission(self, _snapshot): return True
        def close(self): pass
    monkeypatch.setattr(cua_driver, 'CuaWindowDriver', Driver)
    class Gateway:
        order_id, venture_id, execution_id = 'dwo_document', 'ven_ours', None
        claim_attempted = False
        def claim(self): self.claim_attempted, self.execution_id = True, 'dex_document'
        def current(self): return {'state': 'claimed'}
        def decide(self, _invocation, observation):
            decisions.append(observation)
            return Candidate('click' if proof == 'non_typing_effect' else 'type', 'c1', observation.digest(), value_index=0)
        def admit_effect(self, *_args): return True
        def effect_receipt(self, *_args): pass
        def stop_receipt(self, _digest, _stopped, *, task_verified=False, **_kwargs):
            stops.append(task_verified)
            return {'state': 'stopped'}
    goal = f'Type exactly "{expected}" into the selected TextEdit document. Do not save.'
    if proof == 'compound':
        goal += ' Then save the document.'
    manifest = {'applications': [app.bundle_id], 'origins': [], 'actions': ['desktop.read', 'desktop.interact'],
        'duration_seconds': 60, 'instruction': goal, 'desktop_verification': {'operation': 'desktop_goal_v1',
        'application_bundle': app.bundle_id, 'process_id': 101, 'process_start_identity': '123456',
        'window_id': 300, 'window_title': 'Untitled', 'delivery_mode': 'background', 'max_steps': 3,
        'founder_goal': goal, 'reviewed_text': ['Different reviewed literal' if proof == 'unregistered' else expected]}}
    worker = DeviceWorker(Gateway(), lambda _receipt: None, cua_socket='/private/tmp/supymem-cua-test/driver.sock', cua_host_pid=1234)
    monkeypatch.setattr('desktop_runtime.local_notices.push_task_finished', lambda **_kwargs: None)
    worker.run_desktop({'state': 'approved'}, manifest)
    verified = proof == 'exact'
    assert worker.last['task_verified'] is verified and stops == [verified]
    assert len(effects) == (0 if (verified and initially_satisfied) or proof == 'non_typing_effect' else 1)
    if verified:
        assert worker.last['outcome'] == 'verified'
        assert len(decisions) == (0 if initially_satisfied else 1)
        evidence = [step for step in worker.last['evidence']['steps'] if step['kind'] == 'independent_document_goal_observation']
        assert len(evidence) == 1 and evidence[0]['source'] == 'ax'
        assert evidence[0]['expected_sha256'] == hashlib.sha256(expected.encode()).hexdigest()
        assert expected not in str(evidence)
    else:
        assert worker.last['reason_code'] == ('cu_stopped' if proof == 'non_typing_effect' else 'cu_repeated_action')
