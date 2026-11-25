"""QA focus timeline: only source/PID/phase/timing/codes, no content."""
import hashlib
from functools import wraps
import re
import threading
import time

from cu_input_decoy import snapshot_valid


class FocusTrace:
    def __init__(self, decoy, frontmost, *, interval=.25, max_samples=4000):
        self.decoy, self.frontmost = decoy, frontmost
        self.interval, self.max_samples = interval, max_samples
        self.samples, self.actions, self.phases = [], [], []
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.phase, self.action_index = "backend_setup", 0
        self.overflow = False
        self.execution = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="supymem-qa-focus")

    def mark(self, metadata):
        # Public phase/tool enumerations only; never copy model parameters/errors.
        phase = metadata.get("phase")
        if phase not in {"backend_setup", "model", "observe", "act", "verify", "complete", "driver", "verification", "teardown"}:
            phase = "model"
        with self.lock:
            if len(self.phases) < self.max_samples:
                self.phases.append({"at_monotonic": time.monotonic(), "phase": phase})
            else:
                self.overflow = True
            self.phase = ("observe" if metadata.get("tool") in {"get_window_state", "list_windows"} else "act") if phase == "driver" else phase
            if phase == "driver":
                incoming = metadata.get("action_index")
                if type(incoming) is int and incoming > 0:
                    self.action_index = incoming
                else:
                    self.action_index += 1
                if len(self.actions) < self.max_samples:
                    row = {"at_monotonic": time.monotonic(), "action_index": self.action_index}
                    for key in ("tool", "target_pid", "window_id", "delivery", "driver_code", "driver_code_id", "effect", "event_stage"):
                        if key in metadata:
                            row[key] = metadata[key]
                    self.actions.append(row)
                else:
                    self.overflow = True

    def sample(self):
        at = time.monotonic()
        try:
            cg_pid = self.frontmost()[1]
            cg_pid = cg_pid if type(cg_pid) is int else None
        except Exception:
            cg_pid = None
        snap = self.decoy.read()
        valid = snapshot_valid(snap, self.decoy.instance, self.decoy.process.pid)
        with self.lock:
            row = {"at_monotonic": at, "phase": self.phase, "action_index": self.action_index,
                   "cg_source": "cg_top_normal_window", "cg_pid": cg_pid,
                   "appkit_source": "decoy_main_runloop", "appkit_valid": valid,
                   "active_app_pid": snap.get("active_app_pid") if valid and type(snap.get("active_app_pid")) is int else None,
                   "decoy_is_active": snap.get("is_active") if valid and type(snap.get("is_active")) is bool else None,
                   "snapshot_sample": snap.get("samples") if valid else None,
                   "snapshot_age_ms": round(1000 * (time.time() - snap["updated_at"])) if valid else None}
            if self.execution is not None:
                row["execution_span_ids"] = self.execution.active_ids()
            if len(self.samples) < self.max_samples:
                self.samples.append(row)
            else:
                self.overflow = True
        return row

    def _run(self):
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)

    def start(self):
        self.sample()  # explicit first sample, no thread scheduling blind spot
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        joined = not self.thread.is_alive()
        if joined:
            self.sample()
        return joined

    def verified(self, joined):
        return bool(joined) and self.samples_verified()

    def samples_verified(self):
        rows = list(self.samples)
        if self.overflow or len(rows) < 2:
            return False
        own = self.decoy.process.pid
        for row in rows:
            if (row["cg_pid"] != own or not row["appkit_valid"]
                    or row["active_app_pid"] != own or row["decoy_is_active"] is not True):
                return False
        # Missing/late sampler coverage never counts as all-frontmost proof.
        return all(0 <= b["at_monotonic"] - a["at_monotonic"] <= 2
                   for a, b in zip(rows, rows[1:]))


PUBLIC_CODES = {"ax_unresolved", "same_process", "same_pid", "off_space", "pid_keyboard",
    "background_input", "window_pointer", "ambiguity", "type_text_incomplete", "element_outside",
    "element_not_found", "element_token_invalid", "window_target_not_found", "window_not_found",
    "background_input_unavailable", "pid_keyboard_unavailable", "ax_action_unsupported",
    "ax_attribute_not_settable", "stale_element_token", "element_outside_window"}
PUBLIC_TOOLS = {"launch_app", "list_windows", "get_window_state", "click", "double_click",
    "right_click", "drag", "type_text", "set_value", "hotkey", "press_key", "scroll", "invoke_menu"}


def traced_call(call, trace):
    sequence = 0
    @wraps(call)
    def wrapped(tool, args):
        nonlocal sequence
        sequence += 1
        target = args.get("target") or args
        metadata = {"phase": "driver", "action_index": sequence,
            "tool": tool if tool in PUBLIC_TOOLS else "other",
            "target_pid": target.get("pid") if type(target.get("pid")) is int else None,
            "window_id": target.get("window_id") if type(target.get("window_id")) is int else None,
            "delivery": "background" if args.get("delivery_mode") == "background" else "ax_or_lifecycle",
            "event_stage": "start"}
        trace.mark(metadata)
        try:
            result = call(tool, args)
        except Exception:
            trace.mark({**metadata, "event_stage": "end", "driver_code": "transport_exception", "effect": "unknown"})
            raise
        refusal = result.get("refusal") if isinstance(result, dict) else None
        refusal = refusal if isinstance(refusal, dict) else {}
        code = refusal.get("code") or (result.get("code") if isinstance(result, dict) else None)
        effect = result.get("effect") if isinstance(result, dict) else None
        code_id = (hashlib.sha256(code.encode()).hexdigest()[:12]
                   if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else None)
        trace.mark({**metadata, "event_stage": "end", "driver_code_id": code_id,
            "driver_code": code if isinstance(code, str) and code in PUBLIC_CODES else "other_code" if code else "none",
            "effect": effect if isinstance(effect, str) and effect in {"refused", "partial", "unverifiable", "verified", "applied"} else "other"})
        return result
    return wrapped
