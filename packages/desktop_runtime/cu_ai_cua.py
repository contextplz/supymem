"""Window-scoped Mac backend over the embedded cua-driver.

All event input uses the driver's explicit window target and background delivery.
Unsupported background effects refuse; this backend never silently activates a
window or substitutes global keyboard events. Observation pixels and AX tokens
always belong to the same selected window.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import time
from typing import Callable

from coding_agents.cu_ai_loop import excluded_app
from desktop_runtime.cu_ai_engine import NeedsHuman, Observation

SINGLE_LINE = {"AXTextField", "AXSearchField", "AXComboBox"}
TEXT_ROLES = SINGLE_LINE | {"AXTextArea"}
NOISE_ROLES = {"AXRulerMarker", "AXRuler", "AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem",
               "AXScrollBar", "AXValueIndicator", "AXSplitter", "AXGrowArea", "AXUnknown"}
KEY_ALIASES = {"enter": "return", "esc": "escape", "backspace": "delete", "command": "cmd", "control": "ctrl",
               "alt": "option"}
MODS = {"cmd", "shift", "option", "ctrl", "fn"}
SYSTEM_BUNDLES = {"finder": "com.apple.finder", "safari": "com.apple.Safari", "system settings": "com.apple.systempreferences",
                  "notes": "com.apple.Notes", "reminders": "com.apple.reminders", "calendar": "com.apple.iCal",
                  "preview": "com.apple.Preview", "textedit": "com.apple.TextEdit", "calculator": "com.apple.calculator"}
# Smaller window screenshots cut upload + vision time; AX elements carry the precise targets. Tunable per Mac.
SHOT_MAX = max(512, min(1920, int(os.environ.get("SUPYMEM_CU_SHOT_MAX", "1024") or 1024)))
BUNDLE = re.compile(r"[\w-]+(\.[\w-]+)+\Z")


class CuaError(RuntimeError):
    def __init__(self, code, *, no_effect=False, validation_feedback=""):
        super().__init__(code)
        self.no_effect = no_effect  # Set only from owned local flow or a typed refusal.
        self.validation_feedback = validation_feedback



def cli_caller(binary: str, socket: str, timeout: float = 60) -> Callable[[str, dict], dict]:
    """call(tool, args) -> structured dict, through `cua-driver call` against a running daemon."""
    def call(tool: str, args: dict) -> dict:
        proc = subprocess.run([binary, "call", "--socket", socket, tool, json.dumps(args)],
                              capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise CuaError(f"{tool}_bad_output") from None
    return call


_NATIVE_CODES = frozenset({"window_not_found", "owner_pid_mismatch", "off_space_or_ax_unresolved",
                           "minimized_or_hidden_window", "same_pid_keyboard_ambiguity",
                           "element_outside_target_window", "facility_unavailable", "ax_action_failed", "ax_worker_failed"})


_AX_ERROR_NAMES = dict(enumerate((
    "failure", "illegal_argument", "invalid_ui_element", "invalid_ui_element_observer",
    "cannot_complete", "attribute_unsupported", "action_unsupported", "notification_unsupported",
    "not_implemented", "notification_already_registered", "notification_not_registered", "api_disabled",
    "no_value", "parameterized_attribute_unsupported", "not_enough_precision")))


_PROBE_STAGES = frozenset({"capture_preflight", "after_ack_sample", "after_ack_final"})
_PROBE_REASONS = frozenset({"not_eligible", "target_owner_unconfirmed", "ax_timeout_unavailable",
    "focused_element_unavailable", "button_capability_unavailable", "ancestry_unavailable", "ancestry_identity_mismatch",
    "parent_identity_unavailable", "parent_identity_mismatch", "sheets_unavailable", "sheets_malformed", "sheets_partial",
    "sheets_complete", "target_sheet_not_listed", "capture_complete", "postcondition_unconfirmed",
    "postcondition_confirmed", "sample_budget_exhausted"})
_PROBE_BOOL_FIELDS = frozenset({"target_cg_present", "target_cg_owned", "parent_cg_owned", "parent_identity_exact",
    "sheets_available", "target_sheet_listed"})
_PROBE_FIELDS = _PROBE_BOOL_FIELDS | {"stage", "reason", "sample_index", "sheets_count", "ax_error_code"}


def _probe_metadata(value):
    """Whole closed metadata receipt only; unknown/private fields are never forwarded."""
    if (type(value) is not dict or set(value) != {"protocol", "truncated", "samples"}
            or value.get("protocol") != "supymem_press_probe_metadata_v1"
            or type(value.get("truncated")) is not bool or type(value.get("samples")) is not list
            or len(value["samples"]) > 16):
        return None
    samples = []
    for sample in value["samples"]:
        if (type(sample) is not dict or set(sample) != _PROBE_FIELDS
                or type(sample.get("stage")) is not str or sample["stage"] not in _PROBE_STAGES
                or type(sample.get("reason")) is not str or sample["reason"] not in _PROBE_REASONS
                or type(sample.get("sample_index")) is not int or not 0 <= sample["sample_index"] <= 3
                or any(sample[key] is not None and type(sample[key]) is not bool for key in _PROBE_BOOL_FIELDS)):
            return None
        count, code = sample["sheets_count"], sample["ax_error_code"]
        if (count is not None and (type(count) is not int or not 0 <= count <= 32)
                or code is not None and (type(code) is not int or not -25214 <= code <= -25200)):
            return None
        samples.append(dict(sample))
    return {"protocol": "supymem_press_probe_metadata_v1", "truncated": value["truncated"], "samples": samples}


def native_diagnostic(tool, result):
    """Closed protocol metadata only; never inspect private MCP text/image blocks."""
    from desktop_runtime.cua_session import AI_LOOP_TOOLS
    value = getattr(result, "structuredContent", None)
    value = value if type(value) is dict else {}
    refusal = value.get("refusal")
    refusal = refusal if type(refusal) is dict else {}
    code = refusal.get("code") or value.get("code")
    status, effect, verified = value.get("status"), value.get("effect"), value.get("verified")
    row = {"tool": tool if type(tool) is str and tool in AI_LOOP_TOOLS else "withheld",
            "is_error": getattr(result, "isError", False) is not False,
            "status": status if type(status) is str and status in {"refused", "ok", "success", "failed"} else "unknown",
            "effect": effect if type(effect) is str and effect in {"confirmed", "refused", "partial", "unverifiable", "suspected"} else "unknown",
            "verified": verified if type(verified) is bool else None,
            "code": code if type(code) is str and code in _NATIVE_CODES else "withheld"}
    if row["code"] in {"ax_action_failed", "ax_worker_failed"}:
        stage = value.get("stage")
        number, name = value.get("ax_error_code"), value.get("ax_error")
        known = type(number) is int and _AX_ERROR_NAMES.get(-25200 - number) == name and type(name) is str
        row.update(stage=stage if type(stage) is str and stage in {"ax_perform_action", "ax_action", "ax_worker"} else "unknown",
                   ax_error_code=number if known else None, ax_error=name if known else "withheld")
    probe = _probe_metadata(value.get("probe_diagnostics"))
    if probe is not None:
        row["probe_diagnostics"] = probe
    return row


def session_caller(session, *, diagnostic: Callable[[dict], None] | None = None) -> Callable[[str, dict], dict]:
    """Same shape over desktop_runtime.cua_session.CuaSession (the app-owned daemon)."""
    def call(tool: str, args: dict) -> dict:
        try:
            result = session.call(tool, args, timeout=60)
        except Exception as error:
            if tool in {"list_apps", "list_windows", "get_window_state", "check_permissions", "health_report", "get_session_state"}:
                raise CuaError("driver_readonly_failed") from None
            from desktop_runtime.cua_session import CuaSessionError
            # These exact errors originate before the native coroutine is
            # submitted. Timeout, invalid replies, and unknown SDK errors do
            # not carry a no-effect fact, even if their text suggests refusal.
            pre_dispatch = type(error) is CuaSessionError and error.code in {
                "desktop_cua_scope_invalid", "desktop_cua_session_unavailable"}
            raise NeedsHuman("other", "Native input could not be confirmed. Fresh scoped reconciliation is required before another input.",
                             dispatch_state="not_dispatched" if pre_dispatch else "unconfirmed") from None
        if diagnostic is not None:
            try:
                diagnostic(native_diagnostic(tool, result))
            except Exception:
                pass  # An optional diagnostic cannot authorize input or mask a hold.
        if getattr(result, "isError", False) is not False:
            if tool in {"list_apps", "list_windows", "get_window_state"}:
                raise CuaError("driver_readonly_failed")
            raise NeedsHuman("other", "Input delivery is unconfirmed. Verify the target before continuing.")
        value = dict(result.structuredContent or {})
        for block in getattr(result, "content", None) or []:
            if getattr(block, "type", "") == "image" and "screenshot_png_b64" not in value:
                value["screenshot_png_b64"] = block.data   # MCP carries the screenshot as an image block
        if not value:
            for block in getattr(result, "content", None) or []:
                if getattr(block, "type", "") == "text":
                    try:
                        value = json.loads(block.text)
                    except ValueError:
                        value = {"text": block.text}
        return value
    call.close = session.close  # only the client/proxy owned by this adapter
    return call


def _refusal(result: dict) -> str | None:
    """Reason string when the driver refused/partially failed the call, else None (never ignore a refusal)."""
    if not isinstance(result, dict):
        return "bad_response"
    refused = (result.get("status") == "refused" or result.get("effect") in {"refused", "partial"}
               or (result.get("code") and result.get("tool")))
    if not refused:
        return None
    info = result.get("refusal") if isinstance(result.get("refusal"), dict) else result
    code = info.get("code") or result.get("code") or "refused"
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "refused"


class CuaMac:
    def __init__(self, call: Callable[[str, dict], dict], foreground=None):
        self.call = call
        self._owned_close = getattr(call, "close", None)
        self._close_requested = self._close_complete = False
        self._potential_effect_count = 0
        self.fg = foreground          # optional PeekabooMac for the guarded activate->act->restore escalation
        self.pid: int | None = None
        self.app = ""
        self.ax_window: int | None = None
        self.shot_window: int | None = None
        self._capture: tuple[int, int, str, int, int] | None = None
        self.tokens: dict[str, str] = {}
        self.chords: dict[str, int] = {}
        self.raw: list[dict] = []
        self.focus_key: tuple | None = None   # (role, label) of the last text field the model clicked
        self.window_off_screen = False
        self.sheet_active = False   # top window is a remote (AX-unresolved) sheet/panel
        self.last_launch_receipt = None
        self.fronted = 0   # how many foreground escalations were needed (reported)

    def close(self):
        self._close_requested = True
        if self._close_complete:
            return
        if callable(self._owned_close):
            try:
                self._owned_close()
            finally:
                from desktop_runtime.cu_cleanup import public_cleanup_trace
                self.cleanup_trace = public_cleanup_trace(getattr(self._owned_close, "cleanup_trace", None))
        self._close_complete = True

    def _require_open(self):
        if self._close_requested:
            raise CuaError("backend_closed")

    def invalidate_observation(self):
        """External tools may change the surface without this adapter seeing input."""
        self._observation = self._capture = None
        self.tokens, self.raw, self.focus_key = {}, [], None

    # --- guard: every driver call is pinned to the target pid ------------------------------------
    def _t(self, tool: str, **args) -> dict:
        self._require_open()
        if self.pid is None:
            raise CuaError("no_target_app: use open_app first")
        if excluded_app(self.app, ""):
            raise CuaError(f"target {self.app} is off-limits")
        event_tools = {"click", "double_click", "right_click", "drag", "type_text",
                       "hotkey", "press_key", "scroll", "move_cursor"}
        if tool in event_tools:
            window = args.pop("window_id", None)
            if type(window) is not int or window <= 0:
                raise CuaError("window_target_required")
            if args.pop("delivery_mode", "background") != "background":
                raise CuaError("foreground_requires_explicit_owned_session")
            # Any input can change a same-sized surface. A capture admission
            # checks identity/coordinates, not that its old pixels still match.
            # Pixel callers already copied their admitted ID into args.
            self._capture = None
            self._potential_effect_count = getattr(self, "_potential_effect_count", 0) + 1
            try:
                return self.call(tool, {"target": {"kind": "window", "pid": self.pid,
                                                  "window_id": window},
                                        "delivery_mode": "background", **args})
            except NeedsHuman:
                raise
            except Exception:
                raise NeedsHuman("other", "Input delivery is unconfirmed. Verify the target before continuing.") from None
        if tool == "invoke_menu":
            raise NeedsHuman("other", "Native menu invocation requires foreground authority. Use a fresh observed background AX menu item or request assistance.", dispatch_state="not_dispatched")
        if tool == "set_value":
            self._capture = None
            self._potential_effect_count = getattr(self, "_potential_effect_count", 0) + 1
        try:
            return self.call(tool, {"pid": self.pid, **args})
        except NeedsHuman:
            raise
        except Exception:
            if tool in {"set_value", "invoke_menu"}:
                raise NeedsHuman("other", "Input delivery is unconfirmed. Verify the target before continuing.") from None
            raise CuaError("driver_request_failed") from None

    def frontmost(self):  # engine's focus guard: in background mode the "destination" is the target pid
        return (self.app, self.pid)

    def focused_role(self):
        return None

    def activate(self, name):  # background engine never raises windows to repair focus
        return None

    # --- targets --------------------------------------------------------------------------------
    def _windows(self, pid: int) -> list[dict]:
        rows = self.call("list_windows", {"pid": pid, "on_screen_only": False}).get("windows") or []
        rows = [w for w in rows if w.get("pid") == pid and type(w.get("window_id")) is int and w["window_id"] > 0 and w.get("layer") == 0 and w["bounds"]["width"] > 40 and w["bounds"]["height"] > 30
                and not (w["bounds"]["height"] <= 40 and w["bounds"]["width"] >= 800)]  # menu-bar strips
        return rows  # selection is explicit; unknown stacking never becomes array order

    def set_target(self, pid: int, app: str) -> None:
        self.pid, self.app, self.tokens = pid, app, {}
        self.ax_window = self.shot_window = None
        self._capture = None
        self.focus_key = None
        self._observation = None

    def _resolve_app(self, name: str):
        """Read exact native identity; a substring never selects a process."""
        if excluded_app(name, ""):
            raise CuaError("application_off_limits")
        bundle = name if BUNDLE.fullmatch(name) else SYSTEM_BUNDLES.get(name.lower())
        result = self.call("list_apps", {})
        rows = result.get("apps") if isinstance(result, dict) else None
        if not isinstance(rows, list) or _refusal(result):
            raise CuaError("application_inventory_unavailable")
        matches = [row for row in rows if isinstance(row, dict) and (
            row.get("bundle_id") == bundle if bundle else
            isinstance(row.get("name"), str) and row["name"].casefold() == name.casefold())]
        identities = {row.get("bundle_id") for row in matches}
        if len(identities) > 1:
            raise CuaError("ambiguous_app_target")
        running = [row for row in matches if row.get("running") is True]
        if any(type(row.get("pid")) is not int or row["pid"] <= 0
               or not isinstance(row.get("name"), str) or not row["name"] for row in running):
            raise CuaError("application_inventory_invalid")
        if len({row["pid"] for row in running}) > 1:
            raise CuaError("ambiguous_app_target")
        if running:
            return running[0], bundle or running[0].get("bundle_id")
        if not bundle:
            if len(matches) != 1 or not isinstance(matches[0].get("bundle_id"), str) or not BUNDLE.fullmatch(matches[0]["bundle_id"]):
                raise CuaError("exact_application_identity_required")
            bundle = matches[0]["bundle_id"]
        return None, bundle

    def _admit_existing(self, row):
        pid = row["pid"]
        windows = self._windows(pid)
        if not windows:
            raise NeedsHuman("other", "The exact running app has no observed window. Reopening it may change focus and requires assistance.", dispatch_state="not_dispatched")
        self._select_window(windows)  # no arbitrary same-process window order
        self.set_target(pid, row["name"])
        self.last_launch_receipt = {"route": "existing", "requested": False, "focus_status": "not_launched"}
        return "selected exact running application without reopening"

    def open_app(self, name: str) -> str:
        self._require_open()
        row, bundle = self._resolve_app(name)
        if row is not None:
            return self._admit_existing(row)
        self.last_launch_receipt = {"route": "cold", "requested": True, "focus_status": "uncertain"}
        self._potential_effect_count = getattr(self, "_potential_effect_count", 0) + 1
        result = self.call("launch_app", {"bundle_id": bundle})
        pid = result.get("pid")
        if _refusal(result) or type(pid) is not int or pid <= 0:
            raise CuaError("launch_failed")
        if result.get("bundle_id") is not None and result["bundle_id"] != bundle:
            raise CuaError("launched_application_identity_mismatch")
        suppressed = result.get("self_activation_suppressed")
        if type(suppressed) is bool:
            self.last_launch_receipt["driver_self_activation_suppressed"] = suppressed
        self.set_target(pid, result.get("name") or result.get("app_name") or name)
        if suppressed is False:
            raise NeedsHuman("other", "The launched app did not preserve background focus. Verify the desktop before continuing.")
        windows = []
        for _ in range(12):
            windows = self._windows(pid)
            if windows:
                self._select_window(windows)
                break
            time.sleep(0.4)
        if not windows:
            raise NeedsHuman("other", "The launched app has no admitted window. Verify startup before continuing.")
        return "launched exact application; launch_focus_uncertain; fresh observation required"

    def switch_app(self, name: str) -> str:
        self._require_open()
        row, _ = self._resolve_app(name)
        if row is None:
            raise CuaError("application_not_running")
        return self._admit_existing(row)

    @staticmethod
    def _select_window(windows):
        visible = [w for w in windows if w.get("is_on_screen") is True]
        candidates = visible or windows
        if len(candidates) == 1:
            return candidates[0]  # explicit unique-window fallback; no stack order needed
        if any(type(w.get("z_index")) is not int for w in candidates):
            raise CuaError("window_stacking_unknown_choose_explicit_window")
        top = max(w["z_index"] for w in candidates)
        winners = [w for w in candidates if w["z_index"] == top]
        if len(winners) != 1:
            raise CuaError("window_stacking_ambiguous_choose_explicit_window")
        return winners[0]

    def _observe_surface(self, window):
        visited = set()
        modal = getattr(self.call,"is_reviewed_child",lambda *_:False)(self.pid,window["window_id"])
        for _ in range(4):
            wid = window["window_id"]
            if wid in visited:
                raise CuaError("modal_surface_identity_ambiguous")
            visited.add(wid)
            state = self.call("get_window_state", {"pid": self.pid, "window_id": wid,
                             "max_dimension": SHOT_MAX, "max_elements": 400, "max_depth": 20})
            if state.get("modal_protocol") != "supymem_exact_modal_v1":
                if modal:
                    raise CuaError("modal_surface_protocol_unavailable")
                return window, state, False
            if (state.get("pid") != self.pid or type(state.get("pid")) is not int
                    or state.get("window_id") != wid or type(state.get("window_id")) is not int):
                raise CuaError("modal_surface_identity_unconfirmed")
            candidates = state.get("modal_windows")
            if not isinstance(candidates, list):
                raise CuaError("modal_surface_identity_unconfirmed")
            if not candidates:
                if modal and (state.get("degraded") or not state.get("snapshot_id")):
                    raise CuaError("modal_surface_observation_unavailable")
                return window, state, modal
            if len(candidates) != 1 or not isinstance(candidates[0], dict):
                raise CuaError("modal_surface_identity_ambiguous")
            candidate = candidates[0]
            if (type(candidate.get("pid")) is not int or candidate["pid"] != self.pid
                    or type(candidate.get("parent_window_id")) is not int or candidate["parent_window_id"] != wid
                    or type(candidate.get("window_id")) is not int or candidate["window_id"] <= 0):
                raise CuaError("modal_surface_identity_unconfirmed")
            # AX SPI/parentage comes from the patched observer. Independently
            # re-enumerate CG ownership; rectangles never authorize retargeting.
            matches = [row for row in self._windows(self.pid) if row["window_id"] == candidate["window_id"]]
            if len(matches) != 1:
                raise CuaError("modal_surface_identity_unconfirmed")
            window, modal = matches[0], True
        raise CuaError("modal_surface_identity_ambiguous")

    # --- observe --------------------------------------------------------------------------------
    def observe(self) -> Observation:
        self._require_open()
        self._observation = None
        self._capture = None
        self.tokens, self.raw, self.focus_key = {}, [], None
        self.ax_window = self.shot_window = None
        if self.pid is None:
            return Observation(b"", [], "(no target app yet)", "", "", False, None)
        windows = self._windows(self.pid)
        if not windows:
            return Observation(b"", [], self.app, "(no window)", "", False, self.pid)
        # The foremost target window may be an unresolved sheet. Never pair its
        # pixels with the document behind it: that makes model coordinates and
        # AX tokens address different recipients.
        window = self._select_window(windows)
        window, state, modal = self._observe_surface(window)
        wid = window["window_id"]
        if _refusal(state):
            self.tokens, self.raw = {}, []
            self.ax_window = self.shot_window = None
            raise CuaError("window_observation_refused")
        if any(state.get(key) is not None and (type(state[key]) is not int or state[key] != value)
               for key, value in (("pid", self.pid), ("window_id", wid))):
            raise CuaError("window_observation_target_mismatch")
        shot_b64 = state.get("screenshot_png_b64") or ""
        capture_id = state.get("capture_id")
        width, height = state.get("screenshot_width"), state.get("screenshot_height")
        if (shot_b64 and state.get("snapshot_id") and isinstance(capture_id, str) and capture_id.strip()
                and type(state.get("pid")) is int and state["pid"] == self.pid
                and type(state.get("window_id")) is int and state["window_id"] == wid
                and type(width) is int and width > 0 and type(height) is int and height > 0):
            self._capture = (self.pid, wid, capture_id, width, height)
        title = state.get("window_title") or window.get("title") or ""
        self.shot_window = wid
        self.ax_window = wid if state.get("element_count") and not state.get("degraded") else None
        self.window_off_screen = not window.get("is_on_screen", False)
        self.sheet_active = modal  # positively resolved modal identity, never inferred from pixels
        if wid != getattr(self, "_last_shot", None):
            self.chords, self.focus_key = {}, None
        self._last_shot = wid
        self.tokens = {}
        self.focus_key = None  # inferred text focus cannot survive a new snapshot
        self.raw = (state.get("elements") or []) if self.ax_window is not None else []
        if state.get("modal_protocol") == "supymem_exact_modal_v1":
            self.raw = [e for e in self.raw if type(e.get("pid")) is int and e["pid"] == self.pid
                        and type(e.get("window_id")) is int and e["window_id"] == wid]
        elements = []
        for e in self.raw:
            if e.get("role") in NOISE_ROLES:
                continue
            eid = f"e{e['element_index']}"
            self.tokens[eid] = e.get("element_token")
            f = e.get("screenshot_frame") or {}  # AX screen points are not screenshot pixels
            elements.append({"id": eid, "ax_role": e.get("role"), "label": e.get("label"), "value": e.get("value"),
                             "bounds": {"x": f.get("x"), "y": f.get("y"), "width": f.get("w"), "height": f.get("h")},
                             "label_truncated": e.get("label_truncated", False),
                             "value_truncated": e.get("value_truncated", False),
                             "is_enabled": e.get("enabled", True), "screen": e.get("frame")})
        self._observation = Observation(base64.b64decode(shot_b64) if shot_b64 else b"", elements, self.app, title,
                                        state.get("snapshot_id") or "", False, self.pid)
        self._observation_at = time.monotonic()
        return self._observation

    def reviewed_goal_evidence(self, observation, spec, reviewed_pairs):
        """Fresh exact-surface AX predicates; no model claim or label is authority."""
        if (observation is not self._observation or not observation.snapshot_id
                or time.monotonic() - getattr(self, "_observation_at", 0) >= 20
                or observation.pid != self.pid or (self.pid, self.ax_window) not in reviewed_pairs
                or type(spec.get("process_id")) is not int or spec["process_id"] != self.pid
                or type(spec.get("window_id")) is not int or spec["window_id"] != self.ax_window
                or self.ax_window != self.shot_window or not self.raw
                or any(type(e.get("pid")) is not int or e["pid"] != self.pid
                       or type(e.get("window_id")) is not int or e["window_id"] != self.ax_window for e in self.raw)):
            return False, "reviewed_goal_evidence_unavailable"
        expected = spec.get("expected_visible_text")
        document_index = spec.get("document_text_index")
        if expected is None and document_index is None:
            return False, "reviewed_goal_verification_required"
        if expected is not None:
            from desktop_runtime.device_worker import _observed_expected_visible_text
            rows = [{**e, "readout": e.get("role") in {"AXStaticText", "AXHeading", "AXTitle"}}
                    for e in self.raw if e.get("label_truncated") is not True]
            if not isinstance(expected, str) or not expected or not _observed_expected_visible_text(rows, expected):
                return False, "reviewed_goal_predicate_unmet"
        if document_index is not None:
            texts = spec.get("reviewed_text")
            if type(document_index) is not int or document_index != 0 or not isinstance(texts, list) or not texts:
                return False, "reviewed_goal_evidence_unavailable"
            expected_document = texts[document_index]
            rows = [e for e in self.raw if e.get("role") == "AXTextArea"]
            if (not isinstance(expected_document, str) or not 0 < len(expected_document) <= 320 or len(rows) != 1
                    or type(rows[0].get("element_index")) is not int or rows[0]["element_index"] < 0
                    or not isinstance(rows[0].get("element_token"), str) or not rows[0]["element_token"]
                    or rows[0].get("enabled") is False or rows[0].get("value_truncated") is True
                    or not isinstance(rows[0].get("value"), str)):
                return False, "reviewed_goal_evidence_unavailable"
            if rows[0]["value"] != expected_document:
                return False, "reviewed_goal_predicate_unmet"
        return True, "reviewed_goal_predicates_observed"

    # --- act ------------------------------------------------------------------------------------
    def _ladder(self, tool: str, **args) -> tuple[dict, str]:
        """Use only recipient-scoped background input; refusals remain refusals."""
        result = self._t(tool, **args)
        self._check_partial(result)
        if _refusal(result):
            raise CuaError("background_effect_refused", no_effect=self._proven_no_effect(result))
        return result, ""

    def _wid(self, pixel: bool) -> int | None:
        return self.shot_window if pixel else (self.ax_window or self.shot_window)

    def _pixel_source(self, *points, consume: bool = False) -> str:
        """Validate source pixels locally; only click offers atomic driver admission.

        Consume before dispatch, even on refusal/uncertain transport, so an
        attempted effect always requires a fresh observation before repetition.
        Drag/wheel schemas do not accept capture_id; this is no atomic lease.
        """
        capture = self._capture
        if capture is None or capture[:2] != (self.pid, self.shot_window):
            raise CuaError("fresh_pixel_capture_required")
        _, _, capture_id, width, height = capture
        for x, y in points:
            if (type(x) not in {int, float} or type(y) not in {int, float}
                    or (type(x) is float and not math.isfinite(x)) or (type(y) is float and not math.isfinite(y))
                    or not (0 <= x < width and 0 <= y < height)):
                raise CuaError("pixel_coordinate_invalid")
        if consume:
            self._capture = None
        return capture_id

    def act(self, action, obs) -> str:
        before = getattr(self, "_potential_effect_count", 0)
        try:
            return self._act(action, obs)
        except CuaError as error:
            if getattr(self, "_potential_effect_count", 0) == before:
                # Source-owned control flow proves no effect-capable transport
                # was entered. Never use the error code/prose as that proof.
                error.no_effect = True
            raise

    def _act(self, action, obs) -> str:
        self._require_open()
        op = action.op
        if op == "open_app":
            return self.open_app(action.app)
        if op == "switch_app":
            return self.switch_app(action.app)
        if op not in {"open_path", "wait"} and (obs is not getattr(self, "_observation", None)
                                                   or obs is None or obs.pid != self.pid):
            raise CuaError("fresh_window_observation_required")
        if op == "drag":
            def centre(ref):
                if re.fullmatch(r"e\d+", ref or ""):
                    el = next((e for e in obs.elements if e["id"] == ref), None)
                    b = (el or {}).get("bounds") or {}
                    if any(type(b.get(key)) not in {int, float} or not math.isfinite(b[key])
                           for key in ("x", "y", "width", "height")) or b["width"] <= 0 or b["height"] <= 0:
                        raise CuaError("screenshot_element_bounds_required")
                    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
                m = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", ref or "")
                if not m:
                    raise CuaError("drag target must be an element id or 'x,y'")
                return int(m.group(1)), int(m.group(2))
            fx, fy = centre(action.element_id) if action.element_id else (action.x, action.y)
            tx, ty = centre(action.text)
            self._pixel_source((fx, fy), (tx, ty), consume=True)
            self._t_checked("drag", window_id=self._wid(True), from_x=fx, from_y=fy, to_x=tx, to_y=ty)
            return f"dragged {fx},{fy} -> {tx},{ty}"
        if op == "open_path":
            # LaunchServices' -g flag is a request, not a background effect
            # guarantee: an existing app's reopen handler can still activate.
            # This exact-window backend has no authority to suppress or repair
            # that global focus change. Typed tools may open/read their reviewed
            # document, and the harness independently measures their focus.
            raise NeedsHuman("other", "Opening a file through LaunchServices cannot be proven background-safe. Use a scoped document template with readback, or select an already open reviewed window.", dispatch_state="not_dispatched")
        if op == "wait":
            time.sleep(action.ms / 1000)
            return f"waited {action.ms}ms"
        if op in {"click", "double_click", "right_click"}:
            extra = {"button": "right"} if op == "right_click" else {}
            if action.element_id and op != "double_click":
                token = self.tokens.get(action.element_id)
                if not token:
                    raise CuaError("element id not on screen now; use x,y from the screenshot")
                _, note = self._ladder("click", window_id=self._wid(False), element_token=token, **extra)
                clicked = next((e for e in obs.elements if e["id"] == action.element_id), None)
                if op == "click" and clicked and clicked.get("ax_role") in TEXT_ROLES:
                    self.focus_key = (clicked["ax_role"], clicked.get("label"), clicked["id"])
                return f"{op} {action.element_id}{note}"
            x, y = action.x, action.y
            if action.element_id:
                el = next((e for e in obs.elements if e["id"] == action.element_id), None)
                bounds = (el or {}).get("bounds") or {}
                if any(type(bounds.get(key)) not in {int, float} or not math.isfinite(bounds[key])
                       for key in ("x", "y", "width", "height")) or bounds["width"] <= 0 or bounds["height"] <= 0:
                    raise CuaError("screenshot_element_bounds_required")
                x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2
            if op == "double_click":
                extra["count"] = 2
            capture_id = self._pixel_source((x, y), consume=True)
            _, note = self._ladder("click", window_id=self._wid(True), x=x, y=y, capture_id=capture_id, **extra)
            return f"{op} {x},{y}{note}"
        if op == "type":
            token, role = self._focus_target(action, obs)
            text = action.text  # preserve literal backslash-n and actual newlines
            if not token or role not in TEXT_ROLES:
                raise CuaError("fresh_text_element_required")
            if action.element_id:
                value = " ".join(text.split("\n")) if role in SINGLE_LINE else text
                self._t_checked("set_value", window_id=self._wid(False), element_token=token, value=value)
                if not self._landed(action.element_id, obs, value):
                    raise CuaError("ax_write_unconfirmed_verify_before_retry")
                return f"set {role} value ({len(text)} chars)"
            self._t_checked("type_text", window_id=self._wid(False), element_token=token, text=text)
            return f"typed {len(text)} chars into scoped text element"
        if op == "key":
            parts = [KEY_ALIASES.get(p.lower(), p.lower()) for p in re.split(r"\+", action.chord) if p]
            mods, key = [p for p in parts[:-1] if p in MODS], parts[-1]
            if mods:
                _, note = self._ladder("hotkey", window_id=self._wid(False), keys=[*mods, key])
            else:
                _, note = self._ladder("press_key", window_id=self._wid(False), key=key)
            return "pressed " + action.chord + note
        if op == "menu":
            path = [p.strip() for p in re.split(r">", action.text) if p.strip()]
            window = self._wid(False)
            # Native0.29.1 invoke_menu activates/raises its target even on
            # refusal. Only the reviewed background AXPick route is admitted.
            item = self._menu_item(path)
            if item is None:
                raise NeedsHuman("other", "A fresh, unique, enabled AX menu item advertising AXPick is required for background menu input. Foreground menu invocation requires explicit authority.", dispatch_state="not_dispatched")
            self._t_checked("click", window_id=window, element_token=item["element_token"], action="pick")
            return "menu " + " > ".join(path)
        if op == "scroll":
            region = {}
            if action.element_id:
                token = self.tokens.get(action.element_id)
                if not token:
                    raise CuaError("fresh_scroll_element_required")
                region["element_token"] = token
            elif action.x is not None or action.y is not None:
                self._pixel_source((action.x, action.y), consume=True)
                region.update(x=action.x, y=action.y)
            _, note = self._ladder("scroll", window_id=self._wid(False), direction=action.direction,
                                 amount=action.amount, **region)
            return "scrolled" + note
        raise CuaError("unsupported_op")

    def _landed(self, element_id: str, obs, value: str) -> bool:
        """Confirm the exact value on the same window; unreadable is unknown."""
        old = next((e for e in obs.elements if e["id"] == element_id), None)
        window = self.ax_window
        if old is None or window is None:
            return False
        fresh = self.observe()
        if self.ax_window != window:
            return False
        near = [e for e in fresh.elements if e.get("ax_role") == old.get("ax_role")
                and e.get("label") == old.get("label") and e.get("id") == element_id]
        return len(near) == 1 and near[0].get("value") == value

    def _focus_target(self, action, obs) -> tuple[str | None, str | None]:
        """Token + role of the field a `type` goes to: explicit element_id, else the last clicked text field."""
        if action.element_id:
            el = next((e for e in obs.elements if e["id"] == action.element_id), None)
            return self.tokens.get(action.element_id), (el or {}).get("ax_role")
        if self.focus_key:
            for e in obs.elements:
                if (e.get("ax_role"), e.get("label"), e["id"]) == self.focus_key and self.tokens.get(e["id"]):
                    return self.tokens[e["id"]], e["ax_role"]
        return None, None

    def _menu_item(self, path: list[str]) -> dict | None:
        """Admit one complete ordered path from this exact fresh window tree."""
        if not path:
            return None
        by_index = {e["element_index"]: e for e in self.raw}
        if len(by_index) != len(self.raw):
            return None
        norm = lambda t: re.sub(r"[\u2026.]+$", "", str(t or "")).strip().lower()  # noqa: E731
        wanted, matches = [norm(step) for step in path], []
        for e in self.raw:
            if (e.get("role") != "AXMenuItem" or norm(e.get("label")) != wanted[-1]
                    or e.get("enabled") is not True or "AXPick" not in (e.get("actions") or [])
                    or not isinstance(e.get("element_token"), str) or not e["element_token"]):
                continue
            chain, parent, seen = [norm(e.get("label"))], by_index.get(e.get("parent_index")), {e["element_index"]}
            while parent is not None:
                if parent["element_index"] in seen:
                    chain = []
                    break
                seen.add(parent["element_index"])
                if parent.get("role") in {"AXMenuBarItem", "AXMenuItem"} and norm(parent.get("label")):
                    chain.append(norm(parent["label"]))
                parent = by_index.get(parent.get("parent_index"))
            if list(reversed(chain)) == wanted:
                matches.append(e)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _check_partial(result):
        if (not isinstance(result, dict)
                or result.get("effect") not in {None, "confirmed", "refused"}
                or (result.get("effect") != "confirmed" and result.get("verified") is not True
                    and not _refusal(result))
                or result.get("verified") is False and result.get("effect") != "refused"):
            raise NeedsHuman("other", "The input effect is uncertain. Verify the target before continuing.")

    @staticmethod
    def _proven_no_effect(result):
        # Closed native receipt; absence, ACK, contradictory verified:true,
        # status-only refusal, and unknown effects cannot release a reservation.
        return (type(result) is dict and result.get("effect") == "refused"
                and result.get("verified") is False and result.get("status") == "refused"
                and (result.get("suspected_noop") is None or result["suspected_noop"] is False))

    def _t_checked(self, tool: str, **args) -> dict:
        result = self._t(tool, **args)
        self._check_partial(result)
        bad = _refusal(result)
        if bad:
            raise CuaError(bad, no_effect=self._proven_no_effect(result))
        return result
