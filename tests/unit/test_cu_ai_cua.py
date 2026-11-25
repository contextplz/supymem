"""Background-first cua backend: every driver call is pinned to the target pid, off-limits apps are
refused, AX writes that did not land escalate only through the guarded path, key chords map correctly."""
import base64

import pytest
from coding_agents.cu_ai_loop import Action
from desktop_runtime.cu_ai_cua import CuaError, CuaMac
from desktop_runtime.cu_ai_engine import NeedsHuman

PNG = base64.b64encode(b"\x89PNG-fake").decode()


def act(op, **kw):
    return Action(op, **kw)


class FakeDriver:
    def __init__(self, on_screen=True):
        self.calls, self.on_screen = [], on_screen
        self.value = "Untitled"
        self.write_lands = True

    def __call__(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_apps":
            return {"apps": [{"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 4242, "running": True}]}
        if tool == "launch_app":
            return {"pid": 4242, "name": "TextEdit"}
        if tool == "list_windows":
            return {"windows": [{"window_id": 7, "pid": 4242, "title": "Doc", "layer": 0, "is_on_screen": self.on_screen,
                                 "z_index": 5, "bounds": {"width": 600, "height": 400}},
                                {"window_id": 8, "pid": 4242, "title": "", "layer": 0, "is_on_screen": False, "z_index": 9,
                                 "bounds": {"width": 1280, "height": 33}}]}   # menu-bar strip, ignored
        if tool == "get_window_state":
            return {"element_count": 3, "snapshot_id": "s1", "window_title": "Doc", "screenshot_png_b64": PNG,
                    "elements": [
                        {"element_index": 0, "role": "AXWindow", "label": "Doc", "frame": {"x": 0, "y": 0, "w": 600, "h": 400},
                         "element_token": "s1:0"},
                        {"element_index": 1, "role": "AXTextField", "label": "Name", "value": self.value,
                         "frame": {"x": 10, "y": 10, "w": 100, "h": 20}, "screenshot_frame": {"x": 20, "y": 20, "w": 200, "h": 40},
                         "element_token": "s1:1"},
                        {"element_index": 2, "role": "AXMenuItem", "label": "Noise", "element_token": "s1:2"}]}
        if tool == "set_value" and self.write_lands:
            self.value = args["value"]
        return {"effect": "confirmed", "verified": True}


def opened(driver=None):
    driver = driver or FakeDriver()
    mac = CuaMac(driver)
    mac.act(act("open_app", app="TextEdit"), None)
    return mac, driver


def test_observe_is_window_scoped_filters_noise_and_hides_menu_strips():
    mac, driver = opened()
    obs = mac.observe()
    assert obs.png.startswith(b"\x89PNG") and obs.pid == 4242 and obs.frontmost == "TextEdit"
    assert [e["id"] for e in obs.elements] == ["e0", "e1"]            # AXMenuItem noise dropped
    assert all(a.get("window_id") != 8 for t, a in driver.calls if t == "get_window_state")
    assert not any(t == "get_desktop_state" for t, _ in driver.calls)  # never the user's whole screen


def test_every_input_call_is_pinned_to_the_target_pid():
    mac, driver = opened()
    obs = mac.observe()
    for action in (act("click", element_id="e1"), act("type", text="hello"), act("key", chord="return"),
                   act("key", chord="cmd+s"), act("scroll", direction="down", amount=2)):
        mac.act(action, obs)
    sent = [a for t, a in driver.calls if t in {"click", "type_text", "press_key", "hotkey", "scroll"}]
    assert sent and all(a["target"] == {"kind": "window", "pid": 4242, "window_id": 7}
                       and a["delivery_mode"] == "background" for a in sent)


def test_off_limits_apps_cannot_become_the_target():
    mac = CuaMac(FakeDriver())
    with pytest.raises(CuaError):
        mac.act(act("open_app", app="Claude"), None)
    with pytest.raises(CuaError):
        mac.act(act("open_app", app="Terminal"), None)
    mac.set_target(99, "iTerm2")
    with pytest.raises(CuaError):
        mac.act(act("key", chord="return"), None)


def test_key_chords_map_to_press_key_or_hotkey_without_global_events():
    mac, driver = opened()
    obs = mac.observe()
    mac.act(act("key", chord="Enter"), obs)
    mac.act(act("key", chord="cmd+shift+g"), obs)
    assert ("press_key", {"target": {"kind": "window", "pid": 4242, "window_id": 7}, "delivery_mode": "background", "key": "return"}) in driver.calls
    assert ("hotkey", {"target": {"kind": "window", "pid": 4242, "window_id": 7}, "delivery_mode": "background", "keys": ["cmd", "shift", "g"]}) in driver.calls


def test_repeated_chords_never_implicitly_escalate():
    mac, driver = opened(FakeDriver(on_screen=True))
    obs = mac.observe()
    mac.act(act("key", chord="cmd+s"), obs)
    mac.act(act("key", chord="cmd+s"), obs)           # same chord retried: now delivered with brief foreground
    modes = [a.get("delivery_mode") for t, a in driver.calls if t == "hotkey"]
    assert modes == ["background", "background"]
    mac2, driver2 = opened(FakeDriver(on_screen=False))
    obs2 = mac2.observe()
    mac2.act(act("key", chord="cmd+s"), obs2)
    mac2.act(act("key", chord="cmd+s"), obs2)
    assert all(a.get("delivery_mode") == "background" for t, a in driver2.calls if t == "hotkey")


def test_type_with_element_replaces_via_ax_and_verifies_it_landed():
    mac, driver = opened()
    obs = mac.observe()
    out = mac.act(act("type", element_id="e1", text="supymem-note.txt"), obs)
    assert out.startswith("set AXTextField value") and driver.value == "supymem-note.txt"
    assert not any(t in {"type_text", "press_key"} for t, _ in driver.calls)


def test_ax_write_that_did_not_land_refuses_without_helper_instead_of_guessing():
    driver = FakeDriver()
    driver.write_lands = False
    mac, _ = opened(driver)
    obs = mac.observe()
    with pytest.raises(CuaError):   # no foreground helper wired: a clear failure the model can see
        mac.act(act("type", element_id="e1", text="supymem-note.txt"), obs)


def test_unbound_text_refuses_before_driver_input():
    mac, driver = opened()
    obs = mac.observe()
    with pytest.raises(CuaError, match="fresh_text_element_required"):
        mac.act(act("type", text="a\nb"), obs)
    assert not any(t in {"type_text", "press_key"} for t, _ in driver.calls)


def test_clicked_multiline_text_uses_single_token_bound_call():
    mac, driver = opened()
    obs = mac.observe()
    mac.act(act("click", element_id="e1"), obs)
    mac.act(act("type", text="a\nb\\n"), obs)
    calls = [(t, a) for t, a in driver.calls if t in {"type_text", "press_key"}]
    assert len(calls) == 1 and calls[0][1]["element_token"] == "s1:1"
    assert calls[0][1]["text"] == "a\nb\\n"


def test_top_unresolved_window_never_borrows_other_windows_ax_or_pixels():
    driver = FakeDriver()
    original = driver.__call__
    def call(tool, args):
        result = original(tool, args)
        if tool == "list_windows":
            result["windows"].insert(0, {"window_id": 9, "pid": 4242, "title": "Panel",
                "layer": 0, "is_on_screen": True, "z_index": 10,
                "bounds": {"width": 500, "height": 300}})
        if tool == "get_window_state" and args["window_id"] == 9:
            return {"degraded": True, "element_count": 0, "window_title": "Panel",
                    "screenshot_png_b64": base64.b64encode(b"panel").decode()}
        return result
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    assert obs.png == b"panel" and obs.window_title == "Panel" and obs.elements == []
    assert mac.shot_window == 9 and mac.ax_window is None and not mac.sheet_active
    assert [a["window_id"] for t, a in driver.calls if t == "get_window_state"] == [9]
    with pytest.raises(CuaError):
        mac.act(act("type", element_id="e1", text="wrong window"), obs)


def test_decoy_frontmost_cannot_receive_global_or_foreground_fallback_events():
    driver = CaptureDriver()
    original = driver.__call__
    decoy_events = []
    target_events = []
    def call(tool, args):
        if tool in {"hotkey", "press_key", "type_text", "click", "scroll", "drag"}:
            # Simulate driver semantics: missing target/default delivery goes to
            # the user's currently frontmost decoy, regardless of top-level pid.
            if args.get("target") != {"kind": "window", "pid": 4242, "window_id": 7}:
                decoy_events.append(tool)
            elif args.get("delivery_mode") != "background":
                decoy_events.append(tool)
            else:
                target_events.append(tool)
                return {"status": "refused", "code": "pid_keyboard_background_input",
                        "tool": tool, "escalation": {"target": "foreground"}}
        return original(tool, args)
    class ForbiddenForeground:
        def activate(self, *_):
            pytest.fail("background backend activated a window")
        def act(self, *_):
            pytest.fail("background backend emitted global input")
    mac = CuaMac(call, foreground=ForbiddenForeground())
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    for action in (act("key", chord="cmd+a"), act("key", chord="cmd+a"),
                   act("key", chord="return"), act("click", x=10, y=10)):
        obs = mac.observe()  # each independent attempt starts with fresh pixel custody
        with pytest.raises(CuaError):
            mac.act(action, obs)
    assert target_events == ["hotkey", "hotkey", "press_key", "click"]
    assert decoy_events == [] and mac.fronted == 0


def test_ax_confirmation_does_not_accept_shared_prefix_or_missing_value():
    driver = FakeDriver()
    driver.write_lands = False
    driver.value = "a" * 20 + "wrong"
    mac, _ = opened(driver)
    obs = mac.observe()
    with pytest.raises(CuaError, match="ax_write_unconfirmed"):
        mac.act(act("type", element_id="e1", text="a" * 20 + "right"), obs)


def test_observation_refresh_invalidates_old_tokens_and_inferred_focus():
    mac, driver = opened()
    old = mac.observe()
    mac.act(act("click", element_id="e1"), old)
    fresh = mac.observe()
    with pytest.raises(CuaError, match="fresh_window_observation"):
        mac.act(act("key", chord="cmd+a"), old)
    with pytest.raises(CuaError, match="fresh_text_element"):
        mac.act(act("type", text="stale focus"), fresh)
    assert not any(t in {"hotkey", "type_text"} for t, _ in driver.calls)


def test_private_driver_refusal_messages_never_enter_action_errors():
    from desktop_runtime.cu_ai_cua import _refusal
    private = "private document text"
    assert _refusal({"status": "refused", "code": "safe_code", "message": private}) == "safe_code"
    assert _refusal({"status": "refused", "code": private, "detail": private}) == "refused"


@pytest.mark.parametrize("ranks", [(None, None), (5, None), (None, 5), (5, 5), (True, 5), ("unknown", 5)])
def test_unknown_or_tied_window_stacking_refuses_before_observation_or_effect(ranks):
    driver = FakeDriver()
    original = driver.__call__
    def call(tool, args):
        result = original(tool, args)
        if tool == "list_windows":
            first = dict(result["windows"][0], z_index=ranks[0])
            result["windows"] = [first, dict(first, window_id=9, z_index=ranks[1])]
        return result
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    with pytest.raises(CuaError, match="window_stacking"):
        mac.observe()
    assert not any(t == "get_window_state" for t, _ in driver.calls)
    with pytest.raises(CuaError, match="fresh_window_observation"):
        mac.act(act("key", chord="cmd+a"), None)
    assert not any(t == "hotkey" for t, _ in driver.calls)


def test_unique_visible_window_is_explicit_fallback_for_unknown_stacking():
    mac, driver = opened()
    original = driver.__call__
    def call(tool, args):
        result = original(tool, args)
        if tool == "list_windows":
            result["windows"][0]["z_index"] = None
            result["windows"].append(dict(result["windows"][0], window_id=9, is_on_screen=False))
        return result
    mac.call = call
    assert mac.observe().png.startswith(b"\x89PNG")
    assert mac.shot_window == 7


def test_stacking_maximum_not_array_position_selects_foremost_window():
    assert CuaMac._select_window([{"window_id": 7, "z_index": 18, "is_on_screen": True},
                                 {"window_id": 9, "z_index": 19, "is_on_screen": True}])["window_id"] == 9


def test_ambiguous_refresh_invalidates_previous_observation_before_effect():
    mac, driver = opened()
    old = mac.observe()
    original = driver.__call__
    def call(tool, args):
        result = original(tool, args)
        if tool == "list_windows":
            result["windows"] = [dict(result["windows"][0], z_index=None),
                                 dict(result["windows"][0], window_id=9, z_index=None)]
        return result
    mac.call = call
    with pytest.raises(CuaError, match="window_stacking"):
        mac.observe()
    with pytest.raises(CuaError, match="fresh_window_observation"):
        mac.act(act("click", element_id="e1"), old)
    assert mac.tokens == {} and mac.shot_window is None


def test_switch_app_refuses_ambiguous_process_match_without_choosing_array_order():
    def call(tool, args):
        return {"apps": [{"pid": 1, "running": True, "name": "Editor", "bundle_id": "example.editor"},
                         {"pid": 2, "running": True, "name": "Editor", "bundle_id": "example.editor"}]}
    mac = CuaMac(call)
    with pytest.raises(CuaError, match="ambiguous_app_target"):
        mac.switch_app("Editor")
    assert mac.pid is None


@pytest.mark.parametrize("op,kwargs,tool", [
    ("click", {"element_id": "e1"}, "click"),
    ("key", {"chord": "cmd+s"}, "hotkey"),
    ("menu", {"text": "File > Save"}, "click"),
    ("type", {"element_id": "e1", "text": "test"}, "set_value"),
])
def test_partial_effect_pauses_without_pixel_retry_or_menu_variant(op, kwargs, tool):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = FakeDriver()
    original = driver.__call__
    sent = []
    def call(name, args):
        if name == tool:
            sent.append(args)
            return {"effect": "partial", "refusal": {"code": "effect_partial",
                                                       "message": "private input"}}
        return original(name, args)
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    if op == "menu":
        add_menu(mac)
    with pytest.raises(NeedsHuman, match="effect is uncertain") as error:
        mac.act(act(op, **kwargs), obs)
    assert error.value.kind == "other" and "private input" not in str(error.value)
    assert len(sent) == 1
    if op == "click":
        assert "element_token" in sent[0] and "x" not in sent[0]


def test_transport_error_after_possible_effect_holds_without_replay_or_private_details():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = FakeDriver()
    original = driver.__call__
    sent = []
    def call(tool, args):
        if tool == "click":
            sent.append(args)
            raise TimeoutError("private input in command arguments")
        return original(tool, args)
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    with pytest.raises(NeedsHuman, match="delivery is unconfirmed") as error:
        mac.act(act("click", element_id="e1"), obs)
    assert len(sent) == 1 and "private input" not in str(error.value)


def test_owned_session_close_survives_trace_wrapper_and_never_closes_shared_daemon():
    from types import SimpleNamespace
    from desktop_runtime.cu_ai_cua import session_caller
    calls, closed = [], []
    session = SimpleNamespace(call=lambda *args, **kw: calls.append(args), close=lambda: closed.append("proxy"))
    mac = CuaMac(session_caller(session))
    mac.call = lambda *_: pytest.fail("close must not send driver end_session or terminate daemon")
    mac.close()
    mac.close()
    assert closed == ["proxy"] and calls == []
    with pytest.raises(CuaError, match="backend_closed"):
        mac.open_app("TextEdit")


def test_failed_close_blocks_further_effects_and_remains_explicit():
    attempts = []
    def close():
        attempts.append(1)
        raise RuntimeError("cleanup failed")
    driver = FakeDriver()
    driver.close = close
    mac, _ = opened(driver)
    obs = mac.observe()
    with pytest.raises(RuntimeError, match="cleanup failed"):
        mac.close()
    with pytest.raises(CuaError, match="backend_closed"):
        mac.act(act("key", chord="return"), obs)
    assert len(attempts) == 1 and not any(t == "press_key" for t, _ in driver.calls)


class CaptureDriver(FakeDriver):
    """Documented 0.29.1 output metadata, synthetic image and opaque capture."""
    def __init__(self):
        super().__init__()
        self.capture_number = 0
        self.overrides = {}

    def __call__(self, tool, args):
        result = super().__call__(tool, args)
        if tool == "get_window_state":
            self.capture_number += 1
            result.update(pid=4242, window_id=7, capture_id=f"capture-{self.capture_number}",
                          screenshot_width=600, screenshot_height=400)
            result.update(self.overrides)
        return result


@pytest.mark.parametrize("action", [act("click", element_id="e1"), act("key", chord="cmd+a"),
                                   act("menu", text="File > Save"), act("scroll", element_id="e1", direction="down", amount=1)])
def test_semantic_dispatch_invalidates_old_pixels_until_new_observation(action):
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    if action.op == "menu":
        add_menu(mac)
    mac.act(action, obs)
    before = len(driver.calls)
    with pytest.raises(CuaError, match="fresh_pixel_capture_required"):
        mac.act(act("click", x=10, y=20), obs)
    assert len(driver.calls) == before
    fresh = mac.observe()
    mac.act(act("click", x=10, y=20), fresh)
    assert driver.calls[-1][1]["capture_id"] == "capture-2"


def test_semantic_set_value_dispatch_invalidates_capture_before_verification_read():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    call = mac.call
    def inspect(tool, args):
        if tool == "set_value":
            assert mac._capture is None  # Dispatch may mutate a same-sized surface.
        return call(tool, args)
    mac.call = inspect
    mac.act(act("type", element_id="e1", text="synthetic"), obs)


def test_public_external_invalidation_refuses_old_observed_actions_without_retargeting():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    mac.invalidate_observation()
    assert not mac.tokens and mac._capture is None and mac.pid == 4242
    before = len(driver.calls)
    with pytest.raises(CuaError, match="fresh_window_observation_required"):
        mac.act(act("click", x=10, y=20), obs)
    assert len(driver.calls) == before
    fresh = mac.observe()
    mac.act(act("click", x=10, y=20), fresh)


def assert_documented_action_shape(tool, args):
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).parents[1] / "fixtures" /
                         "cua_driver_0_29_1_action_schemas.json").read_text())["schemas"][tool]
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema).validate(args)
    assert args["target"] == {"kind": "window", "pid": 4242, "window_id": 7}
    assert args["delivery_mode"] == "background"


@pytest.mark.parametrize("op,extra", [("click", {}), ("double_click", {"count": 2}),
                                       ("right_click", {"button": "right"})])
def test_pixel_aliases_use_capture_admission_and_documented_click_schema(op, extra):
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    mac.act(act(op, x=10, y=20), obs)
    tool, args = driver.calls[-1]
    assert tool == "click"
    assert args["capture_id"] == "capture-1"
    assert (args["x"], args["y"]) == (10, 20)
    for key, value in extra.items():
        assert args[key] == value
    assert_documented_action_shape(tool, args)
    with pytest.raises(CuaError, match="fresh_pixel_capture_required"):
        mac.act(act(op, x=10, y=20), obs)


@pytest.mark.parametrize("overrides", [{"capture_id": None}, {"capture_id": ""}, {"snapshot_id": None},
    {"screenshot_png_b64": ""}, {"screenshot_width": 0}, {"screenshot_height": True},
    {"pid": None}, {"window_id": None}])
def test_missing_pixel_capture_metadata_refuses_before_input_but_ax_remains_available(overrides):
    driver = CaptureDriver()
    driver.overrides = overrides
    mac, _ = opened(driver)
    obs = mac.observe()
    with pytest.raises(CuaError, match="fresh_pixel_capture_required"):
        mac.act(act("click", x=10, y=20), obs)
    assert not any(t == "click" for t, _ in driver.calls)
    mac.act(act("click", element_id="e1"), obs)
    assert driver.calls[-1][1]["element_token"] == "s1:1"
    assert "capture_id" not in driver.calls[-1][1]


@pytest.mark.parametrize("field,value", [("pid", 99), ("window_id", 9)])
def test_observation_metadata_target_mismatch_invalidates_prior_capture(field, value):
    driver = CaptureDriver()
    mac, _ = opened(driver)
    old = mac.observe()
    driver.overrides[field] = value
    with pytest.raises(CuaError, match="window_observation_target_mismatch"):
        mac.observe()
    with pytest.raises(CuaError, match="fresh_window_observation_required"):
        mac.act(act("click", x=10, y=20), old)
    assert not any(t == "click" for t, _ in driver.calls)


@pytest.mark.parametrize("x,y", [(-1, 1), (600, 1), (1, 400), (True, 1), (float("nan"), 1), (10 ** 400, 1)])
def test_pixel_coordinates_outside_source_capture_refuse_without_dispatch(x, y):
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    with pytest.raises(CuaError, match="pixel_coordinate_invalid"):
        mac.act(act("click", x=x, y=y), obs)
    assert not any(t == "click" for t, _ in driver.calls)


def test_capture_refresh_and_target_change_cannot_reuse_previous_capture():
    mac, driver = opened(CaptureDriver())
    old = mac.observe()
    fresh = mac.observe()
    with pytest.raises(CuaError, match="fresh_window_observation_required"):
        mac.act(act("click", x=10, y=20), old)
    mac.act(act("click", x=10, y=20), fresh)
    assert driver.calls[-1][1]["capture_id"] == "capture-2"
    mac.set_target(4242, "TextEdit")
    with pytest.raises(CuaError, match="fresh_window_observation_required"):
        mac.act(act("click", x=10, y=20), fresh)


def test_driver_capture_refusal_is_not_replayed_or_downgraded():
    driver = CaptureDriver()
    original = driver.__call__
    sent = []
    def call(tool, args):
        if tool == "click":
            sent.append(args)
            return {"effect": "refused", "code": "capture_frame_mismatch"}
        return original(tool, args)
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    with pytest.raises(CuaError):
        mac.act(act("click", x=10, y=20), obs)
    with pytest.raises(CuaError, match="fresh_pixel_capture_required"):
        mac.act(act("click", x=10, y=20), obs)
    assert len(sent) == 1 and sent[0]["capture_id"] == "capture-1"


def test_semantic_right_click_uses_legal_token_route_without_capture():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    mac.act(act("right_click", element_id="e1"), obs)
    tool, args = driver.calls[-1]
    assert tool == "click" and args["button"] == "right" and args["element_token"] == "s1:1"
    assert "capture_id" not in args and "x" not in args
    assert_documented_action_shape(tool, args)
    assert mac.focus_key is None


def test_double_click_element_uses_screenshot_bounds_not_ax_screen_frame():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    mac.act(act("double_click", element_id="e1"), obs)
    tool, args = driver.calls[-1]
    assert tool == "click" and args["count"] == 2 and args["capture_id"] == "capture-1"
    assert (args["x"], args["y"]) == (120, 40)
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    with pytest.raises(CuaError, match="screenshot_element_bounds_required"):
        mac.act(act("double_click", element_id="e0"), obs)
    assert not any(t == "click" for t, _ in driver.calls)


@pytest.mark.parametrize("kwargs,expected", [({"element_id": "e1"}, {"element_token": "s1:1"}),
                                            ({"x": 30, "y": 40}, {"x": 30, "y": 40}), ({}, {})])
def test_scroll_region_is_forwarded_only_with_documented_fields(kwargs, expected):
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    mac.act(act("scroll", direction="down", amount=2, **kwargs), obs)
    tool, args = driver.calls[-1]
    assert tool == "scroll" and "capture_id" not in args
    for key, value in expected.items():
        assert args[key] == value
    assert_documented_action_shape(tool, args)


def test_scroll_invalid_explicit_region_does_not_fall_back_to_focused_region():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    for kwargs in ({"element_id": "e99"}, {"x": 30}, {"x": 600, "y": 40}):
        with pytest.raises(CuaError):
            mac.act(act("scroll", direction="down", **kwargs), obs)
    assert not any(t == "scroll" for t, _ in driver.calls)


def test_drag_checks_both_source_capture_points_without_unsupported_capture_field():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    with pytest.raises(CuaError, match="pixel_coordinate_invalid"):
        mac.act(act("drag", x=10, y=20, text="600,40"), obs)
    assert not any(t == "drag" for t, _ in driver.calls)
    mac.act(act("drag", x=10, y=20, text="30,40"), obs)
    tool, args = driver.calls[-1]
    assert tool == "drag" and "capture_id" not in args
    assert_documented_action_shape(tool, args)


@pytest.mark.parametrize("failure", ["partial", "transport"])
def test_uncertain_pixel_effect_requires_fresh_capture_without_replay(failure):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = CaptureDriver()
    original = driver.__call__
    sent = []
    def call(tool, args):
        if tool == "click":
            sent.append(args)
            if failure == "transport":
                raise TimeoutError("private detail")
            return {"effect": "partial"}
        return original(tool, args)
    mac = CuaMac(call)
    mac.set_target(4242, "TextEdit")
    obs = mac.observe()
    with pytest.raises(NeedsHuman):
        mac.act(act("double_click", x=10, y=20), obs)
    with pytest.raises(CuaError, match="fresh_pixel_capture_required"):
        mac.act(act("double_click", x=10, y=20), obs)
    assert len(sent) == 1 and sent[0]["capture_id"] == "capture-1" and sent[0]["count"] == 2


def test_drag_element_missing_screenshot_bounds_refuses_before_input():
    mac, driver = opened(CaptureDriver())
    obs = mac.observe()
    with pytest.raises(CuaError, match="screenshot_element_bounds_required"):
        mac.act(act("drag", element_id="e0", text="20,30"), obs)
    assert not any(t == "drag" for t, _ in driver.calls)


def add_menu(mac):
    mac.raw.extend([
        {"element_index": 10, "role": "AXMenuBarItem", "label": "File"},
        {"element_index": 11, "role": "AXMenu", "parent_index": 10},
        {"element_index": 12, "role": "AXMenuItem", "label": "Save…", "parent_index": 11,
         "element_token": "s1:12", "enabled": True, "actions": ["AXPick"]},
    ])


def test_background_menu_missing_never_invokes_foreground_vendor_route():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    mac, driver = opened()
    obs = mac.observe()
    before = len(driver.calls)
    with pytest.raises(NeedsHuman, match="fresh, unique"):
        mac.act(act("menu", text="File > Save"), obs)
    with pytest.raises(NeedsHuman, match="foreground authority"):
        mac._t("invoke_menu", window_id=7, path=["File", "Save"])
    assert len(driver.calls) == before


def test_background_menu_single_targeted_axpick_without_foreground_or_retry():
    mac, driver = opened()
    obs = mac.observe()
    add_menu(mac)
    before = len(driver.calls)
    mac.act(act("menu", text="File > Save..."), obs)
    assert driver.calls[before:] == [("click", {"target": {"kind": "window", "pid": 4242, "window_id": 7},
        "delivery_mode": "background", "element_token": "s1:12", "action": "pick"})]
    assert mac.fronted == 0


@pytest.mark.parametrize("bad", ["duplicate", "disabled", "unsupported", "cycle", "wrong_path"])
def test_menu_requires_unique_ordered_enabled_advertised_fresh_path(bad):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    mac, driver = opened()
    obs = mac.observe()
    add_menu(mac)
    item = mac.raw[-1]
    if bad == "duplicate":
        mac.raw.append({**item, "element_index": 13, "element_token": "s1:13"})
    elif bad == "disabled":
        item["enabled"] = False
    elif bad == "unsupported":
        item["actions"] = ["AXPress"]
    elif bad == "cycle":
        mac.raw[-2]["parent_index"] = 12
    else:
        mac.raw[-3]["label"] = "Other"
    before = len(driver.calls)
    with pytest.raises(NeedsHuman):
        mac.act(act("menu", text="File > Save"), obs)
    assert len(driver.calls) == before


def test_existing_app_open_never_sends_reopen_apple_event():
    mac, driver = opened()
    assert not any(tool == 'launch_app' for tool, _ in driver.calls)
    assert mac.last_launch_receipt == {'route': 'existing', 'requested': False, 'focus_status': 'not_launched'}


def test_exact_inventory_name_does_not_select_prefix_decoy_or_guess_unknown():
    base = FakeDriver()
    def call(tool, args):
        if tool == 'list_apps':
            return {'apps': [{'name': 'Editor Beta', 'bundle_id': 'example.beta', 'pid': 3, 'running': True}]}
        pytest.fail('identity refusal must not dispatch launch/window effects')
    with pytest.raises(CuaError, match='exact_application_identity_required'):
        CuaMac(call).open_app('Editor')
    assert base.calls == []


@pytest.mark.parametrize('problem', ['wrong_pid', 'missing_window', 'unknown_stack'])
def test_existing_target_requires_owned_admitted_window_before_selection(problem):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = FakeDriver()
    original = driver.__call__
    def call(tool, args):
        result = original(tool, args)
        if tool == 'list_windows':
            if problem == 'wrong_pid':
                result['windows'][0]['pid'] = 99
            elif problem == 'missing_window':
                result['windows'] = []
            else:
                result['windows'] = [dict(result['windows'][0], z_index=None),
                                     dict(result['windows'][0], window_id=9, z_index=None)]
        return result
    mac = CuaMac(call)
    with pytest.raises((NeedsHuman, CuaError)):
        mac.open_app('TextEdit')
    assert mac.pid is None
    assert not any(t == 'launch_app' for t, _ in driver.calls)


@pytest.mark.parametrize('suppressed', [True, False, None])
def test_cold_launch_exact_bundle_preserved_but_focus_receipt_never_claims_proof(suppressed):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = FakeDriver()
    original = driver.__call__
    def call(tool, args):
        if tool == 'list_apps':
            driver.calls.append((tool, args))
            return {'apps': [{'name': 'TextEdit', 'bundle_id': 'com.apple.TextEdit', 'pid': 0, 'running': False}]}
        result = original(tool, args)
        if tool == 'launch_app':
            result['bundle_id'] = 'com.apple.TextEdit'
            if suppressed is not None:
                result['self_activation_suppressed'] = suppressed
        return result
    mac = CuaMac(call)
    if suppressed is False:
        with pytest.raises(NeedsHuman, match='did not preserve background focus'):
            mac.open_app('TextEdit')
    else:
        assert 'launch_focus_uncertain' in mac.open_app('TextEdit')
    assert [args for tool, args in driver.calls if tool == 'launch_app'] == [{'bundle_id': 'com.apple.TextEdit'}]
    assert mac.last_launch_receipt['focus_status'] == 'uncertain'
    assert mac.last_launch_receipt.get('driver_self_activation_suppressed') is suppressed


@pytest.mark.parametrize('fields', [{'pid': True}, {'pid': 0}, {'name': None}])
def test_running_inventory_invalid_identity_refuses_without_reopen(fields):
    def call(tool, args):
        assert tool == 'list_apps'
        return {'apps': [{'name': 'TextEdit', 'bundle_id': 'com.apple.TextEdit', 'pid': 4242,
                          'running': True, **fields}]}
    with pytest.raises(CuaError, match='application_inventory_invalid'):
        CuaMac(call).open_app('TextEdit')


def test_mcp_error_flag_holds_before_reading_private_error_body():
    from types import SimpleNamespace
    from desktop_runtime.cu_ai_cua import session_caller
    from desktop_runtime.cu_ai_engine import NeedsHuman
    class PrivateResult:
        isError = True
        @property
        def structuredContent(self):
            pytest.fail('must not consume payload after explicit MCP error')
        @property
        def content(self):
            pytest.fail('must not consume private provider/native failure body')
    session = SimpleNamespace(close=lambda: None, call=lambda *a, **kw: PrivateResult())
    with pytest.raises(NeedsHuman, match='Input delivery is unconfirmed'):
        session_caller(session)('hotkey', {})


@pytest.mark.parametrize("effect", ["unverifiable", "suspected_noop", "partial", "unknown"])
def test_uncertain_input_effect_holds_after_single_dispatch(effect):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    driver = FakeDriver()
    mac, _ = opened(driver)
    obs = mac.observe()
    original = driver.__call__
    def call(tool, args):
        if tool == "click":
            driver.calls.append((tool, args))
            return {"effect": effect, "verified": False}
        return original(tool, args)
    mac.call = call
    with pytest.raises(NeedsHuman, match="input effect is uncertain"):
        mac.act(act("click", element_id="e1"), obs)
    assert sum(tool == "click" for tool, _ in driver.calls) == 1
    assert mac.focus_key is None


def test_missing_input_receipt_never_means_success():
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman):
        CuaMac._check_partial({})


def test_mcp_readonly_error_refuses_without_reading_private_body():
    from types import SimpleNamespace
    from desktop_runtime.cu_ai_cua import session_caller
    class Result:
        isError = True
        @property
        def structuredContent(self):
            pytest.fail("private body read")
    session = SimpleNamespace(close=lambda: None, call=lambda *a, **kw: Result())
    with pytest.raises(CuaError, match="driver_readonly_failed"):
        session_caller(session)("list_windows", {})


@pytest.mark.parametrize("effect", ["unknown", "partial", "unverifiable", "suspected_noop"])
def test_contradictory_unknown_effect_true_never_admits_input(effect):
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman):
        CuaMac._check_partial({"effect": effect, "verified": True})


def test_exact_modal_protocol_selects_independently_owned_surface_before_actions():
    driver = FakeDriver()
    calls = []
    def call(tool, args):
        calls.append((tool, args))
        if tool == "list_windows":
            return {"windows": [
                {"pid": 4242, "window_id": 7, "layer": 0, "z_index": 9, "is_on_screen": True,
                 "bounds": {"width": 600, "height": 400}},
                {"pid": 4242, "window_id": 8, "layer": 0, "z_index": 2, "is_on_screen": True,
                 "bounds": {"width": 300, "height": 200}}]}
        if tool == "get_window_state":
            state = driver(tool, args)
            state.update(pid=4242, window_id=args["window_id"], modal_protocol="supymem_exact_modal_v1")
            if args["window_id"] == 7:
                state["modal_windows"] = [{"pid": 4242, "window_id": 8, "parent_window_id": 7}]
            else:
                state["modal_windows"] = []
                for element in state["elements"]:
                    element["window_id"] = 8
                element["pid"] = 4242
            return state
        return driver(tool, args)
    mac = CuaMac(call)
    mac.set_target(4242, "AnyNativeApp")
    mac.observe()
    assert mac.ax_window == mac.shot_window == 8
    assert [a["window_id"] for t, a in calls if t == "get_window_state"] == [7, 8]
    assert not any(t in {"click", "hotkey", "launch_app"} for t, a in calls)


@pytest.mark.parametrize("case", ["foreign_pid", "missing_parent", "ambiguous", "closed"])
def test_modal_identity_never_uses_geometric_overlap(case):
    driver = FakeDriver()
    def call(tool, args):
        if tool == "get_window_state":
            state = driver(tool, args)
            state.update(pid=4242, window_id=args["window_id"], modal_protocol="supymem_exact_modal_v1")
            candidate = {"pid": 4242, "window_id": 99, "parent_window_id": 7}
            if case == "foreign_pid":
                candidate["pid"] = 4243
            if case == "missing_parent":
                candidate.pop("parent_window_id")
            state["modal_windows"] = [candidate, dict(candidate)] if case == "ambiguous" else [candidate]
            return state
        return driver(tool, args)
    mac = CuaMac(call)
    mac.set_target(4242, "AnyNativeApp")
    with pytest.raises(CuaError, match="modal_surface_identity_"):
        mac.observe()
    assert mac.tokens == {} and mac.ax_window is None and mac.shot_window is None
    assert not any(t in {"click", "hotkey", "press_key", "launch_app"} for t, a in driver.calls)


def test_native_menu_pre_admission_phase_never_calls_transport():
    calls=[]
    mac=CuaMac(lambda tool,args:calls.append(tool))
    mac.set_target(4242,'AnyNativeApp')
    with pytest.raises(NeedsHuman) as caught:
        mac._t('invoke_menu')
    assert caught.value.dispatch_state=='not_dispatched' and calls==[]


def test_background_open_path_never_calls_unproven_launchservices_route(monkeypatch):
    from desktop_runtime import cu_ai_engine
    mac, driver = opened()
    observation = mac.observe()
    before = len(driver.calls)
    monkeypatch.setattr(cu_ai_engine, "open_path", lambda *a: pytest.fail("LaunchServices must not dispatch"))
    with pytest.raises(NeedsHuman) as caught:
        mac.act(act("open_path", text="~/Documents/synthetic.txt"), observation)
    assert caught.value.dispatch_state == "not_dispatched"
    assert len(driver.calls) == before


@pytest.mark.parametrize('tool,args', [('press_key',{'window_id':7,'key':'return'}),
                                     ('set_value',{'window_id':7,'element_token':'owned','value':'synthetic'})])
def test_backend_preserves_typed_no_dispatch_fact_from_admission(tool,args):
    def refused(_tool,_args):
        raise NeedsHuman('other','owned local admission',dispatch_state='not_dispatched')
    mac=CuaMac(refused)
    mac.set_target(4242,'AnyNativeApp')
    with pytest.raises(NeedsHuman) as caught:
        mac._t(tool,**args)
    assert caught.value.dispatch_state=='not_dispatched'


@pytest.mark.parametrize("pid,window,accepted", [(11,42,True),(22,99,False),(11,43,False)])
def test_reviewed_completion_predicate_is_primary_bound_even_for_declared_secondary(pid,window,accepted):
    import time
    from desktop_runtime.cu_ai_engine import Observation
    mac=object.__new__(CuaMac)
    mac.pid,mac.ax_window,mac.shot_window=pid,window,window
    obs=Observation(b"owned",[],"Synthetic","","snapshot",False,pid)
    mac._observation,mac._observation_at=obs,time.monotonic()
    mac.raw=[{'pid':pid,'window_id':window,'role':'AXTextArea','value':'expected synthetic document',
              'element_index':1,'element_token':'owned','enabled':True}]
    spec={'operation':'desktop_goal_v2','process_id':11,'window_id':42,
          'document_text_index':0,'reviewed_text':['expected synthetic document']}
    result,code=mac.reviewed_goal_evidence(obs,spec,((11,42),(22,99),(11,43)))
    assert result is accepted
    assert code == ('reviewed_goal_predicates_observed' if accepted else 'reviewed_goal_evidence_unavailable')
