"""Weekly Chief of Staff brief: one private Charlie message per founder per week (flag cos_brief_v1)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from app.chat import store as chat_store
from app.credits.policy import get_policy
from app.db.models import ChatMessage, GtmContact, GtmTouch, Venture
from app.inbox import store
from app.inbox.briefing import _text, quiet
from app.state import db

KIND = "cos_brief"
DEFAULT_SCHEDULE = {"weekday": 0, "time": "08:30"}
log = logging.getLogger(__name__)


def enabled(session=None):
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_worker import capability_enabled
        return capability_enabled("background_work")
    if session is not None:
        from sqlalchemy.exc import SQLAlchemyError

        from app.db.models import CreditPolicy
        try:
            with session.begin_nested():
                row = session.get(CreditPolicy, "feature_flags")
            flags = json.loads(row.value_json) if row is not None else None
        except (SQLAlchemyError, json.JSONDecodeError):
            flags = None
    else:
        flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("cos_brief_v1") is True


def due(now, values):
    """This week's local brief moment, or None before it."""
    schedule = {**DEFAULT_SCHEDULE, **values.get("cos_brief", {})}
    zone = ZoneInfo(values["timezone"])
    local = store.utc(now).astimezone(zone)
    minutes = store.clock_minutes(schedule["time"])
    day = local.date() - timedelta(days=(local.weekday() - schedule["weekday"]) % 7)
    start = datetime.combine(day, time(minutes // 60, minutes % 60), zone)
    return start if start <= local else None


def _item(item_id, label, href):
    return {"id": item_id, "label": label, "href": href}


def _sections(workspace_id, venture_id, since, until):
    from app.experiments import ledger
    from app.experiments.outreach_tests import booked_reply, calendar_booked

    leads = f"/v/{venture_id}/leads"
    replies, calls, bounces, opt_outs = [], [], [], []
    with db()() as session:
        for touch, name in session.execute(select(GtmTouch, GtmContact.first_name).join(
                GtmContact, GtmTouch.contact_id == GtmContact.id).where(
                GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id,
                GtmTouch.direction == "in", GtmTouch.simulated == 0,
                GtmTouch.created_at >= since, GtmTouch.created_at < until).order_by(GtmTouch.created_at, GtmTouch.id)):
            who = _text(name or "") or "a contact"
            headers = json.loads(touch.honesty_json or "{}").get("reply_headers")
            # Mail text is only matched here; it never enters the message or model context.
            if booked_reply(touch.body_text, headers=headers, subject=touch.subject or "") and (
                    touch.classification != "automated" or calendar_booked(touch.body_text, headers, touch.subject or "")):
                calls.append(_item(touch.id, "Call booked with " + who, leads))
            elif touch.classification != "automated":
                replies.append(_item(touch.id, "Reply from " + who, leads))
        # shortcut: bounces and opt-outs are workspace-wide (contacts have no venture), split per venture when founders ask.
        for contact in session.scalars(select(GtmContact).where(GtmContact.workspace_id == workspace_id, or_(
                and_(GtmContact.bounced_at >= since, GtmContact.bounced_at < until),
                and_(GtmContact.unsubscribed_at >= since, GtmContact.unsubscribed_at < until))).order_by(GtmContact.id)):
            who = _text(contact.first_name or "") or "a contact"
            if contact.unsubscribed_at and since <= contact.unsubscribed_at < until:
                opt_outs.append(_item(contact.id, who + " opted out", leads))
            elif contact.bounced_at:
                bounces.append(_item(contact.id, "Email to " + who + " bounced", leads))
    experiments = f"/v/{venture_id}/experiments"
    concluded, running = [], []
    for row in ledger.evidence_for_venture(venture_id):
        label = (row["hypothesis"] or "Your experiment")[:160]
        if row["state"] == "concluded" and row.get("ended_at") and since <= store.naive(row["ended_at"]) < until:
            concluded.append(_item(row["id"], f"{label}: {row.get('outcome') or 'concluded'}", experiments))
        elif row["state"] == "running":
            running.append(_item(row["id"], label, experiments))
    return [{"key": "replies", "title": "Replies and calls booked", "items": calls + replies},
        {"key": "bounces", "title": "Bounces and opt-outs", "items": bounces + opt_outs},
        {"key": "experiments", "title": "Experiments", "items": concluded + [
            {**item, "label": "Still running: " + item["label"]} for item in running]}]


def _moves(venture_id):
    from app.chat import next_moves
    try:
        actions = next_moves.compose(venture_id).get("actions") or []
    except Exception:
        log.warning("cos_brief_next_moves_unavailable", extra={"venture_id": venture_id}, exc_info=True)
        return []
    return [action for action in actions if action.get("action") not in {"resume", "journey.skip_step"}][:3]


def run_founder(founder_id, workspace_id, venture_id, *, now):
    with db()() as session:
        venture = session.get(Venture, venture_id)
        try:
            if venture is None or venture.archived_at is not None or venture.workspace_id != workspace_id \
                    or store.owner(session, workspace_id) != founder_id:
                return None
        except PermissionError:
            return None
        _, values = store.settings(session, founder_id, workspace_id)
        session.rollback()  # read only: the inbox settings row is never created or written here
        start = due(now, values)
        if start is None or quiet(now, values):
            return None
        key = "cos:week:" + start.date().isoformat()
        if session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture_id,
                ChatMessage.kind == KIND, ChatMessage.citations_json.contains(json.dumps(key)))):
            return None
    from app.tenancy import bind
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=founder_id):
        until = store.naive(start)
        return _post(founder_id, workspace_id, venture_id, key, start,
            _sections(workspace_id, venture_id, until - timedelta(days=7), until), _moves(venture_id))


def _post(founder_id, workspace_id, venture_id, key, start, sections, moves):
    from app.chat.conversations import create

    lines = ["Your weekly brief"]
    for section in sections:
        lines.append(f"\n{section['title']} ({len(section['items'])})")
        lines += ["- " + item["label"] for item in section["items"]] or ["Nothing new."]
    lines.append("\nYour next move: " + (moves[0]["label"] if moves else "Nothing new."))
    if moves[1:]:
        lines.append("Other options: " + ", ".join(move["label"] for move in moves[1:]))
    if not any(section["items"] for section in sections):
        lines.insert(1, "Nothing new this week.")
    payload = {"week_key": key, "week_of": start.date().isoformat(), "sections": sections,
        "next_move": moves[0] if moves else None, "alternatives": moves[1:]}

    def authority(session, scope):
        if not enabled(session) or store.owner(session, workspace_id) != founder_id:
            raise PermissionError("cos_brief_unavailable")

    conversation = create(venture_id, idempotency_key="cos_brief:" + founder_id, title="Your weekly brief")
    if chat_store.idempotent_message(venture_id, "assistant", key, conversation_id=conversation["id"],
            output_authority=authority) is not None:
        return None
    return chat_store.append_message(venture_id, role="assistant", kind=KIND,
        body_md="\n".join(lines), actions=moves, preserve_exact=True,
        conversation_id=conversation["id"], citations=[{KIND: payload}],
        idempotency_key=key, output_authority=authority)


def _fan_out(window, payload=None, *, now=None):
    from app.core.scheduled import fanout
    return fanout("inbox.cos_brief.tick", {**(payload or {}), "window": window}, now=now)


async def tick(payload=None, *, now=None):
    """Without a founder: enqueue one job per founder. With one: brief only that founder."""
    if not await asyncio.to_thread(enabled):
        return {"enabled": False}
    payload, now = payload or {}, store.utc(now)
    if not payload.get("founder_id"):
        count = await asyncio.to_thread(_fan_out, payload.get("window") or now.strftime("%Y%m%dT%H%M"), payload, now=now)
        return {"enabled": True, "founders": count}
    if not payload.get("venture_id"):
        from app.core.scheduled import founder_target
        target = await asyncio.to_thread(founder_target, payload["founder_id"])
        if not target:
            return {"enabled": True, "posted": False}
        payload = {**payload, "workspace_id": target[0], "venture_id": target[1]}
    message = await asyncio.to_thread(run_founder, payload["founder_id"], payload["workspace_id"],
        payload["venture_id"], now=now)
    return {"enabled": True, "posted": message is not None}
