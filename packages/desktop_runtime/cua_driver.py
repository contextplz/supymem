"""Exact-window adapter for the signed Supymem app's embedded CUA daemon.

The daemon is started by Tauri, not by this Python process. Every effect still
passes through the durable ComputerUseGate before reaching this adapter.
"""
from __future__ import annotations

import base64
import math
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from PIL import Image

from desktop_runtime.cu_reliability import (
    CHORDS, CU_FIELD_NOT_CLEARED, CU_FIELD_VALUE_MISMATCH, field_is_clear, field_value_matches,
    normalize_entry_text)
from desktop_runtime.cua_session import CuaSession, CuaSessionError, OBSERVATION_PROTOCOL
from desktop_runtime.peekaboo_driver import AppChoice, DriverUnavailable, _vision_text


class CuaDriverUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.detail = detail


def _raw_detail(data: object) -> str | None:
    """Bounded, sanitized raw CUA refusal (code + message) for step evidence."""
    if not isinstance(data, dict):
        return None
    refusal = data.get("refusal") if isinstance(data.get("refusal"), dict) else data
    text = f"{refusal.get('code') or data.get('status') or 'error'}: {refusal.get('message') or data.get('reason') or ''}"
    return re.sub(r"[^A-Za-z0-9 _.:,;'()/-]", "", text)[:200]


def _unpinned_detail(data: object) -> str | None:
    """Which pin check failed (structure only, never screen text)."""
    if not isinstance(data, dict):
        return "no_structured_content"
    # Record shape and a documented enum, never interpolate vendor text.
    def identity(key):
        value = data.get(key)
        return value if type(value) is int else "invalid"
    degraded = data.get("degraded_reason")
    category = next((code for code in ("ax_window_unresolved", "ax_tree_empty")
                     if isinstance(degraded, str) and degraded.startswith(code + ":")), "none")
    return (f"pid={identity('pid')} window_id={identity('window_id')} "
            f"snapshot={isinstance(data.get('snapshot_id'), str)} "
            f"elements={type(data.get('elements')).__name__} "
            f"degraded={category}")[:200]


_BROWSER_BUNDLES = {"com.apple.Safari", "com.google.Chrome", "com.brave.Browser",
                    "org.mozilla.firefox", "com.microsoft.edgemac", "com.operasoftware.Opera"}


def _browser_address_field(bundle_id: str, role: str, label: str) -> bool:
    """Recognize a browser's native location control, never page text fields."""
    return (bundle_id in _BROWSER_BUNDLES
            and role.lower().removeprefix("ax") in {"textfield", "searchfield", "combobox"}
            and bool(re.search(r"(?i)(smart search|address(?: and search)?(?: bar| field)?|location field|omnibox)", label)))


def _native_ax_ancestry(item: dict, indexed: dict[int, dict]) -> bool | None:
    """Prove an indexed text field is outside web content, if the tree permits it."""
    seen: set[int] = set()
    current = item
    while True:
        index = current.get("element_index")
        if type(index) is not int or index in seen:
            return None
        seen.add(index)
        if str(current.get("role") or "").lower().removeprefix("ax") == "webarea":
            return False
        if "parent_index" not in current:
            return None
        parent = current["parent_index"]
        if parent is None:
            return True
        if type(parent) is not int or parent not in indexed:
            return None
        current = indexed[parent]


def _effect_refusal_code(data: dict) -> str | None:
    """Classify a documented CUA no-effect refusal without saving its text.

    CUA uses ``status: refused`` / ``refusal.code`` (or ``effect: refused`` /
    ``code``), rather than Peekaboo's older mutation/retry metadata.
    """
    if data.get("status") != "refused" and data.get("effect") != "refused":
        return None
    if data.get("mutation_dispatched") is True:
        return None
    refusal = data.get("refusal")
    raw = (refusal.get("code") if isinstance(refusal, dict) else None) or data.get("code")
    if not isinstance(raw, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,48}", raw):
        raw = "refused"
    return f"desktop_pre_dispatch_cua_{raw}"


def _observation_refusal_code(data: dict) -> str | None:
    """Keep a missing exact window distinct from a malformed capture.

    Only the documented structured refusal marker is trusted. Free-form MCP
    content can contain private screen text and must not enter a receipt.
    """
    refusal = data.get("refusal")
    raw = (refusal.get("code") if isinstance(refusal, dict) else None) or data.get("code")
    # Pinned vendor 0.29.1 returns these direct structured codes with isError,
    # without status/effect markers. Never follow its suggested owner PID.
    if not isinstance(raw, str):
        raw = None
    if raw in {"window_target_not_found", "window_id_not_found"}:
        return "desktop_cua_window_target_not_found"
    if raw == "window_owner_pid_mismatch":
        return "desktop_cua_window_owner_pid_mismatch"
    if data.get("status") != "refused" and data.get("effect") != "refused":
        return None
    return "desktop_cua_observation_refused"


class CuaWindowDriver:
    def __init__(self, *, app: AppChoice, window_id: int, socket: str,
                 host_pid: int, binary: str | None = None, observation_protocol: str | None = None):
        if observation_protocol not in {None, OBSERVATION_PROTOCOL}:
            raise CuaDriverUnavailable("desktop_cua_protocol_unsupported")
        self.observation_protocol = observation_protocol
        self._owner_inventory = None
        if (not isinstance(window_id, int) or window_id <= 0
                or not isinstance(host_pid, int) or host_pid <= 0):
            raise CuaDriverUnavailable("desktop_cua_scope_invalid")
        self.app, self.window_id = app, window_id
        self._session = CuaSession(binary or str(Path(sys.executable).with_name("cua-driver")), socket,
                                   **({"observation_protocol": observation_protocol, "observation_pid": app.pid} if observation_protocol else {}))
        self._capture_dir = tempfile.TemporaryDirectory(prefix="supymem-cua-capture-")
        self._snapshot_id = None
        self._cua_snapshot_id = None
        self._snapshot_at = 0.0
        self._elements = {}
        self._image_size = None
        try:
            health = self._structured("health_report", {"include": ["bundle_identity"]})
            identity = next((row for row in health.get("checks", [])
                             if isinstance(row, dict) and row.get("name") == "bundle_identity"), {})
            data = identity.get("data") or {}
            if (identity.get("status") != "pass"
                    or data.get("bundle_identifier") != "com.companyos.app"
                    or data.get("identity_source") != "parent_application"
                    or data.get("parent_process_id") != host_pid):
                raise CuaDriverUnavailable("desktop_cua_host_identity_mismatch")
            permissions = self._structured("check_permissions", {})
            if (permissions.get("accessibility") is not True
                    or permissions.get("screen_recording") is not True
                    or (permissions.get("source") or {}).get("attribution") != "host"):
                raise CuaDriverUnavailable("desktop_cua_host_permissions_missing")
        except Exception:
            self.close()
            raise

    def _structured(self, name: str, arguments: dict) -> dict:
        try:
            result = self._session.call(name, arguments)
        except CuaSessionError as error:
            raise CuaDriverUnavailable(str(error), error.detail) from error
        if result.isError or not isinstance(result.structuredContent, dict):
            raise CuaDriverUnavailable("desktop_cua_tool_refused")
        return result.structuredContent

    def close(self):
        self._session.close()
        self._capture_dir.cleanup()

    def snapshot_ready_for_admission(self, snapshot_id: str) -> bool:
        return bool(snapshot_id == self._snapshot_id and self._image_size
                    and time.monotonic() - self._snapshot_at < 20)

    def document_text_observation(self, snapshot_id: str, expected: str) -> dict | None:
        """Compare private AX text from this fresh, pinned window; emit no draft."""
        if (self.app.bundle_id != "com.apple.TextEdit"
                or not self.snapshot_ready_for_admission(snapshot_id)
                or not isinstance(expected, str) or not 0 < len(expected) <= 320):
            return None
        rows = [(eid, item) for eid, item in self._elements.items()
                if str(item.get("role") or "").lower().removeprefix("ax") == "textarea"]
        if len(rows) != 1:
            return None
        eid, row = rows[0]
        if (type(row.get("element_index")) is not int or row["element_index"] < 0
                or not isinstance(row.get("element_token"), str) or not row["element_token"]
                or not isinstance(row.get("value"), str) or row.get("enabled") is False
                or row.get("value_truncated") is True):
            return None
        import hashlib
        return {"snapshot_id": snapshot_id, "pid": self.app.pid, "window_id": self.window_id,
                "element_id": eid, "element_index": row["element_index"],
                "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                "matches": row["value"] == expected}

    def _observe_field(self, original: dict) -> dict:
        """Reobserve the same reviewed control and return its fresh AX row.

        Index, role, label and frame together identify the reviewed control;
        the new snapshot supplies the only token eligible for the next action.
        Unreadable or ambiguous is ``uncertain_verify``; the value decision is
        made by the caller.
        """
        try:
            result = self._window_state(screenshot=False)
        except CuaSessionError as error:
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_verify") from error
        data = result.structuredContent
        if (result.isError or not isinstance(data, dict)
                or data.get("pid") != self.app.pid or data.get("window_id") != self.window_id
                or not isinstance(data.get("snapshot_id"), str)
                or not isinstance(data.get("elements"), list)):
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_verify")
        matches = [item for item in data["elements"] if isinstance(item, dict)
                   and item.get("element_index") == original.get("element_index")
                   and item.get("role") == original.get("role")
                   and item.get("label") == original.get("label")
                   and item.get("frame") == original.get("frame")]
        if not matches and original.get("_browser_address"):
            # Safari re-indexes its toolbar (and moves the field a little) when
            # the edit session opens; the one same-role/label address field is
            # still the reviewed control.
            matches = [item for item in data["elements"] if isinstance(item, dict)
                       and item.get("role") == original.get("role")
                       and item.get("label") == original.get("label")]
        if (len(matches) != 1 or not isinstance(matches[0].get("element_token"), str)
                or not matches[0]["element_token"]):
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_verify")
        return matches[0]

    def _clear_field(self, element: dict, target: dict) -> str:
        """Select-all + delete, then require an observed empty field; return a fresh token.

        A hotkey can return without selecting (or a browser can keep showing
        the page URL), which would turn a replacement into an append or leave
        a stray character such as "ahttps://...". No reviewed text is typed
        unless the field was observed empty. Never retry the delete: on an
        unselected field it would eat the founder's text one character at a time.
        """
        token = element["element_token"]
        try:
            selected = self._session.call("hotkey", {
                "target": target, "element_token": token, "keys": ["cmd", "a"],
                "delivery_mode": "background"})
        except CuaSessionError as error:
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_select", str(error)) from error
        if (selected.isError or not isinstance(selected.structuredContent, dict)
                or _effect_refusal_code(selected.structuredContent)):
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_select",
                                       _raw_detail(selected.structuredContent))
        try:
            cleared = self._session.call("press_key", {
                "target": target, "element_token": token, "key": "delete",
                "delivery_mode": "background"})
        except CuaSessionError as error:
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_clear", str(error)) from error
        if (cleared.isError or not isinstance(cleared.structuredContent, dict)
                or _effect_refusal_code(cleared.structuredContent)):
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_clear",
                                       _raw_detail(cleared.structuredContent))
        row = self._observe_field(element)
        if not field_is_clear(row.get("value") or ""):
            raise CuaDriverUnavailable(CU_FIELD_NOT_CLEARED)
        return row["element_token"]

    def _set_address_value(self, element: dict, text: str) -> bool:
        """AX-write a browser address field and require an observed match.

        Unlike key chords, AX works for a hidden or off-Space browser (CUA
        refuses raw key input there). WebKit/Chromium may ignore the write;
        then the caller falls back to select/clear/type. A failed or unreadable
        write is never trusted: only a read-back match returns True.
        """
        def write(token: str, value: str) -> bool:
            result = self._session.call("set_value", {
                "pid": self.app.pid, "window_id": self.window_id,
                "element_token": token, "value": value})
            return not (result.isError or not isinstance(result.structuredContent, dict)
                        or _effect_refusal_code(result.structuredContent))

        try:
            if isinstance(element.get("value"), str) and element["value"].strip():
                # Live finding (Safari, hidden window): writing AXValue over a
                # loaded page's URL is displayed but AXConfirm then does not
                # navigate. Clearing the field and confirming the empty field
                # first opens Safari's edit session; the write after it commits.
                # Nothing but the reviewed field is touched, and an empty
                # confirm has no navigation target.
                if not write(element["element_token"], ""):
                    return False
                # An emptied Safari field reads back its placeholder text, so
                # the write's own read-back is the clear check here.
                row = self._observe_field(element)
                if row.get("value") != element["value"]:
                    # (Unchanged = ignored write or an already-empty Start Page
                    # field: never confirm an unchanged URL.)
                    confirmed = self._session.call("click", {
                        "target": {"kind": "window", "pid": self.app.pid, "window_id": self.window_id},
                        "element_token": row["element_token"], "action": "confirm",
                        "delivery_mode": "background"})
                    if (confirmed.isError or not isinstance(confirmed.structuredContent, dict)
                            or _effect_refusal_code(confirmed.structuredContent)):
                        return False
                    row = self._observe_field(element)
                element = {**element, "element_token": row["element_token"]}
            if not write(element["element_token"], text):
                return False
            return field_value_matches(text, self._observe_field(element).get("value"))
        except (CuaSessionError, CuaDriverUnavailable):
            return False

    def _replace_without_ax_set_value(self, element: dict, text: str, target: dict) -> dict:
        """Replace native text with an empty-field checkpoint and read-back.

        CUA's foreground keyboard mode restores the founder's previous app
        after *each* call. Background AX-targeted input keeps that field in
        focus without switching the founder's app, and the observed empty
        value prevents a missed selection from becoming an append.
        """
        if element.get("_browser_address") and self._set_address_value(element, text):
            return {"effect": "unverifiable", "cursor_indicator": "not_shown"}
        fresh_token = self._clear_field(element, target)
        try:
            inserted = self._session.call("type_text", {
                "target": target, "element_token": fresh_token, "text": text,
                "delivery_mode": "background"})
        except CuaSessionError as error:
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_insert", str(error)) from error
        if (inserted.isError or not isinstance(inserted.structuredContent, dict)
                or _effect_refusal_code(inserted.structuredContent)):
            raise CuaDriverUnavailable("desktop_cua_effect_uncertain_insert",
                                       _raw_detail(inserted.structuredContent))
        if not field_value_matches(text, self._observe_field(element).get("value")):
            raise CuaDriverUnavailable(CU_FIELD_VALUE_MISMATCH)
        # The replacement is still an attempted effect until the next normal
        # visual observation; the worker must not infer task completion here.
        return {"effect": "unverifiable", "cursor_indicator": "not_shown"}

    def _window_state(self, *, screenshot: bool):
        """get_window_state with a real AX walk budget.

        CUA's default 1s walk budget returns a *partial* tree when the browser
        is busy (sidebar only, no toolbar/address field), which made the model
        decide BLOCKED. Give the walk more time and re-walk a tree that is
        flagged incomplete or, in a browser, lacks its address field.
        """
        result = None
        retried = False
        for budget in (5000, 15000, 15000):
            params = {"pid": self.app.pid, "window_id": self.window_id,
                      "include_accessibility_tree": True, "include_screenshot": screenshot,
                      "max_elements": 200, "timeout_ms": budget}
            if screenshot:
                params["max_image_dimension"] = 1600
            try:
                result = self._session.call("get_window_state", params)
            except CuaSessionError as error:
                # Read-only: a cold first walk can outlast the call timeout; one
                # fresh attempt dispatches nothing and cannot replay an effect.
                if str(error) != "desktop_cua_transport_unavailable" or retried:
                    raise
                retried = True
                continue
            data = result.structuredContent
            if result.isError or not isinstance(data, dict):
                return result
            elements = data.get("elements") if isinstance(data.get("elements"), list) else []
            has_address = any(
                isinstance(item, dict) and _browser_address_field(
                    self.app.bundle_id, str(item.get("role") or ""), str(item.get("label") or ""))
                for item in elements)
            # Only a browser's toolbar can be missing from a partial walk; a
            # native app whose tree stays truncated would burn ~10s per look.
            if elements and (self.app.bundle_id not in _BROWSER_BUNDLES
                             or (has_address and data.get("truncated") is not True
                                 and not (data.get("nodes_pending") or 0) > 0)):
                break
            time.sleep(0.5)
        if result is None:
            raise CuaSessionError("desktop_cua_transport_unavailable")
        return result

    def capture(self) -> dict:
        # A failed recapture must never leave the preceding observation eligible
        # for an effect, even if its age is still below the admission limit.
        self._snapshot_id = None
        self._cua_snapshot_id = None
        self._elements = {}
        self._image_size = None
        try:
            result = self._window_state(screenshot=True)
        except CuaSessionError as error:
            raise CuaDriverUnavailable(str(error), error.detail) from error
        data = result.structuredContent
        if result.isError and isinstance(data, dict):
            refusal = _observation_refusal_code(data)
            if refusal is not None:
                raise CuaDriverUnavailable(refusal)
        if (result.isError or not isinstance(data, dict)
                or data.get("pid") != self.app.pid or data.get("window_id") != self.window_id
                or not isinstance(data.get("snapshot_id"), str)
                or not isinstance(data.get("elements"), list)):
            raise CuaDriverUnavailable("desktop_cua_observation_unpinned", _unpinned_detail(data))
        image = next((part for part in result.content if getattr(part, "type", None) == "image"), None)
        if image is None or getattr(image, "mimeType", None) not in {"image/png", "image/jpeg"}:
            raise CuaDriverUnavailable("desktop_cua_screenshot_missing")
        try:
            raw = base64.b64decode(image.data, validate=True)
            if not 0 < len(raw) <= 12_000_000:
                raise ValueError()
            path = Path(self._capture_dir.name) / "current.png"
            path.write_bytes(raw)
            with Image.open(path) as opened:
                opened.verify()
            with Image.open(path) as opened:
                image_size = opened.size
        except (OSError, ValueError, SyntaxError) as error:
            raise CuaDriverUnavailable("desktop_cua_screenshot_invalid") from error
        indexed = {item["element_index"]: item for item in data["elements"][:200]
                   if isinstance(item, dict) and type(item.get("element_index")) is int}
        elements = {}
        output = []
        for item in data["elements"][:200]:
            if not isinstance(item, dict):
                continue
            token = item.get("element_token")
            role = str(item.get("role") or "")[:80]
            role_key = role.lower().removeprefix("ax")
            # The exact-window CUA snapshot can include app menu controls.
            # Their AX tokens refer to the global menu surface, so the driver
            # correctly refuses them as outside the reviewed window. Do not
            # advertise a control this window-bound task cannot dispatch.
            if role_key in {"menu", "menubar", "menuitem", "menubaritem"}:
                continue
            # An unnamed text field's AXValue can be an arbitrary private
            # draft. Never turn it into a control label sent to the model.
            label = item.get("label") or (
                "" if role_key in {"textfield", "searchfield", "combobox", "textarea", "securetextfield"}
                else item.get("value") or "")
            if not isinstance(label, str):
                continue
            # A structural label exposes the document control, never its draft.
            # The exact token is still validated by the vendor's window gate.
            if (not label and self.app.bundle_id == "com.apple.TextEdit"
                    and role_key == "textarea" and isinstance(token, str) and token):
                label = "Document text area"
            readout_role = role_key in {"statictext", "heading", "title"}
            point_role = role_key in {"button", "checkbox", "radiobutton", "tab"}
            token_valid = isinstance(token, str) and 0 < len(token) <= 1000
            if not token_valid and not (readout_role or point_role):
                continue
            if not token_valid:
                # Without a token, only a later reviewed point may target a
                # bounded button; readout roles remain observation-only.
                item = {**item, "actions": []}
            eid = f"elem_{len(elements) + 1}"
            # AX labels often contain a link title while AXValue contains its
            # actual destination. Preserve only a bounded, observed HTTPS URL
            # for the worker's evidence journal, without exposing other field
            # values such as drafts, credentials, or search input to Jev.
            observed_url = None
            value = item.get("value")
            if (role_key in {"link", "url"} and isinstance(value, str)
                    and len(value) <= 500):
                try:
                    parsed = urlsplit(value.strip())
                    if (parsed.scheme == "https" and parsed.hostname
                            and not parsed.username and not parsed.password):
                        observed_url = value.strip()
                except ValueError:
                    pass
            frame = item.get("frame")
            has_point = (isinstance(frame, dict) and all(
                isinstance(frame.get(k), (int, float)) for k in ("x", "y", "w", "h")))
            actions = item.get("actions") if isinstance(item.get("actions"), list) else []
            # CUA reports all AX actions, including AXSetValue on editable
            # fields. Only AXPress authorizes a token-backed click.
            ax_press = any(isinstance(action, str)
                           and action.lower().removeprefix("ax") == "press"
                           for action in actions)
            item["_ax_press"] = ax_press
            # Some native controls have a labelled AXButton and frame but no
            # AXPress action. A pixel candidate is only offered for a later
            # screenshot-grounded review; it is never an automatic AX click.
            point_candidate = (point_role
                               and not ax_press and item.get("enabled") is not False
                               and has_point and all(math.isfinite(frame[k]) for k in ("x", "y", "w", "h"))
                               and frame["w"] >= 2 and frame["h"] >= 2
                               and 0 <= frame["x"] + frame["w"] / 2 < image_size[0]
                               and 0 <= frame["y"] + frame["h"] / 2 < image_size[1])
            if point_candidate:
                item["_ax_point"] = (frame["x"] + frame["w"] / 2,
                                     frame["y"] + frame["h"] / 2)
            elements[eid] = item
            browser_address = _browser_address_field(self.app.bundle_id, role, label)
            has_ax_set_value = any(isinstance(action, str)
                and action.lower().removeprefix("ax") == "setvalue" for action in actions)
            native_ancestry = _native_ax_ancestry(item, indexed)
            # A browser's native toolbar field can lack parent indices in a
            # partial AX tree. Never treat a known web descendant as native.
            native_field = (native_ancestry is True or browser_address and native_ancestry is None)
            can_replace = (native_field and isinstance(item.get("value"), str)
                           and type(item.get("element_index")) is int)
            # A browser location bar shows the page URL and often ignores or
            # reverts AXValue writes (a `set_value` there came back
            # effect-uncertain). Use the verified select-all/clear/type path.
            use_keys = can_replace and (browser_address or not has_ax_set_value)
            item["_ax_set_value"] = has_ax_set_value and native_field and not use_keys
            item["_native_text_fallback"] = use_keys
            if browser_address:
                # Keep this native-toolbar distinction local. The model sees a
                # text control, not a privilege to navigate outside review.
                item["_browser_address"] = True
            output.append({"id": eid, "role": role, "label": label[:300],
                           "readout": role_key in {"statictext", "heading", "title", "textfield"}
                           and not actions,
                           "actionable": ax_press, "confidence": None,
                           "point_candidate": point_candidate,
                           "has_point": has_point, "browser_address": browser_address,
                           "observed_url": observed_url,
                           "text_entry": role_key in {"textfield", "searchfield", "combobox", "textarea"},
                           "enabled": item.get("enabled") is not False})
        # AX can omit controls drawn over a populated window, such as a modal
        # error dialog above a page with many AX buttons. OCR the exact-window
        # screenshot even when AX is rich so those visible controls are not
        # hidden from the next decision. Dedupe labels against AX controls.
        # CUA's pixel action consumes the native capture ID, not our ps1_
        # review handle. Never infer a screen-global point from an OCR box.
        if len(output) < 200:
            try:
                recognized = _vision_text(path, (0.0, 0.0), image_size)
            except DriverUnavailable:
                recognized = []
            known_labels = {str(item["label"]).strip().casefold() for item in output}
            for item in recognized[:60]:
                label = str(item.get("label") or "").strip()
                box = item.get("bounds") or {}
                confidence = item.get("confidence")
                if (not label or label.casefold() in known_labels
                        or type(confidence) not in (int, float) or confidence < 0.5
                        or not all(type(box.get(key)) in (int, float)
                                   and math.isfinite(box[key]) for key in ("x", "y", "width", "height"))
                        or box["width"] < 2 or box["height"] < 2):
                    continue
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                if not 0 <= x < image_size[0] or not 0 <= y < image_size[1]:
                    continue
                eid = f"ocr_{len(elements) + 1}"
                elements[eid] = {"_ocr_point": (x, y), "frame": box}
                output.append({"id": eid, "role": "OCRText", "label": label[:300],
                               "readout": False, "actionable": False,
                               "confidence": confidence, "has_point": True,
                               "browser_address": False, "observed_url": None,
                               "text_entry": False, "enabled": True})
                known_labels.add(label.casefold())
                if len(output) == 200:
                    break
        self._elements = elements
        self._image_size = image_size
        # Core's visual-review contract uses a host-owned ps1_ handle. The
        # upstream CUA snapshot ID stays inside its token cache and is never
        # presented as a Supymem review handle.
        perception = self._perception(data, image_size)
        self._snapshot_id = "ps1_" + uuid4().hex
        self._cua_snapshot_id = data["snapshot_id"]
        self._snapshot_at = time.monotonic()
        return {"app": self.app.bundle_id, "window_id": self.window_id,
                "snapshot_id": self._snapshot_id, "elements": output, "perception": perception,
                "window_title": (data.get("window_title") or "")[:300]
                if isinstance(data.get("window_title"), str) else "",
                "screenshot_path": str(path)}

    def _perception(self, data: dict, image_size: tuple[int, int]) -> dict:
        """Preserve observation quality; never mint resource/creation authority."""
        result = {"protocol": self.observation_protocol or "legacy_exact_window",
                  "resource_generation": 0, "window_id": self.window_id,
                  "elements_complete": data.get("elements_complete") is True
                  and len(data["elements"]) <= 200,
                  "screenshot_pixels": list(image_size),
                  "screenshot_frame_valid": data.get("screenshot_frame_valid") is True,
                  "inventory_changed": None, "owner_window_count": None}
        bounds = data.get("window_bounds")
        if isinstance(bounds, dict) and all(type(bounds.get(k)) in (int, float)
                and math.isfinite(bounds[k]) for k in ("x", "y", "width", "height")):
            result["window_bounds"] = {k: bounds[k] for k in ("x", "y", "width", "height")}
        scale = data.get("screenshot_scale")
        if type(scale) in (int, float) and math.isfinite(scale) and scale > 0:
            result["screenshot_scale"] = scale
        if self.observation_protocol == OBSERVATION_PROTOCOL:
            # Explicit PID filter is mandatory even though the lower layer also
            # grants only this app. Never expose sibling titles/controls/pixels.
            census = self._structured("list_windows", {"pid": self.app.pid, "on_screen_only": False})
            rows = census.get("windows")
            if (not isinstance(rows, list) or len(rows) > 128 or any(
                    not isinstance(row, dict) or type(row.get("pid")) is not int
                    or row["pid"] != self.app.pid or type(row.get("window_id")) is not int
                    or row["window_id"] <= 0 for row in rows)):
                self._snapshot_id = None
                raise CuaDriverUnavailable("desktop_cua_inventory_invalid")
            inventory = tuple(sorted(row["window_id"] for row in rows))
            if len(set(inventory)) != len(inventory) or self.window_id not in inventory:
                self._snapshot_id = None
                raise CuaDriverUnavailable("desktop_cua_inventory_unpinned")
            result["owner_window_count"] = len(inventory)
            result["inventory_changed"] = (self._owner_inventory is not None
                                            and inventory != self._owner_inventory)
            self._owner_inventory = inventory
        return result

    def __call__(self, arguments: dict, *, session_id: str) -> dict:
        if arguments.get("app") != self.app.bundle_id or not session_id:
            raise CuaDriverUnavailable("desktop_cua_scope_mismatch")
        action = arguments.get("action")
        if action == "capture":
            return self.capture()
        if action == "wait":
            delay = arguments.get("milliseconds", 250)
            if type(delay) is not int or not 0 <= delay <= 1000:
                raise CuaDriverUnavailable("desktop_cua_wait_invalid")
            time.sleep(delay / 1000)
            return {"waited_ms": delay}
        if arguments.get("snapshot_id") != self._snapshot_id or not self.snapshot_ready_for_admission(self._snapshot_id):
            raise CuaDriverUnavailable("desktop_fresh_observation_required")
        element = self._elements.get(arguments.get("element_id"))
        target = {"kind": "window", "pid": self.app.pid, "window_id": self.window_id}
        if (action == "click" and element and element.get("enabled") is not False
                and (element.get("_ax_press") or "_ocr_point" in element
                     or "_ax_point" in element)):
            name = "click"
            params = {"target": target, "delivery_mode": "background"}
            if "_ocr_point" in element or "_ax_point" in element:
                if not self._cua_snapshot_id:
                    raise CuaDriverUnavailable("desktop_fresh_observation_required")
                point = element.get("_ocr_point") or element["_ax_point"]
                params.update({"x": point[0], "y": point[1]})
            else:
                params["element_token"] = element["element_token"]
        elif (action == "type" and element and element.get("enabled") is not False
              and str(element.get("role") or "").lower()
                    .removeprefix("ax")
              in {"textfield", "searchfield", "combobox", "textarea"}
              and isinstance(arguments.get("text"), str)
              and 0 < len(arguments["text"]) <= 320
              and isinstance(element.get("frame"), dict)
              and all(type(element["frame"].get(k)) in (int, float)
                      and math.isfinite(element["frame"][k]) for k in ("x", "y", "w", "h"))
              and element["frame"]["w"] >= 2 and element["frame"]["h"] >= 2):
            # Web renderers need the pixel form. A proven native AX text field
            # can replace its value in one scoped action; a key chord followed
            # by insertion may leave a stray key or append if selection fails.
            frame = element["frame"]
            try:
                if arguments.get("text_entry_profile") is not None:
                    from coding_agents.document_text import textedit_three_lines
                    if (arguments["text_entry_profile"] != "textedit_three_lines_v1"
                            or self.app.bundle_id != "com.apple.TextEdit"
                            or str(element.get("role") or "").lower().removeprefix("ax") != "textarea"):
                        raise ValueError("invalid_reviewed_document_text")
                    text = textedit_three_lines(arguments["text"])
                else:
                    text = normalize_entry_text(arguments["text"])
            except ValueError:
                raise CuaDriverUnavailable("desktop_pre_dispatch_text_invalid") from None
            if (self.app.bundle_id == "com.apple.TextEdit"
                    and str(element.get("role") or "").lower().removeprefix("ax") == "textarea"):
                # Cocoa document insertion has a token-bound AXSelectedText
                # path. CUA proves ancestry and permits semantic-only insertion
                # when another window makes process-scoped keys unsafe. Never
                # substitute coordinates or clear with process-global hotkeys.
                name = "type_text"
                params = {"target": target, "element_token": element["element_token"],
                          "text": text, "delivery_mode": "background"}
            elif element.get("_ax_set_value"):
                name = "set_value"
                params = {"pid": self.app.pid, "window_id": self.window_id,
                          "element_token": element["element_token"],
                          "value": text}
            elif element.get("_native_text_fallback"):
                name = "_replace_text"
                params = {}
            elif element.get("_browser_address"):
                raise CuaDriverUnavailable("desktop_pre_dispatch_action_unavailable")
            else:
                name = "type_text"
                params = {"target": target, "text": text,
                          "x": frame["x"] + frame["w"] / 2,
                          "y": frame["y"] + frame["h"] / 2,
                          "delivery_mode": "background"}
        elif action == "key" and arguments.get("chord") in CHORDS:
            # Reviewed-intent shortcut (Save/New); never a model-supplied chord.
            name = "hotkey"
            params = {"target": target, "keys": CHORDS[arguments["chord"]][1],
                      "delivery_mode": "background"}
        elif action == "key" and arguments.get("chord") == "Return":
            if arguments.get("element_id") is not None and element is None:
                raise CuaDriverUnavailable("desktop_pre_dispatch_action_unavailable")
            name = "press_key"
            params = {"key": "Return", "target": target}
            if element is not None and element.get("_browser_address"):
                # AXConfirm submits the reviewed field with no key delivery or
                # foreground raise, so it also works for a hidden browser.
                name = "click"
                params = {"target": target, "element_token": element["element_token"],
                          "action": "confirm", "delivery_mode": "background"}
            elif element is not None:
                if (element.get("enabled") is False
                        or str(element.get("role") or "").lower().removeprefix("ax")
                        not in {"textfield", "searchfield", "combobox", "textarea"}):
                    raise CuaDriverUnavailable("desktop_pre_dispatch_action_unavailable")
                params["element_token"] = element["element_token"]
        elif (action == "scroll" and arguments.get("direction") in {"up", "down"}
              and arguments.get("element_id") is None and self._image_size
              and self._image_size[0] >= 200 and self._image_size[1] >= 200):
            name = "scroll"
            params = {"direction": arguments["direction"], "by": "page", "amount": 1,
                      "target": target, "x": self._image_size[0] / 2,
                      "y": self._image_size[1] / 2, "delivery_mode": "background"}
        else:
            raise CuaDriverUnavailable("desktop_pre_dispatch_action_unavailable")
        self._snapshot_id = None
        self._cua_snapshot_id = None
        self._elements = {}
        cursor_indicator = "not_shown"
        if name == "_replace_text":
            return self._replace_without_ax_set_value(element, text, target)
        if name == "click":
            # The AX token is the authoritative effect target. An optional
            # click-through marker is shown only while that app is already in
            # front; never draw a misleading cursor over an unrelated app.
            try:
                from AppKit import NSWorkspace
                front = NSWorkspace.sharedWorkspace().frontmostApplication()
                frame = element.get("frame")
                if (front and front.bundleIdentifier() == self.app.bundle_id
                        and isinstance(frame, dict)
                        and all(type(frame.get(k)) in (int, float)
                                and math.isfinite(frame[k]) for k in ("x", "y", "w", "h"))
                        and frame["w"] >= 2 and frame["h"] >= 2):
                    marker = self._session.call("move_cursor", {
                        "x": frame["x"] + frame["w"] / 2,
                        "y": frame["y"] + frame["h"] / 2, "scope": "window"})
                    if not marker.isError:
                        cursor_indicator = "overlay_requested"
            except Exception:
                pass  # Marker failure cannot replace or replay the admitted click.
        try:
            result = self._session.call(name, params)
        except CuaSessionError as error:
            code = str(error)
            if code not in {"desktop_cua_session_unavailable", "desktop_cua_transport_unavailable",
                            "desktop_cua_response_invalid"}:
                code = "desktop_cua_effect_uncertain"
            raise CuaDriverUnavailable(code, error.detail or str(error)) from error
        data = result.structuredContent if isinstance(result.structuredContent, dict) else {}
        if result.isError:
            refusal_code = _effect_refusal_code(data)
            raise CuaDriverUnavailable(refusal_code or "desktop_cua_effect_uncertain",
                                       _raw_detail(data))
        refusal_code = _effect_refusal_code(data)
        if refusal_code is not None:
            raise CuaDriverUnavailable(refusal_code, _raw_detail(data))
        if name == "set_value":
            # A readable, different value is a known failure; an unreadable
            # one stays an unverified effect for the normal visual review.
            try:
                observed = self._observe_field(element).get("value")
            except CuaDriverUnavailable:
                observed = text
            if isinstance(observed, str) and not field_value_matches(text, observed):
                raise CuaDriverUnavailable(CU_FIELD_VALUE_MISMATCH)
        return {"effect": data.get("effect") or "returned_unverified",
                "cursor_indicator": cursor_indicator}
