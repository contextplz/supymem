"""Exact-window Mac desktop driver behind a claimed Supymem work order.

Peekaboo supplies observation and input; it does not decide what is allowed.
The caller must pass every invocation through ComputerUseGate with a current
venture/device review and durable one-use admission. This adapter never runs
Peekaboo's autonomous agent or opens a shell supplied by a model.
"""
from __future__ import annotations

import json
import math
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from coding_agents.computer_use_gate import EffectUncertain
from desktop_runtime.peekaboo_session import PeekabooSession, PeekabooSessionError

MAX_OUTPUT = 512_000
MAX_ELEMENTS = 200
# A remote decision and the durable effect admission can take several seconds.
# Keep the pinned snapshot long enough for that round trip, but refresh before
# admission if fewer than ten seconds remain. Peekaboo still validates the
# exact snapshot and window when it dispatches an action.
SNAPSHOT_MAX_AGE_SECONDS = 30
SNAPSHOT_MIN_REMAINING_SECONDS = 10
APP_ID = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
SNAPSHOT_ID = re.compile(r"ps1_[A-Za-z0-9]+\Z")
ELEMENT_ID = re.compile(r"(?:elem|element|ocr)_[0-9]{1,5}\Z")
CHORD = re.compile(r"(?:cmd\+|ctrl\+|option\+|shift\+)*(?:Return|Tab|Escape|Delete|[a-z0-9])\Z", re.I)
BROWSER_BUNDLES = frozenset({"com.google.Chrome", "com.brave.Browser", "com.apple.Safari",
                             "org.mozilla.firefox", "com.microsoft.edgemac", "com.operasoftware.Opera"})
MCP_ELEMENT = re.compile(
    r'^\s*(elem_[0-9]{1,5}|ocr_[0-9]{1,5}) - "(.*?)" - at '
    r'\((-?[0-9]+), (-?[0-9]+)\) size ([0-9]+)×([0-9]+)(.*)$')


class DriverUnavailable(RuntimeError):
    pass


def _binary(explicit: str | None = None) -> str:
    bundled = str(Path(sys.executable).with_name("peekaboo"))
    if explicit:
        candidates = [explicit]
    elif ".app/Contents/MacOS/" in sys.executable:
        # A distributed app never executes a user-writable Homebrew binary.
        candidates = [bundled]
    else:
        candidates = [bundled, shutil.which("peekaboo"),
                      "/opt/homebrew/bin/peekaboo", "/usr/local/bin/peekaboo"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise DriverUnavailable("desktop_driver_not_installed")


@dataclass(frozen=True)
class AppChoice:
    name: str
    bundle_id: str
    pid: int
    process_start_identity: str


@dataclass(frozen=True)
class InstalledApp:
    name: str
    bundle_id: str
    path: str


def _object(raw: bytes) -> dict:
    if len(raw) > MAX_OUTPUT:
        raise DriverUnavailable("desktop_driver_response_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise DriverUnavailable("desktop_driver_response_invalid") from error
    if not isinstance(value, dict) or value.get("success") is not True or not isinstance(value.get("data"), dict):
        code = ((value.get("error") or {}).get("code") if isinstance(value, dict) else None)
        safe = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else "unavailable"
        raise DriverUnavailable(f"desktop_driver_{safe}")
    return value


def _vision_text(path: Path, origin: tuple[float, float], size: tuple[float, float]) -> list[dict]:
    """Host-local OCR for exact-window pixel captures when AX is incomplete."""
    try:
        import objc
        from Foundation import NSURL

        objc.loadBundle("Vision", globals(), bundle_path="/System/Library/Frameworks/Vision.framework")
        request = objc.lookUpClass("VNRecognizeTextRequest").alloc().init()
        request.setRecognitionLevel_(1)
        handler = objc.lookUpClass("VNImageRequestHandler").alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(str(path)), {})
        if not handler.performRequests_error_([request], None):
            raise DriverUnavailable("desktop_ocr_unavailable")
        recognized = request.results() or []
    except DriverUnavailable:
        raise
    except Exception as error:
        raise DriverUnavailable("desktop_ocr_unavailable") from error
    elements = []
    for item in recognized[:MAX_ELEMENTS]:
        candidates = item.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        label = str(candidate.string() or "").strip()
        if not label:
            continue
        box = item.boundingBox()
        x = origin[0] + float(box.origin.x) * size[0]
        y = origin[1] + (1 - float(box.origin.y) - float(box.size.height)) * size[1]
        width = float(box.size.width) * size[0]
        height = float(box.size.height) * size[1]
        confidence = float(candidate.confidence())
        if (not all(math.isfinite(n) for n in (x, y, width, height, confidence))
                or width < 2 or height < 2):
            continue
        elements.append({"id": f"ocr_{len(elements) + 1}", "role": "OCRText", "label": label[:300],
                         "bounds": {"x": x, "y": y, "width": width, "height": height},
                         "confidence": confidence, "is_enabled": True, "is_actionable": False})
    return elements


def _mcp_elements(text: str) -> list[dict]:
    """Parse Peekaboo's bounded local MCP observation, never model output."""
    role = ""
    elements = []
    for line in text.splitlines():
        heading = re.match(r"^(AX[A-Za-z]+|OCR[A-Za-z]+) \([0-9]+ found", line)
        if heading:
            role = heading.group(1).removeprefix("AX")
            continue
        match = MCP_ELEMENT.match(line)
        if not match or len(elements) >= MAX_ELEMENTS:
            continue
        element_id, label, x, y, width, height, suffix = match.groups()
        confidence = re.search(r"confidence: ([0-9]+)%", suffix)
        elements.append({"id": element_id, "role": "OCRText" if element_id.startswith("ocr_") else role,
                         "label": label[:300],
                         "bounds": {"x": int(x), "y": int(y), "width": int(width), "height": int(height)},
                         "is_enabled": "[disabled]" not in suffix,
                         "is_actionable": "[not actionable]" not in suffix and not element_id.startswith("ocr_"),
                         "confidence": int(confidence.group(1)) / 100 if confidence else None})
    return elements


class PeekabooDriver:
    """One pinned process and exact window; no model-selected app switching."""

    def __init__(self, *, app: AppChoice, window_id: int, binary: str | None = None,
                 allow_reviewed_foreground: bool = False):
        path = _binary(binary)
        if not APP_ID.fullmatch(app.bundle_id) or app.pid <= 0 or not app.process_start_identity:
            raise ValueError("desktop_app_identity_invalid")
        if not isinstance(window_id, int) or isinstance(window_id, bool) or window_id <= 0:
            raise ValueError("desktop_window_identity_invalid")
        self.binary, self.app, self.window_id = path, app, window_id
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._current_snapshot_id: str | None = None
        self._snapshot_at = 0.0
        self._capture_dir = tempfile.TemporaryDirectory(prefix="supymem-desktop-")
        self._window_origin = (0.0, 0.0)
        self._window_size = (0.0, 0.0)
        self._mcp: PeekabooSession | None = None
        self._foreground_handoff_done = False
        self._last_foreground_text: str | None = None
        self._allow_reviewed_foreground = allow_reviewed_foreground is True

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
        self._capture_dir.cleanup()

    def snapshot_ready_for_admission(self, snapshot_id: str) -> bool:
        """Reject a stale proposal before consuming its one-use effect grant."""
        return bool(self._snapshot and self._current_snapshot_id == snapshot_id
                    and time.monotonic() - self._snapshot_at
                    < SNAPSHOT_MAX_AGE_SECONDS - SNAPSHOT_MIN_REMAINING_SECONDS)

    def _call_mcp(self, name: str, arguments: dict, *, timeout: int = 25) -> tuple[dict, str, bool]:
        # No shell, model-provided tool name, remote Bridge or provider key.
        # The long-lived local process owns both the capture and its action.
        try:
            if self._mcp is None:
                self._mcp = PeekabooSession(
                    self.binary, allow_foreground=(self._allow_reviewed_foreground
                                                   and self._foreground_handoff_done))
            return self._mcp.call(name, arguments, timeout=timeout)
        except PeekabooSessionError as error:
            raise DriverUnavailable(str(error)) from error

    @classmethod
    def permissions_status(cls, *, binary: str | None = None) -> dict[str, bool]:
        """Report permissions for the same bundled process identity used for effects."""
        path = _binary(binary)
        try:
            result = subprocess.run([path, "permissions", "status", "--json", "--no-remote"],
                                    capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DriverUnavailable("desktop_driver_transport_unavailable") from error
        rows = _object(result.stdout)["data"].get("permissions")
        if not isinstance(rows, list):
            raise DriverUnavailable("desktop_permissions_invalid")
        return {name: next((row.get("isGranted") is True for row in rows
                            if isinstance(row, dict) and row.get("name") == label), False)
                for name, label in (("screen_recording", "Screen Recording"),
                                    ("accessibility", "Accessibility"),
                                    ("event_synthesizing", "Event Synthesizing"))}

    @classmethod
    def request_permission(cls, kind: str, *, binary: str | None = None) -> dict[str, bool]:
        """Ask macOS for one permission under the bundled helper's TCC identity.

        macOS still owns the decision; this never changes a privacy switch.
        """
        if kind not in {"screen-recording", "accessibility", "event-synthesizing"}:
            raise ValueError("desktop_permission_kind_invalid")
        path = _binary(binary)
        try:
            subprocess.run([path, "permissions", "request", kind, "--no-remote"],
                           capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DriverUnavailable("desktop_permission_request_unavailable") from error
        return cls.permissions_status(binary=path)

    @classmethod
    def installed_apps(cls) -> list[InstalledApp]:
        """Discover local app bundles without trusting a model-provided path."""
        roots = [Path("/Applications"), Path("/System/Applications"),
                 Path("/System/Library/CoreServices"), Path.home() / "Applications"]
        apps: dict[str, InstalledApp] = {}
        for root in roots:
            if not root.is_dir():
                continue
            try:
                result = subprocess.run(["/usr/bin/mdfind", "-onlyin", str(root),
                                         "kMDItemContentType == 'com.apple.application-bundle'"],
                                        capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                continue
            for raw in result.stdout.decode("utf-8", errors="ignore").splitlines()[:256]:
                path = Path(raw)
                if (path.suffix != ".app" or not path.is_dir()
                        or not path.is_relative_to(root)
                        or len(path.relative_to(root).parts) > 3):
                    continue
                try:
                    with (path / "Contents" / "Info.plist").open("rb") as stream:
                        info = plistlib.load(stream)
                except (OSError, ValueError, TypeError):
                    continue
                bundle = info.get("CFBundleIdentifier")
                name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or path.stem
                if (isinstance(bundle, str) and APP_ID.fullmatch(bundle)
                        and isinstance(name, str) and 0 < len(name) <= 160):
                    apps.setdefault(bundle, InstalledApp(name, bundle, str(path)))
        return sorted(apps.values(), key=lambda app: app.name.casefold())[:256]

    @classmethod
    def default_browser_bundle(cls, installed: list[InstalledApp]) -> str | None:
        """Honor macOS's HTTPS handler; never infer a browser from a task keyword."""
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            target = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
                NSURL.URLWithString_("https://example.com"))
            path = Path(str(target.path())).resolve() if target else None
        except Exception:
            return None
        return next((app.bundle_id for app in installed if Path(app.path).resolve() == path), None)

    @classmethod
    def launch_installed_app(cls, bundle_id: str, *, ensure_window: bool = False) -> AppChoice:
        """Start an installed app; open its first window only when the request needs one."""
        if not APP_ID.fullmatch(bundle_id) or bundle_id not in {app.bundle_id for app in cls.installed_apps()}:
            raise DriverUnavailable("desktop_app_not_installed")
        try:
            result = subprocess.run(["/usr/bin/open", "-g", "-b", bundle_id],
                                    capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DriverUnavailable("desktop_app_launch_unavailable") from error
        if result.returncode != 0:
            raise DriverUnavailable("desktop_app_launch_failed")
        for _ in range(20):
            chosen = next((app for app in cls.running_apps() if app.bundle_id == bundle_id), None)
            if chosen:
                if ensure_window and not any(row["on_screen"] for row in cls.running_windows(chosen)):
                    # LaunchServices can report a running app before it has a
                    # window. Peekaboo owns the bounded foreground fallback;
                    # no URL, document, or model-provided command is opened.
                    try:
                        opened = subprocess.run(
                            [_binary(), "app", "launch", "--bundle-id", bundle_id,
                             "--foreground", "--wait-for-window", "--json", "--no-remote"],
                            capture_output=True, timeout=12, check=False)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        raise DriverUnavailable("desktop_app_window_unavailable") from error
                    if opened.returncode != 0:
                        raise DriverUnavailable("desktop_app_window_unavailable")
                    chosen = next((app for app in cls.running_apps() if app.bundle_id == bundle_id), None)
                    if chosen is None or not any(row["on_screen"] for row in cls.running_windows(chosen)):
                        raise DriverUnavailable("desktop_app_window_unavailable")
                return chosen
            time.sleep(0.1)
        raise DriverUnavailable("desktop_app_launch_unconfirmed")

    @classmethod
    def running_apps(cls, *, binary: str | None = None) -> list[AppChoice]:
        path = _binary(binary)
        try:
            result = subprocess.run([path, "app", "list", "--include-hidden", "--json", "--no-remote"], capture_output=True,
                                    timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DriverUnavailable("desktop_driver_transport_unavailable") from error
        apps = _object(result.stdout)["data"].get("apps")
        if not isinstance(apps, list):
            raise DriverUnavailable("desktop_app_inventory_invalid")
        choices = []
        for item in apps[:128]:
            if not isinstance(item, dict):
                continue
            bundle, name, pid = item.get("bundle_id"), item.get("name"), item.get("pid")
            identity = item.get("process_start_identity_decimal")
            if (isinstance(bundle, str) and APP_ID.fullmatch(bundle)
                    and isinstance(name, str) and 0 < len(name) <= 160
                    and type(pid) is int and pid > 0 and isinstance(identity, str) and identity.isdigit()):
                choices.append(AppChoice(name, bundle, pid, identity))
        return choices

    @classmethod
    def running_windows(cls, app: AppChoice, *, binary: str | None = None) -> list[dict]:
        path = _binary(binary)
        if not APP_ID.fullmatch(app.bundle_id):
            raise DriverUnavailable("desktop_driver_not_installed")
        try:
            result = subprocess.run([path, "window", "list", "--app", app.bundle_id, "--json", "--no-remote"],
                                    capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DriverUnavailable("desktop_driver_transport_unavailable") from error
        data = _object(result.stdout)["data"]
        target = data.get("target_application_info") or {}
        if target.get("pid") != app.pid or target.get("bundle_id") != app.bundle_id:
            raise DriverUnavailable("desktop_app_identity_changed")
        rows = data.get("windows")
        if not isinstance(rows, list):
            raise DriverUnavailable("desktop_window_inventory_invalid")
        found = [{"window_id": row["window_id"], "title": str(row.get("window_title") or "")[:200],
                  "on_screen": row.get("is_on_screen") is True,
                  "width": max(0, int((row.get("bounds") or {}).get("width") or 0)),
                  "height": max(0, int((row.get("bounds") or {}).get("height") or 0))}
                 for row in rows[:64] if isinstance(row, dict) and type(row.get("window_id")) is int
                 and row["window_id"] > 0]
        # Windows on another Space or minimized are still the same reviewed
        # windows; Peekaboo can omit them, so add them from the full CG list.
        known = {row["window_id"] for row in found}
        for window_id, title, width, height in cls._off_screen_windows(app.pid):
            if window_id not in known and len(found) < 64:
                found.append({"window_id": window_id, "title": title, "on_screen": False,
                              "width": width, "height": height})
        return found

    @staticmethod
    def _off_screen_windows(pid: int) -> list[tuple[int, str, int, int]]:
        try:
            from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                                kCGWindowListOptionAll)
            rows = CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID) or []
        except Exception:
            return []
        return [(int(row["kCGWindowNumber"]), str(row.get("kCGWindowName") or "")[:200],
                 int((row.get("kCGWindowBounds") or {}).get("Width") or 0),
                 int((row.get("kCGWindowBounds") or {}).get("Height") or 0))
                for row in rows if row.get("kCGWindowOwnerPID") == pid
                and row.get("kCGWindowLayer") == 0 and not row.get("kCGWindowIsOnscreen")
                and type(row.get("kCGWindowNumber")) is int]

    def _check_process(self):
        current = next((item for item in self.running_apps(binary=self.binary)
                        if item.bundle_id == self.app.bundle_id), None)
        if current != self.app:
            raise DriverUnavailable("desktop_app_identity_changed")

    def focus_reviewed_app(self) -> None:
        """Focus the exact reviewed window after a saved foreground review.

        The caller owns the decision to use this fallback. It cannot select a
        different process or window, and the next observation must be fresh.
        """
        if not self._allow_reviewed_foreground:
            raise DriverUnavailable("desktop_foreground_not_reviewed")
        self._check_process()
        from desktop_runtime.native_foreground_input import (
            ForegroundInputUnavailable, focus_reviewed_window)
        try:
            focus_reviewed_window(pid=self.app.pid, window_id=self.window_id)
        except ForegroundInputUnavailable as error:
            raise DriverUnavailable(str(error)) from error
        self._check_process()
        # The reviewed window was checked in the pre-claim inventory and in
        # focus_reviewed_window's exact CG lookup. Peekaboo's AX-backed window
        # inventory can be partial immediately after activation (Safari reports
        # pixels_only), so it is not a sound post-focus identity proof. The
        # native frontmost CG window check below is the final dispatch gate.
        if not self._confirmed_foreground_window():
            raise DriverUnavailable("desktop_foreground_window_unconfirmed")
        self._snapshot = None
        self._current_snapshot_id = None
        self._foreground_handoff_done = True
        # Capture before this handoff ran under background-only authority.
        # Restart the local MCP process before recapturing so foreground
        # authority exists only after the exact reviewed window is attested.
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None

    def _target(self) -> tuple[str, ...]:
        return ("--app", self.app.bundle_id, "--window-id", str(self.window_id))

    def _confirmed_foreground_window(self) -> bool:
        """Permit a global chord only while the reviewed window is foremost.

        This is checked immediately before dispatch. An app-level focus grant
        alone is insufficient when that app has more than one window.
        """
        try:
            from AppKit import NSWorkspace
            from Quartz import (CGWindowListCopyWindowInfo, kCGNullWindowID,
                                kCGWindowListOptionOnScreenOnly)
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if not front or front.processIdentifier() != self.app.pid:
                return False
            windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                                 kCGNullWindowID)
            for window in windows or []:
                if window.get("kCGWindowLayer") == 0:
                    return (window.get("kCGWindowNumber") == self.window_id
                            and window.get("kCGWindowOwnerPID") == self.app.pid)
        except Exception:
            return False
        return False

    def _settled_foreground_window(self) -> bool:
        """Allow a just-dispatched input to finish its focus transition.

        This repeats only a read-only identity check, never an uncertain input.
        A different foreground window still fails closed before another action.
        """
        for attempt in range(3):
            if self._confirmed_foreground_window():
                return True
            if attempt < 2:
                time.sleep(0.15)
        return False

    def _element_point(self, element: dict) -> str | None:
        bounds = element.get("bounds")
        if not isinstance(bounds, dict):
            return None
        values = [bounds.get(key) for key in ("x", "y", "width", "height")]
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            return None
        x, y, width, height = values
        x -= self._window_origin[0]
        y -= self._window_origin[1]
        if (x < 0 or y < 0 or width < 2 or height < 2
                or x + width > self._window_size[0] or y + height > self._window_size[1]):
            return None
        return f"{round(x + width / 2)},{round(y + height / 2)}"

    def _browser_address_ocr(self, element: dict) -> bool:
        bounds = element.get("bounds")
        label = str(element.get("label") or "")
        return bool(self.app.bundle_id in BROWSER_BUNDLES
            and str(element.get("id") or "").startswith("ocr_")
            and type(element.get("confidence")) in (int, float)
            and element["confidence"] >= 0.5
            and isinstance(bounds, dict) and type(bounds.get("y")) in (int, float)
            and 30 <= bounds["y"] - self._window_origin[1] <= 125
            and type(bounds.get("width")) in (int, float) and bounds["width"] >= 70
            and (re.search(r"(?i)(?:[a-z0-9-]+\.)+[a-z]{2,}", label)
                 or "search" in label.lower()))

    def _text_entry_ocr(self, element: dict) -> bool:
        label = str(element.get("label") or "").strip()
        return bool(str(element.get("id") or "").startswith("ocr_")
            and self._element_point(element)
            and type(element.get("confidence")) in (int, float)
            and element["confidence"] >= 0.5
            and re.match(r"(?i)^(?:search|find|enter|type|write|ask)(?:\b|$)", label))

    def capture(self) -> dict:
        with self._lock:
            self._check_process()
            screenshot = Path(self._capture_dir.name) / "current.png"
            screenshot.unlink(missing_ok=True)
            metadata, observed_text, observation_error = self._call_mcp("see", {
                "app_target": self.app.bundle_id, "window_id": self.window_id,
                "ocr": True, "max_elements": MAX_ELEMENTS, "path": str(screenshot)}, timeout=25)
            if observation_error:
                raise DriverUnavailable("desktop_observation_unavailable")
            receipt = metadata.get("target_receipt") or {}
            coordinate = metadata.get("coordinate_context") or {}
            snapshot_id = coordinate.get("reference_id")
            bounds = coordinate.get("logical_bounds") or {}
            if (receipt.get("pid") != self.app.pid or receipt.get("window_id") != self.window_id
                    or str(receipt.get("process_start_identity_decimal")) != self.app.process_start_identity
                    or not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id)
                    or not screenshot.is_file() or screenshot.stat().st_size > 12_000_000):
                raise DriverUnavailable("desktop_observation_not_pinned")
            if (coordinate.get("logical_space") != "global_display_points"
                    or (coordinate.get("window") or {}).get("window_id") != self.window_id
                    or not isinstance(bounds, dict)
                    or any(type(bounds.get(key)) not in (int, float)
                           or not math.isfinite(bounds[key]) for key in ("x", "y", "width", "height"))
                    or bounds["width"] < 2 or bounds["height"] < 2):
                raise DriverUnavailable("desktop_coordinate_context_invalid")
            self._window_origin = (bounds["x"], bounds["y"])
            self._window_size = (bounds["width"], bounds["height"])
            elements = _mcp_elements(observed_text)
            self._snapshot = {item["id"]: item for item in elements[:MAX_ELEMENTS]
                              if isinstance(item, dict) and isinstance(item.get("id"), str)
                              and ELEMENT_ID.fullmatch(item["id"])}
            self._current_snapshot_id = snapshot_id
            self._snapshot_at = time.monotonic()
            return {"app": self.app.bundle_id, "window_id": self.window_id,
                    "snapshot_id": snapshot_id,
                    "elements": [{"id": item["id"], "role": str(item.get("role") or "")[:80],
                                  "label": str(item.get("label") or "")[:300],
                                  "readout": item.get("role") in {"StaticText", "TextField"}
                                  and item.get("is_actionable") is not True,
                                  "actionable": item.get("is_actionable") is True,
                                  "confidence": item.get("confidence") if type(item.get("confidence")) in (int, float) else None,
                                  "has_point": self._element_point(item) is not None,
                                  "browser_address": self._browser_address_ocr(item),
                                  "text_entry": self._text_entry_ocr(item),
                                  "enabled": item.get("is_enabled") is True}
                                 for item in self._snapshot.values()],
                    "screenshot_path": str(screenshot)}

    def __call__(self, arguments: dict, *, session_id: str) -> dict:
        action = arguments.get("action")
        if arguments.get("app") != self.app.bundle_id or not session_id:
            raise PermissionError("desktop_driver_scope_mismatch")
        if action == "capture":
            return self.capture()
        if action == "wait":
            delay = arguments.get("milliseconds", 250)
            if type(delay) is not int or not 0 <= delay <= 1000:
                raise ValueError("desktop_wait_bounds")
            time.sleep(delay / 1000)
            return {"waited_ms": delay}
        with self._lock:
            if not self._snapshot or time.monotonic() - self._snapshot_at > SNAPSHOT_MAX_AGE_SECONDS:
                raise PermissionError("desktop_fresh_observation_required")
            self._check_process()
            snapshot_id = arguments.get("snapshot_id")
            if snapshot_id != self._snapshot_id():
                raise PermissionError("desktop_snapshot_changed")
            if action == "click":
                element_id = arguments.get("element_id")
                element = self._snapshot.get(element_id)
                if not element or element.get("is_enabled") is not True:
                    raise PermissionError("desktop_element_not_actionable")
                if element.get("is_actionable") is True:
                    tool_arguments = {"on": element_id, "snapshot": snapshot_id,
                                      "pid": self.app.pid}
                elif (element_id.startswith("ocr_") and type(element.get("confidence")) in (int, float)
                      and element["confidence"] >= 0.5 and self._element_point(element)):
                    # OCR has no AXPress. Its global logical point is valid only
                    # with the exact window's fresh capture reference.
                    bounds = element["bounds"]
                    point = f'{round(bounds["x"] + bounds["width"] / 2)},{round(bounds["y"] + bounds["height"] / 2)}'
                    tool_arguments = {"coords": point, "coordinate_space": "global_display_points",
                                      "coordinate_reference": snapshot_id, "snapshot": snapshot_id,
                                      "pid": self.app.pid}
                else:
                    raise PermissionError("desktop_element_not_actionable")
                tool_name = "click"
            elif action == "type":
                value = arguments.get("text")
                element_id = arguments.get("element_id")
                element = self._snapshot.get(element_id)
                role = str(element.get("role") or "").lower() if isinstance(element, dict) else ""
                text_entry = bool(isinstance(element, dict) and (
                    self._browser_address_ocr(element) or self._text_entry_ocr(element)))
                if (not element or element.get("is_enabled") is not True
                        or not (role in {"textfield", "searchfield", "combobox", "textarea"} or text_entry)
                        or not self._element_point(element)
                        or not isinstance(value, str) or not 0 < len(value) <= 320):
                    raise PermissionError("desktop_type_requires_reviewed_field")
                if element_id.startswith("ocr_"):
                    bounds = element["bounds"]
                    tool_arguments = {"text": value, "snapshot": snapshot_id, "clear": True}
                    tool_arguments.update(coords=f'{round(bounds["x"] + bounds["width"] / 2)},{round(bounds["y"] + bounds["height"] / 2)}',
                                          coordinate_space="global_display_points",
                                          coordinate_reference=snapshot_id)
                    tool_name = "type"
                else:
                    if self._foreground_handoff_done:
                        # Use the permissioned observer's own input stack.
                        # Quartz events from the separate frozen runtime can
                        # report dispatch while Safari receives no characters.
                        tool_name = "type"
                        tool_arguments = {"on": element_id, "text": value,
                                          "snapshot": snapshot_id, "clear": True,
                                          "foreground": True}
                    else:
                        # Background-only AX replacement gives a confirmed
                        # value receipt without synthesized keyboard events.
                        tool_arguments = {"on": element_id, "value": value, "snapshot": snapshot_id}
                        tool_name = "set_value"
            elif action == "key":
                chord = arguments.get("chord")
                if not isinstance(chord, str) or not CHORD.fullmatch(chord):
                    raise PermissionError("desktop_key_not_reviewed")
                tool_name, tool_arguments = "press", {"keys": [chord], "snapshot": snapshot_id}
            else:
                raise PermissionError("desktop_action_not_supported")
            # One observation authorizes at most one effect. The caller's gate
            # consumes a durable admission before reaching this method.
            self._snapshot = None
            self._current_snapshot_id = None
            if self._foreground_handoff_done and action in {"type", "key"}:
                if not self._settled_foreground_window():
                    raise DriverUnavailable("desktop_pre_dispatch_foreground_window_changed")
                self._check_process()
                if action == "key":
                    from desktop_runtime.native_foreground_input import reviewed_field_matches
                    if (chord != "Return" or not self._last_foreground_text
                            or not reviewed_field_matches(
                                pid=self.app.pid,
                                window_bounds=(*self._window_origin, *self._window_size),
                                expected_text=self._last_foreground_text)):
                        raise DriverUnavailable("desktop_pre_dispatch_focused_field_changed")
                    tool_arguments = {"keys": ["Return"], "snapshot": snapshot_id}
            metadata, _, is_error = self._call_mcp(tool_name, tool_arguments, timeout=15)
            if (is_error and metadata.get("mutation_dispatched") is False
                    and metadata.get("retry_safe") is True
                    and metadata.get("effect") == "refused"):
                raw_code = metadata.get("error_code") or metadata.get("refusal_reason")
                safe_code = (raw_code.lower() if isinstance(raw_code, str)
                             and re.fullmatch(r"[A-Za-z0-9_]{1,60}", raw_code) else "unknown")
                raise DriverUnavailable(f"desktop_pre_dispatch_{safe_code}")
            if (not is_error and metadata.get("state") == "confirmed_no_change"
                    and metadata.get("effect") == "confirmed"
                    and metadata.get("mutation_dispatched") is False
                    and metadata.get("evidence") == "verified_no_change"):
                # Peekaboo may verify that a field already holds the reviewed
                # text without sending another keystroke. This is a conclusive
                # no-op, not an uncertain effect. The caller must observe again
                # before deciding a different next action (such as Return).
                if self._foreground_handoff_done and action == "type":
                    self._last_foreground_text = value
                return {"effect": "no_change", "outcome": "verified_no_change",
                        "window_id": self.window_id, "app": self.app.bundle_id}
            if (metadata.get("mutation_dispatched") is not True
                    or metadata.get("effect") not in {"confirmed", "unverifiable"}):
                # The admitted effect may have reached the app. Preserve a
                # bounded diagnostic code without exposing the target text or
                # treating an undocumented MCP response as a success.
                effect = metadata.get("effect")
                effect_code = effect if isinstance(effect, str) and re.fullmatch(r"[a-z_]{1,32}", effect) else "missing"
                dispatch_code = "dispatched" if metadata.get("mutation_dispatched") is True else "not_confirmed"
                raise EffectUncertain(f"desktop_effect_{effect_code}_{dispatch_code}")
            if self._foreground_handoff_done:
                if action == "type":
                    self._last_foreground_text = value
                elif action == "key":
                    self._last_foreground_text = None
            return {"effect": metadata["effect"], "outcome": metadata.get("outcome"),
                    "window_id": self.window_id, "app": self.app.bundle_id}

    def _snapshot_id(self) -> str | None:
        return self._current_snapshot_id
