from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop_runtime.peekaboo_driver import AppChoice, DriverUnavailable, InstalledApp, PeekabooDriver


def test_inventory_stays_local_without_a_background_host(monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b'{"success":true,"data":{"apps":[],"windows":[],"target_application_info":{"pid":42,"bundle_id":"com.example.Example"}}}')

    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run", fake_run)
    assert PeekabooDriver.running_apps(binary="/bin/echo") == []
    PeekabooDriver.running_windows(AppChoice("Example", "com.example.Example", 42, "12345"), binary="/bin/echo")
    assert commands == [
        ["/bin/echo", "app", "list", "--include-hidden", "--json", "--no-remote"],
        ["/bin/echo", "window", "list", "--app", "com.example.Example", "--json", "--no-remote"],
    ]


def test_off_space_window_is_listed_as_same_reviewed_window(monkeypatch):
    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run", lambda *_a, **_k: SimpleNamespace(
        stdout=b'{"success":true,"data":{"windows":[],"target_application_info":{"pid":42,"bundle_id":"com.example.Example"}}}'))
    monkeypatch.setattr(PeekabooDriver, "_off_screen_windows",
                        staticmethod(lambda pid: [(55104, "Start Page", 800, 600)] if pid == 42 else []))
    rows = PeekabooDriver.running_windows(AppChoice("Example", "com.example.Example", 42, "1"), binary="/bin/echo")
    assert rows == [{"window_id": 55104, "title": "Start Page", "on_screen": False, "width": 800, "height": 600}]


def test_permissions_report_is_scoped_to_local_driver_identity(monkeypatch):
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b'{"success":true,"data":{"permissions":['
                               b'{"name":"Screen Recording","isGranted":false},'
                               b'{"name":"Accessibility","isGranted":true},'
                               b'{"name":"Event Synthesizing","isGranted":false}]}}')

    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run", fake_run)
    assert PeekabooDriver.permissions_status(binary="/bin/echo") == {
        "screen_recording": False, "accessibility": True, "event_synthesizing": False}
    assert commands == [["/bin/echo", "permissions", "status", "--json", "--no-remote"]]


def test_permission_request_is_fixed_to_known_kind_and_local_helper(monkeypatch):
    commands = []
    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run",
                        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(stdout=b""))
    monkeypatch.setattr(PeekabooDriver, "permissions_status", classmethod(
        lambda cls, **kwargs: {"screen_recording": False}))
    assert PeekabooDriver.request_permission("screen-recording", binary="/bin/echo") == {
        "screen_recording": False}
    assert commands == [["/bin/echo", "permissions", "request", "screen-recording", "--no-remote"]]
    try:
        PeekabooDriver.request_permission("unknown", binary="/bin/echo")
    except ValueError as error:
        assert str(error) == "desktop_permission_kind_invalid"
    else:
        assert False, "Unknown permission must never be passed to the helper"


def test_launch_only_a_discovered_installed_app_in_background(monkeypatch):
    chosen = AppChoice("Example", "com.example.Example", 42, "12345")
    commands = []
    monkeypatch.setattr(PeekabooDriver, "installed_apps", classmethod(
        lambda cls: [InstalledApp("Example", chosen.bundle_id, "/Applications/Example.app")]))
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [chosen]))

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run", fake_run)
    assert PeekabooDriver.launch_installed_app(chosen.bundle_id) == chosen
    assert commands == [["/usr/bin/open", "-g", "-b", chosen.bundle_id]]
    try:
        PeekabooDriver.launch_installed_app("com.example.Missing")
    except DriverUnavailable as error:
        assert str(error) == "desktop_app_not_installed"
    else:
        assert False, "Undiscovered app must not launch"


def test_explicit_open_request_can_bootstrap_first_window(monkeypatch):
    chosen = AppChoice("Example", "com.example.Example", 42, "12345")
    commands = []
    windows = iter([[], [{"window_id": 77, "on_screen": True}]])
    monkeypatch.setattr(PeekabooDriver, "installed_apps", classmethod(
        lambda cls: [InstalledApp("Example", chosen.bundle_id, "/Applications/Example.app")]))
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [chosen]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, app: next(windows)))
    monkeypatch.setattr("desktop_runtime.peekaboo_driver._binary", lambda: "/bin/peekaboo")

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run", fake_run)
    assert PeekabooDriver.launch_installed_app(chosen.bundle_id, ensure_window=True) == chosen
    assert commands == [
        ["/usr/bin/open", "-g", "-b", chosen.bundle_id],
        ["/bin/peekaboo", "app", "launch", "--bundle-id", chosen.bundle_id,
         "--foreground", "--wait-for-window", "--json", "--no-remote"],
    ]


def test_window_bootstrap_does_not_claim_an_unobserved_window(monkeypatch):
    chosen = AppChoice("Example", "com.example.Example", 42, "12345")
    monkeypatch.setattr(PeekabooDriver, "installed_apps", classmethod(
        lambda cls: [InstalledApp("Example", chosen.bundle_id, "/Applications/Example.app")]))
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [chosen]))
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(lambda cls, app: []))
    monkeypatch.setattr("desktop_runtime.peekaboo_driver._binary", lambda: "/bin/peekaboo")
    monkeypatch.setattr("desktop_runtime.peekaboo_driver.subprocess.run",
                        lambda _command, **_kwargs: SimpleNamespace(returncode=0))
    with pytest.raises(DriverUnavailable, match="desktop_app_window_unavailable"):
        PeekabooDriver.launch_installed_app(chosen.bundle_id, ensure_window=True)


def test_reviewed_foreground_handoff_pins_process_and_window(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    commands = []
    monkeypatch.setattr(driver, "_check_process", lambda: commands.append("identity"))
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: True)
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, chosen, **_kwargs: [{"window_id": 77}]))
    monkeypatch.setattr("desktop_runtime.native_foreground_input.focus_reviewed_window",
                        lambda **kwargs: commands.append(kwargs))
    try:
        driver.focus_reviewed_app()
        assert driver._foreground_handoff_done is True
        assert commands == ["identity", {"pid": 42, "window_id": 77}, "identity"]
    finally:
        driver.close()


def test_reviewed_handoff_uses_frontmost_cg_proof_when_ax_inventory_is_partial(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: True)
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, chosen, **_kwargs: pytest.fail("partial AX inventory is not post-focus proof")))
    monkeypatch.setattr("desktop_runtime.native_foreground_input.focus_reviewed_window",
                        lambda **_kwargs: None)
    try:
        driver.focus_reviewed_app()
        assert driver._foreground_handoff_done is True
    finally:
        driver.close()


def test_foreground_identity_can_settle_without_replaying_input(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    observations = iter([False, False, True])
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: next(observations))
    monkeypatch.setattr("desktop_runtime.peekaboo_driver.time.sleep", lambda _delay: None)
    try:
        assert driver._settled_foreground_window() is True
    finally:
        driver.close()


def test_mcp_foreground_authority_starts_only_after_reviewed_handoff(monkeypatch):
    from desktop_runtime import peekaboo_driver

    app = AppChoice("Example", "com.example.Example", 42, "12345")
    modes = []

    class Session:
        def __init__(self, _binary, *, allow_foreground=False):
            modes.append(allow_foreground)
        def call(self, *_args, **_kwargs):
            return ({}, "", False)
        def close(self):
            pass

    monkeypatch.setattr(peekaboo_driver, "PeekabooSession", Session)
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, chosen, **_kwargs: [{"window_id": 77}]))
    monkeypatch.setattr("desktop_runtime.native_foreground_input.focus_reviewed_window",
                        lambda **_kwargs: None)
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: True)
    try:
        driver._call_mcp("see", {})
        driver.focus_reviewed_app()
        driver._call_mcp("see", {})
        assert modes == [False, True]
    finally:
        driver.close()


def test_foreground_handoff_rejects_another_window_of_same_app(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: False)
    monkeypatch.setattr(PeekabooDriver, "running_windows", classmethod(
        lambda cls, chosen, **_kwargs: [{"window_id": 77}]))
    monkeypatch.setattr("desktop_runtime.native_foreground_input.focus_reviewed_window",
                        lambda **_kwargs: None)
    try:
        with pytest.raises(DriverUnavailable, match="desktop_foreground_window_unconfirmed"):
            driver.focus_reviewed_app()
    finally:
        driver.close()


def test_foreground_handoff_preserves_native_refusal(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo",
                            allow_reviewed_foreground=True)
    monkeypatch.setattr(driver, "_check_process", lambda: None)
    from desktop_runtime.native_foreground_input import ForegroundInputUnavailable
    def refuse(**_kwargs):
        raise ForegroundInputUnavailable("desktop_reviewed_ax_window_ambiguous")
    monkeypatch.setattr("desktop_runtime.native_foreground_input.focus_reviewed_window", refuse)
    try:
        with pytest.raises(DriverUnavailable,
                           match="desktop_reviewed_ax_window_ambiguous"):
            driver.focus_reviewed_app()
    finally:
        driver.close()


def test_pinned_mcp_capture_and_click_use_the_same_snapshot(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")
    calls = []

    def fake_call(name, arguments, **_kwargs):
        calls.append((name, arguments))
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXButton (1 found, 1 actionable):\n  elem_1 - "Open" - at (200, 150) size 100×30 - value: ""\n'
                    'AXStaticText (1 found, 0 actionable):\n  elem_2 - "22" - at (300, 150) size 40×30 - value: "22" [not actionable]', False)
        return ({"effect": "unverifiable", "mutation_dispatched": True,
                 "outcome": "dispatched"}, "", False)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    try:
        captured = driver.capture()
        assert captured["elements"][0]["actionable"] is True
        assert captured["elements"][1]["readout"] is True
        assert captured["elements"][1]["label"] == "22"
        result = driver({"action": "click", "app": app.bundle_id,
                         "snapshot_id": captured["snapshot_id"], "element_id": "elem_1"},
                        session_id="desktop_test")
        assert result["effect"] == "unverifiable"
        assert calls[0][0] == "see"
        assert calls[1] == ("click", {"on": "elem_1", "snapshot": "ps1_abcd1234", "pid": 42})
    finally:
        driver.close()


def test_snapshot_round_trip_expires_before_durable_admission(monkeypatch):
    from desktop_runtime import peekaboo_driver

    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")
    try:
        driver._snapshot = {"element_1": {"id": "element_1"}}
        driver._current_snapshot_id = "ps1_abcd1234"
        monkeypatch.setattr(peekaboo_driver.time, "monotonic", lambda: 100.0)
        driver._snapshot_at = 85.0
        assert driver.snapshot_ready_for_admission("ps1_abcd1234") is True
        driver._snapshot_at = 79.0
        assert driver.snapshot_ready_for_admission("ps1_abcd1234") is False
        assert driver.snapshot_ready_for_admission("ps1_other") is False
    finally:
        driver.close()


def test_provider_attested_pre_dispatch_refusal_retains_machine_reason(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")

    def fake_call(name, arguments, **_kwargs):
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXButton (1 found, 1 actionable):\n  elem_1 - "Open" - at (200, 150) size 100×30 - value: ""', False)
        return ({"effect": "refused", "mutation_dispatched": False,
                 "retry_safe": True, "error_code": "TARGET_CHANGED"}, "", True)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    try:
        captured = driver.capture()
        try:
            driver({"action": "click", "app": app.bundle_id,
                    "snapshot_id": captured["snapshot_id"], "element_id": "elem_1"},
                   session_id="desktop_test")
        except DriverUnavailable as error:
            assert str(error) == "desktop_pre_dispatch_target_changed"
        else:
            assert False, "An attested refusal must not be treated as a delivered click"
    finally:
        driver.close()


def test_ax_text_entry_uses_confirmed_background_set_value(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")
    calls = []

    def fake_call(name, arguments, **_kwargs):
        calls.append((name, arguments))
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXTextField (1 found, 1 actionable):\n  elem_1 - "Address" - at (200, 150) size 100×30 - value: ""', False)
        return ({"effect": "confirmed", "mutation_dispatched": True,
                 "outcome": "changed"}, "", False)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    try:
        captured = driver.capture()
        result = driver({"action": "type", "app": app.bundle_id,
                         "snapshot_id": captured["snapshot_id"], "element_id": "elem_1",
                         "text": "example.com"}, session_id="desktop_test")
        assert result["effect"] == "confirmed"
        assert calls[1] == ("set_value", {"on": "elem_1", "value": "example.com",
                                          "snapshot": "ps1_abcd1234"})
    finally:
        driver.close()


def test_reviewed_foreground_typing_uses_permissioned_driver_on_pinned_window(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")
    driver._foreground_handoff_done = True
    calls = []

    def fake_call(name, arguments, **_kwargs):
        calls.append((name, arguments))
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXTextField (1 found, 1 actionable):\n  elem_1 - "Address" - at (200, 150) size 100×30 - value: ""', False)
        return ({"effect": "confirmed", "mutation_dispatched": True,
                 "outcome": "changed"}, "", False)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: True)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    try:
        captured = driver.capture()
        result = driver({"action": "type", "app": app.bundle_id,
                         "snapshot_id": captured["snapshot_id"], "element_id": "elem_1",
                         "text": "https://example.com/"}, session_id="desktop_test")
        assert result["effect"] == "confirmed"
        assert calls[1] == ("type", {"on": "elem_1", "text": "https://example.com/",
                                      "snapshot": "ps1_abcd1234", "clear": True,
                                      "foreground": True})
        assert driver._last_foreground_text == "https://example.com/"
    finally:
        driver.close()


def test_verified_no_change_type_is_conclusive_and_not_a_replay(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")

    def fake_call(name, arguments, **_kwargs):
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXTextField (1 found, 1 actionable):\n  elem_1 - "Address" - at (200, 150) size 100×30 - value: ""', False)
        return ({"state": "confirmed_no_change", "effect": "confirmed",
                 "mutation_dispatched": False, "evidence": "verified_no_change"}, "", False)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    try:
        captured = driver.capture()
        result = driver({"action": "type", "app": app.bundle_id,
                         "snapshot_id": captured["snapshot_id"], "element_id": "elem_1",
                         "text": "example.com"}, session_id="desktop_test")
        assert result["effect"] == "no_change"
        assert result["outcome"] == "verified_no_change"
    finally:
        driver.close()


def test_reviewed_foreground_return_requires_focused_reviewed_text(monkeypatch):
    app = AppChoice("Example", "com.example.Example", 42, "12345")
    driver = PeekabooDriver(app=app, window_id=77, binary="/bin/echo")
    driver._foreground_handoff_done = True
    commands = []

    def fake_call(name, arguments, **_kwargs):
        commands.append((name, arguments))
        if name == "see":
            Path(arguments["path"]).write_bytes(b"PNG")
            return ({"target_receipt": {"pid": 42, "window_id": 77,
                     "process_start_identity_decimal": "12345"},
                     "coordinate_context": {"reference_id": "ps1_abcd1234",
                     "logical_space": "global_display_points", "window": {"window_id": 77},
                     "logical_bounds": {"x": 100, "y": 50, "width": 600, "height": 400}}},
                    'AXTextField (1 found, 1 actionable):\n  elem_1 - "Address" - at (200, 150) size 100×30 - value: "example.com"', False)
        return ({"effect": "unverifiable", "mutation_dispatched": True,
                 "outcome": "dispatched"}, "", False)

    monkeypatch.setattr(driver, "_check_process", lambda: None)
    monkeypatch.setattr(driver, "_confirmed_foreground_window", lambda: True)
    monkeypatch.setattr(driver, "_call_mcp", fake_call)
    monkeypatch.setattr("desktop_runtime.native_foreground_input.reviewed_field_matches",
                        lambda **kwargs: kwargs["expected_text"] == "example.com")
    driver._last_foreground_text = "example.com"
    try:
        captured = driver.capture()
        result = driver({"action": "key", "app": app.bundle_id,
                         "snapshot_id": captured["snapshot_id"], "chord": "Return"},
                        session_id="desktop_test")
        assert result["effect"] == "unverifiable"
        assert commands[1] == ("press", {"keys": ["Return"], "snapshot": "ps1_abcd1234"})
        assert driver._last_foreground_text is None
    finally:
        driver.close()
