"""Poll consented mailboxes and post one durable chat receipt per window."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.chat import store as chat_store
from app.connections.mailboxes import MailboxUnavailable, list_read_mailboxes, mailbox_for_conversation
from app.connections.composio_pacing import RateLimited
from app.credits import metered
from app.db.models import GtmContact, GtmTouch, Venture, Workspace
from app.db.supymem_models import Experiment
from app.gtm.supymem import composio_gmail
from app.gtm.supymem.workers import _iter_messages
from app.inbox import classify, store
from app.state import db

log = logging.getLogger(__name__)


def quiet(now, values):
    local = store.utc(now).astimezone(ZoneInfo(values["timezone"]))
    minute = local.hour * 60 + local.minute
    start, end = (store.clock_minutes(values["quiet_hours"][key]) for key in ("start", "end"))
    return start <= minute < end if start < end else start != end and (minute >= start or minute < end)


def window(now, values):
    local = store.utc(now).astimezone(ZoneInfo(values["timezone"]))
    due = [time for time in values["windows"] if store.clock_minutes(time) <= local.hour * 60 + local.minute]
    return local.date().isoformat() + ":" + max(due) if due else None


def _claim(founder_id, workspace_id, ref, now):
    with db()() as session:
        store.lock(session, founder_id)
        mailbox = store.consented(session, founder_id, ref["mailbox_id"])
        if mailbox is None:
            return None
        _, values = store.settings(session, founder_id, workspace_id)
        data = json.loads(mailbox.data_json)
        if data.get("next_poll") and datetime.fromisoformat(data["next_poll"]) > store.naive(now):
            return None
        if data.get("lease_until") and datetime.fromisoformat(data["lease_until"]) > store.naive(now):
            return None
        data["lease_until"] = (store.naive(now) + timedelta(minutes=10)).isoformat()
        from core.ids import new_id
        data["lease_id"] = new_id("ibp")
        store.save(mailbox, data)
        owned = select(Workspace.id).where(Workspace.owner_id == founder_id)
        active_experiments = set(session.scalars(select(Experiment.id).where(
            Experiment.workspace_id.in_(owned), Experiment.state == "running")))
        experiment_contacts = {}
        if active_experiments:
            for contact_id, facts_json in session.execute(select(GtmTouch.contact_id, GtmTouch.honesty_json).join(
                    GtmContact, GtmTouch.contact_id == GtmContact.id).where(GtmContact.workspace_id.in_(owned),
                    GtmTouch.direction == "out", GtmTouch.channel == "email", GtmTouch.simulated == 0)):
                try:
                    facts = json.loads(facts_json or "{}")
                except ValueError:
                    continue
                if isinstance(facts, dict) and facts.get("experiment_id") in active_experiments:
                    experiment_contacts[contact_id] = facts["experiment_id"]
        contacts = {}
        for contact in session.scalars(select(GtmContact).where(GtmContact.workspace_id.in_(
                select(Workspace.id).where(Workspace.owner_id == founder_id)))):
            if not contact.email:
                continue
            mailbox_id = contact.owner_mailbox_id
            if not mailbox_id and contact.last_emailed_at and contact.gmail_thread_id:
                try:
                    original = mailbox_for_conversation(session, contact_id=contact.id)
                    mailbox_id = original["mailbox_id"] if original else None
                except (MailboxUnavailable, AttributeError):
                    mailbox_id = None
            email = contact.email.casefold()
            if email in contacts:
                contacts[email]["outreach"] = False
            else:
                contacts[email] = {"outreach": bool(contact.last_emailed_at),
                    "thread_id": contact.gmail_thread_id, "mailbox_id": mailbox_id,
                    "experiment_id": experiment_contacts.get(contact.id)}
        own = {mailbox["email"].casefold() for mailbox in list_read_mailboxes(session, workspace_id)}
        checkpoint = mailbox.checkpoint or store.naive(now)
        session.commit()
        return checkpoint, data, values, contacts, own


def _authority(founder_id, mailbox_id, generation):
    with db()() as session:
        return store.enabled() and store.consented(session, founder_id, mailbox_id, generation) is not None


def _read(ref, checkpoint, now, workspace_id, founder_id, generation, page_token=None):
    before_epoch = int(store.utc(now).timestamp()) + 1
    query = f"after:{int(store.utc(checkpoint).timestamp()) - 1} before:{before_epoch} -in:sent"
    with metered.charge_sync(workspace_id=workspace_id, purpose="inbox.read", provider="composio",
            estimate_micro=0, payer="founder") as meter:
        meter.protect_outcome()
        if not _authority(founder_id, ref["mailbox_id"], generation):
            raise PermissionError("inbox_consent_revoked")
        result = composio_gmail.fetch_query(composio_user_id=ref["composio_user_id"],
            connected_account_id=ref["connected_account_id"], query=query, max_results=200, include_payload=True,
            **({"page_token": page_token} if page_token else {}))
        payload = result
        next_token = None
        while isinstance(payload, dict):
            next_token = payload.get("nextPageToken") or payload.get("next_page_token") or next_token
            payload = next((payload[key] for key in ("messages", "data", "items", "emails")
                if isinstance(payload.get(key), (list, dict))), None)
        if not isinstance(payload, list) or any(not isinstance(message, dict) for message in payload):
            raise ValueError("inbox_invalid_read_response")
        if next_token is not None and (not isinstance(next_token, str) or next_token == page_token):
            raise ValueError("inbox_invalid_page_token")
        return _iter_messages(result)[:200], next_token


def _reserve(founder_id, workspace_id, count, now):
    with db()() as session:
        store.lock(session, founder_id)
        row, values = store.settings(session, founder_id, workspace_id)
        day = store.utc(now).date().isoformat()
        if values.get("classification_day", "") > day:
            return 0
        if values.get("classification_day", "") < day:
            values["classification_day"], values["classified"] = day, 0
        allowed = min(count, max(0, 300 - values["classified"]))
        values["classified"] += allowed
        store.save(row, values)
        session.commit()
        return allowed


async def poll(founder_id, workspace_id, ref, *, now):
    claimed = await asyncio.to_thread(_claim, founder_id, workspace_id, ref, now)
    if claimed is None:
        return
    checkpoint, data, values, contacts, own = claimed
    generation = data["generation"]
    try:
        if not await asyncio.to_thread(_authority, founder_id, ref["mailbox_id"], generation):
            return
        scan_target = datetime.fromisoformat(data["scan_target"]) if data.get("scan_target") else now
        messages, next_token = await asyncio.to_thread(_read, ref, checkpoint, scan_target, workspace_id,
            founder_id, generation, data.get("page_token"))
        if next_token and next_token in data.get("page_tokens", []):
            raise ValueError("inbox_repeated_page_token")
        seen = data.get("seen", {})
        items, answers, undecided, batch_ids = [], {}, [], set()
        for message in messages:
            item = classify.metadata(message)
            if item["message_id"] in seen or item["message_id"] in batch_ids or item["received_at"] < max(checkpoint, store.naive(now) - timedelta(days=14)) or item["received_at"] > store.naive(now):
                continue
            batch_ids.add(item["message_id"])
            item.update(mailbox_id=ref["mailbox_id"])
            answer = classify.rules_pass(item, message, own_emails=own, contacts=contacts, rules=values["rules"], now=now)
            items.append(item)
            if answer is None:
                undecided.append(item)
            else:
                answers[item["message_id"]] = answer
        if undecided and await asyncio.to_thread(_authority, founder_id, ref["mailbox_id"], generation):
            allowed = await asyncio.to_thread(_reserve, founder_id, workspace_id, len(undecided), now)
            if allowed:
                request = hashlib.sha256(json.dumps([generation, [item["message_id"] for item in undecided[:allowed]]]).encode()).hexdigest()
                try:
                    target_workspace = await asyncio.to_thread(_target_workspace, founder_id, values["venture_id"])
                    from app.tenancy import bind
                    with bind(workspace_id=target_workspace, venture_id=values["venture_id"], actor=founder_id):
                        answers.update(await classify.model_batch(undecided[:allowed], workspace_id=target_workspace,
                            venture_id=values["venture_id"], request_id="inbox:" + request))
                except Exception:
                    log.warning("inbox_classification_failed", extra={"mailbox_id": ref["mailbox_id"]})
        await asyncio.to_thread(_store_poll, founder_id, workspace_id, ref, generation, claimed,
            items, answers, next_token, now)
    finally:
        await asyncio.to_thread(_release, founder_id, ref["mailbox_id"], generation, claimed[1]["lease_id"])


def _target_workspace(founder_id, venture_id):
    with db()() as session:
        target = session.get(Venture, venture_id)
        if target is None or target.archived_at is not None or store.owner(session, target.workspace_id) != founder_id:
            raise PermissionError("inbox_venture_unavailable")
        return target.workspace_id


def _store_poll(founder_id, workspace_id, ref, generation, claimed, items, answers, next_token, now):
    with db()() as session:
        store.lock(session, founder_id)
        mailbox = store.consented(session, founder_id, ref["mailbox_id"], generation)
        if mailbox is None or not store.enabled():
            return
        from core.ids import new_id
        data = json.loads(mailbox.data_json)
        if data.get("lease_id") != claimed[1]["lease_id"]:
            return
        seen = data.get("seen", {})
        filtered = data.get("filtered", {"newsletters": 0, "notifications": 0})
        for item in items:
            if item["message_id"] in seen:
                continue
            seen[item["message_id"]] = item["received_at"].isoformat()
            category, reason, urgent, bucket = answers.get(item["message_id"],
                ("unclassified", "Your daily limit was reached or classification was unavailable.", "", "notifications"))
            if category == "ignore":
                filtered[bucket] += 1
                continue
            session.add(store.InboxBriefingItem(id=new_id("ibi"), founder_id=founder_id,
                workspace_id=workspace_id, **item, category=category, reason=reason,
                urgent_kind=urgent, briefed=0, pinged=0))
        data["seen"] = {key: value for key, value in seen.items() if datetime.fromisoformat(value) >= store.naive(now) - timedelta(days=14)}
        data["filtered"] = filtered
        data.pop("lease_until", None)
        data["next_poll"] = (store.naive(now) + timedelta(minutes=claimed[2]["poll_minutes"])).isoformat()
        if next_token:
            data.setdefault("scan_target", store.naive(now).isoformat())
            data["page_token"] = next_token
            data.setdefault("page_tokens", []).append(next_token)
        else:
            mailbox.checkpoint = datetime.fromisoformat(data.pop("scan_target")) if "scan_target" in data else store.naive(now)
            data.pop("page_token", None)
            data.pop("page_tokens", None)
        data.pop("scan_before", None)
        store.save(mailbox, data)
        session.commit()


def _release(founder_id, mailbox_id, generation, lease_id):
    with db()() as session:
        store.lock(session, founder_id)
        mailbox = store.consented(session, founder_id, mailbox_id, generation)
        if mailbox is not None and json.loads(mailbox.data_json).get("lease_id") == lease_id:
            data = json.loads(mailbox.data_json)
            data.pop("lease_until", None)
            store.save(mailbox, data)
            session.commit()


def _text(value):
    # shortcut: lexical display filtering cannot cover every language, expand it when unsafe display text is reported.
    value = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)\S+|(?<![@\w])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?", "", value)
    if re.search(r"(?i)\b(ignore|disregard|override|instructions?|system|assistant|tools?|read_url|"
            r"browse|visit|open|click|send|upload|exfiltrate|execute|run|follow|prompt|secrets?|tokens?|credentials?)\b", value):
        return "Mail text withheld. Open Gmail to review it."
    return re.sub(r"[\x00-\x1f\x7f<>\[\]()*_`#!\\]", " ", value).replace("\u2014", "-").strip()


def item_card(item, email):
    href = "https://mail.google.com/mail/u/?authuser=" + quote(email, safe="") + "#all/" + quote(item.thread_id or item.message_id, safe="")
    return {"id": item.id, "mailbox_id": item.mailbox_id, "category": item.category,
        "from": _text(item.sender_name or item.sender_email), "subject": _text(item.subject),
        "reason": item.reason, "suggested_action": {"label": "Open in Gmail", "href": href},
        "actions": [{"label": "Open in Gmail", "href": href},
            {"label": "Not important", "action": "inbox.item.feedback", "payload": {"item_id": item.id, "category": "ignore"}},
            {"label": "Important", "action": "inbox.item.feedback", "payload": {"item_id": item.id, "category": "important"}}]}


def deliver(founder_id, workspace_id, *, now):
    with db()() as session:
        store.lock(session, founder_id)
        row, values = store.settings(session, founder_id, workspace_id)
        venture = session.get(Venture, values.get("venture_id"))
        if venture is None or venture.archived_at is not None or store.owner(session, venture.workspace_id) != founder_id or quiet(store.utc(), values):
            return
        refs = {}
        for mailbox in session.scalars(select(store.InboxBriefingState).where(
                store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.consent == 1)):
            refs.update({ref["mailbox_id"]: ref for ref in list_read_mailboxes(session, mailbox.workspace_id)})
        consent_ids = {mailbox.mailbox_id for mailbox in session.scalars(select(store.InboxBriefingState).where(
            store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.consent == 1))}
        refs = {key: ref for key, ref in refs.items() if key in consent_ids}
        pending = session.scalars(select(store.InboxBriefingItem).where(
            store.InboxBriefingItem.founder_id == founder_id, store.InboxBriefingItem.mailbox_id.in_(refs),
            store.InboxBriefingItem.briefed == 0, store.InboxBriefingItem.category != "ignore").order_by(store.InboxBriefingItem.received_at)).all()
        due = window(now, values)
        key = "inbox:window:" + due if due else None
        kind = "inbox_briefing"
        unfinished = next(((saved_key, receipt) for saved_key, receipt in values["deliveries"].items() if not receipt.get("message_id")), None)
        if unfinished:
            key, receipt = unfinished
            kind = receipt["kind"]
            pending = [item for item in pending if item.id in receipt["item_ids"]]
            if kind == "inbox_urgent":
                day = store.utc(now).date().isoformat()
                if values.get("ping_day", "") > day:
                    return
                if values.get("ping_day", "") < day:
                    values["ping_day"], values["pings"] = day, 1
        elif not key or key in values["deliveries"]:
            day = store.utc(now).date().isoformat()
            if values.get("ping_day", "") > day:
                return
            if values.get("ping_day", "") < day:
                values["ping_day"], values["pings"] = day, 0
            if values["pings"] >= 3:
                return
            pending = [item for item in pending if item.urgent_kind and not item.pinged]
            if not pending:
                return
            pending = pending[:1]
            key, kind = "inbox:urgent:" + pending[0].id, "inbox_urgent"
        if not pending:
            if unfinished:
                values["deliveries"][key]["message_id"] = "suppressed"
                store.save(row, values)
                session.commit()
            return
        cards = [item_card(item, refs[item.mailbox_id]["email"]) for item in pending]
        lines = ["Your inbox briefing" if kind == "inbox_briefing" else "Your inbox needs attention"]
        for category, title in (("important", "Important"), ("worth_a_look", "Worth a look"), ("unclassified", "Unclassified")):
            group = [card for card in cards if card["category"] == category]
            if group:
                lines.append(f"\n{title} ({len(group)})")
                for card in group:
                    lines.append("> " + json.dumps({"subject": card["subject"], "from": card["from"]}, ensure_ascii=True))
                    lines.append(f"Reason: {card['reason']} Open in Gmail to review.")
        filtered = {"newsletters": 0, "notifications": 0}
        filtered_by_mailbox = {}
        if kind == "inbox_briefing":
            for mailbox in session.scalars(select(store.InboxBriefingState).where(
                    store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.mailbox_id.in_(refs))):
                data = json.loads(mailbox.data_json)
                counts = (receipt.get("filtered_by_mailbox", {}).get(mailbox.mailbox_id, {}) if unfinished
                    else data.get("filtered", {}))
                filtered_by_mailbox[mailbox.mailbox_id] = counts
                for bucket, count in counts.items():
                    filtered[bucket] += count
        lines.append(f"\nFiltered out: {filtered['newsletters']} newsletters, {filtered['notifications']} notifications.")
        ids = [item.id for item in pending]
        mailbox_ids = sorted({item.mailbox_id for item in pending})
        generations = {mailbox_id: json.loads(store.consented(session, founder_id, mailbox_id).data_json)["generation"] for mailbox_id in mailbox_ids}
        venture_id, venture_workspace_id = venture.id, venture.workspace_id
        if not unfinished:
            values["deliveries"][key] = {"message_id": None, "mailboxes": mailbox_ids,
                "item_ids": ids, "kind": kind, "at": store.naive(now).isoformat()}
            values["deliveries"][key]["filtered_by_mailbox"] = filtered_by_mailbox
            if kind == "inbox_urgent":
                values["pings"] += 1
        store.save(row, values)
        session.commit()

    def authority(session, scope):
        store.lock(session, founder_id)
        _, current_values = store.settings(session, founder_id, workspace_id)
        if quiet(store.utc(), current_values):
            raise PermissionError("inbox_quiet_hours")
        present = set(session.scalars(select(store.InboxBriefingItem.id).where(
            store.InboxBriefingItem.id.in_(ids), store.InboxBriefingItem.founder_id == founder_id,
            store.InboxBriefingItem.received_at >= store.naive(now) - timedelta(days=14))))
        if not store.enabled() or present != set(ids) or any(store.consented(session, founder_id, mailbox_id, generation) is None for mailbox_id, generation in generations.items()):
            raise PermissionError("inbox_consent_revoked")

    from app.chat.conversations import create
    from app.tenancy import bind
    with bind(workspace_id=venture_workspace_id, venture_id=venture_id, actor=founder_id):
        conversation = create(venture_id, idempotency_key="inbox:" + founder_id, title="Your inbox")
        message = chat_store.idempotent_message(venture_id, "assistant", key,
            conversation_id=conversation["id"], output_authority=authority)
        if message is None:
            message = chat_store.append_message(venture_id, role="assistant", kind=kind,
                body_md="\n".join(lines), actions=cards[0]["actions"][:1], preserve_exact=True,
                conversation_id=conversation["id"],
                citations=[{"inbox_briefing": {"untrusted_mail": True, "items": cards, "filtered": filtered, "recommended_action": cards[0]["suggested_action"]}},
                    {"next_moves": {"kind": "next_moves", "body_md": "Review your inbox. Nothing has been sent or changed.",
                        "actions": [cards[0]["suggested_action"]], "citations": []}}],
                idempotency_key=key, output_authority=authority)
    with db()() as session:
        store.lock(session, founder_id)
        if any(store.consented(session, founder_id, mailbox_id, generation) is None for mailbox_id, generation in generations.items()):
            from sqlalchemy import delete
            from app.db.models import ChatMessage
            session.execute(delete(ChatMessage).where(ChatMessage.id == message["id"], ChatMessage.kind == kind))
            session.commit()
            return
        row, values = store.settings(session, founder_id, workspace_id)
        values["deliveries"][key] = {"message_id": message["id"], "mailboxes": mailbox_ids, "at": store.naive(now).isoformat()}
        for item in session.scalars(select(store.InboxBriefingItem).where(store.InboxBriefingItem.id.in_(ids), store.InboxBriefingItem.founder_id == founder_id)):
            if kind == "inbox_briefing":
                item.briefed = 1
            else:
                item.pinged = 1
        if kind == "inbox_briefing":
            for mailbox in session.scalars(select(store.InboxBriefingState).where(
                    store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.mailbox_id.in_(refs))):
                data = json.loads(mailbox.data_json)
                data["filtered"] = {bucket: max(0, data.get("filtered", {}).get(bucket, 0)
                    - filtered_by_mailbox.get(mailbox.mailbox_id, {}).get(bucket, 0)) for bucket in filtered}
                store.save(mailbox, data)
        store.save(row, values)
        session.commit()


def _snapshot(founder_id=None):
    with db()() as session:
        state_query = select(store.InboxBriefingState).where(store.InboxBriefingState.consent == 1)
        target_query = select(Venture, Workspace.owner_id).join(Workspace, Venture.workspace_id == Workspace.id).where(
            Venture.archived_at.is_(None), Workspace.archived_at.is_(None))
        if founder_id:
            state_query = state_query.where(store.InboxBriefingState.founder_id == founder_id)
            target_query = target_query.where(Workspace.owner_id == founder_id)
        states = [(row.founder_id, row.workspace_id, row.mailbox_id) for row in session.scalars(
            state_query)]
        targets = {}
        for venture, owner_id in session.execute(target_query.order_by(Venture.created_at, Venture.id)):
            targets.setdefault(owner_id, (venture.workspace_id, venture.id))
    return states, targets


def _mailbox_ref(founder_id, workspace_id, mailbox_id):
    with db()() as session:
        if store.owner(session, workspace_id) != founder_id:
            return None
        return next((ref for ref in list_read_mailboxes(session, workspace_id) if ref["mailbox_id"] == mailbox_id), None)


async def tick(_payload=None, *, now=None):
    if not await asyncio.to_thread(store.enabled):
        return {"enabled": False}
    now = store.utc(now)
    founder_id = (_payload or {}).get("founder_id")
    if _payload is not None and not founder_id:
        from app.core.scheduled import fanout
        count = await asyncio.to_thread(fanout, "inbox.briefing.tick", _payload, now=now)
        return {"enabled": True, "founders": count}
    await asyncio.to_thread(store.cleanup, now=now, founder_id=founder_id)
    states, targets = await asyncio.to_thread(_snapshot, founder_id)
    founders = {}
    failures = 0
    for founder_id, workspace_id, mailbox_id in states:
        founders.setdefault(founder_id, workspace_id)
        try:
            ref = await asyncio.to_thread(_mailbox_ref, founder_id, workspace_id, mailbox_id)
            if ref:
                from app.tenancy import bind
                with bind(workspace_id=workspace_id, venture_id="", actor=founder_id):
                    await poll(founder_id, workspace_id, ref, now=now)
        except RateLimited:
            raise
        except Exception:
            failures += 1
            log.warning("inbox_poll_failed", extra={"mailbox_id": mailbox_id})
    for founder_id, workspace_id in founders.items():
        try:
            await asyncio.to_thread(deliver, founder_id, workspace_id, now=now)
        except Exception as exc:
            failures += 1
            log.warning("inbox_delivery_failed:%s", type(exc).__name__, extra={"founder_id": founder_id})
    for founder_id, (workspace_id, venture_id) in targets.items():
        try:
            await asyncio.to_thread(offer_consent, founder_id, workspace_id, venture_id, now=now)
        except Exception as exc:
            failures += 1
            log.warning("inbox_consent_offer_failed:%s", type(exc).__name__, extra={"founder_id": founder_id})
    return {"enabled": True, "mailboxes": len(states), "failures": failures}


def offer_consent(founder_id, workspace_id, venture_id, *, now):
    with db()() as session:
        store.lock(session, founder_id)
        if store.owner(session, workspace_id) != founder_id:
            return
        row, values = store.settings(session, founder_id, workspace_id)
        if quiet(store.utc(), values):
            return
        refs = list_read_mailboxes(session, workspace_id)
        consent_ids = set(session.scalars(select(store.InboxBriefingState.mailbox_id).where(
            store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.consent == 1)))
        offered = values.setdefault("offered_mailboxes", {})
        pending = values.get("consent_offer")
        ids = pending or [ref["mailbox_id"] for ref in refs if ref["mailbox_id"] not in consent_ids and ref["mailbox_id"] not in offered][:3]
        refs = [ref for ref in refs if ref["mailbox_id"] in ids]
        if not refs:
            if pending:
                values.pop("consent_offer", None)
                store.save(row, values)
                session.commit()
            return
        values["consent_offer"] = ids
        values.setdefault("venture_id", venture_id)
        store.save(row, values)
        session.commit()
    from app.chat.conversations import create
    from app.tenancy import bind
    key = "inbox:consent:" + hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
    def authority(session, scope):
        store.lock(session, founder_id)
        if not store.enabled() or store.owner(session, workspace_id) != founder_id:
            raise PermissionError("inbox_consent_offer_unavailable")
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=founder_id):
        conversation = create(venture_id, idempotency_key="inbox:" + founder_id, title="Your inbox")
        existing = chat_store.idempotent_message(venture_id, "assistant", key,
            conversation_id=conversation["id"], output_authority=authority)
        if existing is None:
            chat_store.append_message(venture_id, role="assistant", kind="inbox_consent", preserve_exact=True,
                conversation_id=conversation["id"], idempotency_key=key,
                output_authority=authority,
                body_md="You can enable read-only briefings for these inboxes: " + ", ".join(_text(ref["email"]) for ref in refs)
                    + ". I will never send or change your mail. You can revoke consent at any time.",
                actions=[{"label": "Enable briefings for " + _text(ref["email"]), "action": "inbox.briefing.consent",
                    "payload": {"mailbox_id": ref["mailbox_id"], "consent": True}} for ref in refs])
    with db()() as session:
        store.lock(session, founder_id)
        row, values = store.settings(session, founder_id, workspace_id)
        values.setdefault("offered_mailboxes", {}).update({mailbox_id: True for mailbox_id in ids})
        values.pop("consent_offer", None)
        store.save(row, values)
        session.commit()


def action(workspace_id, venture_id, action_name, payload, actor_person_id):
    if not store.enabled():
        return {"kind": "text", "body_md": "Inbox briefings are not enabled for you.", "actions": [], "citations": []}
    with db()() as session:
        if store.owner(session, workspace_id) != actor_person_id:
            raise PermissionError("inbox_founder_required")
    if action_name == "inbox.briefing.consent":
        result = store.set_consent(workspace_id, venture_id, payload.get("mailbox_id"), payload.get("consent"))
        body = "You enabled read-only briefings for this inbox." if result["consent"] else "You stopped briefings for this inbox. Your stored inbox items were deleted."
    elif action_name == "inbox.briefing.settings":
        result = store.update_settings(workspace_id, payload)
        body = "Your inbox briefing schedule is saved. Nothing will be sent or changed in your inbox."
    elif action_name == "inbox.item.feedback":
        result = store.feedback(workspace_id, payload.get("item_id"), payload.get("category"), payload.get("scope", "sender"))
        body = "Your importance rule is saved for your future briefings."
    else:
        raise ValueError("inbox_unknown_action")
    return {"kind": "text", "body_md": body, "actions": [], "citations": [], "inbox_briefing": result}
