"""The embedded driver must stay bound to the reviewed app/window."""
import base64
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from desktop_runtime.cua_driver import CuaDriverUnavailable, CuaWindowDriver
from desktop_runtime.cua_session import CuaSessionError
from desktop_runtime.peekaboo_driver import AppChoice


stream = BytesIO()
Image.new("RGB", (1, 1), "white").save(stream, format="PNG")
PNG = stream.getvalue()


class FakeSession:
    def __init__(self, _binary, _socket):
        self.calls = []
        self.closed = False
        self.host_pid = 1234
        self.image = PNG
        self.effect_result = None
        self.elements = [{"element_token": "bound-token", "role": "AXButton",
                          "label": "Open", "enabled": True, "actions": ["AXPress"],
                          "frame": {"x": 1, "y": 2, "w": 30, "h": 20}}]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "health_report":
            data = {"checks": [{"name": "bundle_identity", "status": "pass", "data": {
                "bundle_identifier": "com.companyos.app", "identity_source": "parent_application",
                "parent_process_id": self.host_pid}}]}
        elif name == "check_permissions":
            data = {"accessibility": True, "screen_recording": True,
                    "source": {"attribution": "host"}}
        elif name == "get_window_state":
            data = {"pid": 5678, "window_id": 99, "snapshot_id": "snapshot_1",
                    "elements": self.elements}
            return SimpleNamespace(isError=False, structuredContent=data,
                content=[SimpleNamespace(type="image", mimeType="image/png",
                                         data=base64.b64encode(self.image).decode())])
        else:
            if self.effect_result is not None:
                return self.effect_result
            data = {"effect": "returned"}
        return SimpleNamespace(isError=False, structuredContent=data, content=[])

    def close(self):
        self.closed = True


class ReplacementSession(FakeSession):
    """Model a native field and make the clear checkpoint observable."""

    def __init__(self, *_):
        super().__init__(*_)
        self.value = "old.example"
        self.selection_landed = True
        self.selected = False
        self.snapshot_count = 0
        self.elements = [{"element_index": 1, "parent_index": None,
                          "element_token": "field-0", "role": "AXTextField",
                          "label": "Address and Search", "value": self.value,
                          "enabled": True, "actions": [],
                          "frame": {"x": 10, "y": 10, "w": 300, "h": 25}}]

    def call(self, name, arguments):
        if name == "get_window_state":
            self.snapshot_count += 1
            field = next(i for i, item in enumerate(self.elements)
                         if item.get("role") == "AXTextField")
            self.elements[field] = {**self.elements[field], "value": self.value,
                                    "element_token": f"field-{self.snapshot_count}"}
        elif name == "hotkey":
            self.calls.append((name, arguments))
            # Foreground delivery restores the founder's prior app at the end
            # of this call; the selection is not durable across that handoff.
            self.selected = (self.selection_landed
                             and arguments.get("delivery_mode") == "background")
            return SimpleNamespace(isError=False, structuredContent={"effect": "unverifiable"},
                                   content=[])
        elif name == "press_key" and arguments.get("key") == "delete":
            self.value = "" if self.selected else self.value[:-1]
            self.selected = False
        elif name == "type_text":
            self.value = (arguments["text"] if self.selected
                          else self.value + arguments["text"])
            self.selected = False
        return super().call(name, arguments)


def test_cua_click_is_bound_to_reviewed_window_and_background(monkeypatch):
    fake = FakeSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver({"action": "capture", "app": app.bundle_id}, session_id="review")
        snapshot = capture["snapshot_id"]
        assert snapshot.startswith("ps1_")
        assert driver.snapshot_ready_for_admission(snapshot)
        driver({"action": "click", "app": app.bundle_id, "snapshot_id": snapshot,
                "element_id": "elem_1"}, session_id="review")
        assert fake.calls[-1] == ("click", {"target": {"kind": "window", "pid": 5678,
            "window_id": 99}, "element_token": "bound-token", "delivery_mode": "background"})
        assert not driver.snapshot_ready_for_admission(snapshot)
    finally:
        driver.close()


def test_ax_set_value_field_is_type_only_and_cannot_dispatch_click(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "field-token", "role": "AXTextField",
                      "label": "Smart Search Field", "enabled": True,
                      "actions": ["AXSetValue"],
                      "frame": {"x": 10, "y": 10, "w": 300, "h": 25}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        field = capture["elements"][0]
        assert field["text_entry"] is True
        assert field["browser_address"] is True
        assert field["actionable"] is False
        with pytest.raises(CuaDriverUnavailable, match="^desktop_pre_dispatch_action_unavailable$"):
            driver({"action": "click", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": field["id"]},
                   session_id="review")
        assert driver.snapshot_ready_for_admission(capture["snapshot_id"])
        assert all(name != "click" for name, _ in fake.calls)
    finally:
        driver.close()


def test_non_press_button_is_only_a_reviewed_point_candidate(monkeypatch):
    fake = FakeSession("", "")
    image = BytesIO()
    Image.new("RGB", (800, 600), "white").save(image, format="PNG")
    fake.image = image.getvalue()
    fake.elements = [{"element_token": "button-token", "role": "AXButton",
                      "label": "Open", "enabled": True, "actions": ["AXShowMenu"],
                      "frame": {"x": 100, "y": 200, "w": 80, "h": 40}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    monkeypatch.setattr("desktop_runtime.cua_driver._vision_text", lambda *_: [])
    app = AppChoice("Example", "com.example.app", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        assert capture["elements"][0]["actionable"] is False
        assert capture["elements"][0]["point_candidate"] is True
    finally:
        driver.close()


@pytest.mark.parametrize("payload,expected", [
    ({"code": "window_target_not_found", "effect": "refused"},
     "desktop_cua_window_target_not_found"),
    ({"code": "window_id_not_found", "pid": 5678, "window_id": 99},
     "desktop_cua_window_target_not_found"),
    ({"code": "window_owner_pid_mismatch", "owner_pid": 8888},
     "desktop_cua_window_owner_pid_mismatch"),
    ({"status": "refused", "refusal": {"code": ["private"]}},
     "desktop_cua_observation_refused"),
    ({"code": {"private": "screen text"}}, "desktop_cua_observation_unpinned"),
    ({"status": "refused", "refusal": {"code": "screen_recording_denied",
     "message": "private screen text"}}, "desktop_cua_observation_refused"),
])
def test_cua_capture_preserves_safe_structured_refusal(monkeypatch, payload, expected):
    fake = FakeSession("", "")
    original_call = fake.call

    def call(name, arguments):
        if name == "get_window_state":
            return SimpleNamespace(isError=True, structuredContent=payload,
                                   content=[SimpleNamespace(type="text", text="private screen text")])
        return original_call(name, arguments)

    fake.call = call
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        with pytest.raises(CuaDriverUnavailable, match=f"^{expected}$"):
            driver.capture()
        assert not driver.snapshot_ready_for_admission("snapshot_1")
        assert [name for name, _ in fake.calls].count("click") == 0
    finally:
        driver.close()


def test_cua_ocr_click_uses_fresh_exact_window_screenshot_pixels(monkeypatch):
    fake = FakeSession("", "")
    image = BytesIO()
    Image.new("RGB", (800, 600), "white").save(image, format="PNG")
    fake.image = image.getvalue()
    fake.elements = []  # Custom-drawn chooser with no actionable AX controls.
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    monkeypatch.setattr("desktop_runtime.cua_driver._vision_text", lambda *_: [{
        "label": "Personal profile", "bounds": {"x": 100, "y": 200,
        "width": 120, "height": 40}, "confidence": 0.92}])
    app = AppChoice("Chrome", "com.google.Chrome", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        assert capture["elements"] == [{"id": "ocr_1", "role": "OCRText",
            "label": "Personal profile", "readout": False, "actionable": False,
            "confidence": 0.92, "has_point": True, "browser_address": False,
            "observed_url": None, "text_entry": False, "enabled": True}]
        driver({"action": "click", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": "ocr_1"},
               session_id="review")
        assert fake.calls[-1] == ("click", {"target": {"kind": "window",
            "pid": 5678, "window_id": 99}, "delivery_mode": "background",
            "x": 160.0, "y": 220.0})
        with pytest.raises(CuaDriverUnavailable, match="desktop_fresh_observation_required"):
            driver({"action": "click", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": "ocr_1"},
                   session_id="review")
        assert [name for name, _ in fake.calls].count("click") == 1
    finally:
        driver.close()


def test_labelled_ax_button_without_press_is_only_a_bounded_point_candidate(monkeypatch):
    fake = FakeSession("", "")
    image = BytesIO()
    Image.new("RGB", (800, 600), "white").save(image, format="PNG")
    fake.image = image.getvalue()
    fake.elements = [{"element_token": "button-token", "role": "AXButton",
                      "label": "Next", "enabled": True, "actions": [],
                      "frame": {"x": 100, "y": 200, "w": 80, "h": 40}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    monkeypatch.setattr("desktop_runtime.cua_driver._vision_text", lambda *_: [])
    app = AppChoice("Example", "com.example.app", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        assert capture["elements"][0]["actionable"] is False
        assert capture["elements"][0]["point_candidate"] is True
        driver({"action": "click", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": "elem_1"},
               session_id="review")
        assert fake.calls[-1] == ("click", {"target": {"kind": "window", "pid": 5678,
            "window_id": 99}, "delivery_mode": "background", "x": 140.0, "y": 220.0})
        assert not driver.snapshot_ready_for_admission(capture["snapshot_id"])
    finally:
        driver.close()


def test_cua_exposes_dialog_text_over_a_rich_ax_window(monkeypatch):
    fake = FakeSession("", "")
    image = BytesIO()
    Image.new("RGB", (800, 600), "white").save(image, format="PNG")
    fake.image = image.getvalue()
    fake.elements = [{"element_token": f"token-{index}", "role": "AXButton",
                      "label": f"Page action {index}", "enabled": True,
                      "actions": ["AXPress"],
                      "frame": {"x": index * 50, "y": 10, "w": 30, "h": 20}}
                     for index in range(4)]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    monkeypatch.setattr("desktop_runtime.cua_driver._vision_text", lambda *_: [{
        "label": "OK", "bounds": {"x": 400, "y": 350,
        "width": 100, "height": 35}, "confidence": 0.95}])
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        assert len([item for item in capture["elements"] if item["actionable"]]) == 4
        dialog = next(item for item in capture["elements"] if item["label"] == "OK")
        assert dialog["id"].startswith("ocr_")
        driver({"action": "click", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": dialog["id"]},
               session_id="review")
        assert "snapshot_id" not in fake.calls[-1][1]
        assert fake.calls[-1][1]["target"]["window_id"] == 99
    finally:
        driver.close()


@pytest.mark.parametrize("data,expected", [
    ({"status": "refused", "refusal": {"code": "stale_element_token"}},
     "desktop_pre_dispatch_cua_stale_element_token"),
    ({"code": "window_target_not_found", "effect": "refused"},
     "desktop_pre_dispatch_cua_window_target_not_found"),
    ({"status": "refused", "refusal": {"code": "private@example.com"}},
     "desktop_pre_dispatch_cua_refused"),
    ({"status": "refused", "refusal": {"code": "stale_element_token"},
      "mutation_dispatched": True}, "desktop_cua_effect_uncertain"),
])
def test_cua_click_refusal_uses_bounded_native_contract(monkeypatch, data, expected):
    fake = FakeSession("", "")
    fake.effect_result = SimpleNamespace(isError=True, structuredContent=data, content=[])
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        with pytest.raises(CuaDriverUnavailable, match=f"^{expected}$"):
            driver({"action": "click", "app": app.bundle_id, "snapshot_id": snapshot,
                    "element_id": "elem_1"}, session_id="review")
        assert [name for name, _ in fake.calls].count("click") == 1
    finally:
        driver.close()


def test_cua_click_transport_failure_keeps_outcome_uncertain(monkeypatch):
    fake = FakeSession("", "")
    original = fake.call
    def call(name, arguments):
        if name == "click":
            raise CuaSessionError("desktop_cua_transport_unavailable")
        return original(name, arguments)
    fake.call = call
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        with pytest.raises(CuaDriverUnavailable, match="^desktop_cua_transport_unavailable$"):
            driver({"action": "click", "app": app.bundle_id, "snapshot_id": snapshot,
                    "element_id": "elem_1"}, session_id="review")
    finally:
        driver.close()


def test_cua_rejects_wrong_host_identity_before_effect(monkeypatch):
    fake = FakeSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    with pytest.raises(CuaDriverUnavailable, match="desktop_cua_host_identity_mismatch"):
        CuaWindowDriver(app=app, window_id=99,
                        socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=9999)
    assert fake.closed
    assert all(name not in {"click", "type_text", "press_key"} for name, _ in fake.calls)


def test_visible_target_gets_overlay_without_real_pointer_motion(monkeypatch):
    fake = FakeSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    front = SimpleNamespace(bundleIdentifier=lambda: "com.apple.finder")
    workspace = SimpleNamespace(frontmostApplication=lambda: front)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        result = driver({"action": "click", "app": app.bundle_id, "snapshot_id": snapshot,
                         "element_id": "elem_1"}, session_id="review")
        assert result["cursor_indicator"] == "overlay_requested"
        assert fake.calls[-2] == ("move_cursor", {"x": 16.0, "y": 12.0, "scope": "window"})
        assert fake.calls[-1][0] == "click"
    finally:
        driver.close()


def test_cua_text_entry_and_return_stay_bound_to_reviewed_window(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "address-token", "role": "AXTextField",
                      "label": "Smart Search", "enabled": True, "actions": ["AXSetValue"],
                      "frame": {"x": 10, "y": 10, "w": 300, "h": 25}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        first = driver.capture()
        assert first["elements"][0]["text_entry"] is True
        assert first["elements"][0]["browser_address"] is True
        driver({"action": "type", "app": app.bundle_id, "snapshot_id": first["snapshot_id"],
                "element_id": "elem_1", "text": "https://example.org/"}, session_id="review")
        assert fake.calls[-2] == ("set_value", {"pid": 5678, "window_id": 99,
            "element_token": "address-token", "value": "https://example.org/"})
        assert fake.calls[-1][0] == "get_window_state"  # read-back after the write
        assert [name for name, _ in fake.calls if name in {"type_text", "press_key"}] == []
        with pytest.raises(CuaDriverUnavailable, match="desktop_fresh_observation_required"):
            driver({"action": "key", "app": app.bundle_id, "snapshot_id": first["snapshot_id"],
                    "chord": "Return"}, session_id="review")
        fake.elements[0]["element_token"] = "fresh-address-token"
        second = driver.capture()
        driver({"action": "key", "app": app.bundle_id, "snapshot_id": second["snapshot_id"],
                "chord": "Return", "element_id": "elem_1"}, session_id="review")
        assert fake.calls[-1] == ("click", {"target": {
            "kind": "window", "pid": 5678, "window_id": 99},
            "element_token": "fresh-address-token", "action": "confirm",
            "delivery_mode": "background"})
    finally:
        driver.close()


def test_browser_address_set_value_refusal_never_types(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "address-token", "role": "AXTextField",
                      "label": "Smart Search Field", "enabled": True, "actions": ["AXSetValue"],
                      "frame": {"x": 10, "y": 10, "w": 300, "h": 25}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        fake.effect_result = SimpleNamespace(isError=True,
            structuredContent={"status": "refused", "refusal": {"code": "no_effect"}},
            content=[])
        with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_cua_no_effect"):
            driver({"action": "type", "app": app.bundle_id, "snapshot_id": snapshot,
                    "element_id": "elem_1", "text": "https://example.net/"}, session_id="review")
        assert [name for name, _ in fake.calls if name == "set_value"] == ["set_value"]
        assert [name for name, _ in fake.calls if name == "type_text"] == []
    finally:
        driver.close()


def test_native_ax_text_field_replaces_value_without_select_all(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [
        {"element_index": 1, "parent_index": None, "element_token": "window",
         "role": "AXWindow", "label": "Document"},
        {"element_index": 2, "parent_index": 1, "element_token": "field",
         "role": "AXTextField", "label": "Title", "value": "Old title",
         "enabled": True, "actions": ["AXSetValue"],
         "frame": {"x": 10, "y": 10, "w": 300, "h": 25}},
    ]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    original_call = fake.call

    def applying_set_value(name, arguments):
        if name == "set_value":
            fake.elements[1] = {**fake.elements[1], "value": arguments["value"]}
        return original_call(name, arguments)

    fake.call = applying_set_value
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        field = next(item for item in capture["elements"] if item["label"] == "Title")
        assert "Old title" not in str(capture["elements"])
        driver({"action": "type", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": field["id"],
                "text": "New title"}, session_id="review")
        assert fake.calls[-2] == ("set_value", {"pid": 5678, "window_id": 99,
            "element_token": "field", "value": "New title"})
        assert [name for name, _ in fake.calls if name in {"press_key", "type_text"}] == []
    finally:
        driver.close()


def test_web_address_label_cannot_take_native_replacement_route(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [
        {"element_index": 1, "parent_index": None, "element_token": "web",
         "role": "AXWebArea", "label": "Page"},
        {"element_index": 2, "parent_index": 1, "element_token": "field",
         "role": "AXTextField", "label": "Address and Search", "enabled": True,
         "actions": ["AXSetValue"],
         "frame": {"x": 10, "y": 10, "w": 300, "h": 25}},
    ]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        field = next(item for item in capture["elements"] if item["label"] == "Address and Search")
        with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_action_unavailable"):
            driver({"action": "type", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": field["id"],
                    "text": "https://example.org/"}, session_id="review")
        assert all(name not in {"set_value", "type_text", "press_key"} for name, _ in fake.calls)
    finally:
        driver.close()


def test_native_text_fallback_keeps_selection_in_background_and_verifies_clear(monkeypatch):
    fake = ReplacementSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        result = driver({"action": "type", "app": app.bundle_id,
                         "snapshot_id": capture["snapshot_id"], "element_id": "elem_1",
                         "text": "https://example.org/"}, session_id="review")
        assert result["effect"] == "unverifiable"
        assert fake.value == "https://example.org/"
        # The AX write is tried first; this fake ignores it, so keys are the fallback.
        assert [name for name, _ in fake.calls[3:7]] == ["set_value", "get_window_state"] * 2
        calls = fake.calls[7:]
        assert [name for name, _ in calls] == ["hotkey", "press_key", "get_window_state",
                                                "type_text", "get_window_state"]
        assert calls[0][1] == {"target": {"kind": "window", "pid": 5678,
            "window_id": 99}, "element_token": "field-1", "keys": ["cmd", "a"],
            "delivery_mode": "background"}
        assert calls[1][1]["element_token"] == "field-1"
        assert calls[1][1]["delivery_mode"] == "background"
        assert calls[3][1]["element_token"] == "field-4"
        assert calls[3][1]["delivery_mode"] == "background"
        assert all(params.get("target", {}).get("window_id") == 99
                   for name, params in calls if name in {"hotkey", "press_key", "type_text"})
    finally:
        driver.close()


def test_native_text_fallback_never_inserts_after_failed_selection(monkeypatch):
    fake = ReplacementSession("", "")
    fake.selection_landed = False
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        with pytest.raises(CuaDriverUnavailable,
                           match="^cu_field_not_cleared$"):
            driver({"action": "type", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": "elem_1",
                    "text": "https://example.org/"}, session_id="review")
        assert fake.value == "old.exampl"
        assert all(name != "type_text" for name, _ in fake.calls)
        assert not driver.snapshot_ready_for_admission(capture["snapshot_id"])
    finally:
        driver.close()


def test_native_text_fallback_reports_selection_refusal_before_insert(monkeypatch):
    fake = ReplacementSession("", "")
    original_call = fake.call

    def refused_selection(name, arguments):
        if name == "hotkey":
            fake.calls.append((name, arguments))
            return SimpleNamespace(isError=True, structuredContent={
                "status": "refused", "refusal": {"code": "background_unavailable"},
                "mutation_dispatched": False}, content=[])
        return original_call(name, arguments)

    fake.call = refused_selection
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        with pytest.raises(CuaDriverUnavailable,
                           match="^desktop_cua_effect_uncertain_select$"):
            driver({"action": "type", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": "elem_1",
                    "text": "New title"}, session_id="review")
        assert fake.value == "old.example"
        assert all(name != "type_text" for name, _ in fake.calls)
    finally:
        driver.close()


def test_native_text_fallback_is_not_browser_specific(monkeypatch):
    fake = ReplacementSession("", "")
    fake.elements.insert(0, {"element_index": 2, "parent_index": None,
                             "element_token": "window", "role": "AXWindow",
                             "label": "Document"})
    fake.elements[1]["parent_index"] = 2
    fake.elements[1]["label"] = "Title"
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        field = next(item for item in capture["elements"] if item["label"] == "Title")
        driver({"action": "type", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": field["id"],
                "text": "New title"}, session_id="review")
        assert fake.value == "New title"
        assert [name for name, _ in fake.calls].count("type_text") == 1
    finally:
        driver.close()


def test_web_ax_text_field_keeps_renderer_typing_route(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [
        {"element_index": 1, "parent_index": None, "element_token": "web",
         "role": "AXWebArea", "label": "Page"},
        {"element_index": 2, "parent_index": 1, "element_token": "field",
         "role": "AXTextField", "label": "Search", "enabled": True,
         "actions": ["AXSetValue"],
         "frame": {"x": 10, "y": 10, "w": 300, "h": 25}},
    ]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        field = next(item for item in capture["elements"] if item["label"] == "Search")
        driver({"action": "type", "app": app.bundle_id,
                "snapshot_id": capture["snapshot_id"], "element_id": field["id"],
                "text": "cats"}, session_id="review")
        assert fake.calls[-1] == ("type_text", {"target": {"kind": "window",
            "pid": 5678, "window_id": 99}, "text": "cats",
            "x": 160.0, "y": 22.5, "delivery_mode": "background"})
        assert all(name != "set_value" for name, _ in fake.calls)
    finally:
        driver.close()


def test_browser_address_without_ax_set_value_refuses_before_dispatch(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "address-token", "role": "AXTextField",
                      "label": "Smart Search", "enabled": True, "actions": [],
                      "frame": {"x": 10, "y": 10, "w": 300, "h": 25}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_action_unavailable"):
            driver({"action": "type", "app": app.bundle_id,
                    "snapshot_id": capture["snapshot_id"], "element_id": "elem_1",
                    "text": "https://example.org/"}, session_id="review")
        assert all(name not in {"set_value", "type_text", "press_key"} for name, _ in fake.calls)
    finally:
        driver.close()


def test_cua_preserves_observed_link_value_without_exposing_other_field_values(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [
        {"element_token": "link", "role": "AXLink", "label": "Pet marketplace",
         "value": "https://pets.example.org/listings", "enabled": True},
        {"element_token": "draft", "role": "AXTextField", "label": "Search",
         "value": "private draft text", "enabled": True},
        {"element_token": "unnamed", "role": "AXTextField", "value": "private unnamed draft",
         "enabled": True},
        {"element_token": "bad", "role": "AXLink", "label": "Invalid",
         "value": "https://[invalid", "enabled": True},
    ]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        elements = driver.capture()["elements"]
        assert elements[0]["observed_url"] == "https://pets.example.org/listings"
        assert elements[1]["observed_url"] is None
        assert elements[2]["label"] == ""
        assert elements[2]["observed_url"] is None
        assert elements[3]["observed_url"] is None
        assert "private draft text" not in str(elements)
        assert "private unnamed draft" not in str(elements)
    finally:
        driver.close()


def test_cua_window_snapshot_does_not_offer_global_menu_controls(monkeypatch):
    fake = FakeSession("", "")
    fake.elements = [
        {"element_token": "menu", "role": "AXMenuItem", "label": "Open Location…",
         "enabled": True, "actions": ["AXPress"]},
        {"element_token": "tab", "role": "AXMenuItem", "label": "New Tab",
         "enabled": True, "actions": ["AXPress"]},
        {"element_token": "address", "role": "AXTextField", "label": "Address and Search",
         "enabled": True, "actions": ["AXSetValue"],
         "frame": {"x": 10, "y": 10, "w": 300, "h": 25}},
    ]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Chrome", "com.google.Chrome", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        elements = driver.capture()["elements"]
        assert [item["label"] for item in elements] == ["Address and Search"]
        assert driver._elements["elem_1"]["element_token"] == "address"
    finally:
        driver.close()


def test_cua_wrong_app_scope_never_dispatches_an_effect(monkeypatch):
    fake = FakeSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Finder", "com.apple.finder", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        with pytest.raises(CuaDriverUnavailable, match="desktop_cua_scope_mismatch"):
            driver({"action": "click", "app": "com.apple.Safari", "snapshot_id": snapshot,
                    "element_id": "elem_1"}, session_id="review")
        assert all(name not in {"click", "type_text", "press_key"} for name, _ in fake.calls)
    finally:
        driver.close()


def test_cua_scroll_uses_only_the_reviewed_window_and_fresh_capture(monkeypatch):
    fake = FakeSession("", "")
    stream = BytesIO()
    Image.new("RGB", (800, 600), "white").save(stream, format="PNG")
    fake.image = stream.getvalue()
    fake.elements = []  # A rendered page can lack actionable AX elements.
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Notes", "com.apple.Notes", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver.capture()["snapshot_id"]
        driver({"action": "scroll", "app": app.bundle_id, "snapshot_id": snapshot,
                "direction": "down"}, session_id="review")
        assert fake.calls[-1] == ("scroll", {"direction": "down", "by": "page",
            "amount": 1, "target": {"kind": "window", "pid": 5678, "window_id": 99},
            "x": 400.0, "y": 300.0, "delivery_mode": "background"})
        with pytest.raises(CuaDriverUnavailable, match="desktop_fresh_observation_required"):
            driver({"action": "scroll", "app": app.bundle_id, "snapshot_id": snapshot,
                    "direction": "down"}, session_id="review")
        assert [name for name, _ in fake.calls].count("scroll") == 1
    finally:
        driver.close()


def test_typed_text_is_normalized_and_stray_prefix_is_rejected(monkeypatch):
    fake = ReplacementSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        driver({"action": "type", "app": app.bundle_id, "snapshot_id": capture["snapshot_id"],
                "element_id": "elem_1", "text": "  https://example.org/\n"}, session_id="review")
        assert fake.value == "https://example.org/"
        # A key chord that leaks a literal "a" is caught by the read-back.
        original = fake.call

        def leaky(name, arguments):
            if name == "type_text":
                arguments = {**arguments, "text": "a" + arguments["text"]}
            return original(name, arguments)

        fake.call = leaky
        capture = driver.capture()
        with pytest.raises(CuaDriverUnavailable, match="^cu_field_value_mismatch$"):
            driver({"action": "type", "app": app.bundle_id, "snapshot_id": capture["snapshot_id"],
                    "element_id": "elem_1", "text": "https://www.google.com"}, session_id="review")
    finally:
        driver.close()


def test_blank_text_is_refused_before_dispatch(monkeypatch):
    fake = ReplacementSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_text_invalid"):
            driver({"action": "type", "app": app.bundle_id, "snapshot_id": capture["snapshot_id"],
                    "element_id": "elem_1", "text": " \n "}, session_id="review")
        assert all(name not in {"hotkey", "type_text"} for name, _ in fake.calls)
    finally:
        driver.close()


def _hidden_browser_session():
    """Hidden/off-Space browser: CUA refuses raw key input, AX writes work."""
    fake = ReplacementSession("", "")
    original_call = fake.call

    def call(name, arguments):
        if name in {"hotkey", "press_key"}:
            fake.calls.append((name, arguments))
            return SimpleNamespace(isError=True, structuredContent={
                "code": "minimized_or_hidden_window", "effect": "refused",
                "reason": "window 99 is minimized or its application is hidden\nsecret=1"},
                content=[])
        if name == "set_value":
            fake.calls.append((name, arguments))
            fake.value = arguments["value"]
            return SimpleNamespace(isError=False, structuredContent={"effect": "confirmed"}, content=[])
        return original_call(name, arguments)

    fake.call = call
    return fake


def _address_type(driver, app, text):
    capture = driver.capture()
    driver({"action": "type", "app": app.bundle_id, "snapshot_id": capture["snapshot_id"],
            "element_id": "elem_1", "text": text}, session_id="review")


def test_browser_address_uses_ax_set_value_when_hidden_browser_refuses_keys(monkeypatch):
    fake = _hidden_browser_session()
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        _address_type(driver, app, "https://example.org/")
        assert fake.value == "https://example.org/"
        assert [n for n, _ in fake.calls if n in {"hotkey", "press_key", "type_text"}] == []
    finally:
        driver.close()


def test_browser_address_ignored_ax_write_falls_back_and_records_raw_refusal(monkeypatch):
    fake = _hidden_browser_session()
    ignored = fake.call

    def call(name, arguments):
        if name == "set_value":  # Browser accepts the call but reverts the value.
            return SimpleNamespace(isError=False, structuredContent={"effect": "confirmed"}, content=[])
        return ignored(name, arguments)

    fake.call = call
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        with pytest.raises(CuaDriverUnavailable, match="^desktop_cua_effect_uncertain_select$") as raised:
            _address_type(driver, app, "https://example.org/")
        detail = raised.value.detail
        assert detail.startswith("minimized_or_hidden_window: window 99 is minimized")
        assert "\n" not in detail and "=" not in detail and len(detail) <= 200
        assert all(name != "type_text" for name, _ in fake.calls)
    finally:
        driver.close()


def test_loaded_page_address_is_cleared_and_confirmed_before_the_reviewed_write(monkeypatch):
    """Live Safari: an AX write over a loaded URL only navigates after an
    empty-field AXConfirm opens the edit session."""
    fake = _hidden_browser_session()
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        _address_type(driver, app, "audit-qa.supymem.com")
        effects = [(n, a.get("value", a.get("action"))) for n, a in fake.calls
                   if n in {"set_value", "click"}]
        assert effects == [("set_value", ""), ("click", "confirm"),
                           ("set_value", "audit-qa.supymem.com")]
        assert fake.value == "audit-qa.supymem.com"
        assert all(n not in {"hotkey", "press_key", "type_text"} for n, _ in fake.calls)
    finally:
        driver.close()


def test_partial_browser_ax_walk_is_rewalked_until_the_address_field_appears(monkeypatch):
    fake = ReplacementSession("", "")
    original = fake.call
    budgets = []
    full = list(fake.elements)

    def call(name, arguments):
        if name == "get_window_state":
            budgets.append(arguments.get("timeout_ms"))
            # First walk ran out of time: sidebar only, no toolbar/address field.
            fake.elements = ([{"element_token": "t", "role": "AXButton", "label": "Forward",
                               "enabled": True, "actions": ["AXPress"],
                               "frame": {"x": 1, "y": 2, "w": 30, "h": 20}}]
                             if len(budgets) == 1 else full)
            if len(budgets) == 1:
                result = FakeSession.call(fake, name, arguments)
                return result
        return original(name, arguments)

    fake.call = call
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    monkeypatch.setattr("desktop_runtime.cua_driver.time.sleep", lambda *_: None)
    app = AppChoice("Safari", "com.apple.Safari", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        capture = driver.capture()
        assert any(item["browser_address"] for item in capture["elements"])
        assert len(budgets) == 2 and min(budgets) >= 5000
    finally:
        driver.close()


def test_unpinned_observation_reports_which_pin_failed(monkeypatch):
    fake = FakeSession("", "")
    original = fake.call

    def call(name, arguments):
        result = original(name, arguments)
        if name == "get_window_state":
            result.structuredContent["window_id"] = 7
        return result

    fake.call = call
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("Notes", "com.apple.Notes", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        with pytest.raises(CuaDriverUnavailable, match="^desktop_cua_observation_unpinned$") as raised:
            driver.capture()
        assert "window_id=7" in raised.value.detail
    finally:
        driver.close()


def test_cua_chord_is_a_fixed_background_hotkey_in_the_reviewed_window(monkeypatch):
    fake = FakeSession("", "")
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
                             socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        snapshot = driver({"action": "capture", "app": app.bundle_id}, session_id="review")["snapshot_id"]
        driver({"action": "key", "app": app.bundle_id, "snapshot_id": snapshot, "chord": "cmd+s"},
               session_id="review")
        assert fake.calls[-1] == ("hotkey", {"target": {"kind": "window", "pid": 5678, "window_id": 99},
                                             "keys": ["cmd", "s"], "delivery_mode": "background"})
        snapshot = driver({"action": "capture", "app": app.bundle_id}, session_id="review")["snapshot_id"]
        with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_action_unavailable"):
            driver({"action": "key", "app": app.bundle_id, "snapshot_id": snapshot, "chord": "cmd+q"},
                   session_id="review")
    finally:
        driver.close()


def _driver(monkeypatch, fake, app):
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    return CuaWindowDriver(app=app, window_id=99,
                           socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)


def test_cold_first_observation_timeout_is_retried_once_without_an_effect(monkeypatch):
    fake = FakeSession("", "")
    real, failures = fake.call, [1]

    def flaky(name, arguments):
        if name == "get_window_state" and failures:
            failures.pop()
            fake.calls.append((name, arguments))
            raise CuaSessionError("desktop_cua_transport_unavailable")
        return real(name, arguments)
    fake.call = flaky
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = _driver(monkeypatch, fake, app)
    assert driver({"action": "capture", "app": app.bundle_id}, session_id="r")["snapshot_id"]
    assert [n for n, _ in fake.calls].count("get_window_state") == 2
    assert not {"click", "type_text", "set_value", "hotkey"} & {n for n, _ in fake.calls}


def test_native_app_with_truncated_tree_is_walked_once(monkeypatch):
    fake = FakeSession("", "")
    real = fake.call

    def truncated(name, arguments):
        result = real(name, arguments)
        if name == "get_window_state":
            result.structuredContent.update(truncated=True, nodes_pending=10)
        return result
    fake.call = truncated
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = _driver(monkeypatch, fake, app)
    driver({"action": "capture", "app": app.bundle_id}, session_id="r")
    assert [n for n, _ in fake.calls].count("get_window_state") == 1


def test_unpinned_detail_never_copies_vendor_screen_text():
    from desktop_runtime.cua_driver import _unpinned_detail
    detail = _unpinned_detail({"pid": "private screen text", "window_id": 99,
                              "degraded_reason": "ax_window_unresolved: private screen text",
                              "elements": [], "snapshot_id": None})
    assert "private" not in detail
    assert "degraded=ax_window_unresolved" in detail
    assert "snapshot=False" in detail and "pid=invalid" in detail


@pytest.mark.parametrize("refused", [False, True])
def test_textedit_document_uses_exact_semantic_token_without_key_or_pixel_fallback(monkeypatch, refused):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "document-token", "role": "AXTextArea",
        "value": "private existing draft", "enabled": True, "actions": [],
        "frame": {"x": 1, "y": 2, "w": 300, "h": 200}}]
    if refused:
        fake.effect_result = SimpleNamespace(isError=True, structuredContent={
            "effect": "refused", "refusal": {"code": "element_outside_target_window"}}, content=[])
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    app = AppChoice("TextEdit", "com.apple.TextEdit", 5678, "start-identity")
    driver = CuaWindowDriver(app=app, window_id=99,
        socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        captured = driver.capture()
        assert captured["elements"][0]["label"] == "Document text area"
        assert "private existing draft" not in str(captured["elements"])
        args = {"action": "type", "app": app.bundle_id, "snapshot_id": captured["snapshot_id"],
                "element_id": "elem_1", "text": "Reviewed document text"}
        if refused:
            with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_cua_element_outside_target_window"):
                driver(args, session_id="review")
        else:
            assert driver(args, session_id="review")["effect"] == "returned"
        effects = [(name, params) for name, params in fake.calls
                   if name in {"type_text", "set_value", "press_key", "hotkey"}]
        assert effects == [("type_text", {"target": {"kind": "window", "pid": 5678, "window_id": 99},
            "element_token": "document-token", "text": "Reviewed document text",
            "delivery_mode": "background"})]
    finally:
        driver.close()


@pytest.mark.parametrize("bundle,role,valid", [
    ("com.apple.TextEdit", "AXTextArea", True),
    ("com.apple.TextEdit", "AXComboBox", False),
    ("com.apple.Safari", "AXTextArea", False),
])
def test_reviewed_three_lines_preserved_only_for_exact_textedit_document(monkeypatch, bundle, role, valid):
    fake = FakeSession("", "")
    fake.elements = [{"element_token": "document-token", "role": role, "label": "Document",
        "enabled": True, "actions": [], "frame": {"x": 1, "y": 2, "w": 300, "h": 200}}]
    monkeypatch.setattr("desktop_runtime.cua_driver.CuaSession", lambda *_: fake)
    driver = CuaWindowDriver(app=AppChoice("App", bundle, 5678, "identity"), window_id=99,
        socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234)
    try:
        captured = driver.capture()
        args = {"action": "type", "app": bundle, "snapshot_id": captured["snapshot_id"],
            "element_id": "elem_1", "text": "Exact first\nExact second\nExact third",
            "text_entry_profile": "textedit_three_lines_v1"}
        if valid:
            from coding_agents.computer_use_gate import ComputerUseGate
            observed = []
            def authorize(_order, _session, payload):
                assert payload["text"] == args["text"]
                return True
            gate = ComputerUseGate(order_id="dwo_review", session_id="review", driver=driver,
                authorize=authorize, admit_effect=lambda *facts: observed.append(facts) or True)
            gate.invoke("din_document", args)
            assert len(observed) == 1
            call = next(params for name, params in fake.calls if name == "type_text")
            assert call["text"] == args["text"] and call["element_token"] == "document-token"
            assert "x" not in call and "y" not in call
        else:
            with pytest.raises(CuaDriverUnavailable, match="desktop_pre_dispatch_text_invalid"):
                driver(args, session_id="review")
            assert not any(name in {"type_text", "hotkey", "press_key", "set_value"} for name, _ in fake.calls)
    finally:
        driver.close()


@pytest.mark.parametrize('value,match', [('Reviewed text', True), ('Reviewed text appended', False), ('Private unrelated draft', False)])
def test_document_readback_matches_private_fresh_same_window_ax_only(monkeypatch, value, match):
    fake = FakeSession('', '')
    fake.elements = [{'element_index': 7, 'element_token': 'document-token', 'role': 'AXTextArea',
        'label': '', 'value': value, 'enabled': True, 'actions': [],
        'frame': {'x': 0, 'y': 0, 'w': 1, 'h': 1}}]
    monkeypatch.setattr('desktop_runtime.cua_driver.CuaSession', lambda *_: fake)
    monkeypatch.setattr('desktop_runtime.cua_driver._vision_text', lambda *_: [])
    driver = CuaWindowDriver(app=AppChoice('TextEdit', 'com.apple.TextEdit', 5678, 'birth'),
        window_id=99, socket='/private/tmp/supymem-cua-test/driver.sock', host_pid=1234)
    try:
        snapshot = driver.capture()
        assert value not in str(snapshot)
        observed = driver.document_text_observation(snapshot['snapshot_id'], 'Reviewed text')
        assert observed['matches'] is match and observed['pid'] == 5678 and observed['window_id'] == 99
        assert observed['element_index'] == 7 and value not in str(observed)
        assert driver.document_text_observation('ps1_stale', 'Reviewed text') is None
        driver._snapshot_at -= 21
        assert driver.document_text_observation(snapshot['snapshot_id'], 'Reviewed text') is None
        fake.elements.append(dict(fake.elements[0]))
        second = driver.capture()
        assert driver.document_text_observation(second['snapshot_id'], 'Reviewed text') is None
    finally:
        driver.close()
