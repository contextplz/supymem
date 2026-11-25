"""Department tick: discovery, research, send, replies, bounces, LinkedIn, learn."""

from __future__ import annotations

import logging
import os
import re
import base64
import json
import hashlib
import time
from datetime import datetime
from email import message_from_string
from email.utils import parseaddr
from typing import Any

from sqlalchemy import select

from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmTouch
from app.gtm.supymem import composio_gmail, factory, learning, linkedin, pipeline, research
from app.state import db
from app.connections import mailboxes
from app.connections.composio_pacing import RateLimited
from app.core.local_mode import is_local

log = logging.getLogger(__name__)

_BOUNCE_Q = "newer_than:7d"
_READ_MAX_PAGES = 10
_READ_MAX_MESSAGES = 200
_READ_MAX_SECONDS = 30
_POS = ("interested", "let's talk", "sounds good", "book a", "book time", "yes", "love this")
# logic adapted from ethanplusai/harvey (MIT)
_NEG = ("unsubscribe", "stop emailing", "remove me", "take me off", "do not contact", "delete my data", "opt out", "opt-out",
    "stop contacting", "stop sending", "do not email", "don't contact", "don't email", "never contact", "delete my info")
_ESCALATE = re.compile(r"\b(?:legal action|lawsuit|cease and desist|lawyer|attorney|harass\w*|report(?:ing)? you|spam complaint|GDPR|CAN-SPAM|FTC|blacklist|angry|furious|fuck off)\b", re.I)
_AUTO = re.compile(r"\b(?:auto(?:matic|mated)?[ -]repl(?:y|ies)|out[ -]of[ -](?:the[ -])?office|on (?:annual )?leave|on vacation|away from (?:the )?office)\b")


def _reply_opt_out(text: str) -> bool:
    from app.gtm.relationships.email_compliance import OPT_OUT

    # shortcut: only intact footer URL prefixes are stripped, extend this if clients split the URL path.
    prefix = r"[ \t>]*" if OPT_OUT in (text or "") else r"[ \t]*>[ \t>]*"
    text = re.sub(rf"(?im)^({prefix}Stop these emails:[ \t]*(?:\r?\n[ \t>]*)?)<?https?://[^\s<>]+/public/unsubscribe", r"\1", text or "")
    return any(w in text.lower() for w in _NEG) or bool(re.search(r"(?im)^[ \t]*(?:stop|no thanks)(?:[.!]|\s*$)", text))


def _reply_intent(text: str) -> str | None:
    if _reply_opt_out(text):
        return "unsubscribe"
    if _ESCALATE.search(text or ""):
        return "escalate"
    if re.search(r"\b(?:wrong person|not (?:the|your) right (?:person|contact)|not (?:my|our) (?:department|responsibility)|talk to (?:my|our|the) (?:colleague|team|manager)|contact my colleague)\b", text or "", re.I):
        return "wrong_person"
    if re.search(r"\b(?:not now|not right now|try (?:again )?later|contact me (?:in|after)|reconnect (?:in|after)|under contract until)\b", text or "", re.I):
        return "not_now"
    if "?" in (text or "") or re.search(r"\b(?:tell me more|more information|how does|what does)\b", text or "", re.I):
        return "question"
    return None


def _classify_reply(text: str, *, headers: Any = None) -> str:
    """Conservative triage hints, never evidence of validated human demand."""
    low = (text or "").lower()
    intent = _reply_intent(text)  # Overrides are deterministic; no model sees an opt-out or legal threat.
    pairs = headers.items() if isinstance(headers, dict) else ((h.get("name"), h.get("value")) for h in headers if isinstance(h, dict)) if isinstance(headers, list) else ()
    automatic = any(str(name).lower() in {"auto-submitted", "x-autoreply", "x-autorespond"}
        and str(value or "").strip().lower() not in {"", "no", "false", "0"} for name, value in pairs)
    if automatic:
        return "automated"
    if intent == "unsubscribe":
        return "human_negative"
    if intent == "escalate":
        return intent
    if _AUTO.search(low):
        return "automated"
    if re.search(r"\b(?:not interested|not a fit|we're all set)\b", low):
        return "human_negative"
    if intent in {"wrong_person", "not_now"}:
        return intent
    if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in _POS):
        return "human_positive"
    if intent:
        return intent
    return "human_neutral"


def _iter_messages(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if isinstance(payload, dict):
        for key in ("messages", "data", "items", "emails"):
            val = payload.get(key)
            if isinstance(val, list):
                return [m for m in val if isinstance(m, dict)]
            if isinstance(val, dict):
                inner = _iter_messages(val)
                if inner:
                    return inner
    return []


def _message_text(message: dict[str, Any]) -> str:
    for key in ("messageText", "body_text"):
        if isinstance(message.get(key), str) and message[key]:
            return message[key]
    texts = []

    def collect(part):
        if not isinstance(part, dict):
            return
        mime = part.get("mimeType") or part.get("mime_type")
        body = part.get("body") or {}
        if mime in {"text/plain", "text/html"} and isinstance(body, dict) and isinstance(body.get("data"), str):
            try:
                text = base64.urlsafe_b64decode(body["data"] + "===").decode("utf-8", errors="replace")
                if mime == "text/html":
                    from html import unescape
                    text = unescape(re.sub(r"<[^>]*>", " ", text))
                texts.append((mime, text))
            except ValueError:
                pass
        for child in part.get("parts") or []:
            collect(child)

    collect(message.get("payload") or message)
    plain = [text for mime, text in texts if mime == "text/plain"]
    return "\n".join(plain or [text for _, text in texts]) or str(message.get("snippet") or "")


def _read_mailbox(session, workspace_id, mailbox_id):
    if is_local():
        from app.gtm.local_mail import settings
        if not settings().imap_host:
            return None
        ref = mailboxes.resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)
        return session.get(GtmEmailAccount, ref["mailbox_id"])
    if not mailboxes.enabled():
        if mailbox_id is not None:
            return session.scalar(pipeline._mailboxes_query(session, workspace_id).where(
                GtmEmailAccount.id == mailbox_id, GtmEmailAccount.status == "active"))
        return pipeline._active_mailbox(session, workspace_id)
    refs = mailboxes.list_read_mailboxes(session, workspace_id)
    if not any(ref["mailbox_id"] == mailbox_id for ref in refs):
        raise mailboxes.MailboxUnavailable("mailbox_unavailable")
    return session.get(GtmEmailAccount, mailbox_id)


def _read_accounts(session, workspace_id, mailbox):
    if is_local():
        return list(session.scalars(select(GtmEmailAccount).where(
            GtmEmailAccount.workspace_id.in_(pipeline._gmail_workspace_ids(session, workspace_id)),
            GtmEmailAccount.provider == "smtp", GtmEmailAccount.connected_account_id == mailbox.connected_account_id)))
    return list(session.scalars(pipeline._mailboxes_query(session, workspace_id).where(
        GtmEmailAccount.connected_account_id == mailbox.connected_account_id).order_by(None).order_by(GtmEmailAccount.id)))


def _fanout(workspace_id, tick, *, replies=False):
    with db()() as session:
        refs = mailboxes.list_read_mailboxes(session, workspace_id)
    result = {"marked": [], "mailboxes": []}
    if replies:
        result["replies"] = []
    seen = set()
    for ref in refs:
        if ref["connected_account_id"] in seen:
            continue
        seen.add(ref["connected_account_id"])
        try:
            outcome = tick(workspace_id, mailbox_id=ref["mailbox_id"])
        except RateLimited:
            raise
        except Exception as exc:
            log.warning("gmail_mailbox_read_failed", extra={"mailbox_id": ref["mailbox_id"], "error_type": type(exc).__name__})
            outcome = {"marked": [], "error": "gmail_mailbox_read_failed"}
        result["mailboxes"].append({"mailbox_id": ref["mailbox_id"], **outcome})
        result["marked"].extend(outcome.get("marked", []))
        if replies:
            result["replies"].extend(outcome.get("replies", []))
    return result


def _read_call(workspace_id, tool, *args, **kwargs):
    from app.connections.composio_pacing import BACKGROUND
    from app.credits import metered
    from app.tasks import usage

    token = BACKGROUND.set(True)
    try:
        usage.before_tool()
        with metered.charge_sync(workspace_id=workspace_id, purpose="gtm.gmail.read",
                provider="composio", estimate_micro=0, payer="founder") as meter:
            result = tool(*args, **kwargs)
            meter.record_micro(0)
        return result
    finally:
        BACKGROUND.reset(token)


def _check_read_budget(started, pages, messages):
    if pages > _READ_MAX_PAGES or messages > _READ_MAX_MESSAGES or time.monotonic() - started >= _READ_MAX_SECONDS:
        raise ValueError("gmail_read_budget_exceeded")


def _fetch_limit():
    return max(1, min(_READ_MAX_MESSAGES, int(os.getenv("GMAIL_READ_BATCH_SIZE", "20"))))


def _read_message(workspace_id, uid, ca, ident):
    message = _read_call(workspace_id, composio_gmail.fetch_message, composio_user_id=uid,
        connected_account_id=ca, message_id=ident)
    if not isinstance(message, dict) or (message.get("id") or message.get("message_id") or message.get("messageId")) != ident:
        raise ValueError("gmail_message_fetch_failed")
    return message


def _history_messages(workspace_id, uid, ca, checkpoint):
    progress = dict(checkpoint) if isinstance(checkpoint, dict) else {"history_id": checkpoint}
    progress.pop("retry_after_s", None)
    ids = progress.pop("pending_ids", [])
    started = time.monotonic()
    if not ids:
        token = progress.get("page_token")
        history = _read_call(workspace_id, composio_gmail.fetch_history, composio_user_id=uid,
            connected_account_id=ca, start_history_id=progress["history_id"], page_token=token)
        if not isinstance(history, dict) or not history.get("historyId"):
            raise ValueError("gmail_history_invalid")
        ids = sorted({item["message"]["id"] for entry in history.get("history") or []
            for item in entry.get("messagesAdded") or [] if item.get("message", {}).get("id")})
        next_token = history.get("nextPageToken")
        seen = progress.get("page_tokens", [])
        if next_token and (next_token == token or next_token in seen):
            raise ValueError("gmail_history_pagination_repeated")
        progress.update(target_history_id=history["historyId"], page_token=next_token,
            page_tokens=seen + ([next_token] if next_token else []))
    messages = []
    completed = 0
    for ident in ids[:_fetch_limit()]:
        try:
            _check_read_budget(started, 1, len(messages))
        except ValueError:
            if not completed:
                raise
            break
        try:
            message = _read_message(workspace_id, uid, ca, ident)
        except RateLimited as exc:
            if not completed:
                raise
            progress["retry_after_s"] = exc.retry_after_s
            break
        except composio_gmail.ReconnectRequired:
            raise
        except composio_gmail.DefiniteFailure as exc:
            if exc.status_code != 404:
                raise ValueError("gmail_message_fetch_failed") from exc
            completed += 1
            continue
        except Exception as exc:
            raise ValueError("gmail_message_fetch_failed") from exc
        messages.append(message)
        completed += 1
    progress["pending_ids"] = ids[completed:]
    next_history = progress if progress["pending_ids"] or progress.get("page_token") else progress["target_history_id"]
    return messages, next_history


def _compliance_history_messages(workspace_id, uid, ca, checkpoint):
    started = time.monotonic()
    messages, pages, progress = [], 0, checkpoint
    while True:
        if not isinstance(progress, dict) or not progress.get("pending_ids"):
            pages += 1
        _check_read_budget(started, pages, len(messages))
        try:
            batch, progress = _history_messages(workspace_id, uid, ca, progress)
        except RateLimited as exc:
            if not messages:
                raise
            return messages, {**progress, "retry_after_s": exc.retry_after_s}
        messages.extend(batch)
        _check_read_budget(started, pages, len(messages))
        if not isinstance(progress, dict):
            return messages, progress
        if progress.get("retry_after_s"):
            return messages, progress


def _bootstrap_replies(workspace_id, uid, ca, tracked, checkpoint):
    progress = dict(checkpoint or {})
    progress.pop("retry_after_s", None)
    if not progress.get("target_history_id"):
        try:
            profile = _read_call(workspace_id, composio_gmail.get_gmail_profile, uid, ca)
            history_id = composio_gmail._payload(profile.get("raw")).get("historyId")
        except (RateLimited, composio_gmail.ReconnectRequired):
            raise
        except Exception:
            history_id = None
        if history_id:
            progress = {"target_history_id": history_id}
    if "bootstrap_tasks" not in progress:
        progress.setdefault("target_history_id", None)
        progress["bootstrap_tasks"] = sorted({"message:" + item[4] for item in tracked
            if not item[3] and item[4] and not str(item[4]).startswith("<")}) + sorted({"thread:" + item[3] for item in tracked if item[3]})
    pending = set(progress["bootstrap_tasks"])
    covered = {"message:" + item[4] for item in tracked
        if item[4] and item[3] and "thread:" + item[3] in pending}
    tasks = list(dict.fromkeys(task for task in progress["bootstrap_tasks"] if task not in covered))
    messages = []
    completed = 0
    pending_messages = []
    started = time.monotonic()
    for task in tasks[:max(1, _fetch_limit() - 1)]:
        try:
            _check_read_budget(started, 1, len(messages))
        except ValueError:
            if not completed:
                raise
            break
        kind, ident = task.split(":", 1)
        try:
            if kind == "message":
                batch = [_read_message(workspace_id, uid, ca, ident)]
            else:
                data = _read_call(workspace_id, composio_gmail.fetch_thread, composio_user_id=uid,
                    connected_account_id=ca, thread_id=ident)
                batch = _iter_messages(data)
        except RateLimited as exc:
            if not completed:
                raise
            progress["retry_after_s"] = exc.retry_after_s
            break
        room = _fetch_limit() - len(messages)
        messages.extend(batch[:room])
        for message in batch[room:]:
            ident = message.get("id") or message.get("message_id") or message.get("messageId")
            if not ident:
                raise ValueError("gmail_message_fetch_failed")
            pending_messages.append("message:" + ident)
        completed += 1
        if len(messages) >= _fetch_limit():
            break
    progress["bootstrap_tasks"] = pending_messages + tasks[completed:]
    return messages, progress if progress["bootstrap_tasks"] else progress.get("target_history_id")


def claim_read(workspace_id, mailbox_id, *, window=None):
    from datetime import timedelta
    from core.clock import utcnow
    from core.ids import new_id
    from app.inbox import store
    store.ensure_schema(db().kw["bind"])
    with db()() as session:
        account = _read_mailbox(session, workspace_id, mailbox_id)
        if account is None or account.id != mailbox_id:
            raise mailboxes.MailboxUnavailable("mailbox_unavailable")
        account = next(account for account in _read_accounts(session, workspace_id, account) if account.status == "active")
        founder_id = store.owner(session, workspace_id)
        store.lock(session, founder_id)
        row = store.state(session, founder_id, account.workspace_id, account.id)
        now = utcnow().replace(tzinfo=None)
        data = json.loads(row.data_json or "{}")
        next_poll = max(data.get("gmail_polls", {}).values(), default=None)
        if window is not None and (data.get("gmail_latest_window", "") > window
                or next_poll and datetime.fromisoformat(next_poll) > now):
            return None
        for field in ("gmail_lease_until", "gmail_backoff_until"):
            if data.get(field):
                wait = (datetime.fromisoformat(data[field]) - now).total_seconds()
                if wait > 0:
                    raise RateLimited(wait)
        if next_poll and datetime.fromisoformat(next_poll) > now:
            raise RateLimited((datetime.fromisoformat(next_poll) - now).total_seconds())
        token = new_id("gml")
        data.update(gmail_lease_id=token, gmail_lease_until=(now + timedelta(minutes=10)).isoformat())
        store.save(row, data)
        session.commit()
        return founder_id, row.id, token, account.workspace_id, account.id


def release_read(claim, *, error=None):
    from datetime import timedelta
    from core.clock import utcnow
    from app.inbox import store
    with db()() as session:
        store.lock(session, claim[0])
        row = session.get(store.InboxBriefingState, claim[1])
        data = json.loads(row.data_json or "{}")
        until = data.get("gmail_lease_until")
        if (data.get("gmail_lease_id") != claim[2] or not until
                or store.naive(datetime.fromisoformat(until)) <= store.naive(utcnow())):
            return
        failures = data.get("gmail_failures", 0) + (not isinstance(error, RateLimited)) if error else 0
        delay = (error.retry_after_s if isinstance(error, RateLimited) else min(1800, 30 * 2 ** min(failures, 6))) if error else 600
        due = (utcnow().replace(tzinfo=None) + timedelta(seconds=delay)).isoformat()
        data["gmail_failures"] = failures
        if error:
            data["gmail_backoff_until"] = due
            if isinstance(error, RateLimited):
                now = utcnow().replace(tzinfo=None)
                warned = data.get("gmail_warning_at")
                if not warned or (now - datetime.fromisoformat(warned)).total_seconds() >= 60:
                    data["gmail_warning_at"] = now.isoformat()
                    log.warning("gmail_read_deferred", extra={"account": row.id, "retry_after_s": delay})
        else:
            data.setdefault("gmail_polls", {})[claim[3]] = due
            data.pop("gmail_backoff_until", None)
        data.pop("gmail_lease_until", None)
        store.save(row, data)
        session.commit()


def _fence_read(session, workspace_id, mailbox_id, claim):
    if claim is None:
        return
    from app.core.jobs import RetryableError
    from app.inbox import store
    from core.clock import utcnow

    if claim[3] != workspace_id:
        raise PermissionError("gmail_read_claim_scope_mismatch")
    store.lock(session, claim[0])
    row = session.get(store.InboxBriefingState, claim[1], with_for_update=True, populate_existing=True)
    if row is None or row.founder_id != claim[0] or row.mailbox_id != mailbox_id:
        raise RetryableError("gmail_read_lease_lost")
    data = json.loads(row.data_json or "{}")
    until = data.get("gmail_lease_until")
    if (data.get("gmail_lease_id") != claim[2] or not until
            or store.naive(datetime.fromisoformat(until)) <= store.naive(utcnow())):
        raise RetryableError("gmail_read_lease_lost")
    if store.owner(session, workspace_id) != claim[0]:
        raise PermissionError("gmail_read_claim_scope_mismatch")


def _bootstrap_bounces(workspace_id, uid, ca, checkpoint):
    progress = dict(checkpoint or {})
    if not progress.get("target_history_id"):
        try:
            profile = _read_call(workspace_id, composio_gmail.get_gmail_profile, uid, ca)
            history_id = composio_gmail._payload(profile.get("raw")).get("historyId")
        except (RateLimited, composio_gmail.ReconnectRequired):
            raise
        except Exception:
            history_id = None
        if history_id:
            progress = {"target_history_id": history_id}
    progress.setdefault("target_history_id", None)
    token = progress.get("bootstrap_page")
    data = _read_call(workspace_id, composio_gmail.fetch_query, composio_user_id=uid,
        connected_account_id=ca, query=_BOUNCE_Q, max_results=_fetch_limit(), include_payload=True,
        **({"page_token": token} if token else {}))
    messages = _iter_messages(data)
    if len(messages) > _fetch_limit():
        raise ValueError("gmail_read_budget_exceeded")
    token = (data.get("nextPageToken") or data.get("next_page_token")) if isinstance(data, dict) else None
    seen = progress.get("page_tokens", [])
    if token and token in seen:
        raise ValueError("gmail_bounce_pagination_repeated")
    progress.update(bootstrap_page=token, page_tokens=seen + ([token] if token else []))
    return messages, progress if token else progress.get("target_history_id")


def _touches_for_mailbox(session, workspace_id, account_id, touches):
    mailbox = session.get(GtmEmailAccount, account_id)
    account_ids = {account.id for account in _read_accounts(session, workspace_id, mailbox)} if mailbox else {account_id}
    events = session.scalars(select(GtmEmailEvent).where(
        GtmEmailEvent.workspace_id.in_({getattr(touch, "workspace_id", workspace_id) for touch in touches}), GtmEmailEvent.email_account_id.is_not(None),
        GtmEmailEvent.event_type.in_(["sent", "SENT", "sent_metadata"])))
    events_by_contact, invalid_contacts = {}, set()
    for event in events:
        try:
            metadata = json.loads(event.metadata_json or "{}")
            if not isinstance(metadata, dict):
                raise ValueError("gmail_attribution_invalid")
        except (TypeError, ValueError):
            invalid_contacts.add(event.contact_id)
            continue
        events_by_contact.setdefault(event.contact_id, []).append((event, metadata))
    attributed = []
    for touch in touches:
        if touch.channel != "email":
            continue
        if getattr(touch, "email_account_id", None):
            if touch.email_account_id in account_ids:
                attributed.append(touch)
            continue
        if touch.contact_id in invalid_contacts:
            continue
        attributed_ids = set()
        for event, metadata in events_by_contact.get(touch.contact_id, []):
            if (event.idempotency_key in {"sent-metadata:" + touch.id, "conversation-mailbox:" + touch.id}
                    or touch.provider_thread_id and metadata.get("thread_id") == touch.provider_thread_id
                    and (event.provider_message_id and event.provider_message_id in {touch.message_id, touch.in_reply_to}
                         or metadata.get("rfc_message_id") and metadata["rfc_message_id"] == touch.message_id)):
                attributed_ids.add(event.email_account_id)
        if attributed_ids and attributed_ids <= account_ids:
            attributed.append(touch)
    return attributed


def _mailbox_message_key(account_id, message_id):
    return "gmail:" + hashlib.sha256(json.dumps([account_id, message_id]).encode()).hexdigest()


def _record_read_event(session, workspace_id, account_id, event_type, message_id, *, contact_id=None, metadata=None):
    from core.clock import utcnow
    from core.ids import new_id

    key = event_type + ":" + _mailbox_message_key(account_id, message_id)
    if not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
        session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=workspace_id, email_account_id=account_id,
            contact_id=contact_id, event_type=event_type, provider_message_id=message_id,
            idempotency_key=key, metadata_json=json.dumps(metadata or {}), created_at=utcnow()))


def tick_replies(workspace_id: str, *, mailbox_id=None, read_claim=None) -> dict[str, Any]:
    from core.clock import utcnow
    from core.ids import new_id
    from app.connections.composio_migration import reconcile_gmail_project
    if is_local():
        from app.gtm import local_mail
        if not local_mail.enabled("read_replies"):
            return {"marked": [], "replies": [], "skipped": "read_replies_disabled", "message": local_mail.blocked_message("read_replies")}
    multi = mailboxes.enabled()
    if multi and mailbox_id is None:
        return _fanout(workspace_id, tick_replies, replies=True)
    if not multi and not is_local():
        reconcile_gmail_project(workspace_id)

    marked, replies = [], []
    with db()() as session:
        _fence_read(session, workspace_id, mailbox_id, read_claim)
        mailbox = _read_mailbox(session, workspace_id, mailbox_id)
        if mailbox is None or str(mailbox.connected_account_id or "").startswith("sim_"):
            paused = session.scalar(pipeline._mailboxes_query(session, workspace_id).where(GtmEmailAccount.status == "needs_reconnect"))
            return {"marked": [], "replies": [], "skipped": "gmail_reconnect_required" if paused else "no_live_mailbox"}
        workspace_ids = pipeline._gmail_workspace_ids(session, workspace_id) if read_claim else [workspace_id]
        touches = list(session.scalars(select(GtmTouch).join(GtmContact, GtmContact.id == GtmTouch.contact_id).where(GtmTouch.workspace_id.in_(workspace_ids),
            GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None),
            GtmContact.bounced_at.is_(None), GtmContact.do_not_contact == 0)))
        if multi:
            account_ids = {account.id for account in _read_accounts(session, workspace_id, mailbox)}
            touches = _touches_for_mailbox(session, workspace_id, mailbox.id, touches)
            for touch in touches:
                key = "sent-metadata:" + touch.id
                existing = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == key))
                if existing and existing.email_account_id not in account_ids | {None}:
                    raise mailboxes.MailboxUnavailable("conversation_mismatch")
                rfc_id = json.loads(existing.metadata_json or "{}").get("rfc_message_id") if existing else None
                if not existing or existing.email_account_id != mailbox.id or json.loads(existing.metadata_json or "{}").get("thread_id") != touch.provider_thread_id:
                    key = "conversation-mailbox:" + touch.id
                    existing = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.idempotency_key == key))
                if not existing:
                    session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=touch.workspace_id, email_account_id=mailbox.id,
                        contact_id=touch.contact_id, event_type="sent_metadata", idempotency_key=key,
                        metadata_json=json.dumps({"thread_id": touch.provider_thread_id,
                            "rfc_message_id": rfc_id or (touch.message_id if str(touch.message_id or "").startswith("<") else None)}), created_at=utcnow()))
            session.commit()
        tracked = [(touch.id, touch.contact_id, touch.venture_id, touch.provider_thread_id,
                    touch.message_id, touch.in_reply_to) for touch in touches]
        by_email = {(contact.email or "").strip().casefold(): contact.id for contact in session.scalars(
            select(GtmContact).where(GtmContact.workspace_id.in_(workspace_ids), GtmContact.times_emailed > 0,
                GtmContact.bounced_at.is_(None))) if contact.email and (not multi or contact.id in {touch.contact_id for touch in touches})}
        metadata_query = select(GtmEmailEvent).where(GtmEmailEvent.workspace_id.in_(workspace_ids), GtmEmailEvent.event_type == "sent_metadata")
        if multi:
            metadata_query = metadata_query.where(GtmEmailEvent.email_account_id.in_(account_ids),
                GtmEmailEvent.contact_id.in_({touch.contact_id for touch in touches}))
        metadata = {event.idempotency_key: json.loads(event.metadata_json or "{}") for event in session.scalars(metadata_query)}
        tracked = [(item[0], item[1], item[2], item[3],
            metadata.get("sent-metadata:" + item[0], {}).get("rfc_message_id")
            or (metadata.get("conversation-mailbox:" + item[0], {}) if multi else {}).get("rfc_message_id") or item[4], item[5]) for item in tracked]
        ca = mailbox.connected_account_id
        uid = mailbox.composio_user_id
        sender, account_id = (mailbox.email or "").casefold(), mailbox.id
        cursor = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == account_id,
            GtmEmailEvent.workspace_id == workspace_id,
            GtmEmailEvent.event_type == "gmail_sync_checkpoint").order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
        checkpoint = json.loads(cursor.metadata_json or "{}") if cursor else None
    if not tracked:
        return {"marked": [], "replies": []}
    messages, next_history = [], None
    if checkpoint and "bootstrap_tasks" not in checkpoint:
        try:
            messages, next_history = _compliance_history_messages(workspace_id, uid, ca, checkpoint)
        except RateLimited:
            raise
        except composio_gmail.ReconnectRequired:
            return {"marked": [], "replies": [], "skipped": "gmail_reconnect_required"}
        except composio_gmail.DefiniteFailure as exc:
            if exc.status_code != 404:
                return {"marked": [], "replies": [], "error": "gmail_history_fetch_failed"}
            checkpoint = None
        except Exception as exc:
            return {"marked": [], "replies": [], "error": "gmail_message_fetch_failed" if str(exc) == "gmail_message_fetch_failed" else "gmail_history_fetch_failed"}
    if not checkpoint or "bootstrap_tasks" in checkpoint:
        try:
            messages, next_history = _bootstrap_replies(workspace_id, uid, ca, tracked, checkpoint)
        except RateLimited:
            raise
        except composio_gmail.ReconnectRequired:
            return {"marked": [], "replies": [], "skipped": "gmail_reconnect_required"}
        except Exception as exc:
            return {"marked": [], "replies": [], "error": str(exc)[:200]}
    threads = {item[3] for item in tracked if item[3]}
    for message in messages:
        ident = message.get("id") or message.get("message_id") or message.get("messageId")
        rfc_id = composio_gmail.message_headers(message).get("message-id")
        if not rfc_id:
            continue
        for index, item in enumerate(tracked):
            if ident in {item[4], item[5]}:
                with db()() as session:
                    _fence_read(session, workspace_id, account_id, read_claim)
                    key = "sent-metadata:" + item[0]
                    if not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
                        session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=session.get(GtmContact, item[1]).workspace_id, email_account_id=account_id,
                            contact_id=item[1], event_type="sent_metadata", idempotency_key=key,
                            provider_message_id=ident, metadata_json=json.dumps({"rfc_message_id": rfc_id, **({"thread_id": item[3]} if multi else {})}), created_at=utcnow()))
                    session.commit()
                tracked[index] = (*item[:4], rfc_id, ident)
    for message in messages:
        headers = composio_gmail.message_headers(message)
        ident = message.get("id") or message.get("message_id") or message.get("messageId")
        thread = message.get("threadId") or message.get("thread_id")
        text = _message_text(message)
        if not ident or "SENT" in (message.get("labelIds") or []) or parseaddr(headers.get("from") or message.get("sender") or message.get("from") or "")[1].casefold() == sender:
            continue
        if _delivery_status(message):
            _apply_delivery_status(message, by_email, email_account_id=account_id if multi else None)
            continue
        kind = _classify_reply(text, headers=headers)
        if kind == "automated" and not _reply_opt_out(text):
            from app.experiments.outreach_tests import calendar_booked
            if not calendar_booked(text, headers, headers.get("subject", "")):
                continue
        references = set(re.findall(r"<[^<>\s]+>", headers.get("in-reply-to", "") + " " + headers.get("references", "")))
        matching = [item for item in tracked if item[4] in references or not multi and thread in threads and item[3] == thread]
        if not matching:
            matching = [item for item in tracked if thread in threads and item[3] == thread]
        owners = {(item[1], item[2]) for item in matching}
        if len(owners) != 1 or any(ident in {item[4], item[5]} for item in tracked):
            continue
        contact_id, venture_id = owners.pop()
        matched_thread = next((item[3] for item in matching if item[3]), None)
        try:
            reply_args = dict(body=text, classification=kind, venture_id=venture_id,
                provider_thread_id=matched_thread, in_reply_to=headers.get("in-reply-to"), references=headers.get("references"))
            recorded = (pipeline.apply_reply(contact_id, email_account_id=account_id, provider_message_id=ident, **reply_args) if multi
                        else pipeline.apply_reply(contact_id, provider_message_id=ident, **reply_args))
        except ValueError:
            return {"marked": marked, "replies": replies, "error": "reply_scope_unconfirmed"}
        if multi:
            from app.connections.bookings import capture_gmail_reply_headers
            capture_gmail_reply_headers(venture_id, ident, headers, email_account_id=account_id,
                reply_message_id=ident)
        if recorded:
            from app.experiments.outreach_tests import capture_booking_headers, record_reply_activity
            if not multi:
                capture_booking_headers(venture_id, ident, headers)
            record_reply_activity(venture_id, [item[0] for item in matching], classification=kind, observed_at=utcnow())
            marked.append(contact_id)
            replies.append({"contact_id": contact_id, "venture_id": venture_id, "kind": kind, "body": text[:500], "message_id": ident,
                **({"mailbox_id": account_id, "message_id": _mailbox_message_key(account_id, ident), "provider_message_id": ident} if multi else {})})
    if next_history:
        with db()() as session:
            _fence_read(session, workspace_id, account_id, read_claim)
            if multi:
                _read_mailbox(session, workspace_id, account_id)
            session.add(GtmEmailEvent(id=new_id("gtmee"), email_account_id=account_id, workspace_id=workspace_id,
                event_type="gmail_sync_checkpoint", created_at=utcnow(), metadata_json=json.dumps(
                    next_history if isinstance(next_history, dict) else {"history_id": str(next_history)})))
            session.commit()
    return {"marked": marked, "replies": replies,
        **({"retry_after_s": next_history["retry_after_s"]} if isinstance(next_history, dict) and next_history.get("retry_after_s") else {})}


def tick_bounces(workspace_id: str, *, mailbox_id=None, read_claim=None) -> dict[str, Any]:
    if is_local():
        from app.gtm import local_mail
        if not local_mail.enabled("read_replies"):
            return {"marked": [], "skipped": "read_replies_disabled", "message": local_mail.blocked_message("read_replies")}
    from app.connections.composio_migration import reconcile_gmail_project
    multi = mailboxes.enabled()
    if multi and mailbox_id is None:
        return _fanout(workspace_id, tick_bounces)
    if not multi and not is_local():
        reconcile_gmail_project(workspace_id)
    marked: list[str] = []
    with db()() as session:
        _fence_read(session, workspace_id, mailbox_id, read_claim)
        mailbox = _read_mailbox(session, workspace_id, mailbox_id)
        if mailbox is None or str(mailbox.connected_account_id or "").startswith("sim_"):
            return {"marked": [], "skipped": "no_live_mailbox"}
        ca = mailbox.connected_account_id
        uid = mailbox.composio_user_id
        account_id = mailbox.id
        workspace_ids = pipeline._gmail_workspace_ids(session, workspace_id) if read_claim else [workspace_id]
        emailed = list(
            session.scalars(
                select(GtmContact).where(
                    GtmContact.workspace_id.in_(workspace_ids),
                    GtmContact.times_emailed > 0,
                    True if multi else GtmContact.bounced_at.is_(None),
                )
            )
        )
        by_email = {(c.email or "").strip().casefold(): c.id for c in emailed if c.email}
        if multi:
            touches = list(session.scalars(select(GtmTouch).where(GtmTouch.workspace_id.in_(workspace_ids),
                GtmTouch.direction == "out", GtmTouch.channel == "email", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))))
            contact_ids = {touch.contact_id for touch in _touches_for_mailbox(session, workspace_id, mailbox.id, touches)}
            by_email = {email: contact_id for email, contact_id in by_email.items() if contact_id in contact_ids}
            if not by_email:
                return {"marked": [], "skipped": "no_tracked_contacts"}
        cursor = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.workspace_id == workspace_id,
            GtmEmailEvent.email_account_id == account_id, GtmEmailEvent.event_type == "gmail_bounce_checkpoint")
            .order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
        checkpoint = json.loads(cursor.metadata_json or "{}") if cursor else None
    try:
        next_history = None
        if checkpoint and "bootstrap_page" not in checkpoint:
            try:
                data, next_history = _compliance_history_messages(workspace_id, uid, ca, checkpoint)
            except composio_gmail.ReconnectRequired:
                raise
            except composio_gmail.DefiniteFailure as exc:
                if exc.status_code != 404:
                    raise
                checkpoint = None
        if not checkpoint or "bootstrap_page" in checkpoint:
            data, next_history = _bootstrap_bounces(workspace_id, uid, ca, checkpoint)
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        log.info("bounce fetch skipped: %s", exc)
        return {"marked": [], "error": str(exc)[:200]}
    messages = _iter_messages(data)
    for message in messages:
        message_id = message.get("id") or message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        marked.extend(_apply_delivery_status(message, by_email, email_account_id=account_id if multi else None))
    if next_history:
        with db()() as session:
            _fence_read(session, workspace_id, account_id, read_claim)
            _read_mailbox(session, workspace_id, account_id)
            _record_read_event(session, workspace_id, account_id, "gmail_bounce_checkpoint", str(next_history),
                metadata=next_history if isinstance(next_history, dict) else {"history_id": str(next_history)})
            session.commit()
    return {"marked": marked,
        **({"retry_after_s": next_history["retry_after_s"]} if isinstance(next_history, dict) and next_history.get("retry_after_s") else {})}


def _apply_delivery_status(message: dict[str, Any], by_email: dict[str, str], *, email_account_id=None) -> list[str]:
    marked = []
    message_id = message.get("id") or message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return marked
    for recipient, action, status in _delivery_status(message):
        contact_id = by_email.get(recipient)
        if contact_id and action == "failed" and re.fullmatch(r"[45]\.\d+\.\d+", status):
            provider_id = message_id
            if email_account_id:
                with db()() as session:
                    contact = session.get(GtmContact, contact_id)
                    if not contact:
                        continue
                    workspace_id = contact.workspace_id
                    _read_mailbox(session, workspace_id, email_account_id)
                    legacy_key = "bounce:" + hashlib.sha256(json.dumps([workspace_id, message_id, recipient]).encode()).hexdigest()
                    if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == legacy_key)):
                        continue
                provider_id = _mailbox_message_key(email_account_id, message_id)
            if pipeline.apply_bounce(contact_id, reason=status, hard_bounce=status.startswith("5."), provider_message_id=provider_id):
                marked.append(contact_id)
            if email_account_id:
                with db()() as session:
                    _record_read_event(session, workspace_id, email_account_id, "gmail_bounce_attribution", message_id,
                        contact_id=contact_id, metadata={"recipient": recipient, "status": status})
                    session.commit()
        elif contact_id and action == "delivered" and re.fullmatch(r"2\.\d+\.\d+", status):
            from app.gtm.email_learning import record
            with db()() as session:
                contact = session.get(GtmContact, contact_id)
                if contact and contact.times_emailed > 0 and not contact.bounced_at:
                    if email_account_id:
                        _read_mailbox(session, contact.workspace_id, email_account_id)
                        _record_read_event(session, contact.workspace_id, email_account_id, "gmail_delivery_attribution", message_id,
                            contact_id=contact_id, metadata={"recipient": recipient})
                    record(session, contact, "delivery", _mailbox_message_key(email_account_id, message_id) if email_account_id else message_id)
                    session.commit()
    return marked


def _delivery_status(message: dict[str, Any]) -> list[tuple[str, str, str]]:
    blocks = []

    def collect(value):
        if isinstance(value, str):
            for part in message_from_string(value).walk():
                if part.get_content_type() == "message/delivery-status" and isinstance(part.get_payload(), list):
                    blocks.extend(part.get_payload())
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            mime = value.get("mimeType") or value.get("mime_type")
            if mime == "message/delivery-status":
                body = value.get("body") or {}
                encoded = body.get("data") if isinstance(body, dict) else None
                try:
                    text = base64.urlsafe_b64decode(encoded + "===").decode("utf-8", errors="replace") if encoded else body if isinstance(body, str) else value.get("text", "")
                    collect("Content-Type: message/delivery-status\n\n" + text)
                except (ValueError, TypeError):
                    pass
            else:
                for key in ("raw", "payload", "parts", "body", "body_text", "messageText"):
                    if key in value:
                        if key == "raw" and isinstance(value[key], str) and "Content-Type:" not in value[key]:
                            try:
                                collect(base64.urlsafe_b64decode(value[key] + "===").decode("utf-8", errors="replace"))
                            except ValueError:
                                pass
                        else:
                            collect(value[key])
    collect(message)
    result = []
    for block in blocks:
        field = block.get("Final-Recipient") or block.get("Original-Recipient") or ""
        recipient = field.split(";", 1)[-1].strip().strip("<>").casefold()
        if ";" in field and field.split(";", 1)[0].strip().lower() == "rfc822" and re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", recipient):
            result.append((recipient, str(block.get("Action") or "").strip().lower(), str(block.get("Status") or "").strip()))
    return result


def _bounce_recipients(message: dict[str, Any]) -> tuple[set[str], str]:
    texts: list[str] = []

    def collect(value):
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, list):
            for part in value:
                collect(part)
        elif isinstance(value, dict):
            for key in ("snippet", "text", "body", "body_text", "messageText", "payload", "parts"):
                if key in value:
                    collect(value[key])
            if isinstance(value.get("data"), str):
                try:
                    texts.append(base64.urlsafe_b64decode(value["data"] + "===").decode("utf-8", errors="replace"))
                except ValueError:
                    pass
            headers = value.get("headers")
            pairs = headers.items() if isinstance(headers, dict) else ((h.get("name"), h.get("value")) for h in headers if isinstance(h, dict)) if isinstance(headers, list) else ()
            for name, content in pairs:
                if str(name).casefold() in {"final-recipient", "original-recipient", "x-failed-recipients"}:
                    texts.append(f"{name}: {content}")

    collect(message)
    diagnostic = "\n".join(texts)
    for text in list(texts):
        for part in message_from_string(text).walk():
            if part.get_content_type() == "message/delivery-status":
                for block in part.get_payload():
                    for name in ("Final-Recipient", "Original-Recipient"):
                        if block.get(name):
                            texts.append(f"{name}: {block[name]}")
    diagnostic = "\n".join(texts)
    fields = re.findall(r"(?im)^(?:Final-Recipient|Original-Recipient):\s*rfc822\s*;\s*([^\r\n]+)|^X-Failed-Recipients:\s*([^\r\n]+)", diagnostic)
    targets = [first or second for first, second in fields]
    targets += re.findall(r"(?i)(?:wasn't|was not|couldn't be|could not be) delivered to\s+<?([^\s<>]+@[^\s<>]+)>?", diagnostic)
    addresses = set()
    for target in targets:
        addresses.update(address.casefold() for address in re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", target))
    return addresses, diagnostic


def tick_discovery(workspace_id: str) -> dict[str, Any]:
    if not factory.discovery_needed(workspace_id):
        return {"ran": False, "reason": "buffer_ok_or_stopped"}
    from app.gtm.localization.experiments import execution_context

    strategy_context = execution_context(workspace_id)
    if not strategy_context["allowed"]:
        return {
            "ran": False,
            "reason": "gtm_thesis_approval_required",
            "blocker": strategy_context["reason"],
        }
    state = factory.get_or_create_state(workspace_id)
    from app.gtm import opengtm_bridge as ogtm
    from app.gtm.lead_pipeline import find_leads_combined

    batch = int(state["find_batch_size"])
    query = state["icp_query"]
    industry = "B2B"
    region = None
    if strategy_context.get("mode") == "thesis":
        strategy = (strategy_context.get("thesis") or {}).get("strategy") or {}
        target = strategy.get("target") or {}
        industries = target.get("industries") or []
        roles = target.get("buyer_roles") or []
        countries = target.get("countries") or []
        industry = str(industries[0]) if industries else "B2B"
        region = str(countries[0]) if countries else None
        query = " ".join(
            part
            for part in (
                industry,
                " ".join(str(role).replace("_", " ") for role in roles[:3]),
                f"in {region}" if region else "",
            )
            if part
        )
    # Prefer OpenGTM when Gemini is configured — yields ICP + pattern A–E copy.
    og_st = ogtm.status()
    source = "auto"
    if og_st.get("ready") and (og_st.get("gemini") or {}).get("ready"):
        source = "opengtm"
        # Cap OpenGTM batches — research is slow / rate-limited.
        batch = min(batch, 12)
    try:
        result = find_leads_combined(
            query=query,
            count=batch,
            industry=industry if strategy_context.get("mode") == "thesis" else ("B2B SaaS" if source == "opengtm" else "B2B"),
            source=source,
            deep=True,
            verified_only=False,
            use_scrapy=source != "opengtm",
            region=region,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("discovery find (%s) failed: %s — falling back to auto", source, exc)
        if source == "opengtm":
            try:
                result = find_leads_combined(
                    query=query,
                    count=int(state["find_batch_size"]),
                    industry=industry,
                    source="auto",
                    deep=True,
                    verified_only=False,
                    use_scrapy=True,
                    region=region,
                )
            except Exception as exc2:  # noqa: BLE001
                log.info("discovery fallback failed: %s", exc2)
                return {"ran": False, "error": str(exc2)[:300]}
        else:
            return {"ran": False, "error": str(exc)[:300]}
    ingested = pipeline.ingest_leads(workspace_id, list(result.get("leads") or []), enrich=True)
    factory.mark_discovered(workspace_id)
    outreach = None
    li = None
    if state["send_enabled"]:
        outreach = pipeline.start_outreach(workspace_id, auto_send=False)
        li = linkedin.enroll_linkedin(workspace_id)
    return {
        "ran": True,
        "source": result.get("source") or source,
        "found": result.get("count"),
        "ingested": ingested.get("ingested"),
        "hunter_filled": ingested.get("hunter_filled"),
        "outreach": outreach,
        "linkedin": li,
    }


def tick_department(workspace_id: str) -> dict[str, Any]:
    """One scheduler pass. Discovery never stops until the founder hits Stop."""
    out: dict[str, Any] = {"workspace_id": workspace_id}
    out["discovery"] = tick_discovery(workspace_id)
    out["research"] = research.tick_research_jobs(limit=4)
    state = factory.get_or_create_state(workspace_id)
    factory.apply_mailbox_ramp(workspace_id)
    if state["send_enabled"]:
        pipeline.start_outreach(workspace_id, auto_send=False)
        linkedin.enroll_linkedin(workspace_id)
        out["email"] = pipeline.tick_send_queue(workspace_id, respect_stop=True)
        # tick_linkedin cannot Unipile-send (OT-04); simulated / human queue only.
        out["linkedin"] = linkedin.tick_linkedin(workspace_id)
    else:
        out["email"] = {"skipped": ["send_stopped"]}
        out["linkedin"] = {"skipped": ["send_stopped"]}
    out["replies"] = tick_replies(workspace_id)
    out["bounces"] = tick_bounces(workspace_id)
    if not state.get("last_learn_at"):
        out["learning"] = learning.summarize(workspace_id)
    else:
        out["learning"] = {"deferred": True}
    out["funnel"] = factory.funnel(workspace_id)
    out["engine"] = factory.get_or_create_state(workspace_id)
    return out
