"""Pinned, private stdin transport for the optional reviewed desktop backend."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from coding_agents.cu_ai_loop import CARD, SECRETISH, safety_block
from coding_agents.cu_ai_risk import assess_risk
from core.redact import is_secret_value

from desktop_runtime.cu_ai_cua import CuaError, CuaMac
from desktop_runtime.cu_ai_engine import NeedsHuman, Observation

VERSION = "0.10.1"
ENVELOPE_VERSION = "2.4"
MAX_BYTES = 1_048_576
BINARY_HASHES = {
    "arm64": {
        "agent-desktop": "7bb4c166bcc91d8faef320d06eb7708fccdb2b1fa2c071fefb9c9b6d685c0899",
        "agent-desktop-macos-helper": "c1c9f563399275c904bf17adefaf8d0e2af000173e1de8509925600af9a7aad1",
    },
    "x86_64": {
        "agent-desktop": "80650ee085d0f3a67f57975fe57ca68e28021b45818dcc6b1a4a389b90b58f3a",
        "agent-desktop-macos-helper": "0a6ef83d858c86400a893ba969035590cfaef167fb553ecb44d5d6c9fb916db6",
    },
}
ROLES = {"button": "AXButton", "textfield": "AXTextField", "combobox": "AXComboBox", "checkbox": "AXCheckBox",
         "statictext": "AXStaticText", "heading": "AXHeading", "link": "AXLink"}


def parse_response(response, command):
    if (type(response) is not dict or response.get("version") != ENVELOPE_VERSION
            or type(response.get("ok")) is not bool or response.get("command") != command
            or type(response.get("data") if response["ok"] else response.get("error"))
            is not (list if response["ok"] and command == "list-windows" else dict)):
        raise CuaError("driver_readonly_failed")
    return response


def refuse_secret_text(text):
    if is_secret_value(text) or SECRETISH.search(text) or CARD.search(text):
        raise NeedsHuman("guardrail", "I cannot enter passwords, tokens, keys or card numbers. Enter that information yourself.",
                         dispatch_state="not_dispatched")


class AgentDesktopCli:
    def __init__(self, binary: Path):
        self._process = self._directory = self._executor = None
        if binary.name != "agent-desktop" or not binary.is_absolute():
            raise CuaError("desktop_cua_not_bundled")
        hashes = BINARY_HASHES.get(platform.machine(), {})
        for name in ("agent-desktop", "agent-desktop-macos-helper"):
            artifact = binary.with_name(name)
            if not artifact.is_file() or artifact.is_symlink() or name not in hashes:
                raise CuaError("desktop_cua_not_bundled")
            try:
                with artifact.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError:
                raise CuaError("desktop_cua_not_bundled") from None
            if digest != hashes[name]:
                raise CuaError("desktop_cua_not_bundled")
        if sys.platform != "darwin":
            raise CuaError("desktop_cua_session_unavailable")
        self._directory = tempfile.TemporaryDirectory(prefix="supymem-agent-desktop-")
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            self._process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env={"HOME": self._directory.name, "PATH": "/usr/bin:/bin",
                    "AGENT_DESKTOP_HOME": self._directory.name, "AGENT_DESKTOP_INTERNAL_COMMAND_HOST": "1"})
            if self(["version"])["data"].get("version") != VERSION:
                raise CuaError("desktop_cua_protocol_unsupported")
        except (OSError, CuaError):
            self.close()
            raise CuaError("desktop_cua_session_unavailable") from None

    def __call__(self, arguments):
        if (type(arguments) is not list or not arguments or any(type(item) is not str for item in arguments)
                or self._process is None):
            raise CuaError("desktop_cua_scope_invalid")
        if arguments[0] in {"type", "set-value"} and len(arguments) > 2:
            refuse_secret_text(arguments[2])
        payload = (json.dumps(arguments, ensure_ascii=False) + "\n").encode()
        if len(payload) > MAX_BYTES:
            raise CuaError("desktop_cua_scope_invalid")

        def exchange():
            self._process.stdin.write(payload)
            self._process.stdin.flush()
            output = self._process.stdout.readline(MAX_BYTES + 1)
            if len(output) > MAX_BYTES or not output.endswith(b"\n"):
                raise ValueError()
            return parse_response(json.loads(output), arguments[0])

        try:
            return self._executor.submit(exchange).result(timeout=60)
        except (OSError, ValueError, RuntimeError, CuaError, concurrent.futures.TimeoutError):
            self.close()
            raise CuaError("desktop_cua_transport_unavailable") from None

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if hasattr(stream, "close"):
                    stream.close()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


class AgentDesktopMac(CuaMac):
    def __init__(self, scope, goal, transport, *, authorized):
        super().__init__(self._tool)
        if scope.extra or scope.modal_parents:
            raise CuaError("desktop_cua_scope_invalid")
        self.scope, self.goal, self.transport, self.authorized = scope, goal, transport, authorized
        self._owned_close = getattr(transport, "close", None)

    def _request(self, arguments):
        self._require_open()
        return parse_response(self.transport(arguments), arguments[0])

    def _tool(self, tool, arguments):
        if tool != "check_permissions":
            raise CuaError("background_effect_refused")
        response = self._request(["permissions"])
        if not response["ok"]:
            raise CuaError("driver_readonly_failed")
        return {key: response["data"].get(key, {}).get("state") == "granted"
                for key in ("accessibility", "screen_recording", "automation")}

    def observe(self):
        self._require_open()
        self.invalidate_observation()
        self._authorize()
        self.scope.live()
        windows = self._request(["list-windows"])
        if not windows["ok"]:
            raise CuaError("driver_readonly_failed")
        selected = [row for row in windows["data"] if type(row) is dict
                    and row.get("id") == f"w-{self.scope.window}"
                    and type(row.get("pid")) is int and row["pid"] == self.scope.pid]
        if len(selected) != 1:
            raise NeedsHuman("other", "Choose your intended window and reopen your saved task before retrying.",
                             dispatch_state="not_dispatched")
        self._authorize()
        self.scope.live()
        response = self._request(["snapshot", "--window-id", selected[0]["id"], "--max-depth", "20"])
        if not response["ok"]:
            if response["error"].get("code") == "AMBIGUOUS_TARGET":
                raise NeedsHuman("other", "More than one window matched. Choose your intended window and reopen your saved task.",
                                 dispatch_state="not_dispatched")
            raise CuaError("driver_readonly_failed")
        data = response["data"]
        if (data.get("complete") is not True or data.get("app") != self.app or type(data.get("window")) is not dict
                or data["window"].get("id") != f"w-{self.scope.window}"
                or not isinstance(data["window"].get("title"), str)
                or not isinstance(data.get("snapshot_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", data["snapshot_id"])
                or type(data.get("tree")) is not dict):
            raise CuaError("window_observation_target_mismatch")
        self.scope.live()
        elements, raw, pending = [], [], [data["tree"]]
        while pending:
            node = pending.pop()
            if (type(node) is not dict or type(node.get("children", [])) is not list
                    or type(node.get("states", [])) is not list
                    or type(node.get("available_actions", [])) is not list or len(raw) >= 2000
                    or not isinstance(node.get("role"), str)
                    or any(type(value) is not str for value in node.get("states", []) + node.get("available_actions", []))
                    or any(node.get(key) is not None and type(node[key]) is not str
                           for key in ("name", "value", "native_id"))):
                raise CuaError("driver_readonly_failed")
            pending.extend(reversed(node.get("children", [])))
            ref = node.get("ref_id")
            if ref is None:
                continue
            token = re.fullmatch(r"@" + re.escape(data["snapshot_id"]) + r":e([0-9]{1,10})", ref) if isinstance(ref, str) else None
            if (token is None or not 0 < int(token.group(1)) < 2**32
                    or any(row["id"] == ref for row in elements)):
                raise CuaError("driver_readonly_failed")
            states = node.get("states", [])
            role = ROLES.get(node.get("role"), "AXUnknown")
            row = {"id": ref, "ax_role": role, "label": node.get("name") or "",
                   "value": None if "secure" in states else node.get("value"),
                   "native_id": node.get("native_id"), "states": states,
                   "available_actions": node.get("available_actions", []), "type": node.get("role"),
                   "enabled": "disabled" not in states}
            elements.append(row)
            raw.append({**row, "role": role, "pid": self.scope.pid, "window_id": self.scope.window,
                        "element_index": len(raw), "element_token": ref})
        observation = Observation(b"", elements, self.app, data["window"].get("title", ""), data["snapshot_id"],
                                  any("secure" in row["states"] and "focused" in row["states"] for row in elements), self.scope.pid)
        self.raw, self.tokens = raw, {row["id"]: row["id"] for row in elements}
        self.ax_window = self.shot_window = self.scope.window
        self._observation, self._observation_at = observation, time.monotonic()
        return observation

    def _authorize(self):
        try:
            allowed = self.authorized() is True
        except Exception:  # noqa: BLE001 - authorization must fail closed without exposing private exception bodies.
            allowed = False
        if not allowed:
            raise NeedsHuman("other", "Your task is no longer authorized. Reopen your saved task before retrying.",
                             dispatch_state="not_dispatched")

    def _guard(self, action, observation, element):
        self._authorize()
        if action.op == "type":
            refuse_secret_text(action.text)
        try:
            admitted = self.scope.admit(action, observation) is None
        except (ValueError, OSError, RuntimeError):
            admitted = False
        if not admitted:
            raise NeedsHuman("other", "Your selected app changed. Refresh your saved task before retrying.",
                             dispatch_state="not_dispatched")
        blocked = safety_block(action, goal=self.goal, frontmost=observation.frontmost,
            target_label=element["label"], focused_secure=observation.secure_focus or "secure" in element["states"],
            window_title=observation.window_title)
        risk = assess_risk(action.op, self.goal, element=element, label=element["label"], text=action.text)
        if (blocked or risk or {"disabled", "hidden", "offscreen"}.intersection(element["states"])
                or action.op == "type" and "readonly" in element["states"]):
            raise NeedsHuman("guardrail", "This control is not safe for your saved task. Review the task before retrying.",
                             dispatch_state="not_dispatched")

    def _act(self, action, observation):
        self._require_open()
        if (action.op not in {"click", "type"} or observation is not self._observation
                or time.monotonic() - self._observation_at >= 20):
            raise CuaError("background_effect_refused")
        original = next((row for row in observation.elements if row["id"] == action.element_id), None)
        if original is None:
            raise CuaError("background_effect_refused")
        self._guard(action, observation, original)
        command = "type" if action.op == "type" else "click"
        # shortcut: the pin folds native multiline roles; refuse multiline writes until exact role evidence exists.
        if (command not in original["available_actions"] or action.op == "type"
                and (original["ax_role"] not in {"AXTextField", "AXComboBox"}
                     or not original.get("native_id") or not isinstance(original.get("value"), str)
                     or "\n" in action.text or "\r" in action.text
                     or original["value"] and "clear" not in original["available_actions"])):
            raise NeedsHuman("other", "Your selected control does not support this action. Choose a supported control.",
                             dispatch_state="not_dispatched")
        cleared = False
        try:
            if action.op == "type" and original["value"]:
                observation = self._perform("clear", action, observation, original)
                cleared = True
                if not action.text:
                    return "applied"
            self._perform(command, action, observation, original)
            return "applied"
        except NeedsHuman as error:
            if cleared and error.dispatch_state == "not_dispatched":
                raise NeedsHuman("other", "Your field changed before the task finished. Check its contents before retrying.") from None
            raise

    def _perform(self, command, action, observation, original):
        element = self._matching(observation, original) if command != "click" else original
        if element is None:
            raise NeedsHuman("other", "Your selected control changed. Refresh the task before retrying.",
                             dispatch_state="not_dispatched")
        for attempt in range(2):
            self._guard(action, observation, element)
            if command not in element["available_actions"] or command == "type" and element.get("value") != "":
                raise NeedsHuman("other", "Your selected control does not support this action. Choose a supported control.",
                                 dispatch_state="not_dispatched")
            arguments = [command, element["id"]]
            if command == "type":
                arguments.append(action.text)
            arguments.extend(["--timeout-ms", "0"])
            self._potential_effect_count += 1
            try:
                response = self._request(arguments)
            except (CuaError, OSError, ValueError):
                raise NeedsHuman("other", "Input delivery could not be confirmed. Check your selected window before retrying.") from None
            if response["ok"]:
                disposition = response["data"].get("disposition", {})
                if (type(disposition) is not dict
                        or disposition.get("delivery") not in {"delivered_verified", "delivered_unverified"}
                        or disposition.get("retry") != "unsafe"):
                    raise NeedsHuman("other", "Input delivery could not be confirmed. Check your selected window before retrying.")
                try:
                    verified = self.observe()
                except (CuaError, OSError, ValueError, NeedsHuman):
                    raise NeedsHuman("other", "Your updated window could not be checked. Check its result before retrying.") from None
                if command in {"clear", "type"}:
                    matched = self._matching(verified, original)
                    expected = "" if command == "clear" else action.text
                    if matched is None or matched.get("value") != expected:
                        raise NeedsHuman("other", "Your entered text could not be verified. Check the field before retrying.")
                    try:
                        self._guard(action, verified, matched)
                        value = self._request(["get", matched["id"], "--property", "value"])
                        self.scope.live()
                    except (CuaError, OSError, ValueError, NeedsHuman):
                        raise NeedsHuman("other", "Your entered text could not be verified. Check the field before retrying.") from None
                    if (not value["ok"] or value["data"].get("property") != "value"
                            or value["data"].get("ref") != matched["id"] or value["data"].get("value") != expected):
                        raise NeedsHuman("other", "Your entered text could not be verified. Check the field before retrying.")
                return verified
            error = response["error"]
            safe = error.get("disposition") == {"delivery": "not_delivered", "retry": "safe"}
            if error.get("code") == "AMBIGUOUS_TARGET":
                raise NeedsHuman("other", "More than one control matched. Choose your intended window and reopen your saved task.",
                                 dispatch_state="not_dispatched" if safe else "unconfirmed")
            if error.get("code") != "STALE_REF" or not safe or attempt:
                raise NeedsHuman("permission" if error.get("code") == "PERM_DENIED" else "other",
                    "Your app refused the action. Check app permissions and the selected control before retrying.",
                    dispatch_state="not_dispatched" if safe else "unconfirmed")
            try:
                observation = self.observe()
                element = self._matching(observation, original)
            except (CuaError, OSError, ValueError, NeedsHuman):
                element = None
            if element is None:
                raise NeedsHuman("other", "Your selected control changed. Refresh the task before retrying.",
                                 dispatch_state="not_dispatched")

    @staticmethod
    def _matching(observation, original):
        # shortcut: retry identity requires native_id, upgrade only with audited stable locator evidence.
        if not original.get("native_id"):
            return None
        rows = [row for row in observation.elements if all(row.get(key) == original.get(key)
                for key in ("native_id", "ax_role", "label"))]
        return rows[0] if len(rows) == 1 else None
