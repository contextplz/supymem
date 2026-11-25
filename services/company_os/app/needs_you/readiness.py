"""Setup readiness: Charlie asks for each setup step at the moment it is needed, with a button, never "go find Settings".

``missing(venture_id)`` lists what the venture needs right now. ``sync(venture_id)`` turns each missing item into ONE
needs_you request (one chat card, with reminders outside chat) and resolves requests whose
condition has cleared. Runs on the worker 5-minute tick for ventures that have work waiting (ready outreach drafts or
God mode on).
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any

from core.clock import utcnow
from sqlalchemy import select

from app.chat import navigation_map as nav
from app.chat.recovery import log_founder_exception
from app.db.models import ChatMessage, Venture
from app.db.supymem_models import NeedsYouRequest as R
from app.state import db

PREFIX = "setup:"
COOLDOWN = timedelta(minutes=30)  # after the founder taps Continue on a still-missing item, do not re-ask at once
log = logging.getLogger(__name__)


@dataclass
class Item:
    key: str
    reason_code: str
    title: str
    what: str
    steps: list[str]
    actions: list[dict[str, Any]] = field(default_factory=list)
    severity: str = "normal"


def _cap() -> int:
    from app.autopilot.guard import DEFAULT_SEND_MAX

    return DEFAULT_SEND_MAX


def _ready_drafts(venture_id: str) -> int:
    from app.autopilot import outreach

    contacts = {contact.id: contact.email for contact in outreach.contacts(venture_id) if contact.email}
    return sum(draft.get("contact_id") in contacts and draft.get("to") == contacts[draft["contact_id"]]
               and not draft.get("awaiting_recipient") for draft in outreach.drafts(venture_id, "draft", "manual"))


def _confirm(action: str, label: str, interpretation: str, consequences: list[str]) -> dict[str, Any]:
    from dataclasses import replace

    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard

    card = ConfirmationCard.for_action(get_action(action), interpretation=interpretation, consequences=consequences)
    return replace(card, confirm_label=label).as_action()


def allow_send_card(name: str) -> dict[str, Any]:
    return _confirm("autopilot.allow_send", "Allow sending",
                    f"Allow Supymem to send up to {_cap()} emails a day from your Gmail for {name}?",
                    [f"I send at most {_cap()} a day, only drafts you can see, from your connected Gmail.",
                     "Say 'stop sending' any time to switch it off."])


def allow_send(venture_id: str) -> dict[str, Any]:
    """The founder's tap on 'Allow sending' (a confirmation card): turn on the venture's capped send permission."""
    from app.approvals.external import execution_enabled
    from app.autopilot import service

    if not _ready_drafts(venture_id):
        return {"kind": "text", "citations": [], "body_md": "No people to write to yet. Find customers first. Sending permission was not changed.",
                "actions": [{"label": "Find customers", "action": "gtm.find", "payload": {"prepare": True}, "style": "primary"}]}
    out = service.update_settings(venture_id, allow_send=True, send_max_per_day=_cap())
    off = "Tap Stop sending below or say 'turn off sending' to stop it."
    note = ("" if execution_enabled("gtm_email_send") else " Sending is not switched on for this server yet (an admin step), so nothing "
            "leaves until it is.")
    from app.core.local_mode import is_local
    if is_local() and note:
        from app.gtm.local_mail import blocked_message
        note = " " + blocked_message("send_email")
    return {"kind": "text", "citations": [], "actions": [{"label": "Stop sending",
            "action": "autopilot.send_off", "style": "ghost"}],
            "body_md": f"Sending is allowed: up to {out['send_max_per_day']} emails a day from your connected Gmail. {off}{note}"}


def missing(venture_id: str) -> list[Item]:
    from app.approvals.external import execution_enabled
    from app.autopilot import guard
    from app.chat import connect

    items: list[Item] = []
    with db()() as s:
        ven = s.get(Venture, venture_id)
        if ven is None:
            return []
        wid, name = ven.workspace_id, ven.name
        last_founder = s.scalar(select(ChatMessage.created_at).where(ChatMessage.venture_id == venture_id, ChatMessage.role == "user")
                                .order_by(ChatMessage.created_at.desc()).limit(1))
    settings = guard.get_settings(venture_id)
    drafts = _ready_drafts(venture_id)
    if not (drafts or settings["god_mode"]):
        return []  # nothing is waiting, so nothing is blocked
    open_page = {"label": f"Open {nav.label('connections')}", "href": nav.path("connections", venture_id), "style": "ghost"}
    gmail_connected = None
    if drafts:
        try:
            gmail_connected = connect._is_connected(wid, "gmail")
        except connect.ConnectionStatusUnavailable as exc:
            log_founder_exception(exc.error, venture_id, "readiness.gmail_status")
            items.append(Item("gmail", "gmail_connect", "Check your Gmail connection",
                              "I could not check your Gmail connection. Your saved drafts are kept. Nothing was sent.",
                              [], [{"label": "Retry", "action": "connections.status", "style": "primary", "retry": True}, open_page],
                              "blocking"))
    if drafts and gmail_connected is False:
        items.append(Item("gmail", "gmail_connect", "Connect Gmail so I can send your outreach",
                          f"I have {drafts} email{'s' if drafts != 1 else ''} ready for {name}, but Gmail is not connected.",
                          [], [{"label": "Connect Gmail", "action": "chat.reply", "text": "connect my gmail", "style": "primary"}, open_page],
                          "blocking"))
    automate_offered = False
    from app.autopilot import outreach
    sending_requested = any(draft.get("requires_journey_review") or draft.get("requires_batch_approval")
        or draft.get("state") in {"delivery_pending", "delivery_unconfirmed", "sent"} for draft in outreach.drafts(venture_id))
    if drafts and not settings["allow_send"]:
        with db()() as s:  # the chat already offered "Automate all emails" (chat/customers.offer_automation): one card, not two
            automate_offered = s.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture_id,
                ChatMessage.actions_json.like('%"gtm.automate"%')).limit(1)) is not None
    if drafts and not settings["allow_send"] and not automate_offered and not sending_requested:
        review = {"label": "Review drafts first", "action": "gtm.status", "payload": {"show_saved": True}, "style": "ghost"}
        if execution_enabled("gtm_email_send"):
            allow = allow_send_card(name)
            items.append(Item("send", "consent_send", "Allow me to send your outreach",
                              f"I have {drafts} email{'s' if drafts != 1 else ''} ready for {name}. Allow me to send up to "
                              f"{_cap()}/day from your Gmail?", [], [allow, review]))
    if not settings["god_mode"] and last_founder is not None and utcnow() - (
            last_founder if last_founder.tzinfo else last_founder.replace(tzinfo=timezone.utc)) > timedelta(hours=24):
        from app.autopilot import service

        items.append(Item("god", "real_world_action", "Let me keep going while you are away",
                          "You have not been here for a day and work is waiting. God mode (beta) lets me take the next steps myself.",
                          ["Tap Turn on God mode and read the beta warning", "Tap confirm if you are happy with its limits"],
                          [_confirm("autopilot.god_on", "Turn on God mode", "Turn on God mode (beta)?", [service.WARNING, *guard.FLOORS])]))
    try:
        from app.credits.accounts import account_for_workspace, balance

        acct = account_for_workspace(wid)
        low = float(os.environ.get("SETUP_LOW_CREDITS", "10"))
        if acct is not None and balance(acct)["available_micro"] < low * 1_000_000:
            items.append(Item("credits", "credit_cap", "Credits are running low",
                              "Your credits are almost used up and paid work will pause. Payments are not switched on yet.",
                              [f"Open {nav.label('settings')}", f"Click {nav.ui('request_credits')} and tell us how many you need"],
                              [{"label": f"Open {nav.label('settings')}", "href": nav.path("settings", venture_id), "style": "primary"}],
                              "blocking"))
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "readiness.credits")
    return items


def _server_off_note(venture_id: str) -> None:
    """Give saved drafts one sending-setup note while their transport is off."""
    from app.approvals.external import execution_enabled
    from app.chat.milestones import post_milestone

    drafts = _ready_drafts(venture_id)
    from app.core.local_mode import is_local
    if is_local() and drafts and not execution_enabled("gtm_email_send"):
        from app.gtm.local_mail import blocked_message
        post_milestone(venture_id, key=f"setup_send_server_off:{venture_id}", kind="setup_info",
            body_md=blocked_message("send_email") + " Your drafts are saved. Nothing was sent.")
        return
    if drafts and not execution_enabled("gtm_email_send"):
        post_milestone(venture_id, key=f"setup_send_server_off:{venture_id}", kind="setup_info",
                       body_md=f"**I have {drafts} email{'s' if drafts != 1 else ''} ready, but sending is not enabled on this "
                               "server yet.** That is an admin step on the Supymem deployment, not something you can switch on. "
                               "You can still review the drafts and send them yourself.")


def sync(venture_id: str) -> dict[str, int]:
    """Open one request per missing item (deduped), resolve cleared ones. Never raises."""
    from app.needs_you import service

    out = {"opened": 0, "resolved": 0}
    try:
        items = {i.key: i for i in missing(venture_id)}
        _server_off_note(venture_id)
        # Older login paths used different blocking refs. Only the mailbox in
        # this venture's workspace can clear its historical Gmail requests.
        with db()() as s:
            ven = s.get(Venture, venture_id)
            wid = ven.workspace_id if ven else None
            gmail_requests = list(s.scalars(select(R.id).where(R.venture_id == venture_id,
                                  R.reason_code == "gmail_connect", R.status == "open")))
        gmail_unknown = False
        if wid and gmail_requests:
            from app.chat import connect
            try:
                gmail_connected = connect._is_connected(wid, "gmail")
            except connect.ConnectionStatusUnavailable:
                gmail_connected = None
                gmail_unknown = True
            if gmail_connected is True:
                for rid in gmail_requests:
                    out["resolved"] += bool(service.resolve(rid, venture_id=venture_id, actor="readiness"))
        now = utcnow()
        with db()() as s:
            rows = s.scalars(select(R).where(R.venture_id == venture_id, R.blocking_ref.like(PREFIX + "%"))).all()
            open_keys = {r.blocking_ref[len(PREFIX):] for r in rows if r.status == "open"}
            recent = {r.blocking_ref[len(PREFIX):] for r in rows if r.status == "resolved" and r.resolved_at
                      and now - (r.resolved_at if r.resolved_at.tzinfo else r.resolved_at.replace(tzinfo=now.tzinfo)) < COOLDOWN}
        for key in open_keys - items.keys():
            if key == "gmail" and gmail_unknown:
                continue
            out["resolved"] += bool(service.resolve_ref(venture_id, PREFIX + key, actor="readiness"))
        for key, it in items.items():
            if key in open_keys or key in recent:
                continue
            rid = service.request_human(venture_id, reason_code=it.reason_code, title=it.title, what_we_need=it.what,
                                        how_to_resume="\n".join(f"{i}. {t}" for i, t in enumerate(it.steps, 1)),
                                        blocking_ref=PREFIX + key, severity=it.severity, actions=it.actions)
            out["opened"] += bool(rid)
    except Exception as exc:  # noqa: BLE001
        extra: dict[str, Any] = {"venture_id": venture_id, "error_type": type(exc).__name__}
        if isinstance(exc, RecursionError):
            functions = Counter()
            traceback = exc.__traceback__
            while traceback is not None:
                functions[traceback.tb_frame.f_code] += 1
                traceback = traceback.tb_next
            extra["repeating_functions"] = list(dict.fromkeys(code.co_name for code, count in functions.items() if count > 1))
        log.warning("readiness_sync_failed", extra=extra)
    return out


def sync_all(*, founder_id=None) -> int:
    """Worker tick: only ventures with work waiting are checked (cheap filter first, then the real checks)."""
    n = 0
    with db()() as s:
        query = select(Venture.id).where(Venture.archived_at.is_(None))
        if founder_id:
            from app.core.scheduled import founder_ventures
            query = query.where(Venture.id.in_(founder_ventures(founder_id)))
        ids = list(s.scalars(query))
    for vid in ids:
        r = sync(vid)
        n += r["opened"]
    return n
