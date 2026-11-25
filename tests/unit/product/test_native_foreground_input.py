import sys
from types import SimpleNamespace

import pytest

from desktop_runtime import native_foreground_input as input_driver


def test_focus_attempt_requires_reviewed_cg_window_before_activation(monkeypatch):
    activated = []
    class Quartz:
        kCGWindowListOptionIncludingWindow = 1
        @staticmethod
        def CGWindowListCopyWindowInfo(_option, _window_id):
            return [{"kCGWindowNumber": 77, "kCGWindowOwnerPID": 43,
                     "kCGWindowLayer": 0, "kCGWindowBounds": {
                         "X": 0, "Y": 0, "Width": 600, "Height": 400}}]
    class Running:
        def isTerminated(self): return False
        def activateWithOptions_(self, _option):
            activated.append(True)
            return True
    class AppKit:
        NSApplicationActivateIgnoringOtherApps = 1
        NSRunningApplication = SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: Running())
    monkeypatch.setitem(sys.modules, "Quartz", Quartz)
    monkeypatch.setitem(sys.modules, "AppKit", AppKit)
    with pytest.raises(input_driver.ForegroundInputUnavailable,
                       match="desktop_reviewed_cg_window_changed"):
        input_driver.focus_reviewed_window(pid=42, window_id=77)
    assert activated == []


def test_focus_attempt_needs_no_ax_grant_and_still_requires_caller_postcheck(monkeypatch):
    activated = []
    class Quartz:
        kCGWindowListOptionIncludingWindow = 1
        @staticmethod
        def CGWindowListCopyWindowInfo(_option, _window_id):
            return [{"kCGWindowNumber": 77, "kCGWindowOwnerPID": 42,
                     "kCGWindowLayer": 0, "kCGWindowBounds": {
                         "X": 0, "Y": 0, "Width": 600, "Height": 400}}]
    class Running:
        def isTerminated(self): return False
        def activateWithOptions_(self, _option):
            activated.append(True)
            return True
    class AppKit:
        NSApplicationActivateIgnoringOtherApps = 1
        NSRunningApplication = SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: Running())
    monkeypatch.setitem(sys.modules, "Quartz", Quartz)
    monkeypatch.setitem(sys.modules, "AppKit", AppKit)
    input_driver.focus_reviewed_window(pid=42, window_id=77)
    assert activated == [True]


def test_return_refuses_focus_that_is_not_the_reviewed_field(monkeypatch):
    posted = []
    monkeypatch.setattr(input_driver, "_focused_editable", lambda pid, bounds: "different text")
    monkeypatch.setattr(input_driver, "_key", lambda code: posted.append(code))
    with pytest.raises(input_driver.ForegroundInputUnavailable,
                       match="desktop_pre_dispatch_focused_field_changed"):
        input_driver.press_return_in_reviewed_field(
            pid=42, window_bounds=(100, 50, 600, 400), expected_text="reviewed text")
    assert posted == []


def test_return_posts_only_after_exact_focused_value(monkeypatch):
    posted = []
    monkeypatch.setattr(input_driver, "_focused_editable", lambda pid, bounds: "reviewed text")
    monkeypatch.setattr(input_driver, "_key", lambda code: posted.append(code))
    input_driver.press_return_in_reviewed_field(
        pid=42, window_bounds=(100, 50, 600, 400), expected_text="reviewed text")
    assert posted == [36]


def test_ax_focus_requires_reviewed_process_and_window(monkeypatch):
    class AX:
        kAXFocusedUIElementAttribute = "focused"
        kAXRoleAttribute = "role"
        kAXPositionAttribute = "position"
        kAXSizeAttribute = "size"
        kAXValueAttribute = "value"
        kAXValueCGPointType = "point"
        kAXValueCGSizeType = "size_type"

        @staticmethod
        def AXUIElementCreateSystemWide():
            return "system"

        @staticmethod
        def AXUIElementCopyAttributeValue(_element, name, _unused):
            values = {"focused": "field", "role": "AXTextField",
                      "position": "position_ref", "size": "size_ref", "value": "reviewed text"}
            return 0, values[name]

        @staticmethod
        def AXUIElementGetPid(_element, _unused):
            return 0, 42

        @staticmethod
        def AXValueGetValue(value, _type, _unused):
            return 0, (SimpleNamespace(x=150, y=70) if value == "position_ref"
                       else SimpleNamespace(width=200, height=30))

    monkeypatch.setitem(sys.modules, "ApplicationServices", AX)
    assert input_driver._focused_editable(42, (100, 50, 600, 400)) == "reviewed text"
    assert input_driver._focused_editable(43, (100, 50, 600, 400)) is None
    assert input_driver._focused_editable(42, (300, 50, 600, 400)) is None
