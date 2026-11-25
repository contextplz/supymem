"""Mutable, tenant-scoped mail sidecars. No changes to outreach history."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import (
    delete,
    inspect,
    select,
)

from app.connections.mailboxes import list_read_mailboxes
from app.credits.policy import get_policy
from app.db.inbox_models import InboxBriefingItem, InboxBriefingState
from app.db.models import (
    Base,
    ChatContextCheckpoint,
    ChatConversation,
    ChatMessage,
    Person,
    Venture,
    Workspace,
)
from app.db.session import serialize_writes
from app.state import db

TABLES = (InboxBriefingState.__table__, InboxBriefingItem.__table__)


def ensure_schema(engine):
    Base.metadata.create_all(engine, tables=TABLES)


def enabled(session=None):
    from app.core.local_mode import is_local
    if is_local():
        from app.core.local_worker import capability_enabled
        return capability_enabled("background_work")
    if session is not None:  # inside a caller's transaction: read through it, never open a second session
        from sqlalchemy.exc import SQLAlchemyError

        from app.db.models import CreditPolicy
        try:
            with session.begin_nested():  # savepoint: a missing policy table must not poison the caller's transaction
                row = session.get(CreditPolicy, "feature_flags")
            flags = json.loads(row.value_json) if row is not None else None
        except (SQLAlchemyError, json.JSONDecodeError):
            flags = None
    else:
        flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("inbox_briefing_v1") is True


def utc(now=None):
    moment = now or datetime.now(timezone.utc)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def naive(now=None):
    return utc(now).replace(tzinfo=None)


def lock(session, founder_id):
    serialize_writes(session, key="inbox-briefing:" + founder_id)


def owner(session, workspace_id):
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise PermissionError("inbox_workspace_unavailable")
    return workspace.owner_id


def state(session, founder_id, workspace_id, mailbox_id=""):
    row = session.scalar(select(InboxBriefingState).where(
        InboxBriefingState.founder_id == founder_id, InboxBriefingState.mailbox_id == mailbox_id))
    if row is None:
        from core.ids import new_id
        row = InboxBriefingState(id=new_id("ibs"), founder_id=founder_id,
            workspace_id=workspace_id, mailbox_id=mailbox_id, consent=0, data_json="{}")
        session.add(row)
        session.flush()
    return row


def settings(session, founder_id, workspace_id):
    row = state(session, founder_id, workspace_id)
    person = session.get(Person, founder_id)
    values = {"timezone": person.timezone or "UTC" if person else "UTC",
        "windows": ["08:30", "17:30"], "quiet_hours": {"start": "21:00", "end": "08:00"},
        "email_copy": False, "poll_minutes": 60, "rules": {}, "deliveries": {}}
    values.update(json.loads(row.data_json))
    return row, values


def save(row, values):
    row.data_json = json.dumps(values, separators=(",", ":"))


def consented(session, founder_id, mailbox_id, generation=None):
    row = session.scalar(select(InboxBriefingState).where(
        InboxBriefingState.founder_id == founder_id,
        InboxBriefingState.mailbox_id == mailbox_id, InboxBriefingState.consent == 1))
    if row is None or (generation is not None and json.loads(row.data_json).get("generation") != generation):
        return None
    if owner(session, row.workspace_id) != founder_id:
        return None
    if not any(ref["mailbox_id"] == mailbox_id for ref in list_read_mailboxes(session, row.workspace_id)):
        return None
    return row


def _delete_deliveries(session, values, founder_id, mailbox_id=None, cutoff=None):
    conversations = set()
    messages = session.scalars(select(ChatMessage).join(ChatConversation,
        ChatMessage.conversation_id == ChatConversation.id).where(
        ChatConversation.creator_person_id == founder_id, ChatConversation.visibility == "private",
        ChatMessage.role == "assistant", ChatMessage.kind.in_(["inbox_briefing", "inbox_urgent"])))
    for message in messages:
        cards = next((entry["inbox_briefing"].get("items", []) for entry in json.loads(message.citations_json or "[]")
            if isinstance(entry.get("inbox_briefing"), dict)), [])
        if ((mailbox_id is not None and any(card.get("mailbox_id") == mailbox_id for card in cards))
                or (cutoff is not None and message.created_at < cutoff)):
            conversations.add(message.conversation_id)
            session.delete(message)
    for key, delivery in list(values["deliveries"].items()):
        if ((mailbox_id is not None and mailbox_id in delivery["mailboxes"])
                or (cutoff is not None and datetime.fromisoformat(delivery["at"]) < cutoff)):
            conversations.update(session.scalars(select(ChatMessage.conversation_id).where(
                ChatMessage.id == delivery["message_id"], ChatMessage.kind.in_(["inbox_briefing", "inbox_urgent"]))))
            session.execute(delete(ChatMessage).where(ChatMessage.id == delivery["message_id"],
                ChatMessage.kind.in_(["inbox_briefing", "inbox_urgent"])))
            del values["deliveries"][key]
    if conversations:
        session.execute(delete(ChatContextCheckpoint).where(ChatContextCheckpoint.conversation_id.in_(conversations)))
    from app.inbox import MESSAGE_KINDS, QUOTED_HISTORY
    for checkpoint in session.scalars(select(ChatContextCheckpoint).join(ChatConversation,
            ChatContextCheckpoint.conversation_id == ChatConversation.id).where(
            ChatConversation.creator_person_id == founder_id, ChatConversation.visibility == "private")):
        if any(item.get("kind") in MESSAGE_KINDS and item.get("body_md") != QUOTED_HISTORY
                for item in json.loads(checkpoint.payload_json).get("items", [])):
            session.delete(checkpoint)


def set_consent(workspace_id, venture_id, mailbox_id, consent, *, now=None):
    if type(consent) is not bool or not isinstance(mailbox_id, str) or not mailbox_id:
        raise ValueError("inbox_invalid_consent")
    with db()() as session:
        founder_id = owner(session, workspace_id)
        lock(session, founder_id)
        venture = session.get(Venture, venture_id)
        if venture is None or venture.workspace_id != workspace_id or venture.archived_at is not None:
            raise PermissionError("inbox_venture_unavailable")
        existing = session.scalar(select(InboxBriefingState).where(
            InboxBriefingState.founder_id == founder_id, InboxBriefingState.mailbox_id == mailbox_id))
        if consent and not any(ref["mailbox_id"] == mailbox_id for ref in list_read_mailboxes(session, workspace_id)):
            raise PermissionError("inbox_mailbox_unavailable")
        if not consent and existing is None:
            raise PermissionError("inbox_mailbox_unavailable")
        mailbox = state(session, founder_id, workspace_id, mailbox_id)
        founder, values = settings(session, founder_id, workspace_id)
        if bool(mailbox.consent) != consent:
            from core.ids import new_id
            mailbox.consent = int(consent)
            mailbox.checkpoint = naive(now)
            save(mailbox, {"generation": new_id("ibg")})
        if not consent:
            session.execute(delete(InboxBriefingItem).where(InboxBriefingItem.founder_id == founder_id,
                InboxBriefingItem.mailbox_id == mailbox_id))
            _delete_deliveries(session, values, founder_id, mailbox_id=mailbox_id)
        values.setdefault("venture_id", venture_id)
        values.setdefault("offered_mailboxes", {})[mailbox_id] = True
        save(founder, values)
        session.commit()
    return {"mailbox_id": mailbox_id, "consent": consent}


def clock_minutes(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdigit():
        raise ValueError("inbox_invalid_time")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("inbox_invalid_time")
    return hour * 60 + minute


def update_settings(workspace_id, changes):
    from app.core.local_mode import is_local
    flags = None if is_local() else get_policy("feature_flags")
    allowed = {"timezone", "windows", "quiet_hours", "email_copy", "poll_minutes"}
    if is_local() or isinstance(flags, dict) and flags.get("cos_brief_v1") is True:
        allowed.add("cos_brief")
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError("inbox_invalid_settings")
    if "timezone" in changes:
        try:
            ZoneInfo(changes["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise ValueError("inbox_invalid_timezone") from None
    if "windows" in changes:
        windows = changes["windows"]
        if not isinstance(windows, list) or not 1 <= len(windows) <= 2:
            raise ValueError("inbox_invalid_windows")
        for window in windows:
            clock_minutes(window)
        if len(set(windows)) != len(windows):
            raise ValueError("inbox_invalid_windows")
    if "quiet_hours" in changes:
        quiet = changes["quiet_hours"]
        if not isinstance(quiet, dict) or set(quiet) != {"start", "end"}:
            raise ValueError("inbox_invalid_quiet_hours")
        for value in quiet.values():
            clock_minutes(value)
    if "cos_brief" in changes:
        schedule = changes["cos_brief"]
        if not isinstance(schedule, dict) or set(schedule) != {"weekday", "time"} or type(schedule["weekday"]) is not int \
                or not 0 <= schedule["weekday"] <= 6:
            raise ValueError("inbox_invalid_cos_brief")
        clock_minutes(schedule["time"])
    if "email_copy" in changes and changes["email_copy"] is not False:
        raise ValueError("inbox_email_copy_unavailable")
    if "poll_minutes" in changes and (type(changes["poll_minutes"]) is not int or not 5 <= changes["poll_minutes"] <= 1440):
        raise ValueError("inbox_invalid_poll_interval")
    with db()() as session:
        founder_id = owner(session, workspace_id)
        lock(session, founder_id)
        row, values = settings(session, founder_id, workspace_id)
        values.update(changes)
        save(row, values)
        session.commit()
        consent_ids = set(session.scalars(select(InboxBriefingState.mailbox_id).where(
            InboxBriefingState.founder_id == founder_id, InboxBriefingState.consent == 1)))
        return {**{key: values[key] for key in ("timezone", "windows", "quiet_hours", "email_copy", "poll_minutes")},
            "mailboxes": [{"mailbox_id": ref["mailbox_id"], "consent": ref["mailbox_id"] in consent_ids}
                for ref in list_read_mailboxes(session, workspace_id)]}


def feedback(workspace_id, item_id, category, scope="sender"):
    if not isinstance(category, str) or category not in {"important", "ignore"} or not isinstance(scope, str) or scope not in {"sender", "domain"}:
        raise ValueError("inbox_invalid_feedback")
    with db()() as session:
        founder_id = owner(session, workspace_id)
        lock(session, founder_id)
        item = session.get(InboxBriefingItem, item_id)
        if item is None or item.founder_id != founder_id or not consented(session, founder_id, item.mailbox_id):
            raise PermissionError("inbox_item_unavailable")
        row, values = settings(session, founder_id, workspace_id)
        target = item.sender_email if scope == "sender" else item.sender_email.rsplit("@", 1)[-1]
        values["rules"][scope + ":" + target] = category
        item.category, item.reason = category, "Your saved importance rule."
        save(row, values)
        session.commit()
    return {"item_id": item_id, "category": category}


def cleanup(*, now=None, founder_id=None):
    cutoff = naive(now) - timedelta(days=14)
    if founder_id is None:
        with db()() as session:
            if not inspect(session.get_bind()).has_table("inbox_briefing_items"):
                return 0
            result = session.execute(delete(InboxBriefingItem).where(InboxBriefingItem.received_at < cutoff))
            count = result.rowcount
            session.commit()
        cursor = ""
        while True:
            with db()() as session:
                founders = list(session.scalars(select(InboxBriefingState.founder_id).where(
                    InboxBriefingState.founder_id > cursor).distinct().order_by(InboxBriefingState.founder_id).limit(100)))
            if not founders:
                return count
            for owner_id in founders:
                count += cleanup(now=now, founder_id=owner_id)
            cursor = founders[-1]
    with db()() as session:
        if not inspect(session.get_bind()).has_table("inbox_briefing_items"):
            return 0
        lock(session, founder_id)
        items = delete(InboxBriefingItem).where(InboxBriefingItem.received_at < cutoff)
        states = select(InboxBriefingState)
        if founder_id:
            items = items.where(InboxBriefingItem.founder_id == founder_id)
            states = states.where(InboxBriefingState.founder_id == founder_id)
        result = session.execute(items)
        for row in session.scalars(states):
            values = json.loads(row.data_json)
            if row.mailbox_id:
                values["seen"] = {key: value for key, value in values.get("seen", {}).items()
                    if datetime.fromisoformat(value) >= cutoff}
            else:
                values.setdefault("deliveries", {})
                _delete_deliveries(session, values, row.founder_id, cutoff=cutoff)
            save(row, values)
        session.commit()
        return result.rowcount
