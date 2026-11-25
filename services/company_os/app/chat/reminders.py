"""Founder reminders in chat: "remind me in 10 minutes to call Ravi".

Each reminder is one delayed ``chat.reminder`` job (durable, survives restarts, no polling loop of
our own). At its time the worker posts the reminder in the venture's chat, sends one Mac notice
and, only for reminders set more than a day ahead while the founder is away, one email.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.clock import as_utc, utcnow
from app.chat.actions import question_reply

KIND = "chat.reminder"
DEFAULT_TZ = "Asia/Kolkata"
EMAIL_AFTER = timedelta(days=1)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
            "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45, "a couple of": 2, "a few": 3}
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_PARTS = {"morning": 9, "noon": 12, "afternoon": 14, "evening": 18, "tonight": 20, "night": 20}

_REMIND = re.compile(r"\A\s*(?:(?:hey\s+)?charlie[,\s]+)?(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
                     r"(?:remind|ping|nudge)\s+me\b(?P<rest>.*?)[\s.!?]*\Z", re.I | re.S)
_LIST = re.compile(r"\A\s*(?:(?:what|which)\s+reminders?\b|(?:show|list)\s+(?:me\s+)?(?:my\s+|the\s+)?reminders?\b|"
                   r"(?:my\s+)?reminders\s*\??\s*\Z|do\s+i\s+have\s+(?:any\s+)?reminders?\b)", re.I)
_CANCEL = re.compile(r"\A\s*(?:please\s+)?(?:cancel|delete|remove|drop|clear)\b(?P<rest>.*\breminders?\b.*)\Z", re.I | re.S)
_SNOOZE = re.compile(r"\A\s*(?:please\s+)?snooze\b(?P<rest>.*?)[\s.!?]*\Z", re.I | re.S)
_N = r"(?P<n>[+-]?\d+|a couple of|a few|forty five|an?|one|two|three|four|five|six|ten|fifteen|twenty|thirty)"
_REL = re.compile(rf"\b(?:in|after|for)\s+(?:(?P<half>half\s+an?\s+hour)|{_N}\s*(?P<u>m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?|years?)\b)", re.I)
_MON = "(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
_DAY = re.compile(r"\b(?:(?P<rel>today|tonight|tomorrow|tmrw|day\s+after\s+tomorrow)|(?:on\s+)?(?:(?:next|this)\s+)?"
                  r"(?P<wd>mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?|"
                  r"(?:on\s+)?(?P<iso>\d{4}-\d{2}-\d{2})|"
                  rf"(?:on\s+)?(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{_MON})[a-z]*|"
                  rf"(?:on\s+)?(?P<m2>{_MON})[a-z]*\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)\b", re.I)
_CLOCK = re.compile(r"\b(?:at\s+(?P<h>\d{1,2})(?:[:.](?P<mi>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?|"
                    r"(?P<h2>\d{1,2})(?:[:.](?P<mi2>\d{2}))?\s*(?P<ap2>am|pm|a\.m\.|p\.m\.)|"
                    r"(?:in\s+the\s+|at\s+|this\s+)?(?P<part>morning|noon|afternoon|evening|night))\b", re.I)

_LEFTOVER_TIME = re.compile(r"\b(?:from\s+now|weeks?|months?|later|next|ish|before|after|every|o'?clock|hours?|minutes?)\b", re.I)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip() or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def founder_zone(venture_id: str) -> ZoneInfo:
    """The venture owner's saved timezone (the console saves the browser's); IST when none is known."""
    from app.db.models import Person, Venture, Workspace
    from app.state import db
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        person = session.get(Person, workspace.owner_id) if workspace else None
        return zone(person.timezone if person else None)


def _subject(text: str) -> str:
    """The reminder phrase with its connector ("to call Ravi", "about pricing")."""
    return re.sub(r"\s+", " ", text).strip(" ,.;:-")


def _what(phrase: str) -> str:
    return re.sub(r"\A(?:to|about|that|of|for|re:?)\s+", "", phrase, flags=re.I).strip(" ,.;:-") or "your reminder"


def parse_when(rest: str, now: datetime, tz: ZoneInfo) -> tuple[datetime, str] | None:
    """Rules for "in N minutes/hours", "tomorrow at 9", weekdays and explicit dates. Returns (due UTC, what)."""
    local = as_utc(now).astimezone(tz)
    if (m := _REL.search(rest)):
        if m["half"]:
            delta = timedelta(minutes=30)
        else:
            n = int(m["n"]) if m["n"].lstrip("+-").isdigit() else _NUMBERS[m["n"].lower()]
            if m["u"].lower().startswith("y"):
                raise ValueError("reminder_time_out_of_range")
            delta = timedelta(**{_UNITS[m["u"][0].lower()]: n})
        return _checked(local + delta, local), _subject(rest[:m.start()] + " " + rest[m.end():])
    day_match, clock_match = _DAY.search(rest), _CLOCK.search(rest)
    if not day_match and not clock_match:
        return None
    day = local.date()
    if day_match:
        found = _day(day_match, local.date())
        if found is None:
            return None
        day = found
    hour, minute = 9, 0
    if clock_match:
        hour, minute = _clock(clock_match)
    elif day_match and day_match["rel"] and day_match["rel"].lower() == "tonight":
        hour = 20
    if hour is None:
        return None
    due = datetime.combine(day, time(hour, minute), tzinfo=tz)
    if not day_match and due <= local:
        due += timedelta(days=1)  # "at 5pm" after 5pm means tomorrow
    spans = sorted([s for s in (day_match, clock_match) if s], key=lambda s: s.start(), reverse=True)
    for span in spans:
        rest = rest[:span.start()] + " " + rest[span.end():]
    return _checked(due, local), _subject(rest)


def _day(m: re.Match, today: date) -> date | None:
    if m["rel"]:
        word = m["rel"].lower()
        return today + timedelta(days=2 if word.startswith("day") else 1 if word in {"tomorrow", "tmrw"} else 0)
    if m["wd"]:
        wd = next(i for i, name in enumerate(_WEEKDAYS) if name.startswith(m["wd"].lower()[:3]))
        return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    try:
        if m["iso"]:
            return date.fromisoformat(m["iso"])
        month = _MONTHS[(m["m1"] or m["m2"]).lower()[:3]]
        found = date(today.year, month, int(m["d1"] or m["d2"]))
        return found if found >= today else found.replace(year=today.year + 1)
    except ValueError:
        return None


def _clock(m: re.Match) -> tuple[int | None, int]:
    if m["part"]:
        return _PARTS[m["part"].lower()], 0
    hour, minute = int(m["h"] or m["h2"]), int(m["mi"] or m["mi2"] or 0)
    ap = (m["ap"] or m["ap2"] or "").replace(".", "").lower()
    if hour > 23 or minute > 59 or (ap and not 1 <= hour <= 12):
        return None, 0
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    elif not ap and 1 <= hour <= 7:
        hour += 12  # "at 5" is almost always 5 PM in a working day
    return hour, minute


def _checked(due: datetime, local: datetime) -> datetime:
    if not local < due <= local + timedelta(days=366):
        raise ValueError("reminder_time_out_of_range")
    return as_utc(due)


async def _model_when(rest: str, local: datetime, venture_id: str) -> tuple[datetime, str] | None:
    """One cheap metered flash call for odd phrasing; the answer is validated like a rule match."""
    from app.credits import metered
    try:
        response = await metered.llm([
            {"role": "system", "content": "Extract a reminder time. Reply with JSON only: "
             '{"due_local": "YYYY-MM-DDTHH:MM" or null, "what": "short reminder text"}. '
             "Use the founder's local time given. null when no time is stated."},
            {"role": "user", "content": json.dumps({"now_local": local.strftime("%Y-%m-%dT%H:%M %A"), "request": rest})}],
            purpose="venture.chat.assistant", venture_id=venture_id, tier="flash", max_tokens=2000)
        raw = str(response.choices[0].message.content or "")
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        due = datetime.fromisoformat(str(data["due_local"])).replace(tzinfo=local.tzinfo)
        return _checked(due, local), _subject(str(data.get("what") or rest))[:300]
    except Exception as exc:  # unusable model output falls back to asking the founder
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "reminder.time")
        return None


def _fmt(due: datetime, tz: ZoneInfo, now: datetime) -> str:
    local, today = as_utc(due).astimezone(tz), as_utc(now).astimezone(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return clock
    if local.date() == today + timedelta(days=1):
        return f"{clock} tomorrow"
    if local.date() < today + timedelta(days=7):
        return f"{clock} on {local.strftime('%A')}"
    return f"{clock} on {local.strftime('%a %d %b').replace(' 0', ' ')}"


def command(text: str) -> tuple[str, str] | None:
    raw = (text or "").strip()
    if len(raw) > 500:
        return None
    if m := _REMIND.match(raw):
        if re.match(r"\s*(?:of|what|which|how|where)\b", m["rest"], re.IGNORECASE) and re.search(
                r"\b(?:you|we)\s+(?:(?:had|have|already|previously)\s+)*(?:wrote|written|drafted|created|prepared|made|generated)\b",
                m["rest"], re.IGNORECASE):
            return None
        return "set", m["rest"]
    if m := _SNOOZE.match(raw):
        return "snooze", m["rest"]
    if m := _CANCEL.match(raw):
        return "cancel", m["rest"]
    if _LIST.match(raw):
        return "list", ""
    return None


def _pending(session, venture_id: str):
    from sqlalchemy import select
    from app.core.jobs import CLAIMABLE
    from app.db.models import Job
    return list(session.scalars(select(Job).where(Job.kind == KIND, Job.venture_id == venture_id,
        Job.status.in_(tuple(CLAIMABLE)), Job.cancel_requested.is_(False)).order_by(Job.run_after, Job.id)))


def _text(job) -> str:
    return str(json.loads(job.payload_json or "{}").get("text") or "your reminder")


def schedule(venture_id: str, what: str, due: datetime) -> str:
    """Persist a delayed job; workers poll for due jobs without an immediate Redis wake."""
    from app.core.jobs import enqueue
    from app.db.models import Venture
    from app.state import db
    with db()() as session:
        venture = session.get(Venture, venture_id)
        job_id = enqueue(session, KIND, {"text": what[:300], "due_at": as_utc(due).isoformat(),
                         "set_at": as_utc(utcnow()).isoformat()}, workspace_id=venture.workspace_id,
                         venture_id=venture_id, run_after=as_utc(due))
        session.commit()
    return job_id


async def run(venture_id: str, action: str, rest: str) -> dict:
    tz, now = founder_zone(venture_id), utcnow()
    if action == "list":
        return _list_reply(venture_id, tz, now)
    if action == "cancel":
        return _cancel(venture_id, rest, tz, now)
    if action == "snooze":
        return _snooze(venture_id, rest, tz, now)
    try:
        parsed = parse_when(rest, now, tz)
    except (ValueError, OverflowError):
        return _reply('Reminders must be in the future and less than a year away (within 366 days). '
                      'No reminder was scheduled. Try "remind me in 30 minutes".',
                      [{"label": "Set a reminder", "action": "chat.prefill",
                        "payload": {"text": "remind me in 30 minutes"}, "style": "primary"}])
    if parsed is None or _LEFTOVER_TIME.search(parsed[1]):  # rules caught only part of the time
        parsed = await _model_when(rest, as_utc(now).astimezone(tz), venture_id) or parsed
    if parsed is None:
        return question_reply('When should I remind you? For example "in 30 minutes", "tomorrow at 9" or "on Friday".', kind="reminder")
    due, phrase = parsed
    schedule(venture_id, _what(phrase), due)
    joined = f" {phrase}" if re.match(r"(?:to|about)\s", phrase, re.I) else f": {_what(phrase)}"
    return _reply(f"Got it, I'll remind you at {_fmt(due, tz, now)}{joined}.",
                  [{"label": "Show my reminders", "action": "chat.reply", "text": "what reminders do I have", "style": "secondary"}])


def _reply(body: str, actions: list | None = None) -> dict:
    return {"kind": "reminder", "body_md": body, "actions": actions or []}


def _list_reply(venture_id: str, tz: ZoneInfo, now: datetime) -> dict:
    from app.state import db
    with db()() as session:
        rows = [(job.run_after, _text(job)) for job in _pending(session, venture_id)]
    if not rows:
        return _reply('You have no reminders set. Say "remind me tomorrow at 9 to check replies" to add one.')
    lines = [f"{i}. {_fmt(due, tz, now)}: {what}" for i, (due, what) in enumerate(rows, 1)]
    return _reply("Your reminders:\n\n" + "\n".join(lines) + '\n\nSay "cancel the 1st reminder" or "cancel the Friday reminder" to remove one.')


def _cancel(venture_id: str, rest: str, tz: ZoneInfo, now: datetime) -> dict:
    from app.core.jobs import request_cancel
    from app.state import db
    words = {w for w in re.findall(r"[a-z0-9]+", rest.lower()) if len(w) > 2} - {
        "the", "reminder", "reminders", "about", "for", "please", "that", "this", "all", "my"}
    with db()() as session:
        rows = _pending(session, venture_id)
        if re.search(r"\ball\b", rest, re.I):
            chosen = rows
        elif (number := re.search(r"\b(\d+)(?:st|nd|rd|th)?\b", rest)) and 1 <= int(number[1]) <= len(rows):
            chosen = [rows[int(number[1]) - 1]]
        else:
            def match(job):
                local = as_utc(job.run_after).astimezone(tz)
                hay = set(re.findall(r"[a-z0-9]+", _text(job).lower())) | {local.strftime("%A").lower(), local.strftime("%a").lower()}
                if local.date() == as_utc(now).astimezone(tz).date() + timedelta(days=1):
                    hay.add("tomorrow")
                return words and words <= hay
            chosen = [job for job in rows if match(job)] if words else rows if len(rows) == 1 else []
        if not rows:
            return _reply("You have no reminders to cancel.")
        if not chosen or (len(chosen) > 1 and not re.search(r"\ball\b", rest, re.I)):
            session.rollback()
            listed = _list_reply(venture_id, tz, now)
            return question_reply("Which one? " + listed["body_md"], listed["actions"], kind="reminder")
        for job in chosen:
            request_cancel(session, job.id)
        names = "; ".join(f"{_fmt(job.run_after, tz, now)}: {_text(job)}" for job in chosen)
        session.commit()
    return _reply(f"Cancelled {'your reminder' if len(chosen) == 1 else f'{len(chosen)} reminders'} ({names}).")


def _snooze(venture_id: str, rest: str, tz: ZoneInfo, now: datetime) -> dict:
    """Snooze the reminder that fired most recently (within a day) by the given time, 10 minutes by default."""
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    try:
        parsed = parse_when(rest if _REL.search(rest) or _DAY.search(rest) or _CLOCK.search(rest) else "in 10 minutes", now, tz)
        if parsed is None and (bare := re.search(r"\b(\d+)\s*(m|mins?|minutes?|h|hrs?|hours?)\b", rest, re.I)):
            parsed = parse_when(f"in {bare[1]} {bare[2]}", now, tz)
    except ValueError:
        parsed = None
    if parsed is None:
        return question_reply('How long should I snooze it? For example "snooze 10 min" or "snooze until 5pm".', kind="reminder")
    with db()() as session:
        last = session.scalar(select(Job).where(Job.kind == KIND, Job.venture_id == venture_id,
            Job.status.in_(("succeeded", "done")), Job.finished_at >= utcnow() - timedelta(days=1))
            .order_by(Job.finished_at.desc()).limit(1))
        what = _text(last) if last else None
    if what is None:
        return _reply("There is no recent reminder to snooze. Say \"what reminders do I have\" to see upcoming ones.")
    schedule(venture_id, what, parsed[0])
    return _reply(f"Snoozed. I'll remind you again at {_fmt(parsed[0], tz, now)}: {what}.")


def _one_tap(what: str) -> list[dict]:
    low = what.lower()
    actions = []
    if re.search(r"\b(repl(?:y|ies)|outreach|leads?|customers?|emails?|drafts?)\b", low):
        actions.append({"label": "Show customers and drafts", "action": "gtm.status", "style": "primary"})
    elif re.search(r"\bresearch\b", low):
        actions.append({"label": "Show research here", "action": "research", "style": "primary"})
    actions.append({"label": "Snooze 10 min", "action": "chat.reply", "text": "snooze 10 min", "style": "secondary"})
    return actions


def deliver(payload: dict) -> None:
    """Worker handler. Each effect is keyed by the job id, so a retried job never repeats one."""
    from sqlalchemy import select
    from app import desktop_notices
    from app.chat.store import append_message
    from app.db.models import Venture, Workspace
    from app.db.supymem_models import Notice
    from app.state import db

    job_id, venture_id = str(payload["_job_id"]), str(payload["venture_id"])
    what = str(payload.get("text") or "your reminder")[:300]
    append_message(venture_id, role="assistant", body_md=f"Reminder: {what}", kind="reminder",
                   actions=_one_tap(what), idempotency_key=f"reminder:{job_id}")
    notice_kind = f"reminder:{job_id}"
    with db()() as session:
        noticed = session.scalar(select(Notice.id).where(Notice.kind == notice_kind).limit(1))
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        owner, name = (workspace.owner_id if workspace else None), (venture.name if venture else "")
    if not noticed:
        desktop_notices.enqueue(title="Reminder", body=what, venture_id=venture_id, kind=notice_kind)
    if owner and _wants_email(payload, owner):
        from app.email.service import send_email
        send_email(owner, "reminder", {"detail": what, "venture_name": name, "path": f"/v/{venture_id}/now"}, ref=job_id)


def _wants_email(payload: dict, owner: str) -> bool:
    """Only a reminder set more than a day ahead, and only while the founder is away from the app."""
    from app.email.service import QUIET_AFTER_ACTIVITY, last_active
    from app.state import db
    try:
        set_at, due = datetime.fromisoformat(payload["set_at"]), datetime.fromisoformat(payload["due_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if due - set_at <= EMAIL_AFTER:
        return False
    with db()() as session:
        active = last_active(session, owner)
    return active is None or as_utc(utcnow()) - as_utc(active) >= QUIET_AFTER_ACTIVITY
