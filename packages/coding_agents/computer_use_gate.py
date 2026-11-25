"""Host-side gate around a desktop driver; never expose it as model authority.

The host must supply authenticated, current scope validation and durable one-use
admission. These hooks are mandatory, including for observations. This wrapper
must replace the raw tool handler in a controlled session; merely constructing it
elsewhere does not secure an existing Hermes tool registration.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

READS = frozenset({"capture", "wait"})
EFFECTS = frozenset({"click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value", "focus_app"})


class EffectUncertain(RuntimeError):
    """An admitted driver call did not return; reconcile rather than retry."""

    def __init__(self, message: str, *, driver_code: str = "driver_failure_unclassified",
                 driver_detail: str | None = None):
        super().__init__(message)
        self.driver_code = driver_code
        self.driver_detail = driver_detail


class ComputerUseGate:
    def __init__(self, *, order_id: str, session_id: str, driver: Callable,
                 authorize: Callable | None = None, admit_effect: Callable | None = None):
        self.order_id, self.session_id = order_id, session_id
        self.driver, self.authorize, self.admit_effect = driver, authorize, admit_effect

    def invoke(self, invocation_id: str, arguments: dict):
        """authorize checks scope/focus/deadline; admission consumes an exact receipt.

        Hooks receive independent copies, so edits made by a hook cannot change
        what is sent to the driver. Neither a truthy object nor a missing callback
        counts as authorization. Credentials and provider pricing are not accepted.
        """
        if not callable(self.authorize) or not callable(self.admit_effect):
            raise PermissionError("computer_use_host_gate_missing")
        if (not isinstance(arguments, dict) or not isinstance(invocation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invocation_id)
                or not self.order_id or not self.session_id):
            raise ValueError("computer_use_request_invalid")
        raw = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 32768:
            raise ValueError("computer_use_request_bounds")
        payload = json.loads(raw)
        action, application = payload.get("action"), payload.get("app")
        if (not isinstance(action, str) or action not in READS | EFFECTS
                or not isinstance(application, str)
                or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", application)
                or any(key in payload for key in ("pid", "window_id", "session_id", "auto_approve", "approval"))):
            raise ValueError("computer_use_explicit_app_required")
        # Background work never escalates through model arguments. Foreground
        # control requires a separate reviewed host path; general tool approval
        # is not permission to interrupt the founder's active application.
        if (payload.get("delivery_mode", "background") != "background"
                or payload.get("bring_to_front", False) is not False
                or payload.get("raise_window", False) is not False):
            raise PermissionError("computer_use_foreground_permission_required")
        payload["delivery_mode"] = "background"
        payload["bring_to_front"] = False
        payload["raise_window"] = False
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(json.dumps({"order": self.order_id, "session": self.session_id,
            "invocation": invocation_id, "arguments": payload}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        def authorize():
            try:
                allowed = self.authorize(self.order_id, self.session_id, json.loads(raw))
            except Exception:
                raise PermissionError("computer_use_scope_unavailable") from None
            if allowed is not True:
                raise PermissionError("computer_use_scope_denied")
        authorize()
        if action in EFFECTS:
            # The host's durable service binds this digest to the exact reviewed
            # action and consumes it before I/O. It must return False on replay.
            if self.admit_effect(self.order_id, self.session_id, invocation_id, digest) is not True:
                raise PermissionError("computer_use_effect_not_admitted")
            authorize()  # Admission may involve an approval/network wait.
        try:
            return self.driver(json.loads(raw), session_id=self.session_id)
        except Exception as error:
            if action in EFFECTS:
                # Preserve only a bounded machine code in the durable receipt.
                # It explains pre-dispatch failures without treating an admitted
                # effect as retryable or leaking arbitrary driver output.
                raw_code = str(error)
                code = (raw_code if re.fullmatch(r"(?:desktop_|computer_use_|cu_)[A-Za-z0-9_]{1,100}", raw_code)
                        else "driver_failure_unclassified")
                detail = getattr(error, "detail", None)
                raise EffectUncertain("computer_use_effect_outcome_uncertain", driver_code=code,
                                      driver_detail=detail[:200] if isinstance(detail, str) else None) from None
            raw_code = str(error)
            code = (raw_code if re.fullmatch(r"(?:desktop_|computer_use_)[A-Za-z0-9_]{1,100}", raw_code)
                    else "computer_use_observation_failed")
            raise RuntimeError(code) from None
