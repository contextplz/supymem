"""Native-owned bounded browser loop. No global desktop/profile attachment.

Only the authenticated bridge constructs this worker. It consumes a saved
review, records progress before I/O, and never treats Jev DONE as task success.
"""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from PIL import Image

from coding_agents.computer_use_gate import ComputerUseGate, EffectUncertain
from coding_agents.jev_decision import (Candidate, Control, Observation, desktop_subgoals,
                                        reviewable_input_edit)
from coding_agents.owned_browser import OwnedBrowser, BrowserPreDispatchRefused
from core.redact import redact_string

from desktop_runtime.crash_report import RuntimeDiagnostics, report_error
from desktop_runtime.cu_public_errors import (public_failure_code, public_exception_code,
                                             public_exception_type)
from desktop_runtime import local_notices, owned_inspect, source_receipt
from desktop_runtime.cu_reliability import (CHORDS, CLAIM_REASONS, CU_CLAIM_REJECTED,
                                            compact_history, effect_budget_line, goal_chords,
                                            reason_code, repeated_type_on_field, retry_transient,
                                            stalled_few_actions)
from desktop_runtime.owned_coding import OwnedCoding, coding_failure_details
from desktop_runtime.device_cursor_activity import DeviceCursorActivity
from desktop_runtime.cu_watch import WatchFeed
from desktop_runtime.owned_processes import OwnedProcesses
from desktop_runtime.owned_terminal import OwnedTerminal
from desktop_runtime.hosted import _is_local as is_local

CHECK_PARAM = "supymem_check"
CHECK_VALUE = "1"
_PRODUCT_FAILURES = frozenset({"coding_sign_in_required", "coding_provider_not_installed",
    "coding_provider_changed", "coding_tool_not_connected",
    "coding_loop_scope_unsupported", "coding_checks_unavailable", "coding_capability_disabled", "deploy_scope_unsupported",
    "deploy_cli_not_installed", "deploy_sign_in_required", "deploy_source_changed",
    "deploy_project_link_changed", "deploy_project_link_invalid", "deploy_authority_unconfirmed",
    "deploy_result_unconfirmed", "deploy_output_bounds", "deploy_secret_detected"})
CONSEQUENTIAL_CONTROL = re.compile(
    r"(?i)\b(send|submit|publish|post|buy|purchase|pay|delete|remove|install|allow|approve|connect|sign in)\b")


def cu_engine() -> str:
    """ai_loop (AI-primary observe/decide/act/verify) or legacy (gated single-window path)."""
    return "legacy" if os.environ.get("SUPYMEM_CU_ENGINE", "ai_loop").strip().lower() == "legacy" else "ai_loop"


def _coding_offline_profile_admitted(saved: dict, verification: dict) -> bool:
    """Fail closed before a provider/workspace effect on stale or unbound cards."""
    from core.static_qa_contracts import Contract
    snapshot = saved.get("task_snapshot")
    if (verification.get("offline_profile") != "static_html_v1"
            or not isinstance(snapshot, dict)
            or snapshot.get("artifact_profile") != "static_landing_v1"):
        return False
    try:
        hints = json.loads(snapshot.get("tool_hints") or "[]")
        if (not isinstance(hints, list) or any(not isinstance(item, str) for item in hints)
                or not set(hints) <= {"coding.subscription_static", "build.run", "landing.waitlist"}):
            return False
        contract = Contract.model_validate((snapshot.get("landing_expectations") or {}).get("contract"))
    except (AttributeError, TypeError, ValueError):
        return False
    return contract.profile == "static_landing_v1"


def _is_ocr_text(item: dict) -> bool:
    """OCR finds visible words, but does not establish an actionable control."""
    return (str(item.get("id") or "").startswith("ocr_")
            or str(item.get("role") or "").lower() == "ocrtext")


def _observed_expected_visible_text(elements: list[dict], expected: str) -> str | None:
    """Match a reviewed final readout in native AX/OCR, never model prose."""
    wanted = " ".join(expected.split()).casefold()
    for item in elements:
        label = item.get("label")
        if not isinstance(label, str) or " ".join(label.split()).casefold() != wanted:
            continue
        role = str(item.get("role") or "").lower().removeprefix("ax")
        if role in {"statictext", "heading", "title"} and item.get("readout") is True:
            return "ax"
        if (role == "ocrtext" and type(item.get("confidence")) in (int, float)
                and 0.9 <= item["confidence"] <= 1.0):
            return "ocr"
    return None


def _stalled_action_cycle(history, operation, label, readouts):
    """Stop a third A/B cycle only when a visible readout proves no progress.

    Control IDs are capture-local. Identical labels alone are insufficient:
    repeated actions can be a legitimate part of the requested task.
    """
    if not readouts or len(history) < 4:
        return False
    recent = history[-4:]
    signatures = [(item["operation"], item["label"]) for item in recent]
    return (signatures[0] == signatures[2] == (operation, label)
            and signatures[1] == signatures[3]
            and signatures[0] != signatures[1]
            and all(item["readouts"] == readouts for item in recent))


_BROWSER_APPS = {"com.google.Chrome", "com.brave.Browser", "com.apple.Safari",
                 "org.mozilla.firefox", "com.microsoft.edgemac", "com.operasoftware.Opera"}
_HOST = re.compile(r"(?i)(?<![\w@.-])(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])")


def _navigation_unproven(bundle_id, goal_texts, initial_title, current_title, evidence_urls):
    """Why a browser-navigation 'done' has no AX proof, else None.

    The screenshot can show a typed address while the page never loaded, so the
    model's claim is only trusted when AX independently shows a navigation: the
    window title moved off the pre-navigation page, or an observed URL is on the
    reviewed host. A URL fact next to the old page's title is a contradiction.
    """
    if bundle_id not in _BROWSER_APPS:
        return None
    hosts = {m.lower() for text in goal_texts for m in _HOST.findall(str(text or ""))}
    if not hosts:
        return None
    if any((urlsplit(url).hostname or "").lower().removeprefix("www.") in
           {host.removeprefix("www.") for host in hosts} for url in evidence_urls):
        return None
    if current_title and initial_title and current_title != initial_title:
        return None
    return "the window title is still the previous page"


def _bounded_vision_png(path: str) -> str:
    """Encode the already pinned window capture for one approved visual review."""
    with Image.open(Path(path)) as original:
        original.verify()
    with Image.open(Path(path)) as original:
        image = original.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        raw = output.getvalue()
        if len(raw) > 1_500_000:
            output = BytesIO()
            image.quantize(colors=128).save(output, format="PNG", optimize=True)
            raw = output.getvalue()
        if not 0 < len(raw) <= 1_500_000:
            raise ValueError("desktop_vision_image_too_large")
        return base64.b64encode(raw).decode("ascii")


class DeviceWorker:
    def __init__(self, gateway, save, *, cua_socket=None, cua_host_pid=None, cua_protocol=None,
                 activity_current=None):
        self.gateway, self.save = gateway, save
        self.cancelled = threading.Event()
        self.stop_after_iteration = threading.Event()
        self._product_preview_lock = threading.RLock()
        self._product_preview_disabled = False
        self._preview_activity_current = activity_current
        self.stopped_by_user = False
        self.continue_event = threading.Event()
        self.handoff_requested = threading.Event()
        self.deadline = 0.0
        self.server_deadline = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._progress_step = 0
        self.current_effect = None
        self._type_local_admit = False
        self._general_cuai = False
        self._general_read_only = False
        self._general_qualification = False
        self._admitted_type_digests: set[str] = set()
        self.last = {"order_id": gateway.order_id, "state": "starting", "task_verified": False}
        self._runtime_diagnostics = RuntimeDiagnostics()
        self.closed = False
        self.app_navigation_event = threading.Event()
        self.app_navigation_result = None
        self._general_mode = False
        self._step_receipts = []
        self._receipt_instance_id = uuid4().hex
        self._active_browser = None
        self._handoff_visible = False
        self.watch = WatchFeed()  # latest browser frame for the console watch panel; memory only
        self._reveal = None  # set when the Supymem browser is a visible window the founder may raise
        self.cua_socket = cua_socket
        self.cua_host_pid = cua_host_pid
        self.cua_protocol = cua_protocol
        self._cursor_route = None
        def current_activity():
            if self.cancelled.is_set() or self.closed or self._deadline_expired():
                return False
            return activity_current() if callable(activity_current) else False
        self._cursor_activity = DeviceCursorActivity(current_activity)

    def _task_title(self, manifest=None) -> str:
        source = manifest if isinstance(manifest, dict) else {}
        title = source.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        try:
            order = self.gateway.read()
            saved = (order.get("manifest") or {}).get("title")
            if isinstance(saved, str) and saved.strip():
                return saved.strip()
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
            return "Mac task"
        return "Mac task"

    def _order_target(self) -> str | None:
        venture_id = getattr(self.gateway, "venture_id", None)
        order_id = getattr(self.gateway, "order_id", None)
        if not isinstance(venture_id, str) or not venture_id:
            try:
                order = self.gateway.read()
                venture_id = order.get("venture_id")
            except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
                return None
        if not isinstance(venture_id, str) or not venture_id:
            return None
        if not isinstance(order_id, str) or not order_id:
            return None
        return f"/v/{venture_id}/device-work-orders/{order_id}"

    def _notify_terminal(self, outcome: str, *, manifest=None) -> None:
        """Founder-facing finish notice once a claimed Mac task ends."""
        if not self.gateway.claim_attempted:
            return
        if outcome == "plan_ready" and is_local():
            return
        if outcome == "verified" or outcome == "pass" and self.last.get("task_verified") is True:
            mapped = "verified"
        elif outcome == "visual_result_review_required":
            mapped = "visual_review_required"
        elif outcome == "changes_require_acceptance_verification":
            mapped = "review_required"
        elif outcome == "outcome_review_required":
            mapped = "outcome_review_required"
        elif outcome in {"cancelled", "cancelled_or_expired"}:
            mapped = "cancelled"
        else:
            mapped = "failed"
        try:
            local_notices.push_task_finished(
                outcome=mapped,
                task_title=self._task_title(manifest),
                target_path=self._order_target(),
                order_id=getattr(self.gateway, "order_id", None),
                actor_id=getattr(self.gateway, "account_id", None),
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return

    def _notify_needs_you(self, prompt: str, pause_kind: str = "other") -> None:
        # These are task pauses, never email alerts for runtime errors. Keep a
        # failed server fan-out independent of the local desktop notification.
        try:
            self.gateway.report_needs_you(pause_kind, prompt)
        except Exception:
            logging.getLogger("supymem.cu").warning("native_needs_you_delivery_unavailable")
        try:
            local_notices.push_needs_you(
                title="Supymem needs you",
                body=prompt,
                target_path=self._order_target(),
                order_id=getattr(self.gateway, "order_id", None),
                actor_id=getattr(self.gateway, "account_id", None),
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return

    @staticmethod
    def strip_check_marker(url: str) -> str:
        """Drop only ``supymem_check``; keep other params (order) and the hash."""
        from coding_agents.owned_browser import strip_check_marker
        return strip_check_marker(url)

    @staticmethod
    def with_check_marker(url: str) -> str:
        """Ensure ``supymem_check=1`` so hosted beacons skip Supymem's own checks."""
        if not isinstance(url, str) or not url:
            return url
        base = DeviceWorker.strip_check_marker(url)
        before_fragment, separator, fragment = base.partition('#')
        join = '&' if '?' in before_fragment else '?'
        return (before_fragment + join + CHECK_PARAM + '=' + CHECK_VALUE
            + separator + fragment)

    @staticmethod
    def browser_start_url(manifest) -> str:
        """Open the first reviewed page (or origin root) with the analytics marker."""
        general = manifest.get("general_browser_verification") if isinstance(manifest, dict) else None
        if isinstance(general, dict) and isinstance(general.get("start_url"), str):
            return DeviceWorker.with_check_marker(general["start_url"])
        verification = manifest.get("browser_verification") if isinstance(manifest, dict) else None
        expected = (
            verification.get("expected_locations")
            if isinstance(verification, dict)
            else None
        )
        if (
            isinstance(expected, list)
            and expected
            and isinstance(expected[0], str)
            and expected[0].startswith("https://")
        ):
            base = expected[0]
        else:
            base = manifest["origins"][0] + "/"
        return DeviceWorker.with_check_marker(base)

    @staticmethod
    def _reviewed_location_progress(manifest, observed_locations):
        verification = manifest.get("browser_verification")
        if not isinstance(verification, dict) or manifest.get("general_browser_verification") is not None:
            return None
        expected = verification.get("expected_locations")
        if (not isinstance(expected, list) or not expected
                or any(not isinstance(value, str) for value in expected)):
            return None
        position = 0
        for location in observed_locations:
            # New sidecar may land on marked URLs; Core expected_locations stay unmarked
            # so installed exact-compare apps keep working after Core deploys.
            normalized = DeviceWorker.strip_check_marker(location)
            if position < len(expected) and normalized == expected[position]:
                position += 1
            elif not position or normalized != expected[position - 1]:
                return None
        return position

    @staticmethod
    def verification_satisfied(manifest, observed_locations):
        position = DeviceWorker._reviewed_location_progress(manifest, observed_locations)
        return position is not None and position == len(manifest["browser_verification"]["expected_locations"])

    @staticmethod
    def next_reviewed_location(manifest, observed_locations):
        """Use only saved exact URLs and source-qualified ordered observations."""
        position = DeviceWorker._reviewed_location_progress(manifest, observed_locations)
        if position is None:
            return None
        expected = manifest['browser_verification']['expected_locations']
        # No current qualified first page means no trusted progression yet.
        return expected[position] if 0 < position < len(expected) else None

    def progress(self, state, **values):
        if state in {"stopped", "not_started", "reconciliation_required"}:
            from desktop_runtime.cu_reliability import outcome_code

            evidence = values.get("evidence") or {}
            code = outcome_code(values.get("outcome") or evidence.get("outcome"),
                verified=values.get("task_verified", self.last.get("task_verified")) is True,
                reason=self.last.get("reason_code"), failure=self.last.get("failure_code"))
            values["outcome_code"] = code
            self.gateway.outcome_code = code
        if state in {"stopped", "not_started", "reconciliation_required"}:
            self._heartbeat_stop.set()
        if state == "closing":
            self._runtime_cleanup(values.get("outcome"))
        else:
            self._runtime_diagnostics.phase(state)
        self.last = {**self.last, "state": state, **values}
        if self.last.get("product_profile") == "static_landing_v1" and self.last.get("build_deadline_at") is not None:
            self.last["build_remaining_seconds"] = (max(0, int(self.last["build_deadline_at"] - time.time()))
                if state == "running_coding" else 0)
        self.save(self.last)
        if self._cursor_route is not None:
            self._cursor_activity.update(self._cursor_route, state)

    def _deadline_expired(self):
        return (self.server_deadline is not None and time.time() >= self.server_deadline
                or time.monotonic() >= self.deadline)

    def _claim(self, saved, manifest):
        if saved.get("state") == "waiting_for_device" and saved.get("manifest", manifest).get("desktop_verification") is not None and cu_engine() == "ai_loop":
            from desktop_runtime import cu_ai_engine
            checkpoint = cu_ai_engine.FileCheckpoint(
                Path(os.environ.get("SUPYMEM_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Supymem")
                / "cu_checkpoints" / f"{self.gateway.order_id}.json")
            restored = checkpoint.load()
            if restored is not None:
                expected = {key: saved.get(key) for key in ("id", "venture_id", "workspace_id", "actor_id", "device_id",
                    "review_digest", "reservation_id", "execution_id", "claimed_at")}
                expected["manifest_sha256"] = hashlib.sha256(json.dumps(manifest,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if restored.get("pending_dispatch") is not False or restored.get("authority_binding") != expected:
                    raise ValueError("native_execution_requires_reconciliation")
        result = self.gateway.claim()
        claimed = result if isinstance(result, dict) else saved
        deadline = claimed.get("execution_deadline_at")
        if deadline:
            self.server_deadline = datetime.fromisoformat(deadline).timestamp()
        elif claimed.get("claimed_at"):
            self.server_deadline = datetime.fromisoformat(claimed["claimed_at"]).timestamp() + manifest["duration_seconds"]
        else:
            self.server_deadline = time.time() + manifest["duration_seconds"]
        if callable(getattr(self.gateway, "heartbeat", None)):
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop,
                name="supymem-device-lease", daemon=True)
            self._heartbeat_thread.start()
        return result

    def _heartbeat_loop(self):
        from desktop_runtime.device_gateway import UpstreamRejected
        while not self._heartbeat_stop.is_set() and not self.cancelled.is_set() and not self.closed:
            state = self.last.get("state", "starting")
            action = ("coding" if "coding" in state else "deploying" if "deploy" in state
                else "checking" if state.startswith("running_") else "waiting" if state == "needs_you"
                else state if state in {"observing", "deciding", "acting", "closing"} else "starting")
            try:
                self.gateway.heartbeat(step=min(10000, self._progress_step),
                    percent=min(99, int(self._progress_step * 100 / 64)), last_action=action)
            except UpstreamRejected as error:
                if error.status in {401, 403, 404, 409}:
                    self.cancelled.set()
                    return
            except PermissionError:
                self.cancelled.set()
                return
            except Exception:
                logging.getLogger("supymem.cu").warning("device_heartbeat_unavailable")
            if self._heartbeat_stop.wait(25):
                return

    def _admit_ai_action(self, action, observation):
        if self._deadline_expired() or self.cancelled.is_set():
            return "device_execution_expired"
        return self._ai_scope.admit(action, observation)

    def _runtime_cleanup(self, outcome):
        # Freeze the failing phase before cleanup; recoverable pauses carry no reason.
        failure = self.last.get("failure_code")
        reason = (self.last.get("reason_code") or reason_code(outcome, failure,
            self.last.get("driver_failure_code"))) if failure else None
        if self.last.get("tool") == "coding":
            reason = "runtime_failure" if failure else None
            if is_local() and failure == "native_worker_request_unconfirmed":
                self.last["message"] = "Your app could not confirm your coding tool's start. Open Build activity to check or stop your tool, then restart your app and ask Charlie to build again."
        rejection = getattr(self.gateway, "claim_rejection", None)
        if rejection and not getattr(self.gateway, "claim_attempted", False) and self.last.get("tool") != "coding":
            reason = CLAIM_REASONS.get(rejection, CU_CLAIM_REJECTED)
        self._runtime_diagnostics.phase("closing", reason=reason)

    def chrome_factory(self):
        """The persistent per-account Supymem browser for owned-browser tasks; None (legacy headless launch)
        when no signed-in account id exists. SUPYMEM_CU_BROWSER_MODE=headless picks headless; default is
        visible-minimized. The factory never deletes the profile; OwnedBrowser.close() kills Chrome."""
        account_id = getattr(self.gateway, "account_id", None)
        if not account_id:
            return None
        from desktop_runtime.cu_ai_browser import ChromeFactory
        from desktop_runtime.cu_browser.chrome import CHROME_BIN
        if not os.path.exists(CHROME_BIN):  # the Supymem browser is the Mac app's Chrome; elsewhere keep the legacy launch
            return None
        data_dir = Path(os.environ.get("SUPYMEM_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Supymem")
        return ChromeFactory(data_dir / "browser", account_id=account_id, profile_email="")

    def watch_browser(self, browser, label):
        """Feed the console watch panel from an owned-browser loop. Never lets a capture failure touch the task."""
        if self.watch.paused or self.cancelled.is_set():
            return
        try:
            frame = browser.watch_frame()
            if frame:
                self._watch_n = getattr(self, "_watch_n", 0) + 1
                self.watch.publish(frame["jpeg"], step=self._watch_n, label=label, viewport=frame.get("viewport"))
        except Exception:
            pass

    def cancel(self):
        self.cancelled.set()
        self.watch.clear()
        self.retire_activity()
        self.continue_event.set()
        self.handoff_requested.set()
        coding = getattr(self, "_product_coding", None)
        if coding is not None and getattr(coding, "tracker", None) is not None:
            try:
                coding.tracker.stop_all(timeout=.5)
            except Exception:
                self.last["process_cleanup_unconfirmed"] = True

    # Seconds before each attempt to tell Core; the first is immediate.
    # ponytail: in-process retries only; pressing Stop again retries after a runtime restart.
    STOP_SYNC_DELAYS = (0, 2, 5, 15, 30, 60)

    def emergency_stop(self, source: str = "app") -> dict:
        """Founder Stop: halt locally first, then tell Core in the background. Idempotent.

        Never touches the network on the caller's thread, so it works with Core unreachable.
        Any effect whose outcome is unknown stays with the run's own uncertain/stop receipt
        (held by Core, never replayed); this only stops the loop and records the founder's intent.
        """
        first = not self.stopped_by_user
        self.stopped_by_user = True
        if first:
            self.last = {**self.last, "stopped_by_user": True, "stop_source": source,
                         "stopped_at": time.time(), "core_stop_sync": "pending"}
            try:
                self.save(self.last)
            except Exception:  # a journal failure must never delay the stop
                logging.getLogger("supymem.cu").warning("emergency_stop_journal_unavailable")
        self.cancel()
        if first:
            threading.Thread(target=self._sync_core_stop, args=(source,), daemon=True,
                             name="supymem-stop-sync").start()
        return {"order_id": self.gateway.order_id, "stopped_by_user": True,
                "core_stop_sync": self.last.get("core_stop_sync")}

    def _sync_core_stop(self, source: str) -> None:
        for delay in self.STOP_SYNC_DELAYS:
            time.sleep(delay)
            try:
                self.gateway.emergency_stop(source)
            except Exception:  # Core unreachable or refusing: the local stop already holds
                continue
            self.last = {**self.last, "core_stop_sync": "done"}
            try:
                self.save(self.last)
            except Exception:
                pass
            return
        logging.getLogger("supymem.cu").warning("emergency_stop_core_sync_unavailable")

    def retire_activity(self):
        self._cursor_activity.close()

    def continue_founder_handoff(self) -> bool:
        """Founder finished a Needs-you pause; resume the owned browser loop."""
        if (self._general_read_only or self._general_qualification or self.cancelled.is_set()
                or self.closed or self.deadline > 0 and self._deadline_expired()):
            return False
        if self.last.get("state") != "needs_you":
            return False
        self.continue_event.set()
        return True

    def request_founder_handoff(self) -> bool:
        """Queue a headed owned-browser window; the worker thread opens it."""
        if (self._general_read_only or self._general_qualification or self.cancelled.is_set()
                or self.closed or self.deadline > 0 and self._deadline_expired()):
            return False
        if self.last.get("state") == "needs_you" and callable(self._reveal):
            return bool(self._reveal())  # the founder's click is the only thing that raises the window
        if self.last.get("state") != "needs_you" or self._active_browser is None:
            return False
        if self.last.get("handoff") == "visible":
            return True
        self.handoff_requested.set()
        return True

    def authorize(self, order_id, session_id):
        if order_id != self.gateway.order_id or self.cancelled.is_set() or self._deadline_expired():
            return False
        self.gateway.current()
        return True

    def admit(self, effect, digest):
        control = effect["control"]
        operation = effect.get("operation")
        if self._general_mode:
            if self._general_read_only and operation not in {"navigate", "scroll", "extract"}:
                return False
            if operation == "type" and not self._general_cuai:
                if (control.get("typed") is not True or not control.get("label")
                        or "value" in control or "text" in control):
                    self.progress("attention_required", reason="action_requires_review")
                    return False
                # Core effect admit is click-bound. Reviewed non-secret typing uses
                # one-use local digests so each field is admitted exactly once.
                if digest in self._admitted_type_digests:
                    return False
                self._admitted_type_digests.add(digest)
                self.current_effect = (effect["invocation"], digest)
                self._type_local_admit = True
                self.progress("admitting_action", effect_digest=digest,
                              operation=operation, control_label=control.get("label"))
                return True
            if operation == 'type' and self._general_cuai:
                if control.get('typed') is not True or 'value' in control or 'text' in control:
                    return False
            elif operation in {"hover", "new_tab", "switch_tab"} and self._general_qualification and self._general_cuai:
                grant = self.gateway.qualification_current()
                if operation not in grant.allowed_operations or control.get("form") or control.get("submit"):
                    return False
                if operation == "new_tab" and (effect.get("tab_url") is not None):
                    if (not re.fullmatch(r"document_[a-f0-9]{64}", str(effect.get("document_target") or ""))
                            or not any(rule.method == "GET" and effect["tab_url"] == grant.origin + rule.path
                                + ("?" + rule.query if rule.query else "") for rule in grant.rules)):
                        return False
                elif operation == "new_tab" and (control.get("tag") != "A" or not any(rule.method == "GET"
                        and control.get("href") == grant.origin + rule.path + ("?" + rule.query if rule.query else "")
                        for rule in grant.rules)):
                    return False
                if operation == "switch_tab" and (not re.fullmatch(r"t[a-f0-9]{32}", str(effect.get("tab_target") or ""))
                        or not any(rule.method == "GET" and effect.get("tab_url") == grant.origin + rule.path
                            + ("?" + rule.query if rule.query else "") for rule in grant.rules)):
                    return False
            elif operation == "navigate" and self._general_qualification and self._general_cuai and effect.get("recovery_url"):
                grant = self.gateway.qualification_current()
                if ("navigate" not in grant.allowed_operations or control.get("tag") != "HTML" or control.get("form")
                        or not re.fullmatch(r"document_[a-f0-9]{64}", str(effect.get("document_target") or ""))
                        or not any(rule.method == "GET" and effect["recovery_url"] == grant.origin + rule.path
                            + ("?" + rule.query if rule.query else "") for rule in grant.rules)):
                    return False
            elif operation in {"click", "navigate"}:
                qualified = False
                if self._general_qualification and self._general_cuai and operation == "click":
                    grant = self.gateway.qualification_current()
                    ids = effect.get("qualification_rule_ids", [])
                    if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
                        return False
                    qualified = any(rule.document_path == control.get("source_document_path")
                        and rule.document_id == control.get("source_document_id")
                        and rule.form_id == control.get("source_container_id")
                        and (rule.method == "POST" and rule.rule_id in ids and rule.control_id == control.get("source_control_id")
                            or any(field.control_id == control.get("source_control_id") and field.kind in {"checkbox", "radio"}
                                for field in rule.fields)) for rule in grant.rules)
                if (control.get("tag") not in {"A", "SUMMARY", "BUTTON"} or control.get("submit")) and not qualified:
                    self.progress("attention_required", reason="action_requires_review")
                    return False
                if operation == "navigate" and (not self._general_cuai or control.get("tag") != "A" or control.get("form")):
                    return False
            elif operation in {"extract", "scroll"} and self._general_cuai:
                if not re.fullmatch(r"document_[a-f0-9]{64}", str(effect.get("document_target") or "")):
                    return False
            else:
                self.progress("attention_required", reason="action_requires_review")
                return False
        elif control.get("form") or control.get("tag") not in {"A", "SUMMARY"}:
            # Ordinary waitlist navigation: links/disclosures only.
            self.progress("attention_required", reason="action_requires_review")
            return False
        self.current_effect = (effect["invocation"], digest)
        self._type_local_admit = False
        self.progress("admitting_action", effect_digest=digest,
                      operation=operation, control_label=control.get("label"))
        admitted=self.gateway.admit_effect(effect["invocation"], effect["observation"], digest)
        if self._general_cuai and admitted is not True:
            self.current_effect=None
        return admitted

    def _clear_effect_receipt(self):
        if not self.current_effect:
            return
        if self._type_local_admit:
            self.current_effect = None
            self._type_local_admit = False
            return
        self.gateway.effect_receipt(*self.current_effect, "returned")
        self.current_effect = None

    def _browser_input_refused(self, error, adapter, operation):
        # Only the source exception raised before input can close this hold.
        # Older/typed Core routes do not accept this receipt; retain their hold.
        if adapter:
            if self.current_effect is not None:
                receipt = self.gateway.effect_receipt(*self.current_effect, "not_dispatched",
                    pre_dispatch_code=error.pre_dispatch_code)
                if not isinstance(receipt, dict) or receipt.get('recorded') is not True:
                    raise PermissionError('browser_pre_dispatch_receipt_unconfirmed')
                self.current_effect = None
            adapter.not_dispatched()
        self._record_step(kind="effect_refused", operation=operation,
            pre_dispatch_code=error.pre_dispatch_code, input_started=False)

    def _receipt_context(self):
        # Source-owned identities only; proposals cannot provide correlation.
        result = {"worker_instance_id": self._receipt_instance_id, "worker_pid": os.getpid(),
                  "observed_at_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()}
        for key in ("order_id", "execution_id", "device_id", "venture_id"):
            value = getattr(self.gateway, key, None)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                result[key] = value
        return result

    def _ai_log_receipt(self, row, *, invocation_id=None):
        brief = {}
        for key in ("app", "action", "outcome", "phase", "error", "kind"):
            value = row.get(key)
            if isinstance(value, str):
                brief[key] = value[:160]
        if type(row.get("step")) is int and row["step"] >= 0:
            brief["step"] = row["step"]
        if isinstance(row.get("ms"), dict):
            brief["ms"] = {key: value for key, value in row["ms"].items()
                           if key in {"observe", "model", "act"} and type(value) is int and value >= 0}
        if isinstance(invocation_id, str) and re.fullmatch(r"ai_[a-f0-9]{32}", invocation_id):
            brief["invocation_id"] = invocation_id
        brief.update(self._receipt_context())
        return brief

    def _record_step(self, *, _owned_context=False, **values):
        forbidden = {"value", "text", "password", "secret", "token", "api_key", "fill"}
        step = {key: value for key, value in values.items()
                if value is not None and key not in forbidden}
        if _owned_context:
            step.update(self._receipt_context())
        self._step_receipts.append(step)
        self._progress_step += 1
        if len(self._step_receipts) > 64:
            self._step_receipts = self._step_receipts[-64:]
        self.last["steps"] = list(self._step_receipts)

    def run(self):
        try:
            return self._run_with_reporting()
        finally:
            self._heartbeat_stop.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=1)
            self._cursor_activity.close()

    def _run_with_reporting(self):
        from desktop_runtime.cu_public_errors import public_exception_details, safe_exception_details
        from core.local_diagnostics import write_error
        try:
            result = self._run()
        except Exception as error:
            write_error(error, code=public_exception_code(error, "native_execution_failed"))
            details = public_exception_details(error)
            self.last.update(details)
            report_error("device_worker", "run_mac_task", label="Mac uncaught_exception",
                runtime_diagnostic=self._runtime_diagnostics.snapshot("uncaught_exception"), exception_details=details)
            raise
        code = self.last.get("failure_code")
        if code and not self.cancelled.is_set():
            write_error(None, code=public_failure_code(code, "native_execution_failed"))
            if self.last.get("reason_code") == "cu_browser_decision_unavailable":
                code = "cu_browser_decision_unavailable"
            report_error("device_worker", "run_mac_task", code=code, label="Mac native_failure",
                runtime_diagnostic=self._runtime_diagnostics.snapshot(code), exception_details=safe_exception_details(self.last))
        rejection = getattr(self.gateway, "claim_rejection", None)
        if rejection and not self.gateway.claim_attempted:
            # Core refused the claim outright: nothing started, so never report an uncertain action.
            self.progress("not_started", outcome="claim_rejected", task_verified=False,
                          failure_code="native_claim_rejected", reason_code=("runtime_failure" if self.last.get("tool") == "coding"
                              else CLAIM_REASONS.get(rejection, CU_CLAIM_REJECTED)))
        return result

    def _run(self):
        try:
            initial = self.gateway.read()
        except Exception:
            self.progress("not_started", failure_code="native_order_verification_unavailable",
                          task_verified=False)
            return
        initial_manifest = initial["manifest"]
        if initial_manifest.get("coding_loop_verification") is not None:
            return self.run_coding_loop(initial, initial_manifest)
        if initial_manifest.get("coding_verification") is not None:
            return self.run_coding(initial, initial_manifest)
        if initial_manifest.get("app_navigation_verification") is not None:
            return self.run_app_navigation(initial, initial_manifest)
        if initial_manifest.get("inspection_verification") is not None:
            return self.run_inspect(initial, initial_manifest)
        if initial_manifest.get("deploy_verification") is not None:
            return self.run_deploy(initial, initial_manifest)
        if initial_manifest.get("terminal_verification") is not None:
            return self.run_terminal(initial, initial_manifest)
        if initial_manifest.get("general_browser_verification") is not None:
            return self.run_general_browser(initial, initial_manifest)
        if initial_manifest.get("desktop_verification") is not None:
            if cu_engine() == "ai_loop":
                return self.run_ai_loop(initial, initial_manifest)
            return self.run_desktop(initial, initial_manifest)
        browser = None
        tracker = None
        claimed = False
        outcome = "execution_unconfirmed"
        try:
            saved = self.gateway.read()
            manifest = saved["manifest"]
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.google.Chrome"]
                    or not manifest["origins"] or "browser.read" not in manifest["actions"]
                    or not set(manifest["actions"]) <= {"browser.read", "browser.interact"}
                    or manifest.get("general_browser_verification") is not None):
                raise ValueError("browser_scope_unsupported")
            # Dependencies are resolved before spending/claiming authority.
            from playwright.sync_api import sync_playwright  # noqa: F401
            tracker = OwnedProcesses()
            self.progress("claiming")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self._cursor_route = "browser"
            start_url = self.browser_start_url(manifest)
            browser = OwnedBrowser(order_id=self.gateway.order_id, goal=manifest["instruction"],
                origins=manifest["origins"], authorize=self.authorize, admit_effect=self.admit,
                process_observer=tracker.capture, chrome_factory=self.chrome_factory())
            self._reveal = browser.reveal if getattr(browser, "visible", False) else None
            self.progress("opening_browser")
            browser.start(start_url)
            tracker.capture()
            observed_locations = []
            verified_documents = []
            recent_actions = []
            for _ in range(32):
                if not self.authorize(self.gateway.order_id, browser.session_id):
                    outcome = "cancelled_or_expired"
                    break
                self.progress("observing")
                observation = browser.observe()
                self.watch_browser(browser, "observe")
                if browser.verified_load(observation):
                    observed_locations.append(observation.location)
                    verified_documents.append(dict(browser.last_load_evidence))
                # Exact review-bound evidence is stronger than another model
                # proposal. Complete immediately once every reviewed location
                # has been visibly observed in order; do not pay for or permit
                # a terminal model answer to veto deterministic success.
                if self.verification_satisfied(manifest, observed_locations):
                    self.last["task_verified"] = True
                    outcome = "verified"
                    break
                if "browser.interact" not in manifest["actions"]:
                    observation.controls = []
                if os.environ.get("JEV_FIXED_ADAPTER", "1") != "0":
                    observation.recent_actions = recent_actions[-10:]
                tracker.capture()
                invocation = "din_" + uuid4().hex
                self.progress("deciding", observation_digest=observation.digest(), invocation_id=invocation)
                candidate = None
                next_location = self.next_reviewed_location(manifest, observed_locations)
                if (getattr(self.gateway, 'typed_navigation_proposal_v1', False) is True
                        and next_location is not None):
                    offer = browser.reviewed_navigation_offer(observation, next_location)
                    if offer is not None:
                        candidate = self.gateway.navigation_proposal(invocation, observation,
                            offer, verified_documents, expected_location=next_location)
                if candidate is None:
                    candidate = self.gateway.decide(invocation, observation)
                if candidate.operation in {"done", "blocked"}:
                    outcome = ("outcome_review_required" if candidate.operation == "done"
                        else "attention_required")
                    break
                if candidate.operation == "wait":
                    self.cancelled.wait(1)
                    continue
                if candidate.operation != "click":
                    outcome = "action_requires_review"
                    break
                self.progress("acting")
                try:
                    browser.click(candidate, invocation)
                except EffectUncertain as error:
                    self.last["driver_failure_code"] = public_failure_code(error.driver_code, "desktop_effect_unconfirmed")
                    if self.current_effect:
                        self.gateway.effect_receipt(*self.current_effect, "uncertain")
                    raise
                label = next((c.label for c in observation.controls if c.id == candidate.target), "")
                recent_actions.append(("click " + " ".join(label.split()))[:120])
                if self.current_effect:
                    self.gateway.effect_receipt(*self.current_effect, "returned")
                    self.current_effect = None
            else:
                outcome = "step_limit_reached"
        except Exception as error:
            # Keep raw page/provider/OS exceptions out of founder-facing logs.
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            failure_code = public_exception_code(error, "native_execution_failed")
            self.last["failure_code"] = failure_code
        finally:
            try:
                self.progress("closing", outcome=outcome)
            except Exception:
                pass  # A journal failure must never skip browser cleanup.
            self._reveal = None
            self.watch.clear()
            self.watch.resume()
            close_returned = browser is None
            if tracker:
                tracker.capture()
            if browser:
                try:
                    browser.close()
                    close_returned = True
                except Exception:
                    close_returned = False
            if tracker:
                tracker.stop_all()
            stopped = bool(tracker and close_returned and tracker.stopped())
            self.closed = stopped or not self.gateway.claim_attempted
            evidence = {"order_id": self.gateway.order_id, "execution_id": self.gateway.execution_id,
                        "close_returned": close_returned, "processes_stopped": stopped,
                        "inventory": tracker.receipt() if tracker else None, "outcome": outcome,
                        "task_verified": self.last.get("task_verified") is True,
                        "observed_locations": observed_locations if 'observed_locations' in locals() else [],
                        "verified_documents": verified_documents if 'verified_documents' in locals() else []}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            terminal = "not_started" if not self.gateway.claim_attempted else "stopped" if stopped else "reconciliation_required"
            self.progress(terminal, evidence=evidence, evidence_digest=digest)
            if claimed:
                try:
                    result = self.gateway.stop_receipt(
                        digest, stopped, task_verified=self.last.get("task_verified") is True)
                    self.progress(self.last["state"], credits_reconciled=result.get("state") == "stopped")
                except Exception:
                    self.progress(self.last["state"], credits_reconciled=False)
                self._notify_terminal(outcome)

    def _ai_backend(self):
        """Native v1 desktop scope only; other work-order kinds retain their executors."""
        from desktop_runtime.cu_ai_cua import CuaMac, session_caller
        from desktop_runtime.cua_session import AI_LOOP_TOOLS, CuaSession
        scope = getattr(self, "_ai_scope", None)
        if scope is None:
            raise ValueError("desktop_scope_unsupported")
        live = scope.live()
        if os.environ.get("SUPYMEM_AGENT_DESKTOP_ENABLED") == "1":
            from desktop_runtime.agent_desktop import AgentDesktopCli, AgentDesktopMac
            if scope.extra or scope.modal_parents:
                raise ValueError("desktop_scope_unsupported")
            native = AgentDesktopMac(scope, self._ai_goal,
                AgentDesktopCli(Path(sys.executable).with_name("agent-desktop")),
                authorized=lambda: self.authorize(self.gateway.order_id, None))
            native.set_target(live.pid, live.name)
            return native
        if self.cua_socket is None:
            raise ValueError("desktop_cua_session_unavailable")
        binary = str(Path(sys.executable).with_name("cua-driver"))
        from desktop_runtime.cu_modal_authority import ReviewedModalAuthority
        authority = ReviewedModalAuthority(scope,stop=self.cancelled) if scope.modal_parents else None
        session = CuaSession(binary, self.cua_socket, tools=AI_LOOP_TOOLS,
                             **({"reviewed_modal_authority":authority} if authority is not None else {}),
                             **({"reviewed_surfaces": scope.pairs} if scope.extra
                                else {"reviewed_surface": (scope.pid, scope.window)}))
        native = CuaMac(scope.caller(session_caller(session), stop=self.cancelled, modal_authority=authority))
        native.set_target(live.pid, live.name)
        return native

    def run_ai_loop(self, saved, manifest):
        """SUPYMEM_CU_ENGINE=ai_loop: the model drives observe -> decide -> act -> verify.

        The saved manifest pins the reviewed process/window; goal prose cannot expand it. Hard blocks (secrets, risky controls, security
        panes, off-limits apps), the step/time/credit caps and the instant Stop live in cu_ai_engine/cu_ai_loop.
        """
        from coding_agents.cu_ai_loop import Caps
        from desktop_runtime import cu_ai_engine as eng
        from desktop_runtime import cu_preflight

        spec = manifest.get("desktop_verification") or {}
        goal = spec.get("founder_goal") or manifest["instruction"]
        mac = None
        claimed = False
        result = None
        outcome = "execution_unconfirmed"
        self.last["task_verified"] = False
        self.last.pop("verification_failure_code", None)
        log = logging.getLogger("supymem.cu")
        recovery_enabled = saved.get("cu_recovery_v1") is True
        try:
            if (saved.get("state") not in {"approved", "waiting_for_device"} or manifest.get("actions") != ["desktop.read", "desktop.interact"]
                    or manifest.get("origins") != []):
                raise ValueError("desktop_scope_unsupported")
            from desktop_runtime.cu_reviewed_scope import ReviewedDesktopScope
            self._ai_scope = ReviewedDesktopScope.from_manifest(manifest)
            self._ai_goal = goal
            local_steps = os.environ.get("SUPYMEM_CU_MAX_STEPS", "0")
            if not local_steps.isascii() or not local_steps.isdecimal():
                raise ValueError("desktop_scope_unsupported")
            steps_cap = min(int(local_steps) or self._ai_scope.max_steps, self._ai_scope.max_steps)
            local_seconds = os.environ.get("SUPYMEM_CU_MAX_SECONDS", "0")
            if not local_seconds.isascii() or not local_seconds.isdecimal():
                raise ValueError("desktop_scope_unsupported")
            seconds_cap = min(int(local_seconds) or manifest["duration_seconds"], manifest["duration_seconds"])
            self._ai_scope.live()  # Validate the reviewed process/window before claim.
            mac = self._ai_backend()
            if recovery_enabled:
                problems = cu_preflight.check(goal, mac, surfaces=self._ai_scope.pairs, strict=True)
                if problems:
                    prompt = " ".join(problems)
                    self.last["failure_code"] = "cu_preflight_blocked"
                    self.progress("needs_you", tool="ai_loop", founder_prompt=prompt,
                                  message=prompt, needs_you={"reason": "other", "what": prompt,
                                                           "resume": "After you fix this, retry the reviewed task."})
                    self._notify_needs_you(prompt, "other")
                    return
            self._reveal = getattr(mac, "reveal_browser", None)
            self.progress("claiming", tool="ai_loop")
            claim = self._claim(saved, manifest)
            claimed = True
            from datetime import datetime, timezone
            binding_keys = ("id", "venture_id", "workspace_id", "actor_id", "device_id", "review_digest",
                            "reservation_id", "execution_id", "claimed_at")
            if (not isinstance(claim, dict) or any(not isinstance(claim.get(k), str) or not claim[k] for k in binding_keys)
                    or claim["id"] != self.gateway.order_id or claim["venture_id"] != self.gateway.venture_id
                    or claim["execution_id"] != self.gateway.execution_id or claim.get("manifest") != manifest):
                raise ValueError("native_claim_unconfirmed")
            claimed_at = datetime.fromisoformat(claim["claimed_at"])
            if claimed_at.tzinfo is None:
                raise ValueError("native_claim_unconfirmed")
            remaining = manifest["duration_seconds"] - (datetime.now(timezone.utc) - claimed_at).total_seconds()
            seconds_cap = min(seconds_cap, remaining)
            self.deadline = time.monotonic() + seconds_cap
            binding = {k: claim[k] for k in binding_keys}
            binding["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            calls = {"n": 0}
            active_step_invocation = {"id": None}

            def ask(kind, context, png=None):
                if self._deadline_expired() or self.cancelled.is_set():
                    raise ValueError("device_execution_expired")
                calls["n"] += 1
                started = time.monotonic()
                invocation_id = "ai_" + uuid4().hex
                if kind == "step":
                    active_step_invocation["id"] = invocation_id
                value = self.gateway.ai_step(invocation_id, kind, context, png,
                                             cancelled=self.cancelled, deadline=self.deadline)
                self._record_step(_owned_context=True, kind=f"ai_{kind}", invocation_id=invocation_id,
                                  latency_ms=int((time.monotonic() - started) * 1000),
                                  model=value.get("model"), input_tokens=value.get("input_tokens"),
                                  output_tokens=value.get("output_tokens"))
                return value

            def decide(payload):
                obs = payload["observation"]
                context = {"subgoals": payload["subgoals"], "subgoal": payload["subgoal"],
                           "memory": payload["memory"], "history": payload["history"], "hint": payload["hint"],
                           "notes": obs.notes[:2500],
                           "frontmost": obs.frontmost, "window_title": obs.window_title,
                           "elements": [{k: e.get(k) for k in ("id", "ax_role", "label", "value", "bounds", "type", "name", "checked", "selected", "focused")}
                                        for e in obs.elements[:140]]}
                if recovery_enabled:
                    condition = recovery.postconditions[payload["subgoal"]] if payload["subgoal"] < len(recovery.postconditions) else {}
                    context["hint"] = (recovery.hint + " Postcondition: " + json.dumps(condition, separators=(",", ":"))
                                       + ". Budget: " + json.dumps(payload["subgoal_budget"])
                                       + ". " + payload["hint"])[:500]
                use_screenshot = not recovery_enabled or not recovery.accessibility_available(obs) or recovery.alternate_used
                value = ask("step", context, eng.b64(obs.png) if obs.png and use_screenshot else None)
                return value["reply"], {"in": value.get("input_tokens"), "out": value.get("output_tokens")}

            def planner(_goal, memory, reason, obs):
                screen = f"{obs.frontmost} | {obs.window_title}" if obs is not None else ""
                if recovery_enabled:
                    reason = ('Each subgoal string must be JSON: {"goal":"...","postcondition":{"role":"AXStaticText","label":"..."}}. '
                              'Use exact observed role+label/value or window_title. No invented outcomes.')
                    screen += "\nAX: " + json.dumps([{key: element.get(key) for key in ("ax_role", "label", "value")}
                                                    for element in obs.elements[:20]], separators=(",", ":"))
                text = ask("plan", {"reason": reason, "memory": memory, "screen": screen[:1500]})["text"]
                subgoals = json.JSONDecoder().raw_decode(text.strip())[0]["subgoals"]
                if recovery_enabled:
                    return [json.loads(item) if isinstance(item, str) else item for item in subgoals]
                return [str(item)[:300] for item in subgoals][:8]

            def verify(claim, obs, memory):
                from coding_agents.cu_ai_loop import parse_verdict
                self._ai_scope.live()
                predicate_check = getattr(mac, "reviewed_goal_evidence", None)
                if not callable(predicate_check):
                    matched, code = False, "reviewed_goal_evidence_unavailable"
                else:
                    matched, code = predicate_check(obs, spec, self._ai_scope.pairs)
                if matched is not True:
                    self.last["verification_failure_code"] = code if code in {
                        "reviewed_goal_evidence_unavailable", "reviewed_goal_predicate_unmet",
                        "reviewed_goal_verification_required"} else "reviewed_goal_evidence_unavailable"
                    return False, self.last["verification_failure_code"]
                if self.cancelled.is_set() or self._deadline_expired():
                    return False, "reviewed_goal_evidence_unavailable"
                value = ask("verify", {"claim": claim, "memory": memory, "frontmost": obs.frontmost,
                                       "window_title": obs.window_title, "notes": obs.notes[:2500],
                                       "elements": [{k: e.get(k) for k in ("id", "ax_role", "label", "value", "type", "name", "checked", "selected", "focused")}
                                                    for e in obs.elements[:200]]})
                verdict = parse_verdict(value["text"])
                if verdict[0] is not True:
                    return verdict
                self._ai_scope.live()
                final_observation = mac.observe()  # Model latency never extends evidence freshness.
                matched, code = predicate_check(final_observation, spec, self._ai_scope.pairs)
                if (matched is not True or self.cancelled.is_set() or self._deadline_expired()):
                    self.last["verification_failure_code"] = "reviewed_goal_predicate_unmet"
                    return False, self.last["verification_failure_code"]
                self.last.pop("verification_failure_code", None)
                self._record_step(kind="independent_reviewed_goal_observation", reviewed_predicates_observed=True)
                return verdict

            def pause(kind, prompt, obs):
                self.watch.pause()  # no frames while the founder may be typing
                self.continue_event.clear()
                self.progress("needs_you", tool="ai_loop", pause_kind=kind, founder_prompt=prompt,
                              handoff="message_only", message=f"Needs you: {prompt}",
                              needs_you={"reason": kind, "what": prompt[:300],
                                         "resume": "Click Continue after handling the blocker. Supymem checks fresh evidence; "
                                                   "unconfirmed inputs remain held."})
                self._record_step(_owned_context=True, kind="pause", pause_kind=kind, founder_prompt=prompt)
                from desktop_runtime import cu_preflight
                verdict = eng.wait_for_human(kind, prompt, mac, self.cancelled, self.continue_event,
                                             notify=lambda text: self._notify_needs_you(text, kind),
                                             ready=(lambda: not cu_preflight.check(goal, mac)) if obs is None else None)
                self.watch.resume()
                self.progress("acting", tool="ai_loop", resumed_after="needs_you")
                return verdict

            watch_step = {"n": 0}

            def capture_watch(row):
                grab = getattr(mac, "watch_frame", None)
                if not callable(grab) or self.watch.paused or not row.get("action") or self.cancelled.is_set():
                    return
                watch_step["n"] += 1
                try:
                    frame = grab()
                except Exception:
                    return
                if frame:
                    self.watch.publish(frame["jpeg"], step=watch_step["n"], label=str(row.get("action"))[:40],
                                       cursor=frame.get("cursor"), viewport=frame.get("viewport"))

            def on_row(row):
                capture_watch(row)
                brief = self._ai_log_receipt(row, invocation_id=active_step_invocation["id"] if row.get("ms") else None)
                log.info("cu_step %s", json.dumps(brief, default=str))
                if row.get("action") or row.get("phase"):
                    self._record_step(kind="ai_action", **{k: v for k, v in brief.items() if k != "kind"})

            def status(text):
                self.progress("acting", tool="ai_loop", message=text, step_text=text)

            seconds_cap = max(0, self.deadline - time.monotonic())
            caps = Caps(max_steps=steps_cap, max_seconds=seconds_cap, explicit=True)
            from desktop_runtime.cu_recovery import Recovery
            recovery = Recovery() if recovery_enabled else None
            checkpoint = eng.FileCheckpoint(
                Path(os.environ.get("SUPYMEM_DATA_DIR") or Path.home() / "Library" / "Application Support" / "Supymem")
                / "cu_checkpoints" / f"{self.gateway.order_id}.json", binding=binding)
            self.progress("acting", tool="ai_loop", message="Starting")
            result = eng.run_loop(goal, decide, mac, self.cancelled, planner=planner, caps=caps, log=on_row,
                                  status=status, checkpoint=checkpoint, verify=verify, pause=pause,
                                  admit_action=self._admit_ai_action,
                                  preflight=None if recovery_enabled else lambda text: cu_preflight.check(text, mac),
                                  recovery=recovery)
            if result.outcome == "done":
                self.last["task_verified"] = True
                outcome = "verified"
            elif result.outcome == "stopped":
                outcome = "cancelled"
            else:
                outcome = "execution_unconfirmed"
                self.last["failure_code"] = "ai_loop_" + public_failure_code(result.reason or "stopped", "failed")
                if self.last.get("verification_failure_code"):
                    self._runtime_diagnostics.phase("needs_you", reason=reason_code(outcome,
                        self.last["failure_code"], self.last.get("driver_failure_code")))
                    self.progress("needs_you", tool="ai_loop", pause_kind="other",
                        founder_prompt="The actions finished without supported reviewed goal evidence. Review the result and prepare a task with a supported final predicate; uncertain inputs will not be replayed.",
                        verification_failure_code=self.last["verification_failure_code"])
        except Exception as error:
            code = public_exception_code(error, "")
            if recovery_enabled and not claimed and not self.cancelled.is_set():
                prompt = cu_preflight.recovery_prompt(code)
                self.progress("needs_you", tool="ai_loop", founder_prompt=prompt, message=prompt,
                              needs_you={"reason": "other", "what": prompt,
                                         "resume": "After you fix this, retry the reviewed task."})
                self._notify_needs_you(prompt, "other")
            if self.cancelled.is_set():
                outcome = "cancelled"
            else:
                outcome = "execution_requires_reconciliation" if claimed else "execution_unconfirmed"
            self.last["failure_code"] = public_failure_code(code, "ai_loop_failed")
            log.error("cu_ai_loop_error code=%s type=%s", self.last["failure_code"], public_exception_type(error))
        finally:
            self._runtime_cleanup(outcome)
            cleanup_ok = True
            if mac is not None and hasattr(mac, "close"):
                try:
                    mac.close()
                except Exception:
                    cleanup_ok = False
                    self.last["cleanup_failure_code"] = "ai_loop_backend_close_unconfirmed"
                    if outcome != "cancelled":
                        outcome = "execution_requires_reconciliation"
            self.closed = cleanup_ok
            reason = reason_code(outcome, self.last.get("failure_code"), None)
            if reason:
                self.last["reason_code"] = reason
            evidence = {"order_id": self.gateway.order_id, "execution_id": self.gateway.execution_id,
                        "outcome": outcome, "task_verified": self.last.get("task_verified") is True,
                        "driver": "ai_loop_cua" if self.cua_socket else "ai_loop_peekaboo",
                        "steps_taken": getattr(result, "steps", 0), "seconds": getattr(result, "seconds", 0),
                        "ai_reason": getattr(result, "reason", ""), "processes_stopped": cleanup_ok,
                        "cleanup_failure_code": self.last.get("cleanup_failure_code"), "steps": list(self._step_receipts)}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            terminal = ("stopped" if cleanup_ok else "reconciliation_required") if claimed else "not_started"
            self.progress(terminal, tool="ai_loop", outcome=outcome, evidence=evidence, evidence_digest=digest,
                          task_verified=self.last.get("task_verified") is True)
            if claimed:
                try:
                    extra = {"reason_code": self.last["reason_code"]} if self.last.get("reason_code") else {}
                    receipt = self.gateway.stop_receipt(
                        digest, cleanup_ok, task_verified=self.last.get("task_verified") is True, **extra)
                    self.progress(terminal, tool="ai_loop", credits_reconciled=receipt.get("state") == "stopped")
                except Exception:
                    self.progress("reconciliation_required", tool="ai_loop", credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_desktop(self, saved, manifest):
        """One reviewed app/window, one admitted effect per observation.

        This is an app-wide loop. It deliberately reports observed evidence as
        unverified until a separate task-specific verifier can prove the goal.
        """
        if saved.get("cu_recovery_v1") is True:
            return self.run_ai_loop(saved, manifest)
        from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver

        spec = manifest.get("desktop_verification") or {}
        driver = None
        claimed = False
        outcome = "execution_unconfirmed"
        evidence_urls: list[str] = []
        effect_history: list[dict] = []
        last_effect_operation = None
        last_effect_status = None
        last_effect_label = None
        last_type_index = None
        last_type_target = None
        last_submitted_type = None
        foreground_handoff_done = False
        vision_report = None
        vision_reviews = 0
        # A long task has many unverifiable effects, each worth one image check.
        max_vision_reviews = min(max(2, int(spec.get("max_steps") or 2) // 2), 24)
        action_log: list[str] = []
        retried_labels: set[str] = set()
        skip_once: set[tuple[str, str, str]] = set()
        repeat_skips = 0
        goal_text = spec.get("founder_goal") or manifest["instruction"]
        from coding_agents.document_text import textedit_typing_only_goal
        typing_only_text = textedit_typing_only_goal(goal_text)
        reviewed_values = list(spec.get("reviewed_text") or [])
        if typing_only_text not in reviewed_values:
            typing_only_text = None
        document_text = (typing_only_text if typing_only_text is not None else
                         reviewed_values[0] if spec.get("document_text_index") == 0 and reviewed_values else None)
        subgoal_line = ""
        subgoals = desktop_subgoals(goal_text)
        if len(subgoals) > 1:
            subgoal_line = " Subgoals in order: " + " | ".join(
                f"{i}) {item}" for i, item in enumerate(subgoals, 1))
            subgoal_line = subgoal_line[:700]
        reviewed_effect_count = 0
        vision_hint = ""
        refused_controls: set[tuple[str, str, str]] = set()
        safe_refusals = 0
        previous_blocked_observation = None
        initial_goal_visible = None
        initial_window_title = None

        def review_image(invocation_id, snapshot_id, png, observation):
            reviewed = self.gateway.review_desktop_image(invocation_id, snapshot_id, png, observation)
            report = reviewed["report"]
            if report.get("verdict") == "done":
                why = _navigation_unproven(
                    live.bundle_id, [spec.get("founder_goal"), manifest["instruction"],
                                     *(spec.get("reviewed_text") or [])],
                    initial_window_title, captured.get("window_title"), evidence_urls)
                if why:
                    reviewed = {**reviewed, "report": {
                        **report, "verdict": "blocked",
                        "visible_facts": [*report["visible_facts"][:7],
                                          "Navigation not confirmed: " + why],
                        "next_step": "The page did not open; " + why + "."}}
            return reviewed
        try:
            if (saved.get("state") not in {"approved", "waiting_for_device"} or spec.get("operation") != "desktop_goal_v1"
                    or spec.get("delivery_mode") not in {"background", "foreground_fallback"}
                    or manifest.get("applications") != [spec.get("application_bundle")]
                    or manifest.get("actions") != ["desktop.read", "desktop.interact"]
                    or manifest.get("origins") != []):
                raise ValueError("desktop_scope_unsupported")
            expected = AppChoice(spec["application_bundle"], spec["application_bundle"],
                                 spec["process_id"], spec["process_start_identity"])
            live = next((app for app in PeekabooDriver.running_apps()
                         if app.bundle_id == expected.bundle_id), None)
            if (live is None or live.pid != expected.pid
                    or live.process_start_identity != expected.process_start_identity):
                raise ValueError("desktop_app_identity_changed")
            use_cua = self.cua_socket is not None
            if spec.get("document_text_index") is not None:
                from coding_agents.document_text import textedit_three_lines
                if (not use_cua or live.bundle_id != "com.apple.TextEdit"
                        or type(spec["document_text_index"]) is not int
                        or spec["document_text_index"] != 0 or not spec.get("reviewed_text")):
                    raise ValueError("desktop_document_profile_unsupported")
                textedit_three_lines(spec["reviewed_text"][0])
            windows = PeekabooDriver.running_windows(live)
            reviewed = next((row for row in windows if row["window_id"] == spec["window_id"]), None)
            if reviewed is None:
                raise ValueError("desktop_window_identity_changed")
            # Same window on another Space or minimized: bring that exact window
            # forward under the reviewed foreground control, never another one.
            bring_forward = (not reviewed["on_screen"] and not use_cua
                             and spec["delivery_mode"] == "foreground_fallback")
            if not reviewed["on_screen"] and not use_cua and not bring_forward:
                raise ValueError("desktop_window_identity_changed")
            # The Core capability/claim checks must precede driver creation or
            # a legacy foreground handoff. A historical approved card does not
            # authorize native effects when current execution is unavailable.
            self.progress("claiming", tool="desktop")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            if use_cua:
                from desktop_runtime.cua_driver import CuaWindowDriver
                # Startup only: no tool call or effect has happened, so a cold
                # daemon's transient failure is retried a bounded number of times.
                driver = retry_transient(lambda: CuaWindowDriver(
                    app=live, window_id=spec["window_id"], socket=self.cua_socket,
                    host_pid=self.cua_host_pid,
                    **({"observation_protocol": self.cua_protocol} if self.cua_protocol else {})))
            else:
                driver = PeekabooDriver(
                    app=live, window_id=spec["window_id"],
                    allow_reviewed_foreground=spec["delivery_mode"] == "foreground_fallback")
            if bring_forward:
                if self.cancelled.is_set() or self._deadline_expired():
                    raise PermissionError("native_execution_cancelled")
                self.gateway.current()
                self.progress("foreground_handoff", tool="desktop")
                driver.focus_reviewed_app()
                foreground_handoff_done = True
            session_id = "desktop_" + uuid4().hex
            current_invocation = {"id": None, "observation": None}

            def authorize(order_id, passed_session, arguments):
                if (order_id != self.gateway.order_id or passed_session != session_id
                        or arguments.get("app") != live.bundle_id
                        or self.cancelled.is_set() or self._deadline_expired()):
                    return False
                self.gateway.current()
                return True

            def admit_effect(order_id, passed_session, invocation_id, digest):
                if (order_id != self.gateway.order_id or passed_session != session_id
                        or invocation_id != current_invocation["id"]
                        or not isinstance(current_invocation["observation"], str)):
                    return False
                admitted = self.gateway.admit_effect(invocation_id, current_invocation["observation"], digest)
                if admitted:
                    self.current_effect = (invocation_id, digest)
                return admitted

            gate = ComputerUseGate(order_id=self.gateway.order_id, session_id=session_id,
                                   driver=driver, authorize=authorize, admit_effect=admit_effect)
            for step_index in range(max(1, min(int(spec.get("max_steps") or 1), 48))):
                self.progress("observing", tool="desktop", step=step_index)
                captured = gate.invoke("din_" + uuid4().hex,
                                       {"action": "capture", "app": live.bundle_id})
                raw_elements = captured.get("elements") or []
                document_observation = None
                if (use_cua and live.bundle_id == "com.apple.TextEdit" and document_text is not None
                        and callable(getattr(driver, "document_text_observation", None))):
                    observed = driver.document_text_observation(captured["snapshot_id"], document_text)
                    if (isinstance(observed, dict) and observed.get("snapshot_id") == captured["snapshot_id"]
                            and observed.get("pid") == live.pid and observed.get("window_id") == spec["window_id"]
                            and observed.get("expected_sha256") == hashlib.sha256(document_text.encode()).hexdigest()
                            and type(observed.get("element_index")) is int and observed["element_index"] >= 0
                            and any(item.get("id") == observed.get("element_id")
                                    and str(item.get("role") or "").lower().removeprefix("ax") == "textarea"
                                    for item in raw_elements)):
                        document_observation = observed
                if typing_only_text is not None and document_observation and document_observation.get("matches") is True:
                    if self.cancelled.is_set() or self._deadline_expired():
                        outcome = "cancelled" if self.cancelled.is_set() else "deadline_reached"
                        break
                    self.gateway.current()
                    self._record_step(kind="independent_document_goal_observation", source="ax",
                        snapshot_id=captured["snapshot_id"], element_index=document_observation["element_index"],
                        expected_sha256=document_observation["expected_sha256"], effect_count=len(effect_history))
                    self.last["task_verified"] = True
                    outcome = "verified"
                    break
                expected_visible_text = spec.get("expected_visible_text")
                observed_goal_source = (_observed_expected_visible_text(raw_elements, expected_visible_text)
                                        if isinstance(expected_visible_text, str) else None)
                if initial_window_title is None:
                    initial_window_title = captured.get("window_title") or ""
                if initial_goal_visible is None:
                    initial_goal_visible = bool(observed_goal_source)
                elif effect_history and not initial_goal_visible and observed_goal_source:
                    self._record_step(kind="independent_goal_observation",
                                      source=observed_goal_source,
                                      snapshot_id=captured["snapshot_id"],
                                      effect_count=len(effect_history))
                    self.last["task_verified"] = True
                    outcome = "verified"
                    break
                needs_editable_control = bool(spec.get("reviewed_text"))
                has_usable_background_ax = any(
                    str(item.get("id") or "").startswith("elem_")
                    and item.get("enabled") and item.get("has_point")
                    and (str(item.get("role") or "").lower()
                         in {"textfield", "searchfield", "combobox", "textarea"}
                         if needs_editable_control else item.get("actionable"))
                    for item in raw_elements)
                if (spec["delivery_mode"] == "foreground_fallback" and not use_cua
                        and not foreground_handoff_done
                        and (needs_editable_control or not has_usable_background_ax)):
                    # The founder explicitly reviewed this reversible handoff.
                    # Background AX value replacement does not establish
                    # keyboard focus for a later reviewed Return. Start a
                    # text-entry task in the reviewed foreground window;
                    # otherwise hand off only for sparse background AX.
                    if self.cancelled.is_set() or self._deadline_expired():
                        outcome = "cancelled_before_foreground_handoff"
                        break
                    self.gateway.current()
                    self.progress("foreground_handoff", tool="desktop", step=step_index)
                    try:
                        driver.focus_reviewed_app()
                    except Exception as error:
                        code = public_exception_code(error, "")
                        self.last["driver_failure_code"] = (
                            public_failure_code(code, "desktop_foreground_handoff_unconfirmed"))
                        outcome = "desktop_foreground_handoff_unconfirmed"
                        break
                    foreground_handoff_done = True
                    self._record_step(kind="foreground_handoff", operation="focus",
                                      application=live.bundle_id, window_id=spec["window_id"])
                    captured = gate.invoke("din_" + uuid4().hex,
                                           {"action": "capture", "app": live.bundle_id})
                    raw_elements = captured.get("elements") or []
                # Document prose must not spill into TextEdit's font/style
                # combobox after a safely refused document insertion. Save
                # filename text fields remain available on a later dialog.
                textedit_document_task = (live.bundle_id == "com.apple.TextEdit"
                    and bool(spec.get("reviewed_text"))
                    and bool(re.search(r"(?i)\b(?:document|lines?|note|file)\b", goal_text)))
                controls = []
                target_ids = {}
                return_focus_id = None
                awaiting_return = False
                for item in raw_elements:
                    label = str(item.get("label") or "").strip()
                    if _is_ocr_text(item):
                        continue
                    control_identity = (str(item.get("role") or "").lower(), label,
                                        "text" if item.get("text_entry") else "control")
                    if control_identity in refused_controls or control_identity in skip_once:
                        continue
                    if (not label or not item.get("enabled")
                            or "@" in label or re.search(
                                r"(?i)(password|secret|token|api.key|bearer|sk-[a-z0-9]|"
                                r"(?:ghp_|github_pat_|vck_|apikey_)[a-z0-9_-]{8})", label)):
                        continue
                    operations = []
                    if item.get("actionable"):
                        operations.append("click")
                    if (item.get("has_point") and (item.get("browser_address") or item.get("text_entry")
                            or str(item.get("role") or "").lower()
                            in {"textfield", "searchfield", "combobox", "textarea"})):
                        if not (textedit_document_task
                                and str(item.get("role") or "").lower().removeprefix("ax") == "combobox"):
                            operations.append("type")
                    if (document_observation and document_observation.get("matches") is True
                            and item.get("id") == document_observation["element_id"]):
                        operations = [operation for operation in operations if operation != "type"]
                    if not operations:
                        continue
                    key = f"c{len(controls) + 1}"
                    controls.append(Control(id=key, label=label[:300], operations=operations,
                                            visible=True, enabled=True))
                    target_ids[key] = item["id"]
                    if len(controls) == 100:
                        break
                skip_once = set()  # One observation only; the model gets a different choice.
                if (use_cua and spec.get("visual_review") is True
                        and not any("click" in control.operations for control in controls)):
                    # Some native apps draw their buttons without AXPress actions.
                    # The exact-window CUA capture still supplies bounded OCR
                    # points. Offer those points only when AX has no click path;
                    # an image review must select one before it can be dispatched.
                    for item in raw_elements:
                        if (not item.get("point_candidate") or item.get("enabled") is False):
                            continue
                        label = str(item.get("label") or "").strip()
                        if (not label or len(label) > 100 or "@" in label
                                or re.search(r"(?i)(password|secret|token|api.key|bearer|"
                                             r"sk-[a-z0-9]|(?:ghp_|github_pat_|vck_|apikey_)[a-z0-9_-]{8})", label)):
                            continue
                        key = f"c{len(controls) + 1}"
                        controls.append(Control(id=key, label=label, operations=["click"],
                                                visible=True, enabled=True))
                        target_ids[key] = item["id"]
                        if len(controls) == 100:
                            break
                    for item in raw_elements:
                        if len(controls) == 100:
                            break
                        if (not _is_ocr_text(item) or item.get("enabled") is False
                                or not item.get("has_point")
                                or type(item.get("confidence")) not in (int, float)
                                or not math.isfinite(item["confidence"])
                                or item["confidence"] < 0.8):
                            continue
                        label = str(item.get("label") or "").strip()
                        if (not label or len(label) > 100 or "@" in label
                                or re.search(r"(?i)(password|secret|token|api.key|bearer|"
                                             r"sk-[a-z0-9]|(?:ghp_|github_pat_|vck_|apikey_)[a-z0-9_-]{8})", label)):
                            continue
                        key = f"c{len(controls) + 1}"
                        controls.append(Control(id=key, label=label, operations=["click"],
                                                visible=True, enabled=True))
                        target_ids[key] = item["id"]
                        if len(controls) == 100:
                            break
                if (last_effect_operation == "type" and live.bundle_id in {
                        "com.google.Chrome", "com.brave.Browser", "com.apple.Safari",
                        "org.mozilla.firefox", "com.microsoft.edgemac", "com.operasoftware.Opera"}
                        ):
                    if len(controls) == 100:
                        # Chrome can fill the entire observation with page controls.
                        # Keep one slot for the navigation key after typing an
                        # address, or the model cannot commit the URL.
                        displaced = controls.pop()
                        target_ids.pop(displaced.id, None)
                    key = f"c{len(controls) + 1}"
                    controls.append(Control(id=key, label="Press Return in the selected browser window",
                                            operations=["key"], visible=True, enabled=True))
                    target_ids[key] = "__return__"
                    awaiting_return = True
                    if use_cua:
                        # A fresh AX token can focus the text field that was
                        # just typed before Return. The old token was consumed
                        # with its snapshot and must never be reused.
                        return_focus_id = next((item["id"] for item in raw_elements
                            if item.get("enabled") and item.get("text_entry")
                            and item.get("label") == last_effect_label
                            and str(item.get("id") or "").startswith("elem_")), None)
                if use_cua and len(controls) <= 98:
                    # The embedded native driver can wheel-scroll an exact
                    # reviewed window. Expose this for any Mac app, not only a
                    # browser; the next capture remains the witness of effect.
                    for direction in ("down", "up"):
                        key = f"c{len(controls) + 1}"
                        controls.append(Control(id=key, label=f"Scroll the selected window {direction}",
                                                operations=["scroll"], visible=True, enabled=True))
                        target_ids[key] = f"__scroll_{direction}__"
                if use_cua and len(controls) <= 96:
                    # Save/New shortcuts, only when the founder's goal names that
                    # intent. A repeat of the same chord is not offered back to back.
                    for chord in goal_chords(goal_text):
                        if CHORDS[chord][0] == last_effect_label:
                            continue
                        key = f"c{len(controls) + 1}"
                        controls.append(Control(id=key, label=CHORDS[chord][0], operations=["key"],
                                                visible=True, enabled=True))
                        target_ids[key] = f"__chord_{chord}__"
                if (last_effect_operation == "type" and return_focus_id
                        and any(item.get("id") == return_focus_id and item.get("browser_address")
                                for item in raw_elements)):
                    # A confirmed type into a freshly observed browser address
                    # field needs a navigation commit. Offering the same field
                    # again led Jev to click/type it repeatedly without ever
                    # opening the reviewed URL. Expose only the observed Return
                    # action for this single transition; all controls return on
                    # the next capture. This never submits an arbitrary form.
                    return_control = next((control for control in controls
                                           if control.operations == ["key"]
                                           and target_ids.get(control.id) == "__return__"), None)
                    if return_control is not None:
                        controls = [return_control]
                        target_ids = {return_control.id: "__return__"}
                # Only URL strings actually observed in the selected window
                # may appear in the result evidence. Never infer result links.
                for item in raw_elements:
                    for source in (item.get("label"), item.get("observed_url")):
                        for url in re.findall(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
                                              str(source or "")):
                            if len(url) <= 500 and url not in evidence_urls:
                                evidence_urls.append(url)
                readouts = []
                for item in raw_elements:
                    label = str(item.get("label") or "").strip()
                    if ((item.get("readout") or _is_ocr_text(item)) and label and len(label) <= 100
                            and "@" not in label and not re.search(
                                r"(?i)(password|secret|token|api.key|bearer|sk-[a-z0-9]|"
                                r"(?:ghp_|github_pat_|vck_|apikey_)[a-z0-9_-]{8})", label)
                            and label not in readouts):
                        readouts.append(label)
                    if len(readouts) == 12:
                        break
                # IDs and raw typed values are deliberately absent. Every label
                # here has already passed the same sensitive-data filter used
                # for the current controls/readouts.
                budget = effect_budget_line(step_index, max(1, min(int(spec.get("max_steps") or 1), 48)),
                                            self.deadline - time.monotonic())
                context = (f"{budget} Selected window: {str(spec.get('window_title') or '')[:200]}."
                           f"{subgoal_line}"
                           f" Dispatched effects so far: {compact_history(action_log)!r}."
                           f" Current visible readouts: {repr(readouts)[:900]}."
                           " Compare the goal, subgoals, readouts and effects to decide which subgoal is next."
                           " An effect marked unverified may not have landed: check the readouts before relying on it."
                           " Repeated targets may be intentional; if an action cycle has not advanced the goal,"
                           " choose a different observed action or blocked."
                           " Do not repeat reviewed text after it was submitted.")
                perception = captured.get("perception")
                if isinstance(perception, dict):
                    context += (f" Observation completeness: {perception.get('elements_complete')}; "
                                f"owner window inventory changed: {perception.get('inventory_changed')}. "
                                "Partial controls do not prove absence or uniqueness. Window inventory is "
                                "observation only: it grants no new window, creation, modal or completion authority.")
                if document_observation and document_observation.get("matches") is True:
                    context += (" Fresh same-window AX document text exactly matches the reviewed document literal. "
                                "Do not type it again. Creation, plain-text formatting and saving remain separate "
                                "requirements and are not proved by this text match.")
                if vision_hint:
                    context += f" Visual review of prior screen: {vision_hint[:300]}"
                context = context[:4000]
                observation = Observation(
                    revision=captured["snapshot_id"], application=live.bundle_id,
                    location=f"mac-window:{spec['window_id']}",
                    goal=spec.get("founder_goal") or manifest["instruction"],
                    context=context,
                    controls=controls, reviewed_text=list(spec.get("reviewed_text") or []),
                    document_text_index=spec.get("document_text_index"),
                    permitted=True, confirmation_pending=False,
                    prior_effect_uncertain=False)
                if (spec.get("visual_review") is True
                        and last_effect_status in {"unverifiable", "returned_unverified"}
                        and not awaiting_return
                        and reviewed_effect_count < len(effect_history)
                        and vision_reviews < max_vision_reviews):
                    # An admitted AX click is only a dispatched effect. Before
                    # planning the next action, inspect the fresh window image
                    # and carry its literal facts into the next decision. This
                    # prevents a multi-step goal from advancing on a click
                    # whose effect the native driver could not establish.
                    self.progress("reviewing_screen", tool="desktop", step=step_index)
                    self.gateway.current()
                    vision_reviews += 1
                    reviewed = review_image(
                        "din_" + uuid4().hex, captured["snapshot_id"],
                        _bounded_vision_png(captured["screenshot_path"]), observation)
                    vision_report = {**reviewed["report"],
                                     "image_sha256": reviewed["image_sha256"],
                                     "snapshot_id": reviewed["snapshot_id"]}
                    reviewed_effect_count = len(effect_history)
                    self._record_step(kind="visual_review", verdict=vision_report["verdict"],
                                      after_effect=last_effect_label,
                                      visible_facts=vision_report["visible_facts"][:3],
                                      next_step=vision_report["next_step"],
                                      snapshot_id=captured["snapshot_id"])
                    if vision_report["verdict"] != "continue":
                        outcome = ("visual_result_review_required" if vision_report["verdict"] == "done"
                                   else "attention_required")
                        break
                    facts = "; ".join(vision_report["visible_facts"][:3])
                    vision_hint = f"Observed after {last_effect_label}: {facts}. Next: {vision_report['next_step']}"[:300]
                    # The image-review round trip can outlive this snapshot's
                    # admission lease. Re-observe before asking for another
                    # action; the next decision will receive this visual fact
                    # but its target IDs will come from a fresh snapshot.
                    continue
                # A read-only result window can have no actionable controls.
                # The decision still needs to see its readouts and may propose
                # DONE; the desktop path never treats that as verified success.
                invocation = "din_" + uuid4().hex
                self.progress("deciding", tool="desktop", step=step_index,
                              observation_digest=observation.digest(), invocation_id=invocation)
                candidate = self.gateway.decide(invocation, observation)
                visually_confirmed_input_edit = False
                if (candidate.operation == "click" and use_cua
                        and (str(target_ids.get(candidate.target) or "").startswith("ocr_")
                             or any(item.get("id") == target_ids.get(candidate.target)
                                    and item.get("point_candidate") for item in raw_elements))):
                    # Jev has no pixels. OCR text or an AX button without an
                    # action cannot alone establish a safe pixel target; require a
                    # screenshot-grounded proposal for this fresh snapshot.
                    self._record_step(kind=("ocr_click_requires_visual_review"
                                            if str(target_ids.get(candidate.target) or "").startswith("ocr_")
                                            else "ax_point_click_requires_visual_review"))
                    candidate = Candidate("blocked", None, observation.digest())
                input_edit_target = next((control for control in controls
                    if control.id == candidate.target), None)
                if (candidate.operation == "click" and input_edit_target is not None
                        and CONSEQUENTIAL_CONTROL.search(input_edit_target.label)
                        and reviewable_input_edit(observation.goal, input_edit_target.label)):
                    # A generic Delete label can mean a calculator backspace or
                    # permanent deletion. Jev sees labels, not pixels; require
                    # an image-grounded transient-input classification.
                    self._record_step(kind="input_edit_requires_visual_review")
                    candidate = Candidate("blocked", None, observation.digest())
                effect_invocation = invocation
                proposed_control = next((control for control in controls
                                         if control.id == candidate.target), None)
                proposed_item = next((item for item in raw_elements
                                      if item.get("id") == target_ids.get(candidate.target)), None)
                self._record_step(kind="decision", operation=candidate.operation,
                                  target=candidate.target, invocation_id=invocation,
                                  available_controls=len(controls),
                                  ax_click_controls=sum("click" in control.operations
                                      and not str(target_ids.get(control.id) or "").startswith("ocr_")
                                      for control in controls),
                                  ocr_click_controls=sum("click" in control.operations
                                      and str(target_ids.get(control.id) or "").startswith("ocr_")
                                      for control in controls),
                                  target_label=proposed_control.label[:120] if proposed_control else None,
                                  target_role=str(proposed_item.get("role") or "")[:40]
                                      if proposed_item else None,
                                  target_text_entry=bool(proposed_item.get("text_entry"))
                                      if proposed_item else None,
                                  target_browser_address=bool(proposed_item.get("browser_address"))
                                      if proposed_item else None,
                                  editable_controls=sum("type" in control.operations for control in controls),
                                  raw_elements=len(raw_elements),
                                  ax_point_candidates=sum(item.get("point_candidate") is True
                                                          for item in raw_elements),
                                  ocr_text_elements=sum(_is_ocr_text(item) for item in raw_elements),
                                  observation_digest=observation.digest())
                if candidate.operation in {"blocked", "done"}:
                    if candidate.operation == "blocked":
                        # A visual `continue` is an observation, not a new
                        # action. If the same usable controls and readouts lead
                        # to a second BLOCKED without an intervening effect,
                        # another image/model round trip cannot advance the
                        # task. Stop before spending the remaining step budget.
                        blocked_observation = (
                            tuple((control.label, tuple(control.operations)) for control in controls),
                            tuple(readouts), len(effect_history))
                        if blocked_observation == previous_blocked_observation:
                            self._record_step(kind="blocked_decision_stopped",
                                              reason="repeated_blocked_observation",
                                              available_controls=len(controls),
                                              editable_controls=sum("type" in control.operations
                                                                    for control in controls),
                                              visible_readouts=len(readouts),
                                              prior_visual_verdict=(vision_report or {}).get("verdict"))
                            self.last["failure_code"] = "desktop_repeated_blocked_observation"
                            outcome = "attention_required"
                            break
                        previous_blocked_observation = blocked_observation
                    if spec.get("visual_review") is True and vision_reviews < max_vision_reviews:
                        self.progress("reviewing_screen", tool="desktop", step=step_index)
                        self.gateway.current()
                        vision_reviews += 1
                        eligible_clicks = [control for control in controls
                            if "click" in control.operations
                            and (not CONSEQUENTIAL_CONTROL.search(control.label)
                                 or reviewable_input_edit(observation.goal, control.label))]
                        vision_invocation = "din_" + uuid4().hex
                        reviewed = review_image(
                            vision_invocation, captured["snapshot_id"],
                            _bounded_vision_png(captured["screenshot_path"]),
                            observation)
                        vision_report = {**reviewed["report"],
                                         "image_sha256": reviewed["image_sha256"],
                                         "snapshot_id": reviewed["snapshot_id"]}
                        self._record_step(kind="visual_review", verdict=vision_report["verdict"],
                                          snapshot_id=captured["snapshot_id"])
                        selected_id = vision_report.get("click_control_id")
                        if (candidate.operation == "blocked" and vision_report["verdict"] == "continue"
                                and selected_id in {control.id for control in eligible_clicks}
                                and reviewed.get("proposal_invocation_id") == vision_invocation
                                and driver.snapshot_ready_for_admission(captured["snapshot_id"])):
                            candidate = Candidate("click", selected_id, observation.digest())
                            proposed_control = next(control for control in controls if control.id == selected_id)
                            visually_confirmed_input_edit = (
                                reviewable_input_edit(observation.goal, proposed_control.label)
                                and vision_report.get("input_edit_control_id") == selected_id)
                            previous_blocked_observation = None
                            effect_invocation = vision_invocation
                            self._record_step(kind="visual_control_proposal", target_label=proposed_control.label[:120],
                                              snapshot_id=captured["snapshot_id"])
                        elif vision_report["verdict"] == "continue" and vision_report["next_step"]:
                            # Jev receives AX/OCR controls, not pixels. Carry the
                            # bounded visual observation into its next fresh
                            # decision so it can identify an ordinary modal
                            # or page obstruction without inventing a control.
                            facts = "; ".join(vision_report["visible_facts"][:3])
                            vision_hint = f"Observed: {facts}. Next: {vision_report['next_step']}"[:300]
                            continue
                        else:
                            outcome = ("visual_result_review_required" if vision_report["verdict"] == "done"
                                       else "attention_required")
                            break
                    else:
                        outcome = "attention_required" if candidate.operation == "blocked" else "outcome_review_required"
                        break
                if candidate.operation == "wait":
                    self.cancelled.wait(1)
                    continue
                if candidate.operation not in {"click", "type", "key", "scroll"} or candidate.target not in target_ids:
                    self._record_step(kind="action_rejected", reason="unavailable_control",
                                      operation=candidate.operation,
                                      target_label=proposed_control.label[:120] if proposed_control else None)
                    outcome = "action_requires_review"
                    break
                if typing_only_text is not None and (
                        candidate.operation != "type"
                        or candidate.value_index != reviewed_values.index(typing_only_text)
                        or not any(item.get("id") == target_ids[candidate.target]
                                   and str(item.get("role") or "").lower().removeprefix("ax") == "textarea"
                                   for item in raw_elements)):
                    # The narrow completion proof cannot cover additional
                    # effects, including a forbidden Save or Return action.
                    self._record_step(kind="action_rejected", reason="typing_only_document_scope")
                    outcome = "action_requires_review"
                    break
                if (candidate.operation == "type" and last_effect_operation == "type"
                        and candidate.value_index == last_type_index
                        and target_ids[candidate.target] == last_type_target):
                    # A returned type effect can still be visually ambiguous.
                    # Re-dispatching the same text to the same control risks a
                    # duplicate input; require a different observed next move.
                    outcome = "repeated_action_stopped"
                    break
                if (candidate.operation == "type" and last_effect_operation == "key"
                        and proposed_control is not None
                        and last_submitted_type == (candidate.value_index, proposed_control.label)):
                    # A fresh observation after Return may still show the old
                    # page while navigation settles. Do not enter the same
                    # reviewed text again: two type/Return cycles can burn
                    # credits and obscure whether the first one worked.
                    self._record_step(kind="repeated_action_prevented", operation="type",
                                      target_label=proposed_control.label[:120],
                                      previous_effect=last_effect_status,
                                      snapshot_id=captured["snapshot_id"])
                    if spec.get("visual_review") is True and vision_reviews < max_vision_reviews:
                        self.progress("reviewing_screen", tool="desktop", step=step_index)
                        self.gateway.current()
                        vision_reviews += 1
                        reviewed = review_image(
                            "din_" + uuid4().hex, captured["snapshot_id"],
                            _bounded_vision_png(captured["screenshot_path"]))
                        vision_report = {**reviewed["report"],
                                         "image_sha256": reviewed["image_sha256"],
                                         "snapshot_id": reviewed["snapshot_id"]}
                        self._record_step(kind="visual_review", verdict=vision_report["verdict"],
                                          snapshot_id=captured["snapshot_id"])
                        outcome = ("visual_result_review_required" if vision_report["verdict"] == "done"
                                   else "repeated_action_stopped")
                    else:
                        outcome = "repeated_action_stopped"
                    break
                if not driver.snapshot_ready_for_admission(captured["snapshot_id"]):
                    # Jev and the Core round trip may outlive a safe snapshot.
                    # No effect was admitted, so a fresh observation can safely
                    # replace this proposal without repeating an action.
                    self._record_step(kind="observation_expired_before_admission",
                                      invocation_id=invocation)
                    continue
                label = next(control.label for control in controls if control.id == candidate.target)
                if (candidate.operation == "type"
                        and repeated_type_on_field(effect_history, label[:120])):
                    # Typing into the same field a third time is a loop (for
                    # example a location bar that keeps rejecting the text).
                    self._record_step(kind="repeated_action_prevented", operation="type",
                                      target_label=label[:120], reason="repeated_type_on_field",
                                      snapshot_id=captured["snapshot_id"])
                    outcome = "repeated_action_stopped"
                    break
                if _stalled_action_cycle(effect_history, candidate.operation, label[:120], tuple(readouts)):
                    self._record_step(kind="repeated_action_prevented", operation=candidate.operation,
                                      target_label=label[:120], reason="unchanged_readout_cycle",
                                      snapshot_id=captured["snapshot_id"])
                    outcome = "repeated_action_stopped"
                    break
                if stalled_few_actions(effect_history, tuple(readouts)):
                    # Six effects, at most two distinct actions, nothing visible changed.
                    self._record_step(kind="repeated_action_prevented", operation=candidate.operation,
                                      target_label=label[:120], reason="no_visible_progress",
                                      snapshot_id=captured["snapshot_id"])
                    outcome = "repeated_action_stopped"
                    break
                retry_noop = (candidate.operation == "click" and last_effect_operation == "click"
                              and last_effect_status == "suspected_noop"
                              and label == last_effect_label and label not in retried_labels)
                if retry_noop:
                    # The driver said the click did nothing; one fresh retry is safe.
                    retried_labels.add(label)
                    self._record_step(kind="effect_retry", operation="click", target_label=label[:120],
                                      reason="suspected_noop", snapshot_id=captured["snapshot_id"])
                if (candidate.operation == "click" and last_effect_operation == "click"
                        and last_effect_status in {"unverifiable", "returned_unverified", "suspected_noop"}
                        and label == last_effect_label and not retry_noop):
                    # Control IDs are regenerated for each screenshot. A second
                    # proposal for the same labelled target after an uncertain
                    # click is a repeat even if its c-number changed. Review
                    # the fresh image, but do not dispatch the click again.
                    self._record_step(kind="repeated_action_prevented", operation="click",
                                      target_label=label[:120], previous_effect=last_effect_status,
                                      snapshot_id=captured["snapshot_id"])
                    if spec.get("visual_review") is True and vision_reviews < max_vision_reviews:
                        self.progress("reviewing_screen", tool="desktop", step=step_index)
                        self.gateway.current()
                        vision_reviews += 1
                        reviewed = review_image(
                            "din_" + uuid4().hex, captured["snapshot_id"],
                            _bounded_vision_png(captured["screenshot_path"]))
                        vision_report = {**reviewed["report"],
                                         "image_sha256": reviewed["image_sha256"],
                                         "snapshot_id": reviewed["snapshot_id"]}
                        self._record_step(kind="visual_review", verdict=vision_report["verdict"],
                                          snapshot_id=captured["snapshot_id"])
                        if vision_report["verdict"] == "done":
                            outcome = "visual_result_review_required"
                        elif vision_report["verdict"] == "continue" and repeat_skips < 2:
                            # Recover instead of stopping: hide this control for one
                            # observation so the model must pick another route.
                            repeat_skips += 1
                            chosen = next((item for item in raw_elements
                                           if item.get("id") == target_ids[candidate.target]), None)
                            if chosen:
                                skip_once.add((str(chosen.get("role") or "").lower(),
                                               str(chosen.get("label") or "").strip(),
                                               "text" if chosen.get("text_entry") else "control"))
                            facts = "; ".join(vision_report["visible_facts"][:3])
                            vision_hint = (f"{label[:60]} was already clicked and its effect is unverified. "
                                           f"Observed: {facts}. Next: {vision_report['next_step']}")[:300]
                            continue
                        else:
                            outcome = "repeated_action_stopped"
                    else:
                        outcome = "repeated_action_stopped"
                    break
                if CONSEQUENTIAL_CONTROL.search(label) and not visually_confirmed_input_edit:
                    self._record_step(kind="action_rejected", reason="consequential_control",
                                      operation=candidate.operation, target_label=label[:120])
                    outcome = "action_requires_review"
                    break
                arguments = {"action": candidate.operation, "app": live.bundle_id,
                             "snapshot_id": captured["snapshot_id"]}
                if candidate.operation == "key":
                    key_target = str(target_ids[candidate.target])
                    if (key_target.startswith("__chord_") and key_target.endswith("__")
                            and key_target[8:-2] in CHORDS):
                        arguments["chord"] = key_target[8:-2]
                    elif key_target == "__return__" and last_effect_operation == "type":
                        arguments["chord"] = "Return"
                        if return_focus_id:
                            arguments["element_id"] = return_focus_id
                    else:
                        outcome = "action_requires_review"
                        break
                elif candidate.operation == "scroll":
                    direction = target_ids[candidate.target]
                    if direction not in {"__scroll_down__", "__scroll_up__"}:
                        outcome = "action_requires_review"
                        break
                    arguments["direction"] = direction.removeprefix("__scroll_").removesuffix("__")
                else:
                    arguments["element_id"] = target_ids[candidate.target]
                if candidate.operation == "type":
                    index = candidate.value_index
                    values = spec.get("reviewed_text") or []
                    if type(index) is not int or not 0 <= index < len(values):
                        outcome = "action_requires_review"
                        break
                    arguments["text"] = values[index]
                    if spec.get("document_text_index") == index:
                        arguments["text_entry_profile"] = "textedit_three_lines_v1"
                current_invocation.update(id=effect_invocation, observation=observation.digest())
                self.progress("acting", tool="desktop", operation=candidate.operation,
                              step=step_index, target_label=label[:120],
                              driver="embedded_cua" if use_cua else "peekaboo")
                try:
                    result = gate.invoke(effect_invocation, arguments)
                except EffectUncertain as error:
                    self.last["driver_failure_code"] = public_failure_code(error.driver_code, "desktop_effect_unconfirmed")
                    if error.driver_detail:
                        # Free-form driver detail may contain private application text.
                        self.last["driver_failure_detail"] = public_failure_code(error.driver_detail, "desktop_driver_detail_private")
                        self._record_step(kind="effect_failure", operation=candidate.operation,
                                          code=self.last["driver_failure_code"],
                                          detail=self.last["driver_failure_detail"])
                    if self.current_effect:
                        if (error.driver_code == "desktop_fresh_observation_required"
                                or error.driver_code.startswith("desktop_pre_dispatch_")):
                            self.gateway.effect_receipt(*self.current_effect, "not_dispatched",
                                                        error.driver_code)
                            self.current_effect = None
                            self._record_step(kind="safe_refusal", operation=candidate.operation,
                                              target_label=label[:120], code=error.driver_code)
                            safe_refusals += 1
                            if (use_cua and safe_refusals <= 3
                                    and error.driver_code == "desktop_fresh_observation_required"):
                                # Admission can outlive the native snapshot lease,
                                # especially after an image review. The driver
                                # confirmed no effect was dispatched. Reobserve
                                # and obtain a new decision with fresh control
                                # tokens and a new one-use invocation.
                                if (vision_report and vision_report.get("snapshot_id")
                                        == captured["snapshot_id"]):
                                    facts = "; ".join(vision_report["visible_facts"][:3])
                                    vision_hint = (f"Previous visual review: {facts}. "
                                                   f"Next: {vision_report['next_step']}")[:300]
                                continue
                            if (use_cua and safe_refusals <= 3
                                    and error.driver_code.startswith("desktop_pre_dispatch_cua_")):
                                chosen = next((item for item in raw_elements
                                               if item.get("id") == target_ids[candidate.target]), None)
                                if chosen:
                                    refused_controls.add((str(chosen.get("role") or "").lower(),
                                                          str(chosen.get("label") or "").strip(),
                                                          "text" if chosen.get("text_entry") else "control"))
                                continue
                            outcome = "pre_dispatch_rejected"
                            break
                        self.gateway.effect_receipt(*self.current_effect, "uncertain")
                    raise
                if self.current_effect:
                    self.gateway.effect_receipt(*self.current_effect, "returned")
                    self.current_effect = None
                self.last.pop("driver_failure_code", None)
                self._record_step(kind="effect", operation=candidate.operation,
                                  effect=result.get("effect"), invocation_id=effect_invocation,
                                  target_label=label[:120],
                                  cursor_indicator=result.get("cursor_indicator"),
                                  driver="embedded_cua" if use_cua else "peekaboo")
                effect_status = result.get("effect")
                if effect_status not in {"confirmed", "unverifiable", "returned_unverified",
                                         "suspected_noop"}:
                    effect_status = "unknown"
                action_log.append(f"{candidate.operation} '{label[:40]}' "
                                  + ("ok" if effect_status == "confirmed" else "unverified"))
                action_log = action_log[-60:]
                effect_history.append({"operation": candidate.operation, "label": label[:120],
                                       "status": effect_status,
                                       "readouts": tuple(readouts)})
                effect_history = effect_history[-6:]
                if (candidate.operation == "key" and arguments.get("chord") == "Return"
                        and last_effect_operation == "type"
                        and isinstance(last_type_index, int)):
                    last_submitted_type = (last_type_index, last_effect_label)
                if (candidate.operation == "key" and arguments.get("chord") == "Return"
                        and live.bundle_id in _BROWSER_APPS and last_effect_operation == "type"):
                    # A page load is not instant; capture its title only after a pause.
                    for _ in range(3):
                        gate.invoke("din_" + uuid4().hex, {"action": "wait", "app": live.bundle_id,
                                                           "milliseconds": 1000})
                last_effect_operation = candidate.operation
                last_effect_status = result.get("effect")
                last_effect_label = label
                last_type_index = candidate.value_index if candidate.operation == "type" else None
                last_type_target = target_ids[candidate.target] if candidate.operation == "type" else None
            else:
                outcome = "step_limit_reached"
        except Exception as error:
            code = public_exception_code(error, "")
            detail = getattr(error, "detail", None) or getattr(error, "driver_detail", None)
            if isinstance(detail, str) and detail:
                self.last["driver_failure_detail"] = public_failure_code(detail, "desktop_driver_detail_private")
            if self.cancelled.is_set():
                outcome = "cancelled"
            elif (claimed and effect_history and code in {
                    "desktop_cua_window_target_not_found", "desktop_cua_observation_unpinned"}):
                # A dialog can close after its one admitted click. CUA 0.29.1
                # sometimes returns an unpinned capture instead of a structured
                # missing-window refusal. Inspect only process/window identities;
                # a successor window is not reviewed for capture or further input.
                # Never replay the click.
                try:
                    current_app = next((app for app in PeekabooDriver.running_apps()
                                        if app.bundle_id == expected.bundle_id
                                        and app.pid == expected.pid
                                        and app.process_start_identity
                                            == expected.process_start_identity), None)
                    same_process = current_app is not None
                    remaining = (PeekabooDriver.running_windows(current_app)
                                 if same_process else [])
                    reviewed_present = any(row["window_id"] == spec["window_id"]
                                           for row in remaining)
                    self._record_step(kind="window_identity_after_capture_refusal",
                                      after_effect=effect_history[-1]["label"][:120],
                                      same_process=same_process,
                                      reviewed_window_present=reviewed_present,
                                      other_window_count=min(sum(
                                          row["window_id"] != spec["window_id"]
                                          for row in remaining), 64))
                    outcome = ("reviewed_window_closed_after_effect" if effect_history
                               and same_process and not reviewed_present
                               else "reviewed_window_unavailable")
                except Exception:
                    outcome = "execution_requires_reconciliation"
            elif not claimed and code in {"desktop_app_identity_changed", "desktop_window_identity_changed"}:
                # The founder approved an exact process and window. A changed
                # identity invalidates that review before any effect or claim;
                # this is not an uncertain execution to reconcile or retry.
                outcome = "reviewed_window_changed_before_start"
            elif not claimed and code == "native_execution_unavailable":
                # Core definitely refused availability before any native driver
                # or effect. This is not an uncertain execution to reconcile.
                outcome = "execution_unavailable"
            else:
                outcome = "execution_requires_reconciliation"
            self.last["failure_code"] = public_failure_code(code, "desktop_execution_failed")
        finally:
            self._runtime_cleanup(outcome)
            if driver:
                driver.close()
            self.closed = True
            reason = reason_code(outcome, self.last.get("failure_code"),
                                 self.last.get("driver_failure_code"))
            if reason:
                self.last["reason_code"] = reason
            evidence = {"order_id": self.gateway.order_id,
                        "execution_id": self.gateway.execution_id,
                        "outcome": outcome, "task_verified": self.last.get("task_verified") is True,
                        "driver": "embedded_cua" if self.cua_socket else "peekaboo",
                        "visual_review": vision_report,
                        "observed_urls": evidence_urls[:20],
                        "steps": list(self._step_receipts)}
            if self.last.get("driver_failure_detail"):
                evidence["failure_detail"] = self.last["driver_failure_detail"]
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            terminal = "stopped" if claimed else "not_started"
            self.progress(terminal, tool="desktop", outcome=outcome,
                          evidence=evidence, evidence_digest=digest,
                          task_verified=self.last.get("task_verified") is True)
            if claimed:
                try:
                    extra = {"reason_code": self.last["reason_code"]} if self.last.get("reason_code") else {}
                    result = self.gateway.stop_receipt(
                        digest, True, task_verified=self.last.get("task_verified") is True, **extra)
                    self.progress(terminal, tool="desktop",
                                  credits_reconciled=result.get("state") == "stopped")
                except Exception:
                    self.progress("reconciliation_required", tool="desktop", credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_general_browser(self, saved, manifest):
        """Explicit saved backend over one browser and unchanged founder handoff."""
        browser = None
        tracker = None
        claimed = False
        outcome = "execution_unconfirmed"
        observed_locations = []
        general = manifest.get("general_browser_verification") or {}
        profile = general.get('execution_profile')
        self._general_read_only = manifest.get("actions") == ["browser.read"]
        self._general_qualification = saved.get("browser_qualification_grant") is not None
        adapter = None
        terminal_operation = ""
        terminal_summary = ""
        def terminal_request(operation, summary):
            nonlocal terminal_operation, terminal_summary
            terminal_operation = operation
            terminal_summary = redact_string(summary).strip()[:500]
            self.last.update(result_summary=terminal_summary, task_verified=False)
            self._record_step(kind="terminal", operation=operation,
                summary_sha256=hashlib.sha256(terminal_summary.encode()).hexdigest())
            if operation == "needs_you":
                self.progress("needs_you", tool="general_browser", founder_prompt=terminal_summary,
                    message=f"Needs you: {terminal_summary}")
        self._general_cuai = profile is not None
        max_steps = int(general.get("max_steps") or 32)
        max_steps = max(1, min(max_steps, 64))
        self._general_mode = True
        recent_actions = []
        recent_labels = []
        try:
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.google.Chrome"]
                    or not manifest["origins"] or manifest["actions"] not in (["browser.read", "browser.interact"], ["browser.read"])
                    or self._general_read_only and (profile is None or general.get("allow_type") is not False
                        or general.get("fill_values"))
                    or not isinstance(general, dict) or general.get("operation") != "general_goal_v1"
                    or not isinstance(general.get("start_url"), str)):
                raise ValueError("general_browser_scope_unsupported")
            from playwright.sync_api import sync_playwright  # noqa: F401
            tracker = OwnedProcesses()
            self.progress("claiming", tool="general_browser")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self._cursor_route = "browser"
            # A fixture grant binds the complete GET query. Analytics decoration
            # would turn the reviewed route into an unreviewed request.
            start_url = general["start_url"] if self._general_qualification else self.browser_start_url(manifest)
            browser = OwnedBrowser(
                order_id=self.gateway.order_id, goal=manifest["instruction"],
                origins=manifest["origins"], authorize=self.authorize, admit_effect=self.admit,
                process_observer=tracker.capture, general_mode=True, chrome_factory=self.chrome_factory(),
                fill_values=general.get("fill_values") or {}, read_only=self._general_read_only,
                **({"read_urls": general["read_urls"]} if general.get("read_urls") else {}),
                **({"qualification_grant": saved["browser_qualification_grant"],
                    "qualification_current": self.gateway.qualification_current,
                    "qualification_gateway": self.gateway}
                   if saved.get("browser_qualification_grant") is not None else {}),
                **({'cuai_mode':True, 'bounded_reads':True,
                    **({'cuai_version':profile['version']} if profile['version']=='browser_cuai_text_v2' else {})} if self._general_cuai else {}))
            self._active_browser = browser
            self._reveal = browser.reveal if getattr(browser, "visible", False) else None
            self.progress("opening_browser", tool="general_browser")
            browser.start(start_url)
            tracker.capture()
            if self._general_cuai:
                from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
                adapter=CuOwnedBrowserAdapter(browser=browser,gateway=self.gateway,profile=profile,
                    goal=manifest['instruction'],stop=self.cancelled,deadline=self.deadline,record=self._record_step)
            for record in ([] if adapter else browser.fill_reviewed_fields()):
                self._record_step(**record)
            for step_index in range(max_steps):
                if not self.authorize(self.gateway.order_id, browser.session_id):
                    outcome = "cancelled_or_expired"
                    break
                self.progress("observing", tool="general_browser", step=step_index)
                started = time.monotonic()
                observation = browser.observe()
                observe_ms = int((time.monotonic() - started) * 1000)
                self.watch_browser(browser, "observe")
                observed_locations.append(observation.location)
                self._record_step(kind="observation", digest=observation.digest(),
                                  location=observation.location, latency_ms=observe_ms,
                                  confirmation_pending=observation.confirmation_pending,
                                  pause_kind=browser.pause_kind,
                                  control_summary=getattr(browser, "last_control_summary", {}))
                if (self._general_read_only or self._general_qualification) and (observation.confirmation_pending or browser.pause_kind):
                    outcome = "attention_required"
                    prompt = browser.founder_prompt or "Please take over at the protected input on this page."
                    if self._general_qualification and browser.pause_kind == 'payment':
                        payment_fact = getattr(browser,'qualification_payment_fact',None)
                        try:
                            fact = payment_fact(observation) if callable(payment_fact) else None
                        except Exception:
                            fact = None  # Unknown source delivery keeps the protected handoff generic.
                        if fact:
                            prompt = fact + '. ' + prompt
                    terminal_request("needs_you",prompt)
                    break
                if observation.confirmation_pending and browser.pause_kind:
                    prompt = browser.founder_prompt or "Supymem needs you to continue."
                    # A login wall in the persistent Supymem browser is a sign-in takeover, not a generic pause.
                    notify_kind = "other"
                    if browser.pause_kind == "password" and getattr(browser, "visible", False):
                        notify_kind, prompt = "sign_in", "Sign in in the Supymem browser, then click Continue."
                    self.watch.pause()  # no frames while the founder may be typing a password
                    self.continue_event.clear()
                    self.handoff_requested.clear()
                    self._handoff_visible = False
                    # Never steal focus: founder opens the headed window explicitly.
                    handoff = "message_only"
                    self.progress(
                        "needs_you", tool="general_browser", pause_kind=browser.pause_kind,
                        founder_prompt=prompt, handoff=handoff,
                        message=f"Needs you: {prompt}")
                    self._record_step(kind="pause", pause_kind=browser.pause_kind,
                                      founder_prompt=prompt, handoff=handoff)
                    self._notify_needs_you(prompt, notify_kind)
                    authority_checks = 0
                    while not self.continue_event.wait(0.25):
                        if self.cancelled.is_set() or self._deadline_expired():
                            break
                        authority_checks += 1
                        if authority_checks % 8 == 0:
                            try:
                                self.gateway.current()
                            except Exception:
                                # The founder can cancel from another Supymem
                                # window while this worker is paused. Stop the
                                # owned browser without requiring its UI button.
                                self.cancelled.set()
                                break
                        if self.handoff_requested.is_set() and not self._handoff_visible:
                            self.handoff_requested.clear()
                            try:
                                browser.open_founder_handoff()
                                self._handoff_visible = True
                                handoff = "visible"
                                self.progress(
                                    "needs_you", tool="general_browser",
                                    pause_kind=browser.pause_kind, founder_prompt=prompt,
                                    handoff=handoff, message=f"Needs you: {prompt}")
                                self._record_step(kind="handoff", handoff="visible")
                            except Exception:
                                handoff = "message_only"
                                self.progress(
                                    "needs_you", tool="general_browser",
                                    pause_kind=browser.pause_kind, founder_prompt=prompt,
                                    handoff=handoff, message=f"Needs you: {prompt}")
                    if self.cancelled.is_set() or self._deadline_expired():
                        outcome = "cancelled_or_expired"
                        break
                    self.watch.resume()
                    if self._handoff_visible:
                        try:
                            browser.return_to_background()
                        except Exception as error:
                            failure = public_exception_code(error, "browser_handoff_failed")
                            raise ValueError(failure) from None
                        self._handoff_visible = False
                    self.progress("observing", tool="general_browser", step=step_index,
                                  resumed_after="needs_you")
                    continue
                if "browser.interact" not in manifest["actions"] and not self._general_read_only:
                    observation.controls = []
                if adapter is None and os.environ.get("JEV_FIXED_ADAPTER", "1") != "0":  # Jev route only; the CUAI adapter binds the plain observation
                    observation.recent_actions = recent_labels[-10:]
                tracker.capture()
                invocation = "din_" + uuid4().hex
                decide_started = time.monotonic()
                self.progress("deciding", tool="general_browser",
                              observation_digest=observation.digest(), invocation_id=invocation)
                try:
                    proposal = adapter.decide(observation) if adapter else None
                    if proposal:
                        candidate,invocation=proposal.candidate,proposal.invocation_id
                    else:
                        candidate = self.gateway.decide(invocation, observation)
                except Exception as error:
                    # A missing/unknown decision never authorizes a fallback or replay.
                    outcome = "cancelled" if self.cancelled.is_set() else "attention_required"
                    self.last["failure_code"] = public_exception_code(error, "native_execution_failed")
                    if not self.cancelled.is_set():
                        self.last["reason_code"] = "cu_browser_decision_unavailable"
                    self._record_step(kind="decision", error="decision_unavailable",
                                      code=self.last["failure_code"], exception_type=public_exception_type(error))
                    break
                decide_ms = int((time.monotonic() - decide_started) * 1000)
                self._record_step(kind="decision", invocation_id=invocation,
                                  operation=candidate.operation, target=candidate.target,
                                  latency_ms=decide_ms, observation_digest=observation.digest())
                action_key = (candidate.operation, candidate.target, observation.location)
                if adapter and candidate.operation == "wait":
                    action_key = (candidate.operation, adapter.subgoal, observation.location)
                if adapter and candidate.operation in {"extract", "scroll"}:
                    action_key = (candidate.operation, (proposal.action.text, proposal.action.direction, proposal.action.amount), observation.location)
                recent_actions.append(action_key)
                label = next((c.label for c in observation.controls if c.id == candidate.target), "")
                recent_labels.append((candidate.operation + " " + " ".join(label.split()))[:120])
                if len(recent_actions) >= 3 and len(set(recent_actions[-3:])) == 1:
                    outcome = "repeated_action_stopped"
                    break
                if candidate.operation == "done":
                    if adapter:
                        self.last['semantic_satisfied']=adapter.semantic_satisfied is True
                        self.last['result_summary']=redact_string(adapter.answer)
                        self.last['task_verified']=False
                    outcome = "outcome_review_required"
                    terminal_operation = "done"
                    break
                if candidate.operation == "blocked":
                    outcome = "attention_required"
                    if adapter and proposal.terminal_operation in {"fail", "needs_you"}:
                        terminal_request(proposal.terminal_operation, proposal.summary)
                    break
                if candidate.operation == "wait":
                    self.cancelled.wait(1)
                    continue
                if candidate.operation == "type":
                    grant = getattr(browser, "qualification_grant", None)
                    qualified_fill = bool(self._general_qualification and adapter and grant is not None
                        and set(grant.allowed_operations) & {"type", "select"})
                    if not general.get("allow_type", True) and not qualified_fill:
                        outcome = "action_requires_review"
                        break
                    text = browser.resolve_fill(candidate.target)
                    if not text:
                        outcome = "attention_required"
                        terminal_request("needs_you", "What value should I use for this field? Add it on the review before retrying.")
                        break
                    self.progress("acting", tool="general_browser", operation="type")
                    try:
                        if adapter:
                            adapter.before_dispatch(proposal,observation)
                        browser.type_text(candidate, invocation, text)
                    except BrowserPreDispatchRefused as error:
                        self._browser_input_refused(error, adapter, "type")
                        raise
                    except PermissionError:
                        if adapter and self.current_effect is None:
                            adapter.not_dispatched()
                        raise
                    except EffectUncertain:
                        if self.current_effect and not self._type_local_admit:
                            self.gateway.effect_receipt(*self.current_effect, "uncertain")
                        raise
                    self._clear_effect_receipt()
                    if adapter:
                        adapter.delivered(proposal)
                    self._record_step(kind="effect", operation="type",
                                      label=browser.last_typed_label, typed=True,
                                      invocation_id=invocation)
                    continue
                if adapter and (candidate.operation in {"scroll", "extract"}
                        or self._general_qualification and candidate.operation in {"hover", "new_tab", "switch_tab"}
                        or candidate.operation == "navigate" and str(candidate.target).startswith("document_")):
                    self.progress("acting", tool="general_browser", operation=candidate.operation)
                    try:
                        adapter.before_dispatch(proposal, observation)
                        if candidate.operation == "hover":
                            browser.hover(candidate, invocation)
                        elif candidate.operation == "switch_tab" or candidate.operation == "new_tab" and str(candidate.target).startswith("document_"):
                            browser.tab_action(candidate, invocation, proposal.action)
                        elif candidate.operation == "new_tab":
                            browser.click(candidate, invocation, new_tab_url=proposal.action.text)
                        else:
                            browser.document_action(candidate, invocation, proposal.action)
                    except BrowserPreDispatchRefused as error:
                        self._browser_input_refused(error, adapter, candidate.operation)
                        raise
                    except PermissionError:
                        if self.current_effect is None:
                            adapter.not_dispatched()
                        raise
                    except EffectUncertain:
                        if self.current_effect:
                            self.gateway.effect_receipt(*self.current_effect, "uncertain")
                        raise
                    self._clear_effect_receipt()
                    adapter.delivered(proposal)
                    self._record_step(kind="effect", operation=candidate.operation, invocation_id=invocation)
                    continue
                if candidate.operation not in {"click", "navigate"} or candidate.operation == "navigate" and not adapter:
                    outcome = "action_requires_review"
                    break
                self.progress("acting", tool="general_browser", operation=candidate.operation)
                try:
                    if adapter:
                        adapter.before_dispatch(proposal,observation)
                    if candidate.operation == "navigate":
                        browser.click(candidate, invocation, navigate_url=proposal.action.text)
                    else:
                        browser.click(candidate, invocation)
                except BrowserPreDispatchRefused as error:
                    self._browser_input_refused(error, adapter, candidate.operation)
                    raise
                except PermissionError:
                    if adapter and self.current_effect is None:
                        adapter.not_dispatched()
                    raise
                except EffectUncertain:
                    if self.current_effect:
                        self.gateway.effect_receipt(*self.current_effect, "uncertain")
                    raise
                if self.current_effect:
                    self.gateway.effect_receipt(*self.current_effect, "returned")
                    self.current_effect = None
                if adapter:
                    adapter.delivered(proposal)
                self._record_step(kind="effect", operation=candidate.operation, target=candidate.target,
                                  invocation_id=invocation)
            else:
                outcome = "step_limit_reached"
        except PermissionError as error:
            code = public_exception_code(error, "")
            if code == "browser_location_out_of_scope":
                outcome = "origin_violation_stopped"
                self.last["failure_code"] = code
            else:
                outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
                self.last["failure_code"] = public_failure_code(code, "native_execution_failed")
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            failure_code = public_exception_code(error, "native_execution_failed")
            self.last["failure_code"] = failure_code
        finally:
            self._general_mode = False
            self._general_cuai = False
            self._reveal = None
            self.watch.clear()
            self.watch.resume()
            self._active_browser = None
            self._handoff_visible = False
            try:
                self.progress("closing", tool="general_browser", outcome=outcome)
            except Exception:
                pass
            close_returned = browser is None
            if tracker:
                tracker.capture()
            if browser:
                try:
                    browser.close()
                    close_returned = True
                except Exception:
                    close_returned = False
            if tracker:
                tracker.stop_all()
            stopped = bool(tracker and close_returned and tracker.stopped())
            self.closed = stopped or not self.gateway.claim_attempted
            stop_reason = (self.last.get("reason_code") or reason_code(outcome, self.last.get("failure_code"))
                           if outcome != "outcome_review_required" else None)
            if stop_reason:
                self.last["reason_code"] = stop_reason
            evidence = {"order_id": self.gateway.order_id, "execution_id": self.gateway.execution_id,
                        "close_returned": close_returned, "processes_stopped": stopped,
                        "inventory": tracker.receipt() if tracker else None, "outcome": outcome,
                        "task_verified": self.last.get("task_verified") is True,
                        "observed_locations": observed_locations, "steps": list(self._step_receipts)}
            if stop_reason:
                evidence["reason_code"] = stop_reason
            if adapter:
                evidence['execution_profile_digest']=adapter.profile_digest
                evidence['semantic_satisfied']=adapter.semantic_satisfied is True
                visible_summary = terminal_summary or self.last.get('result_summary') or ""
                evidence['answer_sha256']=hashlib.sha256(visible_summary.encode()).hexdigest()
                evidence['pending_dispatch']=adapter.pending_dispatch
                evidence['browser_phase_requests_attempted']=adapter.calls
            if terminal_operation:
                evidence['terminal_operation'] = terminal_operation
                evidence['answer_sha256'] = hashlib.sha256((terminal_summary or self.last.get('result_summary') or "").encode()).hexdigest()
            download = getattr(browser, 'last_qualification_download', None) if browser else None
            if saved.get('browser_qualification_grant') is not None and isinstance(download, dict):
                keys = ('filename', 'sha256', 'size_bytes', 'media_type', 'request_id', 'rule_id')
                captured = {key: download.get(key) for key in keys}
                if (isinstance(captured['filename'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}\.csv', captured['filename'])
                        and isinstance(captured['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', captured['sha256'])
                        and type(captured['size_bytes']) is int and 0 < captured['size_bytes'] <= 1_048_576
                        and captured['media_type'] == 'text/csv'
                        and all(isinstance(captured[key], str) and 0 < len(captured[key]) <= 128 for key in ('request_id', 'rule_id'))):
                    evidence['downloads'] = [captured]
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            terminal = "not_started" if not self.gateway.claim_attempted else "stopped" if stopped else "reconciliation_required"
            self.progress(terminal, tool="general_browser", evidence=evidence, evidence_digest=digest)
            if claimed:
                try:
                    result = self.gateway.stop_receipt(
                        digest, stopped, task_verified=self.last.get("task_verified") is True,
                        **({"reason_code": stop_reason} if stop_reason else {}))
                    self.progress(self.last["state"], tool="general_browser",
                                  credits_reconciled=result.get("state") == "stopped")
                except Exception:
                    self.progress(self.last["state"], tool="general_browser", credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def submit_app_navigation_result(self, observed_url: str) -> bool:
        """Accept one result from the signed Tauri owner, never from the WebView."""
        if (self.last.get("state") != "awaiting_native_navigation"
                or self.app_navigation_event.is_set() or not isinstance(observed_url, str)
                or len(observed_url) > 1000):
            return False
        self.app_navigation_result = observed_url
        self.app_navigation_event.set()
        return True

    def run_app_navigation(self, saved, manifest):
        claimed = False
        outcome = "execution_unconfirmed"
        verification = manifest.get("app_navigation_verification")
        observed_url = None
        try:
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.companyos.app"]
                    or manifest["origins"] or manifest["actions"] != ["app.navigate"]
                    or not isinstance(verification, dict)
                    or verification.get("operation") != "open_venture_screen_v1"
                    or verification.get("route") not in {"builds", "settings"}):
                raise ValueError("app_navigation_scope_unsupported")
            self.progress("claiming", tool="app_navigation")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self.progress("awaiting_native_navigation", tool="app_navigation",
                          target_route=verification["route"])
            while not self.app_navigation_event.wait(0.25):
                if self.cancelled.is_set() or self._deadline_expired():
                    break
                self.authorize(self.gateway.order_id, "app_navigation")
            if self.cancelled.is_set() or self._deadline_expired():
                raise ValueError("app_navigation_cancelled_or_expired")
            observed_url = self.app_navigation_result
            parsed = urlsplit(observed_url or "")
            expected_path = f"/v/{self.gateway.venture_id}/{verification['route']}"
            if (parsed.scheme != "https" or parsed.hostname not in
                    {"app.supymem.com", "app.staging.supymem.com"}
                    or parsed.port not in (None, 443)
                    or parsed.path != expected_path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password):
                raise ValueError("app_navigation_not_verified")
            self.gateway.current()
            self.last["task_verified"] = True
            outcome = "verified"
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_review"
            failure_code = public_exception_code(error, "native_execution_failed")
            self.last["failure_code"] = failure_code
        finally:
            try:
                self.progress("closing", tool="app_navigation", outcome=outcome)
            except Exception:
                pass
            self.closed = claimed or not self.gateway.claim_attempted
            evidence = {"order_id": self.gateway.order_id,
                        "execution_id": self.gateway.execution_id,
                        "outcome": outcome, "task_verified": self.last.get("task_verified") is True,
                        "observed_url": observed_url,
                        "target_route": verification.get("route") if isinstance(verification, dict) else None,
                        "processes_stopped": True}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            self.progress("stopped" if claimed else "not_started", tool="app_navigation",
                          evidence=evidence, evidence_digest=digest)
            if claimed:
                try:
                    settled = self.gateway.stop_receipt(
                        digest, True, task_verified=self.last.get("task_verified") is True)
                    self.progress(self.last["state"], tool="app_navigation",
                                  credits_reconciled=settled.get("state") == "stopped")
                except Exception:
                    self.progress(self.last["state"], tool="app_navigation",
                                  credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_terminal(self, saved, manifest):
        tracker = None
        claimed = False
        outcome = "execution_unconfirmed"
        result = None
        try:
            verification = manifest.get("terminal_verification")
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.companyos.app"]
                    or manifest["origins"] or manifest["actions"] != ["terminal.execute"]
                    or not isinstance(verification, dict)
                    or verification.get("operation") != "git_status_v1"):
                raise ValueError("terminal_scope_unsupported")
            tracker = OwnedProcesses()
            self.progress("claiming", tool="terminal")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            terminal = OwnedTerminal(venture_id=self.gateway.venture_id,
                workspace_slug=verification["workspace_slug"], tracker=tracker,
                authorize=lambda: self.authorize(self.gateway.order_id, "terminal"))
            self.progress("running_terminal", tool="terminal")
            result = terminal.git_status(self.deadline)
            if result.get("workspace_marker_verified") is not True:
                raise ValueError("terminal_workspace_verification_failed")
            self.last["task_verified"] = True
            outcome = "verified"
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            failure_code = public_exception_code(error, "native_execution_failed")
            self.last["failure_code"] = failure_code
        finally:
            try:
                self.progress("closing", tool="terminal", outcome=outcome)
            except Exception:
                pass
            if tracker:
                tracker.capture()
                tracker.stop_all()
            stopped = bool(tracker and tracker.stopped())
            self.closed = stopped or not self.gateway.claim_attempted
            evidence = {"order_id": self.gateway.order_id, "execution_id": self.gateway.execution_id,
                        "processes_stopped": stopped, "inventory": tracker.receipt() if tracker else None,
                        "outcome": outcome, "task_verified": self.last.get("task_verified") is True,
                        "terminal": result}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            terminal_state = "not_started" if not self.gateway.claim_attempted else "stopped" if stopped else "reconciliation_required"
            self.progress(terminal_state, tool="terminal", evidence=evidence, evidence_digest=digest)
            if claimed:
                try:
                    settled = self.gateway.stop_receipt(
                        digest, stopped, task_verified=self.last.get("task_verified") is True)
                    self.progress(self.last["state"], tool="terminal",
                                  credits_reconciled=settled.get("state") == "stopped")
                except Exception:
                    self.progress(self.last["state"], tool="terminal", credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_inspect(self, saved, manifest):
        claimed = False
        outcome = "execution_unconfirmed"
        result = None
        try:
            verification = manifest.get("inspection_verification")
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.companyos.app"]
                    or manifest["origins"] or manifest["actions"] != ["editor.read"]
                    or not isinstance(verification, dict)
                    or verification.get("operation") != "workspace_inspect_v1"
                    or verification.get("provider") != "cursor"
                    or verification.get("read_scope") != "venture_workspace"
                    or verification.get("write_scope") != "none"):
                raise ValueError("inspect_scope_unsupported")
            if not owned_inspect.cursor_installed():
                raise ValueError("coding_provider_not_installed")
            self.progress("claiming", tool="inspect")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self.progress("running_inspect", tool="inspect")
            result = owned_inspect.OwnedInspect(venture_id=self.gateway.venture_id,
                workspace_slug=verification["workspace_slug"]).inventory()
            if (result.get("workspace_marker_verified") is not True
                    or result.get("writes_performed") is not False
                    or result.get("cursor_edit_session") is not False
                    or result.get("git_unchanged") is not True):
                raise ValueError("inspect_workspace_verification_failed")
            self.last["task_verified"] = True
            outcome = "verified"
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            failure_code = public_exception_code(error, "native_execution_failed")
            self.last["failure_code"] = failure_code
        finally:
            try:
                self.progress("closing", tool="inspect", outcome=outcome)
            except Exception:
                pass
            self.closed = True
            evidence = {"order_id": self.gateway.order_id,
                        "execution_id": self.gateway.execution_id,
                        "processes_stopped": True, "outcome": outcome,
                        "task_verified": self.last.get("task_verified") is True,
                        "inspect": result}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            self.progress("stopped" if claimed else "not_started", tool="inspect",
                          evidence=evidence, evidence_digest=digest)
            if claimed:
                try:
                    settled = self.gateway.stop_receipt(
                        digest, True, task_verified=self.last.get("task_verified") is True)
                    self.progress(self.last["state"], tool="inspect",
                                  credits_reconciled=settled.get("state") == "stopped")
                except Exception:
                    self.progress(self.last["state"], tool="inspect",
                                  credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_coding(self, saved, manifest):
        self.last["tool"] = "coding"
        tracker = None
        coding = None
        claimed = False
        outcome = "execution_unconfirmed"
        result = None
        try:
            verification = manifest.get("coding_verification")
            if (saved["state"] not in {"approved", "waiting_for_device"} or manifest["applications"] != ["com.companyos.app"]
                    or manifest["origins"] or manifest["actions"] != ["editor.edit", "terminal.execute"]
                    or not isinstance(verification, dict)
                    or verification.get("operation") != "workspace_edit_v1"
                    or verification.get("read_scope") != "device_filesystem"
                    or verification.get("write_scope") != "venture_workspace"
                    or verification.get("command_network") != "disabled"):
                raise ValueError("coding_scope_unsupported")
            if not _coding_offline_profile_admitted(saved, verification):
                raise ValueError("coding_offline_profile_unavailable")
            candidate = verification.get("pagebuilder_candidate")
            if candidate is not None:
                from desktop_runtime.pagebuilder_reference import validate_binding
                candidate = validate_binding(candidate, venture_id=self.gateway.venture_id,
                                             workspace_id=saved.get("workspace_id"))
            tracker = OwnedProcesses()
            # Resolve provider support before claiming reviewed execution authority.
            coding = OwnedCoding(venture_id=self.gateway.venture_id,
                workspace_slug=verification["workspace_slug"],
                provider=verification["provider"], tracker=tracker,
                authorize=lambda: self.authorize(self.gateway.order_id, "coding"),
                cancelled=self.cancelled.is_set)
            if verification["provider"] not in {"codex", "claude", "cursor", "opencode"}:
                raise ValueError("coding_provider_not_yet_supported")
            coding.preflight()
            self.progress("claiming", tool="coding", provider=verification["provider"])
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self._cursor_route = "coding"
            if candidate is not None:
                blob = self.gateway.pagebuilder_reference(candidate, stop=self.cancelled, deadline=self.deadline)
                coding.stage_pagebuilder_reference(candidate, blob, self.deadline)
            self.progress("running_coding", tool="coding", provider=verification["provider"])
            from desktop_runtime import coding_loop, loop_diagnose
            coding_workspace = coding._workspace()
            coding.line_callback = lambda line: coding_loop._event(self, text, coding_workspace) if (
                text := loop_diagnose.stream_text(line, coding.provider, coding_workspace)) else None
            result = coding.run(manifest["instruction"], self.deadline)
            # A clean, bounded change is independently verified. Natural-language
            # plan acceptance remains unproven until its dedicated QA verifier runs.
            outcome = "changes_require_acceptance_verification"
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            result = coding_failure_details(error, getattr(coding, "stage", self.last.get("state")),
                provider=getattr(coding, "provider", None))
            self.last.update(result)
            if coding is not None and getattr(coding, "failure_message", None):
                self.last["message"] = coding.failure_message
        finally:
            try:
                self.progress("closing", tool="coding", outcome=outcome)
            except Exception:
                pass
            if tracker:
                tracker.capture()
                tracker.stop_all()
            pre_spawn_stopped = bool(coding is not None and not coding.spawn_attempted
                                     and tracker is not None and tracker.complete
                                     and not tracker.observed)
            stopped = pre_spawn_stopped or bool(tracker and tracker.stopped())
            if stopped and coding is not None:
                coding.clear_temporary()
            self.closed = stopped or not self.gateway.claim_attempted
            evidence = {"order_id": self.gateway.order_id,
                        "execution_id": self.gateway.execution_id,
                        "processes_stopped": stopped,
                        "inventory": tracker.receipt() if tracker else None,
                        "outcome": outcome, "task_verified": False,
                        "coding": result}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            terminal_state = ("not_started" if not self.gateway.claim_attempted else
                              "stopped" if stopped else "reconciliation_required")
            self.progress(terminal_state, tool="coding", provider=(manifest.get(
                "coding_verification") or {}).get("provider"), evidence=evidence,
                evidence_digest=digest, task_verified=False)
            if claimed:
                try:
                    settled = self.gateway.stop_receipt(
                        digest, stopped, task_verified=self.last.get("task_verified") is True)
                    self.progress(self.last["state"], tool="coding",
                                  credits_reconciled=settled.get("state") == "stopped")
                    if (outcome == "changes_require_acceptance_verification" and stopped
                            and result is not None and settled.get("state") == "stopped"):
                        try:
                            source = source_receipt.capture(
                                venture_id=self.gateway.venture_id,
                                workspace_id=saved["workspace_id"],
                                actor_id=saved["actor_id"],
                                device_id=saved["device_id"],
                                plan_task_id=saved["task_snapshot"]["task_id"],
                                workspace_slug=verification["workspace_slug"],
                                order_id=self.gateway.order_id,
                                execution_id=self.gateway.execution_id,
                                provider=verification["provider"],
                            )
                            self.progress(self.last["state"], tool="coding", source_receipt=source)
                            try:
                                stored = self.gateway.store_coding_source(
                                    source, source_receipt.load_archive(source))
                                self.progress(self.last["state"], tool="coding",
                                              source_storage_state=stored["state"],
                                              source_storage_sha256=stored["sha256"])
                            except Exception:
                                # Local custody survives a failed or uncertain upload.
                                # This never marks the coding task verified or retries it.
                                self.progress(self.last["state"], tool="coding",
                                              source_storage_state="needs_reconciliation")
                        except Exception as error:
                            code = public_exception_code(error, "source_capture_failed")
                            self.progress(self.last["state"], tool="coding", source_capture_error=code)
                except Exception:
                    self.progress(self.last["state"], tool="coding", credits_reconciled=False)
                self._notify_terminal(outcome, manifest=manifest)

    def run_coding_loop(self, saved, manifest):
        self.last["tool"] = "coding"
        from desktop_runtime import agent_status
        from desktop_runtime.coding_loop import run_coding_loop
        descriptor = manifest.get("coding_loop_verification") or {}
        tracker, coding, claimed, result = None, None, False, None
        outcome = "execution_unconfirmed"
        local_recipes = {"static_landing_v1": "static_landing_checks_v1", "workspace_utility_v1": "workspace_utility_checks_v1"}
        requested_profile = descriptor.get("profile")
        local_recipe = local_recipes.get(requested_profile) if is_local() and isinstance(requested_profile, str) else None
        profile = requested_profile if local_recipe else "web_app_vercel_v1"
        self.last["product_profile"] = profile
        try:
            if (saved.get("state") not in {"approved", "waiting_for_device"} or manifest.get("applications") != ["com.companyos.app"]
                    or manifest.get("origins") or manifest.get("actions") != ["editor.edit", "terminal.execute"]
                    or manifest.get("credit_ceiling_micro") != 0
                    or descriptor.get("operation") != "workspace_loop_v1"
                    or descriptor.get("profile") != profile
                    or descriptor.get("check_recipe") != (local_recipe or "web_app_vercel_checks_v1")
                    or descriptor.get("network") != "none"
                    or descriptor.get("read_scope") != "venture_workspace"
                    or descriptor.get("write_scope") != "venture_workspace"
                    or descriptor.get("phase") not in {"plan", "implement"}
                    or (saved.get("task_snapshot") or {}).get("artifact_profile") != profile
                    or local_recipe and descriptor.get("phase") != "implement"
                    or profile == "workspace_utility_v1" and (descriptor.get("max_iterations") != 1
                        or descriptor.get("plan_order_id") is not None or descriptor.get("plan_digest") is not None)):
                raise ValueError("coding_loop_scope_unsupported")
            status = agent_status._status(descriptor.get("provider"))
            if is_local() and status.get("unsafe_location_message"):
                raise ValueError("coding_provider_unsafe_location")
            if is_local() and status.get("build_blocked_message"):
                raise ValueError("coding_git_not_installed")
            if is_local() and status.get("changed") is True:
                raise ValueError("coding_provider_changed")
            if status.get("available") is not True:
                raise ValueError("coding_sign_in_required" if status.get("installed") else "coding_provider_not_installed")
            if descriptor["phase"] == "implement" and not local_recipe:
                from desktop_runtime.loop_checks import preflight
                if not preflight():
                    raise ValueError("coding_checks_unavailable")
            tracker = OwnedProcesses()
            coding = OwnedCoding(venture_id=self.gateway.venture_id,
                workspace_slug=descriptor["workspace_slug"], provider=descriptor["provider"],
                tracker=tracker, authorize=lambda: self.authorize(self.gateway.order_id, "coding"),
                cancelled=self.cancelled.is_set)
            self._product_coding = coding
            self._product_manifest = manifest
            coding.preflight()
            if is_local():
                from desktop_runtime.device_gateway import build_capability_ready
                if not build_capability_ready():
                    raise ValueError("coding_capability_disabled")
            self.progress("claiming", tool="coding", provider=descriptor["provider"])
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self._cursor_route = "coding"
            self.loop_saved = saved
            result = run_coding_loop(self, manifest, self.gateway, coding, self.deadline)
            outcome = result.get("outcome", result.get("final_outcome", "execution_unconfirmed"))
            self.last["task_verified"] = result.get("task_verified") is True
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            stage = self.last.get("loop_phase")
            if stage != "check":
                stage = getattr(coding, "stage", stage or self.last.get("state"))
            result = coding_failure_details(error, stage, provider=descriptor.get("provider"))
            if str(error) in _PRODUCT_FAILURES:
                result["failure_code"] = str(error)
            self.last.update(result)
            if stage == "check":
                from desktop_runtime.coding_loop import _event
                _event(self, result["message"], None, exception_frames=result.get("exception_frames"))
            if is_local():
                if str(error) == "coding_capability_disabled":
                    try:
                        from app.core.local_capabilities import blocked_message
                        self.last["message"] = blocked_message("build_with_my_tool")
                    except (ImportError, KeyError):
                        self.last["message"] = "Open Settings > Capabilities and switch on Build with my tool, then ask Charlie to build again."
                elif str(error) == "coding_checks_unavailable":
                    from desktop_runtime.loop_checks import checks_next_step
                    self.last["message"] = checks_next_step()
                elif str(error) in {"coding_sign_in_required", "coding_provider_not_installed", "coding_auth_required"}:
                    self.last["message"] = "Open Settings > Keys and connections > Coding tools, connect and sign in to your coding tool, then retry from Build activity."
        finally:
            self._finish_product_execution(manifest, tracker, claimed, result, outcome, coding)

    def next_requested_build(self):
        descriptor = ((getattr(self, "loop_saved", {}) or {}).get("manifest") or {}).get("coding_loop_verification") or {}
        evidence = self.last.get("evidence") or {}
        if (not is_local() or not (getattr(self, "loop_saved", {}) or {}).get("requested_build")
                or descriptor.get("phase") != "plan" or not self.closed
                or self.cancelled.is_set() or self.stop_after_iteration.is_set()
                or self.last.get("state") != "stopped" or self.last.get("task_verified") is not True
                or evidence.get("outcome") != "plan_ready" or evidence.get("processes_stopped") is not True
                or self.last.get("plan_posted") is not True or not self.last.get("plan")
                or self.last.get("journal_unconfirmed") or self.last.get("next_order_id")
                or any(not row.get("posted") for row in self.last.get("iteration_receipts", []))):
            return None
        order = self.gateway.prepare_product_run(self.last["plan"])
        self.progress("stopped", next_order_id=order["id"])
        return order

    def run_deploy(self, saved, manifest):
        from desktop_runtime.owned_deploy import OwnedDeploy
        descriptor = manifest.get("deploy_verification") or {}
        tracker, deploy, claimed, result = None, None, False, None
        outcome = "execution_unconfirmed"
        self.last["product_profile"] = "web_app_vercel_v1"
        try:
            if (saved.get("state") not in {"approved", "waiting_for_device"} or manifest.get("applications") != ["com.companyos.app"]
                    or manifest.get("origins") or manifest.get("actions") != ["terminal.execute"]
                    or manifest.get("credit_ceiling_micro") != 0
                    or (saved.get("task_snapshot") or {}).get("artifact_profile") != "web_app_vercel_v1"
                    or descriptor.get("operation") != "workspace_deploy_v1"
                    or descriptor.get("provider") != "vercel" or descriptor.get("target") not in {"preview", "production"}):
                raise ValueError("deploy_scope_unsupported")
            tracker = OwnedProcesses()
            deploy = OwnedDeploy(venture_id=self.gateway.venture_id, verification=descriptor,
                tracker=tracker, authorize=lambda: self.authorize(self.gateway.order_id, "deploy"),
                cancelled=self.cancelled.is_set)
            deploy.preflight(check_login=True)
            self.progress("claiming", tool="deploy", provider="vercel")
            self._claim(saved, manifest)
            claimed = True
            self.deadline = time.monotonic() + manifest["duration_seconds"]
            self.progress("deploying", tool="deploy", provider="vercel")
            result = deploy.run(self.deadline)
            verified = result.get("http_status") == result.get("health_status") == 200
            protected = descriptor.get("target") == "preview" and result.get("protected") is True
            outcome = "deployed" if verified else "protected" if protected else "deployment_requires_live_verification"
            receipt = {key: result[key] for key in ("provider", "target", "url", "source_commit",
                "file_manifest_sha256", "http_status", "health_status", "project_link_sha256")}
            receipt.update(status="deployed" if verified else "protected" if protected else "unverified",
                           exit_status=result["exit_code"])
            receipt.update({key: result[key] for key in ("availability_status", "functional_status", "http_journey") if key in result})
            self.last["task_verified"] = verified or protected
            self.progress("deploying", tool="deploy", deploy_receipt=receipt, deploy_receipt_posted=False)
            self.gateway.deploy_receipt(receipt)
            self.progress("deploying", deploy_receipt_posted=True)
        except Exception as error:
            outcome = "cancelled" if self.cancelled.is_set() else "execution_requires_reconciliation"
            self.last["failure_code"] = str(error) if str(error) in _PRODUCT_FAILURES else public_exception_code(error, "native_execution_failed")
        finally:
            self._finish_product_execution(manifest, tracker, claimed, result, outcome, deploy)

    def start_product_preview(self, order=None):
        from desktop_runtime.coding_loop import start_product_preview
        with self._product_preview_lock:
            manifest = order.get("manifest", {}) if order is not None else getattr(self, "_product_manifest", {})
            descriptor = manifest.get("coding_loop_verification") or {}
            evidence = self.last.get("evidence") or {}
            build = evidence.get("product_build") or {}
            if (descriptor.get("profile") != "web_app_vercel_v1" or descriptor.get("phase") != "implement"
                    or descriptor.get("provider") not in {"claude", "codex", "cursor", "opencode"}
                    or self.closed is not True or self.last.get("state") != "stopped"
                    or not is_local() and self.last.get("credits_reconciled") is not True
                    or self.last.get("task_verified") is not True
                    or self.cancelled.is_set() or self.last.get("journal_unconfirmed")
                    or callable(self._preview_activity_current) and self._preview_activity_current() is not True
                    or any(self.last.get(key) for key in ("actual_usage_unconfirmed", "effect_outcome_unconfirmed"))
                    or evidence.get("order_id") != self.gateway.order_id
                    or evidence.get("execution_id") != self.gateway.execution_id
                    or evidence.get("processes_stopped") is not True or build.get("outcome") != "pass"
                    or build.get("provider") != descriptor.get("provider")
                    or hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        != self.last.get("evidence_digest")):
                raise ValueError("native_product_preview_unavailable")
            if order is not None:
                native_stop = order.get("native_stop") or {}
                if (order.get("id") != self.gateway.order_id or order.get("venture_id") != self.gateway.venture_id
                        or order.get("device_id") != self.gateway.device_id or order.get("state") != "stopped"
                        or not order.get("closed_at") or native_stop.get("execution_id") != evidence["execution_id"]
                        or order.get("execution_id") != evidence["execution_id"]
                        or native_stop.get("evidence_digest") != self.last["evidence_digest"]
                        or native_stop.get("task_verified") is not True or native_stop.get("processes_stopped") is not True):
                    raise ValueError("native_product_preview_unavailable")
                self._product_preview_disabled = False
            elif self._product_preview_disabled:
                return self.last.get("product_preview") or {"status": "stopped", "ready": False}
            previous = self.last.get("product_preview") or {}
            if (getattr(self, "product_preview", None) is None and previous.get("status") in {"ready", "expired", "failed"}
                    and previous.get("processes_stopped") is not True):
                raise ValueError("native_product_preview_unavailable")
            coding = getattr(self, "_product_coding", None) or OwnedCoding(venture_id=self.gateway.venture_id,
                workspace_slug=descriptor["workspace_slug"], provider=descriptor["provider"], tracker=None,
                authorize=lambda: False)
            receipt = start_product_preview(self, coding, build)
            try:
                if callable(self._preview_activity_current) and self._preview_activity_current() is not True:
                    receipt = self.product_preview.stop(status="expired")
                self.progress("stopped", product_preview={**receipt, "transport": "device_order_v1"})
            except Exception:
                self.stop_product_preview()
                raise
            return self.last["product_preview"]

    def product_preview_status(self):
        with self._product_preview_lock:
            owner = getattr(self, "product_preview", None)
            previous = self.last.get("product_preview")
            if (owner is None and previous and previous.get("status") in {"ready", "failed", "expired"}
                    and (previous.get("status") == "ready" or previous.get("processes_stopped") is not True)):
                receipt = {**previous, "status": "expired", "ready": False, "stop_available": False}
                receipt.pop("url", None)
                return receipt
            if owner is not None:
                receipt = {**owner.status(), "transport": "device_order_v1"}
                if receipt != self.last.get("product_preview"):
                    try:
                        self.progress(self.last["state"], product_preview=receipt)
                    except Exception:
                        self.stop_product_preview()
                        self.last["journal_unconfirmed"] = True
            return self.last.get("product_preview")

    def stop_product_preview(self):
        with self._product_preview_lock:
            self._product_preview_disabled = True
            owner = getattr(self, "product_preview", None)
            if owner is None:
                return self.product_preview_status()
            receipt = {**owner.stop(), "transport": "device_order_v1"}
            try:
                self.progress(self.last["state"], product_preview=receipt)
            except Exception:
                self.last["product_preview"] = receipt
                self.last["journal_unconfirmed"] = True
            return receipt

    def _finish_product_execution(self, manifest, tracker, claimed, result, outcome, executor):
        try:
            self.progress("closing", outcome=outcome)
        except Exception:
            pass  # Journal failure cannot skip process cleanup.
        stopped = False
        inventory = None
        if tracker:
            try:
                tracker.capture()
                tracker.stop_all()
                pre_spawn = bool(executor is not None and not executor.spawn_attempted
                                 and tracker.complete and not tracker.observed)
                stopped = pre_spawn or tracker.stopped()
                inventory = tracker.receipt()
            except Exception:
                # Unknown process custody is never a clean Stop or a verified app.
                try:
                    tracker.stop_all()
                except Exception:
                    pass
        self.closed = stopped or not self.gateway.claim_attempted
        if self.cancelled.is_set():
            outcome = "cancelled"
            self.last["task_verified"] = False
            if result is not None:
                result = {**result, "outcome": "stopped", "final_outcome": "stopped", "task_verified": False,
                          "task_acceptance_verified": False}
        verified = bool(stopped and self.last.get("task_verified") is True)
        evidence = {"order_id": self.gateway.order_id, "execution_id": self.gateway.execution_id,
            "processes_stopped": stopped, "inventory": inventory,
            "outcome": outcome, "task_verified": verified,
            "product_build": {key: value for key, value in (result or {}).items() if key != "plan"}}
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        state = "not_started" if not self.gateway.claim_attempted else "stopped" if stopped else "reconciliation_required"
        try:
            self.progress(state, evidence=evidence, evidence_digest=digest, task_verified=verified)
        except Exception:
            self.last.update(state=state, task_verified=verified, evidence=evidence, evidence_digest=digest,
                             journal_unconfirmed=True)
        if (is_local() and not claimed and not self.gateway.claim_attempted
                and manifest.get("coding_loop_verification") and self.last.get("failure_code")
                and (executor is None or not executor.spawn_attempted)):
            failure = self.last["failure_code"]
            failure = failure if failure in _PRODUCT_FAILURES and failure.startswith("coding_") else "native_execution_failed"
            try:
                self.gateway.fail_before_start(failure)
                self.progress("not_started", failure_finalized=True)
            except Exception:
                self.last.update(failure_finalized=False,
                    message="Open Build activity for your failed build and choose Cancel, then retry your request.")
        if claimed:
            if manifest.get("coding_loop_verification"):
                from desktop_runtime.coding_loop import post_pending
                post_pending(self, self.gateway)
            saved = getattr(self, "loop_saved", None)
            descriptor = manifest.get("coding_loop_verification") or {}
            landing_archive = None
            if is_local() and stopped and verified and saved and descriptor.get("profile") == "static_landing_v1":
                try:
                    from core.artifact_snapshot import manifest_digest
                    from desktop_runtime.landing_loop import checked_archive, source

                    files, archive = source(executor._workspace())
                    if manifest_digest(files) != (result or {}).get("file_manifest_sha256"):
                        raise ValueError("pagebuilder_tool_result_changed")
                    checked_archive(self.gateway.venture_id, self.gateway.order_id, self.gateway.execution_id, blob=archive)
                    landing_archive = archive
                except Exception:
                    self.last["message"] = "Your page could not be saved. Check space in Settings > Keys and connections > Your data folder and backup, then open Website."
            try:
                settled = self.gateway.stop_receipt(digest, stopped, task_verified=verified)
                self.last["credits_reconciled"] = settled.get("state") == "stopped"
            except Exception:
                self.last["credits_reconciled"] = False
            if stopped and manifest.get("coding_loop_verification") and getattr(self, "loop_saved", None):
                saved = self.loop_saved
                descriptor = manifest["coding_loop_verification"]
                if landing_archive is not None and self.last.get("credits_reconciled") is True:
                    try:
                        self.last["page_result"] = self.gateway.landing_result(
                            saved["task_snapshot"]["pagebuilder_job_id"], landing_archive)
                    except Exception:
                        self.last["message"] = "Your checked page could not be saved. On Website, choose Retry saving. Your previous page is unchanged."
                try:
                    # Local immutable custody also survives failed/Stopped tries.
                    # Product source is not sent to the static landing publisher.
                    self.last["source_receipt"] = source_receipt.capture(
                        venture_id=self.gateway.venture_id, workspace_id=saved["workspace_id"],
                        actor_id=saved["actor_id"], device_id=saved["device_id"],
                        plan_task_id=saved["task_snapshot"]["task_id"], workspace_slug=descriptor["workspace_slug"],
                        order_id=self.gateway.order_id, execution_id=self.gateway.execution_id,
                        provider=descriptor["provider"])
                except Exception:
                    self.last["source_capture_error"] = "source_capture_failed"
        if stopped and isinstance(executor, OwnedCoding):
            executor.clear_temporary()
        if claimed:
            try:
                self.progress(state)
            except Exception:
                self.last["journal_unconfirmed"] = True
            if (not is_local() and verified and outcome == "pass" and (manifest.get("coding_loop_verification") or {}).get("phase") == "implement"
                    and self.last.get("product_profile") != "workspace_utility_v1"
                    and self.last.get("credits_reconciled") is True
                    and not self.last.get("journal_unconfirmed")):
                try:
                    self.start_product_preview()
                except (ValueError, OSError, RuntimeError) as error:
                    from core.local_diagnostics import write_error
                    write_error(error, code=public_exception_code(error, "native_product_preview_unavailable"))
                    self.last["product_preview"] = self.stop_product_preview() or {
                        "status": "unavailable", "ready": False, "transport": "device_order_v1"}
                    try:
                        self.progress(state)
                    except Exception:
                        self.last["journal_unconfirmed"] = True
            self._notify_terminal(outcome, manifest=manifest)
