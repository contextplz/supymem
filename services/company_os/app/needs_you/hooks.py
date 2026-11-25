"""Small call-in points for the subsystems that can hit a human-only blocker. Every one never raises."""
from __future__ import annotations

import logging

from app.needs_you.policy import ALIASES, Blocker, escalate

log = logging.getLogger("app.needs_you")


def _go(venture_id: str, blocker: Blocker, **kw) -> object:
    try:
        return escalate(venture_id, blocker, **kw)
    except Exception:  # noqa: BLE001
        log.warning("needs_you_hook_failed", exc_info=True)
        return None


def from_failure(venture_id: str, *, step: str, code: str, ref: str, exhausted: bool = False) -> object:
    """recovery.post_failure: the failure card already exists, so no second card."""
    return _go(venture_id, Blocker(code, f"job:{ref}", step=step, exhausted=exhausted), chat_card=False)


def from_godmode(venture_id: str, name: str, reason: str, *, card: bool = True, detail: str = "") -> object:
    """God mode guard stops and needs_you replies. A credit/consent/login stop pauses that action only."""
    if reason in {"daily_cap_reached", "lifetime_cap_reached"}:
        return None
    known = reason in ALIASES
    blocker = Blocker(reason if known else "needs_you", f"godmode:{name}", other_work_available=True,
                      exhausted=not known)
    return _go(venture_id, blocker, what_we_need=detail or f"God mode paused “{name}”: {reason.replace('_', ' ')}.",
               how_to_resume="Add credits, then tap Continue." if reason == "insufficient_credits" else "Fix it, then tap Continue.", chat_card=card)


def from_device_event(venture_id: str, order_id: str, pause_kind: str, prompt: str = "") -> object:
    """Mac runtime 'needs_you' progress event (CAPTCHA, login, permission, guardrail) -> request."""
    code = (pause_kind or "").lower()
    return _go(venture_id, Blocker(code, f"device:{order_id}", exhausted=True),
               what_we_need=(prompt or "Your Mac task is waiting for you.")[:500],
               how_to_resume="Do it in the window Supymem opened, then return to the Mac task. It resumes when the blocker clears; Continue records your update.")


def from_mac_stop(venture_id: str, order_id: str, guide_code: str) -> object:
    if guide_code != "computer_use_permissions":
        return None
    return _go(venture_id, Blocker("os_permission", f"device:{order_id}"), chat_card=False,
               what_we_need="Turn on Screen Recording and Accessibility for Supymem in macOS System Settings.",
               how_to_resume="Open Mac setup, switch both on, then tap Continue.")


def hosting_login(venture_id: str, host: str) -> object:
    return _go(venture_id, Blocker("login", f"host:{host}"), chat_card=False,
               what_we_need=f"Log in to {host} in Terminal; only you can sign in.",
               how_to_resume="Run the steps in the plan, then paste your live address with “verify my product”.")
