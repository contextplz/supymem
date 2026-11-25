"""Founder policy: try every autonomous option first; ask the human only when nothing else can move.

``must_ask_human`` is the decision table. ``try_autonomous_first`` names the next autonomous move
(the caller runs it); ``escalate`` ties both to ``service.request_human``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Only a person can do these: verification, login, payment, an OS permission, explicit consent, the real world.
HUMAN_ONLY = frozenset({
    "captcha", "verification", "login", "two_factor", "payment", "os_permission",
    "consent_send", "consent_delete", "consent_spend", "credit_cap", "real_world_action", "ambiguous_high_stakes",
    "gmail_connect",
})
# Urgent: these hold the venture up and bypass quiet hours.
BLOCKING = frozenset({"captcha", "verification", "login", "two_factor", "payment", "os_permission", "credit_cap", "gmail_connect"})
TITLES = {
    "captcha": "A CAPTCHA needs you", "verification": "A verification step needs you", "login": "A login needs you",
    "two_factor": "A 2FA code needs you", "payment": "A payment needs you", "os_permission": "A Mac permission needs you",
    "consent_send": "Approve before I send", "consent_delete": "Approve before I delete",
    "consent_spend": "Approve before I spend", "credit_cap": "Insufficient credits",
    "real_world_action": "A real-world step needs you", "ambiguous_high_stakes": "A big choice needs you",
    "exhausted": "I tried everything and need you", "gmail_connect": "Connect Gmail to continue",
}
# pause_kind / guard reason strings from other subsystems -> reason_code
ALIASES = {
    "recaptcha": "captcha", "human_check": "captcha", "login_required": "login", "sign_in": "login",
    "password": "login", "2fa": "two_factor", "otp": "two_factor", "mfa": "two_factor",
    "permission": "os_permission", "screen_recording": "os_permission", "accessibility": "os_permission",
    "sending_not_allowed": "consent_send", "send": "consent_send", "delete": "consent_delete",
    "insufficient_credits": "credit_cap", "guardrail": "consent_spend",
    "credentials_never_entered": "login", "gmail_not_connected": "gmail_connect", "missing_fill": "real_world_action",
}


@dataclass
class Blocker:
    reason_code: str
    blocking_ref: str = ""
    step: str = ""                    # recovery.STEPS key, e.g. "build"
    retries_left: int = 0
    other_work_available: bool = False
    exhausted: bool = False           # every autonomous route already tried and failed
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        c = (self.reason_code or "").strip().lower()
        return ALIASES.get(c, c)


def must_ask_human(blocker: Blocker) -> bool:
    """True only for human-only reasons, or when every autonomous option is exhausted."""
    return blocker.code in HUMAN_ONLY or blocker.exhausted


@dataclass
class Plan:
    action: str  # retry | alternative | continue_other | none
    detail: str = ""


def try_autonomous_first(blocker: Blocker) -> Plan:
    """The next autonomous move, in order: retry, automatic "Do it another way", other independent work."""
    if blocker.retries_left > 0:
        return Plan("retry", f"{blocker.retries_left} retries left")
    from app.chat import recovery

    auto = recovery.STEPS.get(blocker.step, {}).get("auto") or ()
    if auto and not blocker.exhausted:
        return Plan("alternative", auto[0])
    if blocker.other_work_available:
        return Plan("continue_other")
    return Plan("none")


def escalate(venture_id: str, blocker: Blocker, *, title: str | None = None, what_we_need: str = "",
             how_to_resume: str = "", deadline=None, chat_card: bool = True):
    """Autonomous first. Returns a Plan when Supymem can move on its own, else the request id (None if deduped away)."""
    from app.needs_you import service

    if not must_ask_human(blocker):
        return try_autonomous_first(blocker)  # nothing proven impossible: never bug the founder
    if blocker.code not in HUMAN_ONLY:
        plan = try_autonomous_first(blocker)  # exhausted flag but a retry/other branch remains
        if plan.action in {"retry", "continue_other"}:
            return plan
    code = blocker.code if blocker.code in HUMAN_ONLY else "exhausted"
    return service.request_human(
        venture_id, reason_code=code, title=title or TITLES.get(code, "Supymem needs you"),
        what_we_need=what_we_need or "Something only you can do.",
        how_to_resume=how_to_resume or "Tap Continue once it is done and I carry on.",
        blocking_ref=blocker.blocking_ref or f"{code}:{blocker.step}",
        severity="blocking" if code in BLOCKING else "normal", deadline=deadline, chat_card=chat_card)
