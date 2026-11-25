"""Pure helpers that keep Mac computer-use text entry, startup and stops predictable.

Nothing here touches the desktop; every decision is testable without a Mac.
Reason codes (``cu_*``) are the stable founder-facing vocabulary that
``app/chat/blockers.py`` maps to guidance.
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable

CU_PERMISSION_MISSING = "cu_permission_missing"
CU_BRIDGE_NOT_READY = "cu_bridge_not_ready"
CU_SESSION_UNAVAILABLE = "cu_session_unavailable"
CU_FIELD_NOT_CLEARED = "cu_field_not_cleared"
CU_FIELD_VALUE_MISMATCH = "cu_field_value_mismatch"
CU_EFFECT_UNVERIFIED = "cu_effect_unverified"
CU_REPEATED_ACTION = "cu_repeated_action"
CU_WINDOW_CHANGED = "cu_window_changed"
CU_RECONCILE_UNAVAILABLE = "cu_reconcile_unavailable"
CU_STOPPED = "cu_stopped"
# Core refused the claim, so nothing started.
CU_CLAIM_REJECTED = "cu_claim_rejected"
CLAIM_REASONS = {
    "device_order_expired": "cu_claim_expired",
    "device_task_has_active_order": "cu_claim_busy",
    "device_order_review_changed": "cu_claim_changed",
    "device_order_task_changed": "cu_claim_changed",
}

# Failures before the first tool call; nothing was dispatched, so a retry is safe.
TRANSIENT_SESSION_CODES = frozenset({
    "desktop_cua_session_unavailable", "desktop_cua_proxy_handshake_unavailable",
    "desktop_cua_transport_unavailable"})

_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")
_SCHEME = re.compile(r"^https?://", re.I)
_AUTOCOMPLETE_MAX = 64


def normalize_entry_text(text: str) -> str:
    """Text as it should be typed: NFC, no control characters or line breaks, trimmed.

    A newline typed into a browser address bar would submit early and a tab
    moves focus, so both are replaced rather than sent.
    """
    cleaned = _CONTROL.sub(" ", unicodedata.normalize("NFC", text)).strip() if isinstance(text, str) else ""
    if not cleaned:
        raise ValueError(CU_FIELD_VALUE_MISMATCH)
    return cleaned


def _canon(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def field_value_matches(expected: str, observed: object) -> bool:
    """Did the field end up holding what was typed?

    Exact after trim/case-fold. A browser may also drop a leading scheme, add a
    trailing slash, or append an inline autocomplete suffix; those are accepted.
    A stray leading character ("ahttps://...") never matches.
    """
    if not isinstance(observed, str):
        return False
    want, got = _canon(expected), _canon(observed)
    if _SCHEME.match(want) or _SCHEME.match(got):
        want, got = _SCHEME.sub("", want).rstrip("/"), _SCHEME.sub("", got).rstrip("/")
    return bool(want) and got.startswith(want) and len(got) - len(want) <= _AUTOCOMPLETE_MAX


def field_is_clear(observed: object) -> bool:
    return isinstance(observed, str) and observed.strip() == ""


def wait_until(check: Callable[[], bool], *, timeout: float, interval: float = 0.25,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> bool:
    """Poll ``check`` until true or ``timeout`` seconds pass. Always checks once."""
    deadline = clock() + timeout
    while True:
        if check():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def retry_transient(make: Callable[[], object], *, attempts: int = 3, delay: float = 1.0,
                    transient: frozenset[str] = TRANSIENT_SESSION_CODES,
                    sleep: Callable[[float], None] = time.sleep):
    """Call ``make`` again only while it fails with a known pre-effect code."""
    for attempt in range(attempts):
        try:
            return make()
        except Exception as error:
            if str(error) not in transient or attempt + 1 == attempts:
                raise
            sleep(delay)


def repeated_type_on_field(history: list[dict], label: str, limit: int = 3) -> bool:
    """True when the last ``limit - 1`` effects were all typing into this same field."""
    recent = history[-(limit - 1):]
    return len(recent) == limit - 1 and all(
        item.get("operation") == "type" and item.get("label") == label for item in recent)


_OUTCOME_REASON = {
    "repeated_action_stopped": CU_REPEATED_ACTION,
    "reviewed_window_changed_before_start": CU_WINDOW_CHANGED,
    "reviewed_window_closed_after_effect": CU_WINDOW_CHANGED,
    "reviewed_window_unavailable": CU_WINDOW_CHANGED,
    "cancelled": CU_STOPPED,
}


def outcome_code(outcome=None, *, verified=False, reason=None, failure=None):
    if verified is True:
        return "ok"
    codes = {value for value in (outcome, reason, failure) if isinstance(value, str)}
    if codes & {"timeout", "timed_out", "wall_time_exceeded", "step_limit_reached", "cu_timeout",
                "cu_time_limit", "cu_wall_timeout", "cu_budget_exhausted", "ai_loop_timeout", "ai_loop_time_limit"}:
        return "timeout"
    if codes & {"auth_lost", "auth_required", "cu_auth_lost", "cu_auth_required", "cu_login_required",
                "cu_session_expired", "native_account_changed", "native_session_unavailable", "desktop_auth_required"}:
        return "auth_lost"
    if codes & {"model_unavailable", "cu_model_unavailable", "cu_browser_decision_unavailable",
                "cu_provider_unavailable", "ai_loop_model_unavailable", "desktop_model_unavailable"}:
        return "model_unavailable"
    if codes & {"blocked", "origin_violation_stopped", "action_requires_review", "cu_blocked", "cu_policy_blocked",
                "cu_permission_denied", "cu_permission_missing", "cu_safety_blocked", "host_permissions_missing"}:
        return "blocked"
    return "needs_founder"


def reason_code(outcome: str | None, failure_code: str | None = None,
                driver_code: str | None = None) -> str | None:
    """One founder-readable ``cu_*`` code for how a Mac task stopped (None = it did not fail)."""
    if outcome in {"verified", "visual_result_review_required"}:
        return None
    codes = " ".join(c for c in (driver_code, failure_code) if isinstance(c, str))
    found = re.search(r"\bcu_[a-z_]+\b", codes)
    if found:
        return found.group(0)
    if "host_permissions_missing" in codes or "permission_denied" in codes:
        return CU_PERMISSION_MISSING
    if any(key in codes for key in ("session_unavailable", "proxy_handshake", "transport_unavailable",
                                    "cua_start_unconfirmed", "cua_host_")):
        return CU_SESSION_UNAVAILABLE
    if "window_target_not_found" in codes or "identity_changed" in codes:
        return CU_WINDOW_CHANGED
    if "effect_uncertain" in codes or "effect_outcome_uncertain" in codes:
        return CU_EFFECT_UNVERIFIED
    if "reconcil" in codes:
        return CU_RECONCILE_UNAVAILABLE
    return _OUTCOME_REASON.get(outcome or "", CU_EFFECT_UNVERIFIED if codes else CU_STOPPED)


# Fixed, reviewed-intent shortcuts. The founder's own goal must name the intent;
# the model can only pick one of these, never supply a chord. Native menus are
# outside the reviewed window, so a shortcut is the only way to Save or open a
# new document without leaving the window binding.
CHORDS = {
    "cmd+s": ("Press Command+S (Save) in the selected window", ["cmd", "s"],
              re.compile(r"(?i)\bsav(?:e|ing)\b")),
    "cmd+n": ("Press Command+N (New) in the selected window", ["cmd", "n"],
              re.compile(r"(?i)\bnew (?:document|note|file|window|folder|message)\b")),
    "cmd+t": ("Press Command+T (New Tab) in the selected window", ["cmd", "t"],
              re.compile(r"(?i)\bnew tab\b")),
    "cmd+o": ("Press Command+O (Open) in the selected window", ["cmd", "o"],
              re.compile(r"(?i)\b(?:reopen|open (?:the |that |it |this )?(?:saved )?(?:file|document))\b")),
}


def goal_chords(goal: str) -> list[str]:
    """Chord names the founder's goal asks for (empty for most goals)."""
    return [name for name, (_label, _keys, intent) in CHORDS.items() if intent.search(goal or "")]


def effect_budget_line(step: int, max_steps: int, seconds_left: float) -> str:
    """One line telling the decision model how much of the budget remains."""
    line = f"Step {step + 1} of {max_steps}; about {max(0, int(seconds_left))}s left."
    if max_steps - step - 1 <= 3:
        line += " Budget nearly spent: finish the goal now or choose blocked/done."
    return line


def compact_history(log: list[str], recent: int = 10, limit: int = 1100) -> str:
    """Last ``recent`` dispatched effects verbatim, older ones as a count.

    ``log`` holds short "operation 'label' status" strings (no IDs, no typed text).
    """
    older = len(log) - recent
    tail = "; ".join(item[:70] for item in log[-recent:])
    head = f"{older} earlier effects done; " if older > 0 else ""
    return (head + tail)[-limit:]


def stalled_few_actions(history: list[dict], readouts: tuple, window: int = 6) -> bool:
    """Last ``window`` effects used at most two distinct actions while visible readouts never changed."""
    recent = history[-window:]
    return (bool(readouts) and len(recent) == window
            and len({(item["operation"], item["label"]) for item in recent}) <= 2
            and all(item["readouts"] == readouts for item in recent))
