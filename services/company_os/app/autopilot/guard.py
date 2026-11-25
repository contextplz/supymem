"""The one place God mode's safety floors live. Autopilot asks here before every action.

Floors that stay on in God mode: no credentials or payment details, no sends to
real people unless the separate "Allow sending" toggle is on (and then only up to its daily max), no
real-money spend, no deletes. Anything not on AUTO_ACTIONS is refused, so a new chat action is
never auto-approved by accident.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from app.autopilot import claims
from app.authz.roles import ROLES
from app.credits.units import MICRO_PER_CREDIT
from app.db.models import AnalyticsEvent, EventLog, GtmTouch, Venture, WorkspaceMember
from app.db.supymem_models import AutopilotEvent, AutopilotSetting
from app.state import db
from core.clock import utcnow

DEFAULT_CAP_CREDITS = 200
MAX_CAP_CREDITS = 100_000
MAX_CONSEQUENTIAL_PER_TICK = 3
OFFLINE_AFTER = timedelta(minutes=10)  # the desktop app beats every 60 s
OFFLINE_STATUS = "Paused: your Mac is offline. Turn on 'Keep working while my Mac is off' to continue."

# Chat actions autopilot may run: the journey's own next steps and the cards they raise.
AUTO_ACTIONS = frozenset({
    "research.start", "plan.generate", "plan.approve", "plan.resources_none", "build.prepare",
    "build.app.create", "build.start", "work.start", "waitlist.enable", "waitlist.test", "site.publish",
    "experiment.start", "gtm.diagnosis", "outreach.draft", "outreach.targets", "job.retry",
    # lifecycle drivers (autopilot/lifecycle.py): answer clarifications, validate and confirm, approve a reviewed Mac task
    "idea.answer", "idea.advance", "mac.approve", "content.drafts", "pmf.iterate",
    # the automatic "Do it another way" options: a template plan (no credits) and a draft-only page rewrite
    "recovery.plan_template", "site.rewrite",
})
# Actions that reach real people. Autopilot has no path that picks one; the guard still refuses them
# unless sending is allowed and today's sends are under the max.
SEND_ACTIONS = frozenset({"social_post", "outreach.send"})
EVENT_SENDS = frozenset({"social_post"})  # sends counted by their timeline row; outreach counts each recipient (gtm_touches)
DEFAULT_SEND_MAX = 15
MAX_SEND_MAX = 100


def send_max_confirmed(session, venture_id, maximum):
    return session.scalar(select(EventLog.id).join(Venture, Venture.id == EventLog.venture_id).join(
        WorkspaceMember, (WorkspaceMember.workspace_id == Venture.workspace_id)
        & (WorkspaceMember.person_id == EventLog.actor_id)).where(
            EventLog.venture_id == venture_id, EventLog.workspace_id == Venture.workspace_id,
            EventLog.topic == "autopilot.send_cap.confirmed",
            WorkspaceMember.role.in_([role for role, permissions in ROLES.items() if "change_autonomy" in permissions]),
            EventLog.actor_kind == "person", EventLog.outcome == "ok", EventLog.ts.is_not(None),
            EventLog.payload_json == json.dumps({"send_max_per_day": maximum})).limit(1)) is not None


def effective_send_max(session, setting):
    if setting is None:
        return 0
    maximum = setting.send_max_per_day
    if maximum > DEFAULT_SEND_MAX and (maximum > MAX_SEND_MAX or not send_max_confirmed(
            session, setting.venture_id, maximum)):
        return DEFAULT_SEND_MAX
    return max(0, maximum)


def mailbox_send_max(session, account):
    from app.connections import mailboxes
    if mailboxes.enabled():
        from app.db.models import GtmEmailAccount
        from app.gtm.supymem.factory import daily_email_cap
        from core.clock import as_utc
        first_connected = session.scalar(select(func.min(GtmEmailAccount.created_at)).where(
            func.lower(func.trim(GtmEmailAccount.email)) == (account.email or "").strip().casefold()))
        if first_connected is None:
            return 0
        age = max(0, (utcnow() - as_utc(first_connected)).days)
        return min(int(account.daily_send_limit or DEFAULT_SEND_MAX), daily_email_cap(age // 7 + 1),
                   workspace_send_max(session, account.workspace_id))
    default = min(DEFAULT_SEND_MAX, int(account.daily_send_limit or DEFAULT_SEND_MAX))
    if not account.email:
        return default
    settings = session.scalars(select(AutopilotSetting).join(
        Venture, Venture.id == AutopilotSetting.venture_id).where(
            Venture.workspace_id == account.workspace_id, Venture.archived_at.is_(None),
            AutopilotSetting.allow_send.is_(True), AutopilotSetting.send_max_per_day > DEFAULT_SEND_MAX))
    overrides = [effective_send_max(session, setting) for setting in settings]
    return max([default, *(maximum for maximum in overrides if maximum > DEFAULT_SEND_MAX)])


def workspace_send_max(session, workspace_id):
    from app.db.models import GtmEngineState
    from app.gtm.supymem.factory import daily_email_cap
    state = session.get(GtmEngineState, workspace_id)
    default = min(DEFAULT_SEND_MAX, daily_email_cap(state.week_index)) if state else DEFAULT_SEND_MAX
    settings = session.scalars(select(AutopilotSetting).join(Venture, Venture.id == AutopilotSetting.venture_id).where(
        Venture.workspace_id == workspace_id, Venture.archived_at.is_(None), AutopilotSetting.allow_send.is_(True)))
    return max([default, *(effective_send_max(session, setting) for setting in settings)])

# Price of a step that shows no quote of its own (quoted steps carry ceiling_micro).
UNQUOTED_COST_MICRO = {"research.start": 20 * MICRO_PER_CREDIT, "gtm.diagnosis": 10 * MICRO_PER_CREDIT}

_SECRET_KEY = re.compile(r"pass(?:word|code)|secret|token|api[_-]?key|credential|card|cvv|iban|routing|ssn|otp|payment", re.I)
READ_ONLY_MAC = frozenset({"browser.read", "desktop.read", "editor.read", "app.navigate"})
# With "Allow Mac actions": reviewed interaction and edits too, never terminal commands.
ALLOWED_MAC = READ_ONLY_MAC | frozenset({"browser.interact", "desktop.interact", "editor.edit"})

FLOORS = (
    "No daily or lifetime spending cap. Credit-funded work stops only when there are insufficient credits.",
    "Never enters passwords, credentials or payment details.",
    "Mac tasks: only read-only ones run on their own, unless you turn on Allow Mac actions (still never terminal commands or credentials).",
    "Never sends messages, emails or posts to real people (drafts only) unless you turn on Allow sending (max per day, default 15) or approve a batch yourself. Outreach comes from your own Gmail, once per person, never to anyone who opted out.",
    "Never spends real money and never deletes data or ventures.",
    "Pausing a venture blocks new work for that venture. Turning God mode off blocks new autonomous steps. "
    "Work already running may finish; review its recorded result and credit settlement. Unconfirmed usage remains unresolved.",
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    cost_micro: int = 0


def day_start():
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


def last_desktop_seen_at(person_id: str | None):
    """Newest desktop.process_heartbeat for this person (analytics_events already persists it), else None."""
    if not person_id:
        return None
    with db()() as s:
        return s.scalar(select(func.max(AnalyticsEvent.occurred_at)).where(
            AnalyticsEvent.person_id == person_id, AnalyticsEvent.event_name == "desktop.process_heartbeat"))


def mac_offline(venture_id: str, owner: str | None) -> bool:
    """Hosted only: the owner's Mac has been silent >10 min (or never seen) and the founder has not allowed offline work."""
    from app.worker.handlers import hosted_mode

    if not hosted_mode() or get_settings(venture_id)["continue_when_offline"]:
        return False
    from app.authz.workspace import subject_person_id

    seen = last_desktop_seen_at(subject_person_id(owner))
    return seen is None or utcnow().replace(tzinfo=None) - seen.replace(tzinfo=None) > OFFLINE_AFTER


def get_settings(venture_id: str) -> dict[str, Any]:
    with db()() as s:
        row = s.get(AutopilotSetting, venture_id)
        spent = s.scalar(select(func.coalesce(func.sum(AutopilotEvent.credits_micro), 0)).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.outcome.in_(["done", "attempt"]),
            AutopilotEvent.created_at >= day_start())) or 0
        sends = s.scalar(select(func.count(AutopilotEvent.id)).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.outcome.in_(["done", "attempt"]),
            AutopilotEvent.action.in_(EVENT_SENDS), AutopilotEvent.created_at >= day_start())) or 0
        lifetime = s.scalar(select(func.coalesce(func.sum(AutopilotEvent.credits_micro), 0)).where(
            AutopilotEvent.venture_id == venture_id, AutopilotEvent.outcome.in_(["done", "attempt"]))) or 0
        sends += s.scalar(select(func.count(GtmTouch.id)).where(
            GtmTouch.logged_by == f"outreach:{venture_id}", GtmTouch.direction == "out",
            GtmTouch.sent_at >= day_start().replace(tzinfo=None))) or 0
        send_max = effective_send_max(s, row)
    return {
        "god_mode": bool(row and row.god_mode),
        "daily_cap_credits": None,
        "allow_send": bool(row and row.allow_send), "allow_mac": bool(row and row.allow_mac),
        "send_max_per_day": send_max,
        "configured_send_max_per_day": row.send_max_per_day if row else 0,
        "continue_when_offline": bool(row and row.continue_when_offline),
        "lifetime_cap_credits": None,
        "lifetime_spent_micro": int(lifetime), "spent_today_micro": int(spent), "sends_today": int(sends),
    }


def cost_micro(action: str, payload: dict[str, Any] | None) -> int:
    ceiling = (payload or {}).get("ceiling_micro")
    return ceiling if type(ceiling) is int and ceiling >= 0 else UNQUOTED_COST_MICRO.get(action, 0)


def _secret_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEY.search(str(k)) or _secret_keys(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_secret_keys(v) for v in value)
    return False


def check(venture_id: str, action: str, payload: dict[str, Any] | None = None,
          settings: dict[str, Any] | None = None, *, human_approved: bool = False) -> Verdict:
    """human_approved: the founder tapped Go ahead on a card that showed this action. God mode need not be on
    while sends, credentials and the allow-list still apply."""
    s = settings or get_settings(venture_id)
    from app.core.local_mode import is_local
    if is_local() and action == "outreach.send":
        from app.core.local_worker import blocked_reason
        if reason := blocked_reason("gtm.outreach.retry" if human_approved else "gtm.journey.maintain"):
            return Verdict(False, reason)
    if not s["god_mode"] and not human_approved:
        return Verdict(False, "god_mode_off")
    if action in SEND_ACTIONS:
        by_hand = human_approved and action == "outreach.send"  # the founder approved this batch: capped, not toggled
        if by_hand and not s["allow_send"]:
            if s["sends_today"] >= DEFAULT_SEND_MAX:
                return Verdict(False, "send_limit_reached")
        elif not s["allow_send"] or s["send_max_per_day"] <= 0:
            return Verdict(False, "sending_not_allowed")
        elif s["sends_today"] >= s["send_max_per_day"]:
            return Verdict(False, "send_limit_reached")  # consent is given; today's max is used up
    elif action == "site.publish" and not human_approved and (bad := claims.site_violations(venture_id)):
        return Verdict(False, "public_claims_unverified: " + "; ".join(bad)[:200])
    if action not in SEND_ACTIONS and action not in AUTO_ACTIONS and not (human_approved and action == "pagebuilder.build"):
        return Verdict(False, "action_not_allowed")
    if _secret_keys(payload or {}):
        return Verdict(False, "credentials_never_entered")
    cost = cost_micro(action, payload)
    return Verdict(True, "", cost)


def check_mac_manifest(venture_id: str, manifest: dict[str, Any], settings: dict[str, Any] | None = None, *,
                       human_approved: bool = False) -> Verdict:
    """A Mac task is auto-approved when it only reads and navigates; with Allow Mac actions also reviewed
    interaction and edits. Never terminal commands, never credentials."""
    s = settings or get_settings(venture_id)
    if not s["god_mode"] and not human_approved:
        return Verdict(False, "god_mode_off")
    allowed = ALLOWED_MAC if (s["allow_mac"] or human_approved) else READ_ONLY_MAC
    if not set(manifest.get("actions") or []) <= allowed or not manifest.get("actions"):
        return Verdict(False, "mac_scope_not_read_only")
    if _secret_keys(manifest):
        return Verdict(False, "credentials_never_entered")
    cost = int(manifest.get("credit_ceiling_micro") or 0)
    return Verdict(True, "", cost)
