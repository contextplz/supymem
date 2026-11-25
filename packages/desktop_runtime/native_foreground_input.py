"""Small native input fallback for a reviewed, foreground Mac window.

Adapted from the MIT-licensed typesafe-computer-use macOS adapter. The caller
pins the process and exact window, admits a durable effect, and observes again.
Quartz posting does not attest delivery; every returned effect is unverifiable.
"""
from __future__ import annotations

import math
import time


class ForegroundInputUnavailable(RuntimeError):
    pass


def focus_reviewed_window(*, pid: int, window_id: int) -> None:
    """Activate a pinned app; the caller must attest the exact frontmost window.

    App activation can select another window of the same process. It is only a
    foreground *attempt*, never permission to send input. The caller checks
    the reviewed CG ID at the top of the screen before dispatching anything.
    This avoids requiring a separate Accessibility grant for the runtime helper
    when its signed Peekaboo observer already has one.
    """
    if type(pid) is not int or pid <= 0 or type(window_id) is not int or window_id <= 0:
        raise ForegroundInputUnavailable("desktop_reviewed_window_identity_invalid")
    try:
        import AppKit
        import Quartz

        rows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionIncludingWindow, window_id) or []
        rows = [row for row in rows if row.get("kCGWindowNumber") == window_id
                and row.get("kCGWindowOwnerPID") == pid
                and row.get("kCGWindowLayer") == 0]
        if len(rows) != 1:
            raise ForegroundInputUnavailable("desktop_reviewed_cg_window_changed")
        rectangle = rows[0].get("kCGWindowBounds") or {}
        cg_bounds = tuple(float(rectangle.get(key, float("nan")))
                          for key in ("X", "Y", "Width", "Height"))
        if not all(math.isfinite(value) for value in cg_bounds) or min(cg_bounds[2:]) < 1:
            raise ForegroundInputUnavailable("desktop_reviewed_cg_window_invalid")

        running = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is None or running.isTerminated():
            raise ForegroundInputUnavailable("desktop_reviewed_app_exited")
        if not running.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps):
            raise ForegroundInputUnavailable("desktop_native_activation_failed")
    except ForegroundInputUnavailable:
        raise
    except Exception as error:
        raise ForegroundInputUnavailable("desktop_native_focus_unavailable") from error


def _focused_editable(pid: int, bounds: tuple[float, float, float, float]) -> str | None:
    """Return the focused field's value only when it belongs to this window."""
    try:
        import ApplicationServices as AS

        system = AS.AXUIElementCreateSystemWide()
        error, element = AS.AXUIElementCopyAttributeValue(
            system, AS.kAXFocusedUIElementAttribute, None)
        if error != 0 or element is None:
            return None
        error, owner_pid = AS.AXUIElementGetPid(element, None)
        if error != 0 or owner_pid != pid:
            return None
        def attribute(name):
            code, value = AS.AXUIElementCopyAttributeValue(element, name, None)
            return value if code == 0 else None
        if str(attribute(AS.kAXRoleAttribute) or "") not in {
                "AXTextField", "AXSearchField", "AXComboBox", "AXTextArea"}:
            return None
        position = attribute(AS.kAXPositionAttribute)
        size = attribute(AS.kAXSizeAttribute)
        if position is None or size is None:
            return None
        _, point = AS.AXValueGetValue(position, AS.kAXValueCGPointType, None)
        _, extent = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        x, y, width, height = bounds
        if (not all(math.isfinite(v) for v in (point.x, point.y, extent.width, extent.height))
                or extent.width < 1 or extent.height < 1
                or point.x < x or point.y < y
                or point.x + extent.width > x + width
                or point.y + extent.height > y + height):
            return None
        value = attribute(AS.kAXValueAttribute)
        return value if isinstance(value, str) else None
    except Exception:
        return None


def reviewed_field_matches(*, pid: int,
                           window_bounds: tuple[float, float, float, float],
                           expected_text: str) -> bool:
    """Read-only check before a reviewed Return; never synthesize input here."""
    return bool(expected_text) and _focused_editable(pid, window_bounds) == expected_text


def _post(event) -> None:
    import Quartz
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.008)


def _key(code: int, *, command: bool = False) -> None:
    import Quartz
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if command:
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        _post(event)


def type_in_reviewed_field(*, pid: int, window_bounds: tuple[float, float, float, float],
                           point: tuple[float, float], text: str) -> None:
    """Focus one observed field, clear it, and type bounded reviewed text."""
    if not text or len(text) > 320:
        raise ForegroundInputUnavailable("desktop_type_text_invalid")
    x, y, width, height = window_bounds
    if not (x <= point[0] <= x + width and y <= point[1] <= y + height):
        raise ForegroundInputUnavailable("desktop_field_outside_reviewed_window")
    import Quartz
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved,
                                         point, Quartz.kCGMouseButtonLeft))
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown,
                                         point, Quartz.kCGMouseButtonLeft))
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp,
                                         point, Quartz.kCGMouseButtonLeft))
    if _focused_editable(pid, window_bounds) is None:
        # A click may already have reached the app. The caller must reconcile.
        raise ForegroundInputUnavailable("desktop_focused_field_unconfirmed_after_click")
    _key(0, command=True)  # Select all in the focused field.
    for character in text:
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(character), character)
            _post(event)


def press_return_in_reviewed_field(*, pid: int,
                                   window_bounds: tuple[float, float, float, float],
                                   expected_text: str) -> None:
    """Submit only the field this worker just typed, with a fresh AX proof."""
    if not expected_text or _focused_editable(pid, window_bounds) != expected_text:
        raise ForegroundInputUnavailable("desktop_pre_dispatch_focused_field_changed")
    _key(36)
