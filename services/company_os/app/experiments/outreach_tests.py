"""Confirmed outreach comparisons using the existing draft, review and send gates."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import select

from app.core.fsm import advance
from app.core.jobs import enqueue
from app.core.local_mode import is_local
from app.credits import metered
from app.credits.errors import CreditsUnavailable
from app.db.models import GtmContact, GtmEmailEvent, GtmTouch, Venture, VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.experiments import activity, ledger, methods
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import founder_copy, no_dashes
from core.ids import new_id

CATALOG = {
    "E19": ("Worst-week reverse pitch", "Q4", "angle"),
    "E08": ("Geography reply comparison", "Q2", "city"),
    "E09": ("Company-size reply comparison", "Q2", "segment"),
    "E39": ("Anti-persona reply comparison", "Q2", "segment"),
    "E21": ("Send time-of-day test", "Q4", "send_time"),
    "E26": ("Interview booking test", "Q5", "none"),
}


def validate_booking_link(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or re.search(r"\s|[\\<>]", value):
        raise ValueError("Your booking link must be an https URL with a valid host.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif (len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host)
                or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split("."))):
            raise ValueError
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or not host or port == 0):
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Your booking link must be an https URL with a valid host.") from exc
    authority = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, authority,
        *(quote(part, safe="/%:@!$&'()*+,;=?") for part in (parsed.path, parsed.query, parsed.fragment))))


def booking_link(venture_id, *, session=None, draft=None):
    from app.gtm import journey

    if session is None:
        with db()() as session:
            return booking_link(venture_id, session=session, draft=draft)
    identity = tags(draft or {})
    if identity["experiment_id"]:
        row = session.get(Experiment, identity["experiment_id"])
        spec = json.loads(row.context).get("spec", {}) if row and row.venture_id == venture_id else {}
        if spec.get("catalog_key") == "E26":
            return validate_booking_link(spec["runner_params"]["booking_link"])
    row = journey._row(session, venture_id)
    value = journey._json(row.provenance_json).get("booking_link") if row else None
    if is_local():
        from app.gtm.contact_work import booking_link as saved_booking_link
        value = saved_booking_link(venture_id) or value
    return validate_booking_link(value) if value else None


def configure_booking(venture_id, payload):
    from app.experiments import autonomous
    from app.gtm import journey

    link = validate_booking_link(payload.get("booking_link"))
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage in {"archived", "purged", "paused"}:
            raise ValueError("Your venture is not active.")
        serialize_writes(session, key="market-experiments:" + venture_id)
        row = journey._row(session, venture_id, create=True)
        row.provenance_json = json.dumps({**journey._json(row.provenance_json), "booking_link": link})
        session.commit()
    spec = next((item for item in autonomous.available(venture_id) if item["catalog_key"] == "E26"), None)
    if is_local() and spec:
        return {**autonomous._reply("Your booking link is saved. Choose your interview booking test when you are ready. Nothing was sent."),
            "actions": [{"label": "Run your interview booking test", "action": "experiment.prepare", "payload": {"experiment_id": spec["id"]}, "style": "secondary"}]}
    return confirmation(spec) if spec else autonomous._reply("Your booking link is saved for your problem-interview drafts. Nothing was sent.")


@cache
def _zone_countries():
    for directory in TZPATH:
        path = Path(directory) / "zone.tab"
        if path.is_file():
            return {fields[2]: fields[0] for line in path.read_text().splitlines()
                if line and not line.startswith("#") and len(fields := line.split()) >= 3}
    return {}


def recipient_zone(contact, founder):
    from app.gtm import enrich, journey
    from app.gtm.relationships.geo_policy import normalize_geo

    data = journey._json(contact.enrichment_json) if contact else {}
    profile = data.get("profile_facts") if isinstance(data.get("profile_facts"), dict) else {}
    for value in (contact.timezone if contact else None, data.get("timezone"), data.get("time_zone"), profile.get("timezone")):
        if isinstance(value, str):
            try:
                return ZoneInfo(value), "recipient"
            except (ZoneInfoNotFoundError, ValueError):
                pass
    country = str((contact.recipient_geo if contact else "") or data.get("country") or profile.get("country") or "")
    country = enrich._COUNTRY_NAMES.get(country.casefold(), normalize_geo(country))
    zones = _zone_countries()
    candidates = [name for name, code in zones.items() if country in code.split(",")] if country else []
    city = str(data.get("city") or profile.get("city") or "").split(",", 1)[0].strip().casefold().replace(" ", "_")
    if city:
        matches = [name for name in (candidates or sorted(available_timezones())) if name.rsplit("/", 1)[-1].casefold() == city]
        if len(matches) == 1:
            return ZoneInfo(matches[0]), "recipient"
    if len(candidates) == 1:
        return ZoneInfo(candidates[0]), "recipient"
    # ponytail: ambiguous country/city uses your saved zone, add verified city-zone data for finer geography.
    return founder, "your timezone fallback"


def send_slot(venture_id, draft, *, now=None, next_day=False):
    from app.chat.reminders import founder_zone

    identity = tags(draft)
    if not identity["experiment_id"]:
        return None
    with db()() as session:
        row = session.get(Experiment, identity["experiment_id"])
        spec = json.loads(row.context).get("spec", {}) if row and row.venture_id == venture_id else {}
        if spec.get("catalog_key") != "E21":
            return None
        arm = next((arm for arm in spec["variants"] if arm["key"] == identity["variant"]), None)
        if arm is None:
            raise ValueError("experiment_assignment_changed")
        window = arm["params"]["send_window"]
        if (not isinstance(window, list) or len(window) != 2 or any(type(hour) is not int for hour in window)
                or not 7 <= window[0] < window[1] <= 20):
            raise ValueError("experiment_send_window_invalid")
        zone, source = recipient_zone(session.get(GtmContact, draft.get("contact_id")), founder_zone(venture_id))
    now = as_utc(now or utcnow())
    local = now.astimezone(zone)
    day = local.date() + timedelta(days=int(next_day or local.hour >= window[1]))
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).replace(hour=window[0])
    due = max(now, as_utc(start)) if not next_day else as_utc(start)
    return {"due_at": due.isoformat(), "timezone": zone.key, "zone_source": source, "window": window}


def schedule_send(venture_id, draft, payload, actor_subject, slot):
    from app.experiments import autonomous

    approval = (payload.get("journey_approvals") or {}).get(draft["doc_id"])
    if not approval or draft.get("requires_journey_review") is not True:
        raise ValueError("Your scheduled experiment email needs its exact saved approval.")
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row = session.get(VentureDocument, draft["doc_id"])
        experiment = session.get(Experiment, tags(draft)["experiment_id"])
        if not row or row.venture_id != venture_id or not experiment or experiment.venture_id != venture_id:
            raise ValueError("experiment_job_scope_changed")
        data, context = json.loads(row.provenance_json), json.loads(experiment.context)
        from app.autopilot import outreach
        if outreach.mailboxes.enabled():
            if not data.get("sender"):
                raise outreach.mailboxes.MailboxUnavailable("mailbox_review_required")
            sender = outreach.draft_mailbox(experiment.workspace_id, venture_id, data, session=session)
        if (data.get("state") != "draft" or experiment.state != "running" or context.get("result") or context.get("error")
                or not experiment.started_at or datetime.fromisoformat(slot["due_at"]) >= as_utc(experiment.started_at) + timedelta(days=context["spec"]["max_duration_days"])):
            return False
        previous = data.get("scheduled_send") or {}
        if (previous.get("approval_id") == approval and previous.get("due_at") == slot["due_at"]
                and previous.get("job_id") != payload.get("scheduled_job_id")):
            return True
        job_id = enqueue(session, autonomous.KIND, {"experiment_id": experiment.id, "run_id": context.get("run_id"),
            "scheduled_draft_id": row.id, **({"mailbox_id": sender["mailbox_id"]} if outreach.mailboxes.enabled() else {})},
            workspace_id=experiment.workspace_id, venture_id=venture_id,
            dedupe_key=f"experiment-slot:{row.id}:{approval}:{slot['due_at']}", max_attempts=1,
            run_after=datetime.fromisoformat(slot["due_at"]))
        data["scheduled_send"] = {**slot, "job_id": job_id, "approval_id": approval, "actor_subject": actor_subject,
            "auto_send": payload.get("auto_send") is True, "run_id": context.get("run_id")}
        if outreach.mailboxes.enabled():
            data["scheduled_send"].update(mailbox_id=sender["mailbox_id"], sender=sender)
        row.provenance_json, row.updated_at = json.dumps(data), utcnow()
        session.commit()
    return True


async def send_scheduled_job(payload):
    from app.autopilot import outreach
    from app.chat.milestones import post_milestone
    from app.core.jobs import FatalError, _leased_row

    with db()() as session:
        job = _leased_row(session, payload.get("_job_id"), payload.get("_worker_id"), payload.get("_job_attempt"))
        row = session.get(VentureDocument, payload["scheduled_draft_id"])
        experiment = session.get(Experiment, payload["experiment_id"])
        data = json.loads(row.provenance_json) if row else {}
        saved = data.get("scheduled_send") or {}
        if outreach.mailboxes.enabled() and (not saved.get("sender") or saved["sender"] != data.get("sender")
                or saved.get("mailbox_id") != data.get("mailbox_id")
                or payload.get("mailbox_id") not in {None, saved.get("mailbox_id")}):
            raise FatalError("mailbox_binding_changed")
        context = json.loads(experiment.context) if experiment else {}
        if (not job or not row or row.kind != outreach.KIND or not experiment
                or (experiment.venture_id, experiment.workspace_id) != (payload["venture_id"], payload["workspace_id"])
                or row.venture_id != experiment.venture_id or tags(data)["experiment_id"] != experiment.id
                or context.get("run_id") != payload.get("run_id") or saved.get("run_id") != payload.get("run_id")
                or context.get("spec", {}).get("catalog_key") != "E21"):
            raise FatalError("experiment_job_scope_changed")
        if data.get("state") != "draft" or saved.get("job_id") != job.id:
            return
        if (experiment.state != "running" or context.get("result") or context.get("error")
                or not experiment.started_at or utcnow() >= as_utc(experiment.started_at) + timedelta(days=context["spec"]["max_duration_days"])):
            return
        if as_utc(datetime.fromisoformat(saved["due_at"])) > utcnow():
            raise FatalError("experiment_send_slot_not_due")
    draft = {**data, "doc_id": row.id}
    send_payload = {"batch_id": data.get("batch_id"), "approved_by_founder": not saved["auto_send"],
        "auto_send": saved["auto_send"], "journey_approved_ids": [row.id],
        "journey_approvals": {row.id: saved["approval_id"]}, "batch_cap": 1, "scheduled_job_id": job.id}
    reply = await outreach.send_batch(payload["venture_id"], send_payload, actor_subject=saved["actor_subject"])
    if reply.get("code") in {"gmail_reconnect_required", "mailbox_unavailable"}:
        with db()() as session:
            paused = _leased_row(session, payload.get("_job_id"), payload.get("_worker_id"), payload.get("_job_attempt"))
            if paused:
                from app.gtm.supymem import pipeline
                if outreach.mailboxes.enabled():
                    try:
                        connected = outreach.draft_mailbox(payload["workspace_id"], payload["venture_id"], draft, session=session)
                    except outreach.mailboxes.MailboxUnavailable:
                        connected = None
                else:
                    connected = pipeline._active_mailbox(session, payload["workspace_id"])
                paused.status = "pending" if connected else "paused"
                paused.last_error = None if connected else "gmail_reconnect_required"
                paused.attempts = max(0, paused.attempts - 1)
                paused.locked_at, paused.locked_by = None, None
                session.commit()
        if reply.get("code") == "mailbox_unavailable":
            post_milestone(payload["venture_id"], key=job.id + ":inbox", kind="needs_you", requested=True,
                body_md=reply["body_md"], actions=reply["actions"], citations=[])
        return
    if reply["kind"] == "wait" and send_block(payload["venture_id"], draft) not in {"experiment_not_running", "experiment_assignment_changed"}:
        settings = outreach.guard.get_settings(payload["venture_id"])
        cap = settings["send_max_per_day"] if settings["allow_send"] else outreach.DEFAULT_SEND_MAX
        capped = outreach.send_slots_used_today(payload["venture_id"]) >= cap
        # ponytail: five-minute contention retries, group slot jobs if throughput outgrows a small experiment.
        slot = send_slot(payload["venture_id"], draft, now=utcnow() + timedelta(minutes=5), next_day=capped)
        schedule_send(payload["venture_id"], draft, send_payload, saved["actor_subject"], slot)
    post_milestone(payload["venture_id"], key=job.id + ":slot", kind=reply["kind"], requested=True,
        body_md=reply["body_md"], actions=reply.get("actions", []), citations=[])


def calendar_booked(text, headers, subject=""):
    from app.gtm.supymem.workers import _reply_opt_out

    headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
    return bool(re.search(r"text/calendar\b.*\bmethod\s*=\s*\"?reply", headers.get("content-type", ""), re.I)
        and not _reply_opt_out(text or "") and not re.search(r"\b(?:cancel\w*|declin\w*|tentative)\b", text or "", re.I)
        and (re.search(r"PARTSTAT=ACCEPTED", text or "", re.I) or re.match(r"Accepted\s*:", subject or "", re.I)))


def booked_reply(text, *, headers=None, subject=""):
    from app.gtm.supymem.workers import _classify_reply, _reply_opt_out

    text = re.split(r"(?im)^\s*(?:>|On .+wrote:|From:|-----Original Message-----)", text or "", maxsplit=1)[0]
    headers = {str(key).casefold(): str(value) for key, value in (headers or {}).items()}
    if calendar_booked(text, headers, subject):
        return True
    if (_reply_opt_out(text) or re.search(r"\b(?:not|never|haven't|haven’t|hasn't|hasn’t|didn't|didn’t|unbooked|cancel\w*|declin\w*)\b", text, re.I)
            or "?" in text):
        return False
    if _classify_reply(text, headers=headers) == "automated":
        return False
    # ponytail: explicit English confirmations only, add verified locale phrases when needed.
    return bool(re.search(r"\b(?:i(?:'ve|’ve| have)?|we(?:'ve|’ve| have)?)\s+(?:just\s+|already\s+)?(?:booked|scheduled|reserved)\s+(?:(?:a|an|the|my|our)\s+)?(?:call|meeting|interview|appointment|slot|time)\b|\b(?:booking|call|meeting|interview)\s+(?:is\s+)?confirmed\b|^\s*(?:(?:i(?:'ve|’ve| have)?|we(?:'ve|’ve| have)?)\s+)?(?:just\s+)?booked(?:\s+it)?[.!]?\s*$", text, re.I))


def capture_booking_headers(venture_id, message_id, headers):
    kept = {key.casefold(): value for key, value in headers.items() if key.casefold() == "content-type"}
    if not kept or "text/calendar" not in str(kept).casefold():
        return
    with db()() as session:
        touch = session.scalar(select(GtmTouch).where(GtmTouch.venture_id == venture_id, GtmTouch.direction == "in", GtmTouch.message_id == message_id))
        if touch:
            key = "booking-headers:" + touch.id
            if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
                return
            session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=touch.workspace_id, contact_id=touch.contact_id,
                event_type="booking_reply_headers", provider_message_id=message_id, idempotency_key=key,
                metadata_json=json.dumps({"reply_id": touch.id, "reply_headers": kept, "subject": headers.get("subject", "")}), created_at=utcnow()))
            session.commit()


def record_reply_activity(venture_id, source_touch_ids, *, classification, observed_at):
    if classification == "automated":
        return

    def context(session):
        from app.autopilot.outreach import _draft_facts
        contexts = []
        for touch in session.scalars(select(GtmTouch).where(GtmTouch.id.in_(source_touch_ids),
                GtmTouch.venture_id == venture_id, GtmTouch.direction == "out", GtmTouch.simulated == 0)):
            facts = _draft_facts({"honesty_json": touch.honesty_json})
            resolved = activity.experiment_context(session, venture_id, facts.get("experiment_id") or facts.get("channel_experiment_id"))
            arm = next((arm for arm in resolved.get("variants", []) if arm.get("key") == facts.get("variant")), {})
            contexts.append({**resolved, **({"option_id": arm["option_id"]} if arm.get("option_id") else {})})
        if not contexts or len({context.get("option_id") for context in contexts}) != 1:
            return {}
        return contexts[0]

    return activity.record_safely(context, action="reply", tool="composio_gmail", evidence_level="market_response", started_at=observed_at, ended_at=observed_at, evidence_summary="1")


def observe_bookings(spec, touches):
    from app.experiments.autonomous import _outcome_line, _uncertain_claim, outcome
    from app.gtm.journey import _json
    window = spec.get("response_window_days", spec["max_duration_days"]) if ledger.quality_enabled() else spec["max_duration_days"]

    with db()() as session:
        row = session.get(Experiment, spec["id"])
        marks = json.loads(row.context).get("booking_marks", {}) if row else {}
        metadata = [_json(event.metadata_json) for event in session.scalars(select(GtmEmailEvent).where(
            GtmEmailEvent.workspace_id == spec["workspace_id"], GtmEmailEvent.event_type == "booking_reply_headers"))]
        headers = {item.get("reply_id"): item for item in metadata}
    firsts, replies = {}, []
    for touch in touches:
        facts = _json(touch.honesty_json)
        if (touch.direction == "out" and touch.sent_at and facts.get("experiment_id") == spec["id"]
                and facts.get("variant") == spec["variants"][0]["key"] and facts.get("day", 1) == 1
                and (not ledger.quality_enabled() or as_utc(touch.sent_at) <= utcnow())):
            firsts.setdefault(touch.contact_id, touch.sent_at)
    signal = None
    if spec.get("catalog_key") == "E26":
        from app.connections.bookings import read_bookings
        with db()() as session:
            start = min((as_utc(sent) for sent in firsts.values()), default=utcnow())
            # shortcut: check 30 days ahead after the test window, widen it when interviews book further ahead.
            signal = read_bookings(session, spec["venture_id"], start,
                max(utcnow(), start + timedelta(days=spec["max_duration_days"])) + timedelta(days=30),
                feature_flag="experiments_autopilot_v1")
    for touch in touches:
        sent = firsts.get(touch.contact_id)
        if touch.direction != "in" or not sent or not as_utc(sent) <= as_utc(touch.created_at) <= as_utc(sent) + timedelta(days=window):
            continue
        if ledger.quality_enabled() and as_utc(touch.created_at) > utcnow():
            continue
        saved = headers.get(touch.id, {})
        reply_headers = saved.get("reply_headers") or _json(touch.honesty_json).get("reply_headers")
        subject = touch.subject or saved.get("subject", "")
        detected = booked_reply(touch.body_text, headers=reply_headers, subject=subject)
        if signal:
            body = re.split(r"(?im)^\s*(?:>|On .+wrote:|From:|-----Original Message-----)", touch.body_text or "", maxsplit=1)[0]
            detected = detected and not _uncertain_claim(body)
        detected = detected and (touch.classification != "automated" or calendar_booked(touch.body_text, reply_headers, subject))
        replies.append({"reply_id": touch.id, "contact_id": touch.contact_id, "text": founder_copy(touch.body_text or "")[:500],
            "counted": marks.get(touch.contact_id, detected), "source": "you marked it" if touch.contact_id in marks else "reply confirmation"})
    counted = {reply["contact_id"] for reply in replies if reply["counted"]}
    counted.update(contact_id for contact_id in firsts if marks.get(contact_id) is True)
    if signal and signal["connected"]:
        counted = {booking["contact_id"] for booking in signal["bookings"]
            if booking["contact_id"] in firsts and as_utc(firsts[booking["contact_id"]]) <= datetime.fromisoformat(booking["starts_at"])
            and marks.get(booking["contact_id"]) is not False}
        for reply in replies:
            reply["counted"] = reply["contact_id"] in counted
            reply["source"] = "your booking account" if signal["available"] else "your bookings have not been checked"
    contacted, booked = len(firsts), len(counted)
    measured = {"events": contacted, "contacted": contacted, "booked": booked, "booking_rate": booked / contacted if contacted else 0}
    opening = f"You have {booked} confirmed bookings from {contacted} people you contacted ({measured['booking_rate']:.1%})."
    insight = opening if signal else _outcome_line(spec, measured) + f" Confirmed bookings: {booked} / {contacted} contacted ({measured['booking_rate']:.1%})."
    numbers = {person: index + 1 for index, person in enumerate(firsts)}
    if replies:
        insight += "\n\nYour reviewed replies:\n" + "\n".join(f"- Contact {numbers[reply['contact_id']]}: {'Counted' if reply['counted'] else 'Not counted'} ({reply['source']}): {reply['text']}" for reply in replies)
    insight += "\n" + "\n".join(f"- Contact {numbers[person]}: {'booked' if person in counted else 'not booked'}; no reply recorded."
        for person in firsts if not any(reply["contact_id"] == person for reply in replies))
    if signal and signal["connected"]:
        insight += "\nYour connected booking account confirms these calls. Each person counts once. You can exclude a meeting that is not a customer interview."
        if not signal["available"]:
            insight = "Your booking account could not be checked yet. Your experiment is saved. Try checking your bookings again."
    else:
        insight += "\nYou can mark or correct each contacted person's booking. Each person counts once."
        if signal:
            insight += "\nConnect your booking account so I can check your booked calls for you. Nothing is changed in your account."
    result = {"outcome": "inconclusive" if signal and signal["connected"] and not signal["available"] else outcome(spec, measured),
        "measured": measured, "by_variant": [{"key": spec["variants"][0]["key"],
        "label": spec["variants"][0]["label"], "sent": contacted, "booked": booked}], "booking_replies": replies,
        "booking_contacts": [{"contact_id": person, "counted": person in counted} for person in firsts],
        "insight": founder_copy(insight), "sources": [{"url": f"/v/{spec['venture_id']}/gtm", "title": "Your contacted people and booking replies"}], "actual_micro": 0}
    if signal:
        result["booking_signal"] = signal
    if ledger.quality_enabled():
        from app.experiments import engine
        point = spec.get("checkpoint") or {}
        checkpoint_contacts = {contact for contact, sent in firsts.items() if as_utc(sent) +
            timedelta(days=point.get("wait_days", spec["max_duration_days"])) <= utcnow()}
        result["by_variant"][0].update(checkpoint_sent=len(checkpoint_contacts),
            checkpoint_replies=len(checkpoint_contacts & counted))
        result = engine.mature_result(result, engine.response_maturity(spec, list(firsts.values()), utcnow()))
    return result


def booking_readout(venture_id, payload):
    from app.experiments import autonomous

    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = session.get(Experiment, payload.get("experiment_id"))
        if not venture or not row or (row.venture_id, row.workspace_id) != (venture_id, venture.workspace_id) or row.kind != ledger.KIND:
            raise ValueError("Your booking experiment was not found.")
        spec = json.loads(row.context).get("spec", {})
        if spec.get("catalog_key") != "E26":
            raise ValueError("Your experiment is not an interview booking test.")
    result = observe(spec)
    reply = autonomous._reply(result["insight"], result["sources"])
    reply["actions"] = [{"label": ("Correct booking" if person["counted"] else "Mark booked") + f" ({index + 1})",
        "action": "experiment.booking.mark", "payload": {"experiment_id": spec["id"], "contact_id": person["contact_id"], "booked": not person["counted"]}}
        for index, person in enumerate(result["booking_contacts"])] + [manual_action(spec)]
    signal = result.get("booking_signal") or {}
    if signal.get("connected"):
        reply["actions"] = [action for action in reply["actions"]
            if action.get("action") != "experiment.booking.mark" or action["payload"]["booked"] is False]
    if signal.get("connection_action"):
        reply["actions"].insert(0, signal["connection_action"])
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        if context.get("result"):
            result["actual_micro"] = context["result"].get("actual_micro", 0)
            row.context = json.dumps({**context, "result": result})
            row.outcome, row.learnings = result["outcome"], result["insight"]
            session.commit()
    return reply


def mark_booking(venture_id, payload):
    if type(payload.get("booked")) is not bool:
        raise ValueError("Your booking correction must be true or false.")
    booking_readout(venture_id, payload)
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row = session.get(Experiment, payload["experiment_id"])
        context = json.loads(row.context)
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == row.workspace_id,
            GtmTouch.venture_id == venture_id, GtmTouch.contact_id == payload.get("contact_id"),
            GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))))
        if not any(tags({"honesty_json": touch.honesty_json})["experiment_id"] == row.id for touch in touches):
            raise ValueError("You can only mark a person contacted in this experiment.")
        context["booking_marks"] = {**context.get("booking_marks", {}), payload["contact_id"]: payload["booked"]}
        row.context = json.dumps(context)
        session.commit()
    return booking_readout(venture_id, payload)


def _labels(raw):
    if not isinstance(raw, list):
        return []
    labels = [item.get("who") or item.get("label") if isinstance(item, dict) else item for item in raw]
    return list(dict.fromkeys(founder_copy(label.strip()) for label in labels
        if isinstance(label, str) and 3 <= len(label.strip()) <= 160))


def specs(snapshot, base, provider):
    from app.approvals.external import outbound_quote
    from app.autopilot import guard
    from app.gtm import journey, playbooks

    view, icp = snapshot["journey"], base["icp"]
    chosen = [item["key"] for item in view.get("playbooks") or []
        if item.get("key") in playbooks.PLAYBOOKS and item["key"] != "worst_week"]
    if is_local() and not chosen:
        chosen = ["problem_interview"]
    if not chosen or not icp.get("who") or not icp.get("problem") or icp.get("needs_input"):
        return []
    email_micro = 0 if is_local() else outbound_quote("gtm_email_send", "composio_gmail").get("credit_estimate_micro")
    if type(email_micro) is not int or email_micro < (0 if is_local() else 1):
        return []
    default = chosen[0]
    daily_limit = guard.get_settings(base["venture_id"])["send_max_per_day"] or guard.DEFAULT_SEND_MAX
    proposals = {"E19": [("Current opening", {"playbook": default}),
                         ("Worst-week question", {"playbook": "worst_week"})],
        "E21": [("07:00-09:00 recipient local time", {"playbook": default, "send_window": [7, 9]}),
                ("13:00-15:00 recipient local time", {"playbook": default, "send_window": [13, 15]})],
        "E26": [("15-minute interview", {"playbook": "problem_interview"})]}
    link = booking_link(base["venture_id"])
    if provider and (is_local() or provider.name == "firecrawl"):
        cities = _labels(icp.get("cities")) or [", ".join(filter(None, city)) for city in journey._business_cities(icp["who"], context=icp)]
        if len(cities) >= 2:
            proposals["E08"] = [(city, {"city": city, "who": icp["who"], "playbook": default}) for city in cities[:2]]
        consumer = is_local() and journey._consumer(icp)
        sizes = [] if consumer else _labels(icp.get("company_sizes"))
        size_range = None if consumer else re.fullmatch(r"(\d+)\s*(?:-|to)\s*(\d+)", str(icp.get("company_size") or ""))
        segments = _labels(icp.get("segments") or snapshot.get("segments"))
        if size_range and 0 < int(size_range[1]) < int(size_range[2]):
            lower, upper = int(size_range[1]), int(size_range[2])
            middle = (lower + upper) // 2
            proposals["E09"] = [(f"{start}-{stop} employees", {"who": icp["who"], "min_size": start, "max_size": stop, "playbook": default})
                for start, stop in ((lower, middle), (middle + 1, upper))]
        elif len(sizes) >= 2:
            proposals["E09"] = [(size, {"who": icp["who"], "size": size, "playbook": default}) for size in sizes[:2]]
        elif len(segments) >= 2:
            proposals["E09"] = [(segment, {"who": segment, "playbook": default}) for segment in segments[:2]]
        excluded = _labels(icp.get("excluded_segments") or icp.get("anti_personas"))
        if excluded:
            proposals["E39"] = [(icp["who"], {"who": icp["who"], "playbook": default}),
                (excluded[0], {"who": excluded[0], "playbook": default})]
    output = []
    for key, arms in proposals.items():
        title, question, variable = CATALOG[key]
        if is_local() and journey._consumer(icp) and key == "E09":
            title = "Audience segment reply comparison"
        variants = [{"key": f"arm_{index}", "label": label, "params": params} for index, (label, params) in enumerate(arms)]
        searches = 0 if key in {"E19", "E21", "E26"} else 4
        search_micro = (provider.search_micro if is_local() else metered.price_micro("firecrawl_search")) if searches else 0
        scrape_micro = (provider.scrape_micro if is_local() else metered.price_micro("firecrawl_scrape")) if searches else 0
        for arm in variants:
            params = arm["params"]
            if searches:
                size = f"{params['min_size']}-{params['max_size']} employees" if "min_size" in params else params.get("size")
                scope = " ".join(filter(None, (params["who"], params.get("city"), size)))
                context = {**icp, "who": scope}
                context["geography"] = journey._audience_location(icp["who"], icp)
                if params.get("city"):
                    context.update(location=params["city"], geography=params["city"])
                params["queries"] = [f"{scope} contact", f"{scope} team email"]
                params["icp"] = context
        ratio = 1.5 if key in {"E19", "E21"} else 2
        hypothesis = (f"Asking about a worst week gets more concrete human replies from {icp['who']}." if key == "E19"
            else f"At least 10% of contacted {icp['who']} book a 15-minute interview." if key == "E26"
            else f"Human reply rates differ between {variants[0]['label']} and {variants[1]['label']}.")
        if key == "E21":
            hypothesis = f"Morning emails get at least 1.5 times the human reply rate of afternoon emails from {icp['who']}."
        if key == "E39":
            hypothesis = f"The audience you excluded, {variants[1]['label']}, replies more than {variants[0]['label']}."
        search_ceiling = searches * search_micro + (24 * scrape_micro if searches else 0)
        sample, days = (30, 10) if key == "E26" else (20, 7)
        days = max(days, math.ceil(sample / daily_limit))
        output.append({**{field: value for field, value in base.items() if field != "window_end"},
            "scan": "outreach_split", "runner": "outreach_split", "catalog_key": key, "source": "charlie_generated",
            "title": title, "question": question, "variable": variable, "variants": variants,
            "hypothesis": founder_copy(hypothesis)[:160], "assumption": founder_copy(hypothesis)[:160],
            "method": "Prepare balanced first emails for the next eligible contacts. You review each batch through your usual approval flow.",
            "runner_params": {"playbooks": [arm["params"]["playbook"] for arm in variants], "pages_per_arm": 12,
                **({"booking_link": link} if key == "E26" else {})},
            "metric": "booking_rate" if key == "E26" else "reply_rate_ratio",
            "success_threshold": {"op": ">=", "value": .1 if key == "E26" else ratio, "min_events": sample,
                "metric": "booking_rate" if key == "E26" else "qualified_reply_rate_ratio"},
            "failure_threshold": {"op": "<", "value": .03 if key == "E26" else ratio, "min_events": sample,
                "metric": "booking_rate" if key == "E26" else "qualified_reply_rate_ratio"},
            "min_sample": sample, "min_sample_per_arm": sample // len(variants), "sample_unit": "emails", "max_duration_days": days,
            "send_plan": {"daily_limit": daily_limit, "days_to_reach_sample": math.ceil(sample / daily_limit)},
            "founder_minutes": 10, "approval": "each_send", "state": "proposed", "result": None,
            "compliance": ["source_disclosure", "opt_out", "sender_address", "eu_consent", "suppression", "daily_cap"],
            "requires": (["send_email"] + (["research"] if searches else [])) if is_local() else ["outreach", "gmail"] + (["firecrawl"] if searches else []),
            "provider": provider.name if searches else None, "search_micro": search_micro, "scrape_micro": scrape_micro,
            "search_ceiling_micro": search_ceiling, "email_micro": email_micro,
            "ceiling_micro": search_ceiling + sample * email_micro,
            "next_suggestion": {"catalog_key": "E18", "params": {}}})
        if ledger.quality_enabled():
            output[-1].update(response_window_days=days, checkpoint={"after_sample": math.ceil(sample / 2), "wait_days": 2, "max_response_rate": .05})
    return output


def saved_specs(venture_id, fresh):
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage in {"archived", "purged", "paused"}:
            return fresh
        direction = ledger.current_direction(session, venture_id)
        known = {item["id"] for item in fresh}
        for row in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
                Experiment.workspace_id == venture.workspace_id, Experiment.kind == ledger.KIND)):
            context = json.loads(row.context)
            spec = context.get("spec") or {}
            if context.get("runner") == "system" and spec.get("runner") == "outreach_split" and spec.get("direction") == direction and row.id not in known and (context.get("claimed") or ledger.quality_enabled() and spec.get("checkpoint_parent_id") and not context.get("superseded")):
                fresh.append(spec)
    return fresh


def confirmation(spec):
    from dataclasses import replace
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard

    if spec["catalog_key"] == "E26" and not spec["runner_params"].get("booking_link"):
        if is_local():
            return {"kind": "question", "body_md": "In Settings > Keys and connections > Your outgoing mail, save your https booking link. Then choose your interview booking test in Experiments. Nothing was sent.",
                "actions": [{"label": "Add your booking link", "href": "/settings/local", "style": "primary"}], "citations": []}
        return {"kind": "question", "body_md": "I can recruit people and run this booking experiment for you. You only take the calls. I need your https booking link once; I will save it without fetching it, then prepare your experiment. Every send still needs your approval.",
            "actions": [{"label": "Add your booking link", "action": "chat.prefill", "text": "start an interview booking test with "}], "citations": []}
    arms = "; ".join(f"{arm['label']}: {arm['params']['playbook']}" for arm in spec["variants"])
    thresholds = f"Support: reply rate ratio >= {spec['success_threshold']['value']}; refute below it, only after {spec['min_sample_per_arm']} sent emails per arm and {spec['max_duration_days']} days."
    thresholds += (" The worst-week arm must also contain a concrete incident and no more than half hypothetical replies."
        if spec["catalog_key"] == "E19" and spec.get("variable") != "value_proposition" else " The winning arm needs at least 2 human replies; otherwise no effect is established at this size.")
    sample = f"Sample: {spec['min_sample_per_arm']} first emails per arm, {spec['min_sample']} total, over {spec['max_duration_days']} days. Follow-ups use your existing cadence and the same budget."
    if spec["catalog_key"] == "E21":
        thresholds = "Support: morning / afternoon human reply rate >= 1.5; refute below 1.5 after 10 contacted per window and at least 3 human replies, over 7 days. Otherwise inconclusive."
        arms += ". Approved emails wait for their window. Unknown recipient zones use your saved timezone; never send outside 07:00-20:00 local."
    elif spec["catalog_key"] == "E26":
        sample = "Sample: 30 first emails over 10 days. Count each contacted person once."
        thresholds = "Support: booked / contacted >= 10%; refute below 3%, only after 30 contacted. Between these thresholds is inconclusive. You can review and correct every counted booking."
        arms += f". Your booking link: {spec['runner_params']['booking_link']}"
    if ledger.quality_enabled() and spec.get("checkpoint_parent_id"):
        sample = f"Sample: at most {spec['min_sample']} fresh contacts within your remaining budget."
        thresholds = f"Measure human reply rate, support at {spec['success_threshold']['value']:.0%} or above and refute below {spec['failure_threshold']['value']:.0%}, only after the recorded sample and each contact's response window matures."
    card = ConfirmationCard.for_action(get_action("experiment.run"),
        interpretation=(f"I can run this experiment for you. I will prepare {spec['min_sample']} matched first emails"
            + (" with your booking link and compare confirmed bookings. You only take the calls." if spec["catalog_key"] == "E26"
               else " in two balanced versions and compare human replies.")),
        consequences=[f"Your idea to test: {spec['hypothesis']}", f"Arms: {arms}",
            sample,
            thresholds,
            f"Every send needs your usual approval. Only your existing first-email auto-approval opt-in can approve automatically. Your daily cap is unchanged, {methods.send_plan(spec)['daily_limit']} per day.",
            "Public source disclosure, opt-out, your sender address, suppression and eligibility checks remain required. EU and UK contacts need recorded consent. No LinkedIn automation."],
        resulting_state={"experiment_id": spec["id"], "fingerprint": spec["fingerprint"],
            "ceiling_micro": spec["ceiling_micro"], "variants": spec["variants"]})
    if ledger.quality_enabled() and spec.get("checkpoint"):
        point = spec["checkpoint"]
        card.consequences.append(f"You also approve holding further sends after {point['after_sample']} contacts, giving each {point.get('wait_days', spec['max_duration_days'])} days to respond. If early response is below {point['max_response_rate']:.0%}, I will pause and propose one new version within your remaining budget. This is not a final verdict. No arm changes until you review its confirmation card.")
    if ledger.quality_enabled() and spec.get("checkpoint_parent_id"):
        card = replace(card, interpretation=f"I can prepare one reviewed alternative angle for you, version {spec['arm_version']}, for at most {spec['min_sample']} fresh contacts within your remaining budget.")
        card.consequences.append("You are approving one new versioned angle, not changing your active comparison. Your old arms and results stay saved. This sequential arm cannot establish a causal winner against them.")
    return {"kind": "confirmation", "body_md": card.interpretation,
        "actions": [replace(card, confirm_label="Run it for me").as_action()], "citations": []}


def manual_action(spec):
    arms = " and ".join(arm["label"] for arm in spec["variants"])
    return {"label": "Prepare drafts for me", "action": "outreach.draft", "style": "secondary",
            "payload": {"recruitment_request": f"Prepare balanced experiment email drafts for {arms}: {spec['hypothesis']}. "
                        "Keep public source disclosure, opt-out, sender address, suppression, consent, approvals and the daily cap."}}


def tags(draft):
    facts = draft.get("honesty_json") or {}
    if not isinstance(facts, dict):
        from app.gtm.journey import _json
        facts = _json(facts)
    return {key: draft.get(key) or facts.get(key) for key in ("experiment_id", "variant")}


def scoped_audience(venture_id, draft, fallback):
    identity = tags(draft)
    if not identity["experiment_id"]:
        return fallback
    with db()() as session:
        row = session.get(Experiment, identity["experiment_id"])
        venture = session.get(Venture, venture_id)
        context = json.loads(row.context) if row else {}
        spec = context.get("spec") or {}
        if (not row or not venture or (row.venture_id, row.workspace_id) != (venture_id, venture.workspace_id)
                or row.kind != ledger.KIND or spec.get("runner") != "outreach_split"
                or spec.get("direction") != ledger.current_direction(session, venture_id)
                or context.get("assignments", {}).get(draft.get("contact_id")) != identity["variant"]):
            return fallback
    params = next((arm["params"] for arm in spec["variants"] if arm["key"] == identity["variant"]), {})
    return " ".join(filter(None, (params.get("who") or spec["icp"]["who"], params.get("city"),
        (params.get("icp") or spec["icp"]).get("geography"))))


def balanced_pending(pending):
    ordered, groups = [], {}
    for draft in sorted(pending, key=lambda draft: draft.get("day", 1)):
        experiment_id = tags(draft)["experiment_id"]
        if not experiment_id or draft.get("day", 1) != 1:
            ordered.append(draft)
        else:
            groups.setdefault(experiment_id, {}).setdefault(tags(draft)["variant"], []).append(draft)
    for arms in groups.values():
        while any(arms.values()):
            for items in arms.values():
                if items:
                    ordered.append(items.pop(0))
    return ordered


def send_block(venture_id, draft):
    if is_local():
        from app.experiments.autonomous import _local_enabled, _local_blocked_message

        if not _local_enabled("send_email"):
            return _local_blocked_message("send_email")
    if draft.get("referral_ask"):
        from app.experiments.channel_tests import send_block as referral_send_block
        return referral_send_block(venture_id, draft)
    from app.approvals.external import outbound_quote
    from app.autopilot import guard, outreach

    identity = tags(draft)
    if outreach.send_slots_used_today(venture_id) >= (guard.get_settings(venture_id)["send_max_per_day"] or guard.DEFAULT_SEND_MAX):
        return "experiment_daily_cap"
    if ledger.quality_enabled() and draft.get("waitlist_entry_id"):
        from app.experiments.channel_tests import send_block as survey_send_block
        return survey_send_block(venture_id, draft, survey=True)
    with db()() as session:
        row = session.get(Experiment, identity["experiment_id"])
        venture = session.get(Venture, venture_id)
        if not row or not venture or (row.venture_id, row.workspace_id) != (venture_id, venture.workspace_id):
            return "experiment_not_running"
        context = json.loads(row.context)
        spec = context.get("spec") or {}
        from app.experiments.belief_autopilot import send_block as plan_send_block
        if spec.get("checkpoint_parent_id") and not ledger.quality_enabled():
            return "experiment_quality_disabled"
        if context.get("checkpoint_paused"):
            return "experiment_checkpoint_review_required"
        if context.get("belief_plan_id") and (blocked := plan_send_block(session, venture_id, context)):
            return blocked
        if (venture.archived_at or venture.stage in {"archived", "purged", "paused"}
                or row.kind != ledger.KIND or row.state != "running" or context.get("error")
                or spec.get("runner") != "outreach_split" or spec.get("direction") != ledger.current_direction(session, venture_id)
                or not row.started_at or utcnow() >= as_utc(row.started_at) + timedelta(days=spec["max_duration_days"])):
            return "experiment_not_running"
    keys = [arm["key"] for arm in spec["variants"]]
    if identity["variant"] not in keys or draft.get("contact_id") not in context.get("assignments", {}) or context["assignments"][draft["contact_id"]] != identity["variant"]:
        return "experiment_assignment_changed"
    if spec.get("variable") == "value_proposition":
        statement = next(arm["params"]["value_proposition"] for arm in spec["variants"] if arm["key"] == identity["variant"])
        if draft.get("value_proposition") != statement or no_dashes(statement) not in (draft.get("body") or ""):
            return "experiment_message_changed"
    if spec["catalog_key"] in {"E21", "E26"} and draft.get("requires_journey_review") is not True:
        return "experiment_review_required"
    saved = [item for item in outreach.drafts(venture_id) if tags(item)["experiment_id"] == spec["id"]]
    held = [item for item in saved if item.get("state") in {"sent", "delivery_pending", "delivery_unconfirmed"}]
    if (ledger.quality_enabled() and context.get("checkpoint_approved") and not context.get("checkpoint_complete")
            and sum(item.get("day", 1) == 1 for item in held) >= spec["checkpoint"]["after_sample"]):
        return "experiment_checkpoint_waiting"
    rate = 0 if is_local() else outbound_quote("gtm_email_send", "composio_gmail").get("credit_estimate_micro")
    if type(rate) is not int or rate < (0 if is_local() else 1) or any(type(item.get("experiment_send_micro")) is not int for item in held):
        return "experiment_send_price_unavailable"
    if context.get("actual_micro", 0) + sum(item["experiment_send_micro"] for item in held) + rate > spec["ceiling_micro"]:
        return "experiment_credit_ceiling"
    if draft.get("day", 1) == 1:
        counts = {key: sum(item.get("day", 1) == 1 and tags(item)["variant"] == key for item in held) for key in keys}
        if counts[identity["variant"]] >= spec["min_sample_per_arm"] or (spec["catalog_key"] != "E21" and counts[identity["variant"]] > min(counts.values())):
            return "experiment_arm_balance"
    draft["experiment_send_micro"] = rate
    return None


def observe(spec):
    from app.experiments.autonomous import _outcome_line, outcome
    from app.gtm.journey import _json
    from app.gtm.relationships.interviews import extract_mom_test_flags
    from app.gtm.supymem.workers import _classify_reply
    window = spec.get("response_window_days", spec["max_duration_days"]) if ledger.quality_enabled() else spec["max_duration_days"]

    with db()() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == spec["workspace_id"],
            GtmTouch.venture_id == spec["venture_id"], GtmTouch.channel == "email", GtmTouch.simulated == 0)
            .order_by(GtmTouch.created_at, GtmTouch.id)))
    if spec["catalog_key"] == "E26":
        return observe_bookings(spec, touches)
    by_variant = []
    sent_times = []
    for arm in spec["variants"]:
        firsts = {}
        for touch in touches:
            facts = _json(touch.honesty_json)
            if touch.direction == "out" and touch.sent_at and facts.get("experiment_id") == spec["id"] and facts.get("variant") == arm["key"] and facts.get("day", 1) == 1 and (not ledger.quality_enabled() or as_utc(touch.sent_at) <= utcnow()):
                firsts.setdefault(touch.contact_id, touch.sent_at)
        sent_times.extend(firsts.values())
        replies = {}
        for touch in touches:
            if touch.direction != "in" or touch.contact_id not in firsts or as_utc(touch.created_at) < as_utc(firsts[touch.contact_id]):
                continue
            if as_utc(touch.created_at) > as_utc(firsts[touch.contact_id]) + timedelta(days=window):
                continue
            if ledger.quality_enabled() and as_utc(touch.created_at) > utcnow():
                continue
            kind = touch.classification or _classify_reply(touch.body_text)
            if kind == "automated" or not touch.body_text or (not (touch.message_id or "").startswith("manual") and _classify_reply(touch.body_text) == "automated"):
                continue
            replies.setdefault(touch.contact_id, []).append(touch.body_text)
        flags = [extract_mom_test_flags("\n".join(texts)) for texts in replies.values()]
        sent, human = len(firsts), len(replies)
        by_variant.append({"key": arm["key"], "label": arm["label"], "sent": sent, "replies": human,
            "human_reply_rate": human / sent if sent else 0,
            "stories": sum(bool(flag["incident"]) and not flag["hypotheticals_flagged"] for flag in flags),
            "hypothetical_share": sum(bool(flag["hypotheticals_flagged"]) for flag in flags) / human if human else 0})
        if ledger.quality_enabled():
            mature = {contact for contact, sent_at in firsts.items() if as_utc(sent_at) +
                timedelta(days=spec.get("response_window_days", spec["max_duration_days"])) <= utcnow()}
            by_variant[-1].update(mature_sent=len(mature), mature_replies=len(mature & replies.keys()))
            checkpoint_contacts = {contact for contact, sent_at in firsts.items() if as_utc(sent_at) +
                timedelta(days=(spec.get("checkpoint") or {}).get("wait_days", window)) <= utcnow()}
            by_variant[-1].update(checkpoint_sent=len(checkpoint_contacts), checkpoint_replies=len(checkpoint_contacts & replies.keys()))
        if spec.get("variable") == "value_proposition":
            by_variant[-1].update(positive_replies=sum(any(_classify_reply(text) == "human_positive" for text in texts)
                for texts in replies.values()))
            by_variant[-1]["positive_reply_rate"] = by_variant[-1]["positive_replies"] / sent if sent else 0
            by_variant[-1]["booked"] = observe_bookings({**spec, "variants": [arm]}, touches)["by_variant"][0]["booked"]
    if spec.get("checkpoint_parent_id"):
        arm = by_variant[0]
        measured = {"events": arm["sent"], "human_reply_rate": arm["human_reply_rate"]}
        result = {"outcome": outcome(spec, measured), "measured": measured, "by_variant": by_variant,
            "insight": f"Your new angle: {arm['replies']}/{arm['sent']} human replies. This sequential arm is not a causal comparison with your earlier arms.",
            "sources": [{"url": f"/v/{spec['venture_id']}/gtm", "title": "Your experiment emails and human replies"}], "actual_micro": 0}
        from app.experiments import engine
        return engine.mature_result(result, engine.response_maturity(spec, sent_times, utcnow()))
    control, treatment = by_variant
    if spec["catalog_key"] == "E21":
        treatment, control = by_variant
    if spec["catalog_key"] not in {"E19", "E39", "E21"} or spec.get("variable") == "value_proposition":
        treatment, control = sorted(by_variant, key=lambda arm: arm["human_reply_rate"], reverse=True)
    numerator, denominator = treatment["human_reply_rate"], control["human_reply_rate"]
    if spec.get("variable") == "value_proposition":
        treatment, control = sorted(by_variant, key=lambda arm: arm["positive_reply_rate"], reverse=True)
        numerator, denominator = treatment["positive_reply_rate"], control["positive_reply_rate"]
    ratio = numerator / denominator if denominator else spec["success_threshold"]["value"] if numerator else 1.0
    eligible = (treatment["stories"] >= 1 and treatment["hypothetical_share"] <= .5 if spec["catalog_key"] == "E19"
        else sum(arm["replies"] for arm in by_variant) >= 3 if spec["catalog_key"] == "E21" else treatment["replies"] >= 2)
    if spec.get("variable") == "value_proposition":
        eligible = treatment["positive_replies"] >= 2
    measured = {"events": min(arm["sent"] for arm in by_variant) * 2,
        "reply_rate_ratio": ratio, "qualified_reply_rate_ratio": ratio if eligible else None if spec["catalog_key"] == "E21" else 0,
        "winner": treatment["key"] if eligible else None}
    if ledger.quality_enabled() and spec.get("variable") == "value_proposition":
        measured["positive_reply_rate_ratio"] = ratio
    decision = outcome(spec, measured)
    if spec["catalog_key"] == "E21" and decision != "supported":
        measured["winner"] = None
    counts = " ".join(f"{arm['label']}: {arm['replies']}/{arm['sent']} human replies ({arm['human_reply_rate']:.1%})." for arm in by_variant)
    if spec.get("variable") == "value_proposition":
        counts += " " + " ".join(f"{arm['label']}: {arm['positive_replies']} positive replies and {arm['booked']} confirmed bookings." for arm in by_variant)
    insight = counts + "\n" + _outcome_line(spec, measured)
    if not denominator and numerator:
        insight += " Control had zero replies; the reported ratio is a lower bound at the support threshold, not a finite rate estimate."
    if spec["catalog_key"] == "E19" and spec.get("variable") != "value_proposition":
        insight += f" Concrete worst-week stories: {by_variant[1]['stories']}; hypothetical share: {by_variant[1]['hypothetical_share']:.0%}."
    elif spec["catalog_key"] == "E21":
        insight += " Morning / afternoon needs at least 3 human replies and 10 contacted per window."
    else:
        insight += " A winner needs at least 2 human replies. Otherwise no city or segment effect is established at this size."
    result = {"outcome": decision, "measured": measured, "by_variant": by_variant, "insight": founder_copy(insight),
        "sources": [{"url": f"/v/{spec['venture_id']}/gtm", "title": "Your experiment emails and human replies"}], "actual_micro": 0}
    if ledger.quality_enabled():
        from app.experiments import engine
        result = engine.mature_result(result, engine.response_maturity(spec, sent_times, utcnow()))
        if result["outcome"] == "inconclusive":
            result["measured"]["winner"] = None
    return result


def checkpoint_remaining(spec, context):
    from app.autopilot import outreach

    held = [draft for draft in outreach.drafts(spec["venture_id"]) if tags(draft)["experiment_id"] == spec["id"]
        and draft.get("state") in {"sent", "delivery_pending", "delivery_unconfirmed"}]
    actual = context.get("actual_micro", 0)
    if (type(actual) is not int or actual < 0 or any(draft.get("state") != "sent" or
            type(draft.get("experiment_send_micro")) is not int or draft["experiment_send_micro"] < 0 for draft in held)):
        return None
    return max(0, spec["ceiling_micro"] - actual - sum(draft["experiment_send_micro"] for draft in held))


def review_checkpoint(spec, result):
    point = spec.get("checkpoint")
    if not ledger.quality_enabled() or not point:
        return None
    sent = sum(arm["sent"] for arm in result["by_variant"])
    mature = sum(arm.get("checkpoint_sent", arm.get("mature_sent", 0)) for arm in result["by_variant"])
    replies = sum(arm.get("checkpoint_replies", arm.get("mature_replies", 0)) for arm in result["by_variant"])
    if mature < point["after_sample"]:
        return None
    from app.experiments import autonomous
    from app.gtm import playbooks
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + spec["venture_id"])
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        if not context.get("checkpoint_approved") or row.state not in {"running", "concluded"}:
            return None
        if not context.get("checkpoint_paused") and replies / mature >= point["max_response_rate"]:
            context["checkpoint_complete"] = True
            row.context = json.dumps(context)
            session.commit()
            return None
        remaining = checkpoint_remaining(spec, context)
        if remaining is None:
            return None
        if context.get("checkpoint_alternative", {}).get("ceiling_micro", 0) > remaining:
            previous = session.get(Experiment, context["checkpoint_alternative"]["id"])
            previous.context = json.dumps({**json.loads(previous.context), "superseded": True})
            context.pop("checkpoint_alternative")
        if context.get("checkpoint_alternative"):
            alternative = context["checkpoint_alternative"]
        else:
            sample = max(0, spec["min_sample"] - sent)
            if spec["email_micro"]:
                sample = min(sample, remaining // spec["email_micro"])
            current = {arm["params"]["playbook"] for arm in spec["variants"]}
            angle = next((key for key in playbooks.PLAYBOOKS if key not in current), None)
            if sample < 1 or not angle:
                return None
            alternative = deepcopy(spec)
            alternative.update(checkpoint_parent_id=spec["id"], arm_version=spec.get("arm_version", 1) + 1,
                title="Reviewed alternative angle",
                variable="angle", variants=[{"key": "arm_v" + str(spec.get("arm_version", 1) + 1),
                    "label": playbooks.PLAYBOOKS[angle], "params": {"playbook": angle}}],
                runner_params={"playbooks": [angle], "pages_per_arm": 0}, search_ceiling_micro=0, provider=None,
                min_sample=sample, min_sample_per_arm=sample, ceiling_micro=sample * spec["email_micro"],
                metric="human_reply_rate", hypothesis=f"Your reviewed alternative angle receives human replies from {spec['icp']['who']}.",
                success_threshold={"op": ">=", "value": .1, "min_events": max(sample, spec["min_sample_per_arm"]), "metric": "human_reply_rate"},
                failure_threshold={"op": "<", "value": .03, "min_events": max(sample, spec["min_sample_per_arm"]), "metric": "human_reply_rate"},
                state="proposed", result=None, requires=[key for key in spec["requires"] if key != "firecrawl"])
            for key in ("checkpoint", "option_ids", "option_id", "fingerprint", "id", "evidence_binding"):
                alternative.pop(key, None)
            alternative["fingerprint"] = autonomous._fingerprint(alternative)
            alternative["id"] = "exp_" + alternative["fingerprint"][:24]
            alternative["evidence_binding"] = methods.evidence_binding(alternative)
            session.add(Experiment(id=alternative["id"], venture_id=spec["venture_id"], workspace_id=spec["workspace_id"],
                kind=ledger.KIND, hypothesis=alternative["hypothesis"], action=alternative["method"], state="proposed",
                context=json.dumps({"runner": "system", "spec": alternative})))
            context.update(checkpoint_paused=True, checkpoint_alternative=alternative, outreach_state="waiting_data")
            row.context = json.dumps(context)
            session.commit()
    return confirmation(alternative)


async def _search(spec, provider):
    from app.gtm import enrich, journey, reachable
    from app.research.source_quality import relevant_company_lead, relevant_search_lead

    buckets, actual = {}, 0
    consumer = is_local() and journey._consumer(spec["icp"])
    for arm in spec["variants"]:
        params, rows, seen = arm["params"], [], set()
        for query in params["queries"]:
            with activity.track(spec, action="search", tool=provider.name, evidence_level="desk_research", evidence_summary=query):
                result = await provider.search(query)
            actual += metered.firecrawl_actual_micro(result, spec["search_micro"]) if is_local() or provider.name == "firecrawl" else spec["search_micro"]
            for hit in result:
                url = journey._signal_url(hit.get("url") or "")
                if not url or url in seen or len(seen) >= spec["runner_params"]["pages_per_arm"] or not consumer and enrich.classify(url, hit.get("title", "")) != "company":
                    continue
                seen.add(url)
                with activity.track(spec, action="fetch", tool=provider.name, evidence_level="desk_research", source_url=url) as receipt:
                    page = await provider.scrape(url)
                    receipt["evidence_summary"] = str(page or "").strip()[:500]
                    if not receipt["evidence_summary"]:
                        receipt["outcome"] = "empty"
                actual += metered.firecrawl_actual_micro(page, spec["scrape_micro"]) if is_local() or provider.name == "firecrawl" else spec["scrape_micro"]
                text = str(page)
                relevant = (relevant_search_lead(params["who"] + " " + spec["icp"]["problem"], {**hit, "snippet": text}) if consumer else
                    relevant_company_lead({"category_terms": [params["who"]]}, {**hit, "snippet": text}))
                if not relevant:
                    continue
                city = str(params.get("city") or "").split(",", 1)[0].strip()
                if city and not re.search(r"\b" + re.escape(city) + r"\b", text, re.I):
                    continue
                profile = reachable.profile_facts(hit.get("title", ""), text)
                # ponytail: explicit employee counts only, use a verified size extractor for other formats.
                counts = [(int(lower), int(upper or lower)) for lower, upper in re.findall(
                    r"\b(\d+)(?:\s*(?:-|to)\s*(\d+))?\s+(?:employees|staff|people|person teams?)\b", text, re.I)]
                if "min_size" in params and not any(params["min_size"] <= lower <= upper <= params["max_size"] for lower, upper in counts):
                    continue
                if params.get("size") and params["size"].casefold() not in text.casefold():
                    continue
                if journey._geography_mismatch(params["icp"]["who"] + " " + journey._audience_location(params["icp"]["who"], params["icp"]), url, text, require_match=True):
                    continue
                if journey._same_offer(url, hit.get("title", ""), text, spec["icp"].get("competitors") or [], spec["icp"].get("offer", ""), spec["icp"]["problem"]):
                    continue
                facts = enrich.extract(enrich._host(url), [(url, text)], published_mailboxes=True)
                if not facts.get("email"):
                    continue
                seed = enrich.merge({"source_url": url, "website": url, "audience": params["who"], "evidence_label": "fit_only"}, facts)
                seed["profile_facts"] = {**profile, "employee_ranges": counts, "size_source_url": url}
                if city:
                    seed["profile_facts"].update(city=city, city_source_url=url, city_quote=enrich._evidence(text, city))
                seed["company_text"] = text
                rows.append(seed)
        buckets[arm["key"]] = rows
    return {"buckets": buckets, "actual_micro": actual}


def _assign(spec, buckets, existing_assignments=None):
    from app.autopilot import outreach
    from app.gtm import journey

    existing_assignments = existing_assignments or {}
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + spec["venture_id"])
        candidates = {arm["key"]: [] for arm in spec["variants"]}
        drafted = {item.get("contact_id") for item in outreach.drafts(spec["venture_id"]) if item.get("day", 1) == 1 and item.get("state") != "review_abandoned"}
        if buckets:
            for arm in spec["variants"]:
                journey._ingest(spec["workspace_id"], spec["venture_id"], buckets[arm["key"]], session=session)
            session.flush()
        for contact in session.scalars(select(GtmContact).where(GtmContact.workspace_id == spec["workspace_id"],
                GtmContact.source.like(f"venture:{spec['venture_id']}:%")).order_by(GtmContact.created_at, GtmContact.id)):
            if contact.id in drafted or contact.id in existing_assignments or contact.times_emailed or journey._block(session, spec["workspace_id"], contact):
                continue
            if (not buckets or spec.get("variable") == "value_proposition") and outreach._foreign_contact(session, spec["icp"]["who"] + " " + journey._audience_location(spec["icp"]["who"], spec["icp"]), contact):
                continue
            if spec.get("variable") == "value_proposition":
                key = min(candidates, key=lambda key: len(candidates[key]))
                candidates[key].append(contact.id)
            elif buckets:
                matches = [arm["key"] for arm in spec["variants"] if (contact.email or "").lower() in
                    {str(row.get("email") or "").lower() for row in buckets[arm["key"]]}]
                if len(matches) == 1:
                    candidates[matches[0]].append(contact.id)
            else:
                key = min(candidates, key=lambda key: len(candidates[key]))
                candidates[key].append(contact.id)
        remaining = spec["min_sample_per_arm"] - max((list(existing_assignments.values()).count(key) for key in candidates), default=0)
        size = min(remaining, *(len(items) for items in candidates.values()))
        assignments = {**existing_assignments, **{contact: key for key, items in candidates.items() for contact in items[:size]}}
        session.commit()
    return assignments


async def run(spec, payload):
    from app.experiments import autonomous, engine
    if spec.get("checkpoint_parent_id") and not ledger.quality_enabled():
        raise ValueError("Your checkpoint alternative is unavailable while experiment quality is switched off.")

    if payload.get("variants") != spec["variants"]:
        raise ValueError("experiment_confirmation_changed")
    if ledger.quality_enabled() and (payload.get("fingerprint") != spec["fingerprint"] or payload.get("ceiling_micro") != spec["ceiling_micro"]):
        raise ValueError("experiment_confirmation_changed")
    if spec["catalog_key"] == "E26":
        validate_booking_link(spec["runner_params"].get("booking_link"))
    with db()() as session:
        if ledger.quality_enabled() and spec.get("checkpoint_parent_id"):
            serialize_writes(session, key="market-beliefs:" + spec["venture_id"])
        serialize_writes(session, key="market-experiments:" + spec["venture_id"])
        row = session.get(Experiment, spec["id"])
        context = json.loads(row.context)
        if context.get("result"):
            return autonomous.result_reply({**spec, "result": context["result"]})
        if context.get("claimed"):
            return autonomous._reply("Your experiment is already running. Review the saved drafts through your usual approval flow.", kind="experiment_running")
        if ledger.quality_enabled() and spec.get("checkpoint_parent_id"):
            parent = session.get(Experiment, spec["checkpoint_parent_id"])
            parent_context = json.loads(parent.context) if parent else {}
            if (not parent or (parent.venture_id, parent.workspace_id) != (spec["venture_id"], spec["workspace_id"])
                    or not parent_context.get("checkpoint_paused") or parent.state not in {"running", "concluded"}
                    or parent_context.get("checkpoint_alternative", {}).get("fingerprint") != spec["fingerprint"]):
                raise ValueError("Your checkpoint changed. Review a fresh confirmation card.")
            remaining = checkpoint_remaining(parent_context["spec"], parent_context)
            if remaining is None or spec["ceiling_micro"] > remaining:
                raise ValueError("Your remaining budget changed. Review a fresh checkpoint confirmation card.")
            from app.experiments.belief_autopilot import approve_checkpoint
            approve_checkpoint(session, parent_context, context, spec)
            if parent.state == "running":
                advance(session, parent, "aborted", actor="founder", reason="approved_checkpoint_alternative")
            parent.ended_at = parent.ended_at or utcnow()
            parent_context["outreach_state"] = parent.state
            parent.context = json.dumps(parent_context)
        history = []
        for previous in session.scalars(select(Experiment).where(Experiment.venture_id == spec["venture_id"], Experiment.id != spec["id"], Experiment.kind == ledger.KIND)):
            previous_context = json.loads(previous.context)
            history.append({**previous_context.get("spec", {}), "venture_id": previous.venture_id,
                "state": previous_context.get("outreach_state") or previous.state, "ended_at": previous.ended_at})
        reason = engine.blocked_reason(spec, history, utcnow())
        if reason:
            return autonomous._reply(reason, kind="experiment_blocked")
        run_id = new_id("exr")
        job_id = enqueue(session, autonomous.KIND, {"experiment_id": spec["id"], "run_id": run_id},
            workspace_id=spec["workspace_id"], venture_id=spec["venture_id"], dedupe_key="experiment:" + run_id, max_attempts=1)
        advance(session, row, "running", actor="founder", reason="confirmed_outreach_experiment")
        row.started_at = utcnow()
        from app.credits import spend
        spend.record_run(context, run_id)
        row.context = json.dumps({**context, "claimed": True, "run_id": run_id, "job_id": job_id,
            **({"checkpoint_approved": bool(spec.get("checkpoint"))} if ledger.quality_enabled() else {})})
        session.commit()
    return {**autonomous._reply("Your experiment is running. I will prepare balanced drafts. Ask me to send them when you are ready; nothing is sent before your request." if is_local() else
        "Your experiment is running. I will prepare balanced drafts, then wait for your usual send approvals and human replies.", kind="experiment_running"), "run_id": job_id}


async def run_job(payload):
    from app.chat.milestones import post_milestone
    from app.core.jobs import FatalError, _leased_row
    from app.experiments import autonomous
    from app.gtm import journey
    from app.autopilot.send_admission import try_send_admission

    if payload.get("scheduled_draft_id"):
        return await send_scheduled_job(payload)

    with db()() as session:
        serialize_writes(session, key="market-experiments:" + payload["venture_id"])
        row = session.get(Experiment, payload["experiment_id"])
        context = json.loads(row.context) if row else {}
        job = _leased_row(session, payload.get("_job_id"), payload.get("_worker_id"), payload.get("_job_attempt"))
        if (not job or not row or row.kind != ledger.KIND or context.get("runner") != "system"
                or (row.venture_id, row.workspace_id) != (payload["venture_id"], payload["workspace_id"])
                or context.get("run_id") != payload["run_id"]):
            raise FatalError("experiment_job_scope_changed")
        pending = ledger.quality_enabled() and (context.get("result") or {}).get("response_window_open")
        if context.get("result") and not pending or context.get("error") or context.get("handled_job") == job.id:
            return
        if ledger.quality_enabled() and row.state == "aborted":
            return
        if context.get("job_id") != job.id:
            raise FatalError("experiment_job_scope_changed")
        if context.get("spend_stopped"):
            raise FatalError("experiment_stopped")
        spec = activity.run_context(context)
        if is_local() and not autonomous.autopilot_enabled():
            context["error"] = autonomous._local_blocked_message("run_for_me_experiments")
            row.context = json.dumps(context)
            session.commit()
        if spec.get("checkpoint_parent_id") and not ledger.quality_enabled():
            return
        expired = utcnow() >= as_utc(row.started_at) + timedelta(days=spec["max_duration_days"])
        if ledger.current_direction(session, row.venture_id) != spec["direction"]:
            expired = True
        if not context.get("prepared") and not expired:
            if context.get("research_started"):
                context["error"] = "Your search needs account recovery before retrying. No experiment email was sent."
            else:
                context["research_started"] = True
            row.context = json.dumps(context)
            session.commit()
    if ledger.quality_enabled() and context.get("prepared"):
        current = await asyncio.to_thread(observe, spec)
        card = confirmation(context["checkpoint_alternative"]) if context.get("checkpoint_paused") else await asyncio.to_thread(review_checkpoint, spec, current)
        if card:
            with db()() as session:
                post_milestone(spec["venture_id"], key=context["run_id"] + ":checkpoint", kind="confirmation", requested=True,
                    body_md="Your pre-approved checkpoint has paused this test. Review one alternative angle within your remaining budget.",
                    citations=[], actions=card["actions"], session=session)
                session.commit()
            return
        with db()() as session:
            saved = json.loads(session.get(Experiment, spec["id"]).context)
            if saved.get("checkpoint_complete"):
                context["checkpoint_complete"] = True
    try:
        if not context.get("prepared") and not expired and not context.get("error"):
            buckets, actual = {}, 0
            if spec["search_ceiling_micro"] or is_local() and spec.get("provider"):
                provider = autonomous._provider()
                if not provider or provider.name != spec["provider"]:
                    raise CreditsUnavailable("experiment_search_unavailable")
                async def checkpoint(result, _receipt):
                    with db()() as session:
                        row = session.get(Experiment, spec["id"])
                        row.context = json.dumps({**json.loads(row.context), "search_result": result})
                        session.commit()

                if is_local():
                    result = await _search(spec, provider)
                    await checkpoint(result, None)
                else:
                    result = await metered.call(lambda: _search(spec, provider), purpose="experiment.outreach_split",
                        estimate_micro=spec["search_ceiling_micro"], workspace_id=spec["workspace_id"], venture_id=spec["venture_id"],
                        provider=provider.name, request_id=payload["run_id"], actual_micro=lambda result: result["actual_micro"], before_meter_exit=checkpoint)
                buckets, actual = result["buckets"], result["actual_micro"]
                context["search_result"] = result
            with activity.track(spec, action="discover", tool="eligible_contacts", evidence_level="prospect_discovery") as receipt:
                assignments = await asyncio.to_thread(_assign, spec, buckets)
                receipt["evidence_summary"] = str(len(assignments))
            if not assignments:
                raise ValueError("experiment_no_balanced_eligible_contacts")
            with db()() as session:
                row = session.get(Experiment, spec["id"])
                context.update(assignments=assignments, actual_micro=actual)
                row.context = json.dumps(context)
                session.commit()
            def draft():
                with try_send_admission(spec["venture_id"]) as admitted:
                    if not admitted:
                        raise ValueError("gtm_send_in_progress")
                    made = journey._draft_ready_admitted(spec["workspace_id"], spec["venture_id"], limit=20,
                        review_only=True, experiment={**spec, "assignments": assignments})
                    methods.prepare_messages(spec)
                    return made
            with activity.track(spec, action="draft", tool="email_drafts", evidence_level="drafted_outreach") as receipt:
                made = await asyncio.to_thread(draft)
                receipt["evidence_summary"] = str(made)
            if not made:
                raise ValueError("experiment_drafts_not_ready")
            context.update(prepared=True, outreach_state="waiting_data")
        elif context.get("prepared") and not expired and not context.get("error"):
            assignments = await asyncio.to_thread(_assign, spec, context.get("search_result", {}).get("buckets", {}), context["assignments"])
            if assignments != context["assignments"]:
                context["assignments"] = assignments
                with db()() as session:
                    row = session.get(Experiment, spec["id"])
                    row.context = json.dumps(context)
                    session.commit()
            def top_up():
                with try_send_admission(spec["venture_id"]) as admitted:
                    if admitted:
                        with activity.track(spec, action="draft", tool="email_drafts", evidence_level="drafted_outreach") as receipt:
                            made = journey._draft_ready_admitted(spec["workspace_id"], spec["venture_id"], limit=20,
                                review_only=True, experiment={**spec, "assignments": assignments})
                            methods.prepare_messages(spec)
                            receipt["evidence_summary"] = str(made)
            await asyncio.to_thread(top_up)
        result = None
        if expired:
            with activity.track(spec, action="compare", tool="email_responses", evidence_level="conclusion") as receipt:
                result = await asyncio.to_thread(observe, spec)
                receipt["outcome"] = result["outcome"]
            if ledger.quality_enabled() and result.get("response_window_open") and result["outcome"] == "inconclusive":
                context["observed_result"] = result
                context.pop("result", None)
                result = None
    except Exception as exc:
        from app.chat.recovery import founder_failure

        failure = founder_failure(exc, spec["venture_id"], "your email experiment",
            manual_action(spec), safety="Your saved drafts and sending rules are unchanged. Review any earlier sends before approving more emails.")
        context["error"] = failure["body_md"]
        result = None
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + spec["venture_id"])
        row = session.get(Experiment, spec["id"])
        if "search_result" not in context and json.loads(row.context).get("search_result"):
            context["search_result"] = json.loads(row.context)["search_result"]
        context["handled_job"] = payload["_job_id"]
        if context.get("error"):
            context["outreach_state"] = "blocked"
            reply = autonomous.failure_reply({**spec, "error": context["error"]})
        elif result:
            from app.autopilot import outreach
            result["actual_micro"] = context.get("actual_micro", 0) + sum(draft.get("experiment_send_micro", 0)
                for draft in outreach.drafts(spec["venture_id"], "sent") if tags(draft)["experiment_id"] == spec["id"])
            context.update(result=result, outreach_state="concluded")
            if row.state == "running":
                advance(session, row, "concluded", actor="system", reason="outreach_observation")
            row.ended_at, row.outcome, row.learnings = utcnow(), result["outcome"], result["insight"]
            reply = autonomous.result_reply({**spec, "result": result})
        else:
            deadline = as_utc(row.started_at) + timedelta(days=spec["max_duration_days"])
            if ledger.quality_enabled() and context.get("observed_result", {}).get("response_window_open"):
                readable = context["observed_result"].get("readable_at")
                deadline = as_utc(datetime.fromisoformat(readable)) if readable else utcnow() + timedelta(days=1)
            context["job_id"] = enqueue(session, autonomous.KIND, {"experiment_id": spec["id"], "run_id": context["run_id"]},
                workspace_id=spec["workspace_id"], venture_id=spec["venture_id"],
                dedupe_key=f"experiment:{context['run_id']}:{payload['_job_id']}", max_attempts=1,
                run_after=min(utcnow() + timedelta(days=1), deadline))
            note = ""
            if is_local():
                from app.experiments.autonomous import _local_enabled, _local_blocked_message

                note = (" Ask me to send the drafts when you are ready." if _local_enabled("send_email") else
                    " " + _local_blocked_message("send_email"))
            reply = autonomous._reply(f"Your experiment drafts are saved. Nothing is sent without your request. Your daily cap stays unchanged. I will compare your counts after {spec['max_duration_days']} days." + note if is_local() else
                f"Your experiment drafts are saved. Every send still needs your usual approval, or your existing auto-approval opt-in. Your daily cap stays unchanged. I will compare your counts after {spec['max_duration_days']} days.", kind="experiment_running")
            reply["actions"] = [{"label": "Review your drafts", "href": f"/v/{spec['venture_id']}/gtm"}]
            if ledger.quality_enabled() and context.get("observed_result", {}).get("response_window_open"):
                reply["body_md"] = context["observed_result"]["insight"]
        row.context = json.dumps(context)
        if spec.get("option_ids"):
            measured_result = {"error": context["error"]} if context.get("error") else result or observe(spec)
            methods.record_result(spec, measured_result, session=session, ended_at=utcnow())
        if not spec.get("option_ids"):
            post_milestone(spec["venture_id"], key=payload["run_id"] + (":finished" if result or context.get("error") else ":drafts"),
                kind=reply["kind"], requested=True, body_md=reply["body_md"], citations=reply["citations"], actions=reply["actions"], session=session)
        session.commit()
    if spec.get("option_ids"):
        from app.experiments import exploration
        if exploration.enabled():
            await asyncio.to_thread(exploration._refresh, spec["venture_id"])
            await asyncio.to_thread(exploration.refresh_messages, spec["venture_id"])
