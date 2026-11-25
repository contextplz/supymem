"""Charlie comes to the founder: a "since you left" brief and event nudges, grounded only in saved state.

* ``on_chat_open`` (GET /chat): record the visit on the existing reply and reuse its saved next moves.
* ``sweep_all`` (worker 5-minute tick): one nudge per background event (research done/failed/stale, leads
  ready, drafts ready, first reply, send blocked, job stuck, Idea page changed) in chat plus a Mac notice.
  At most ``DAILY_NUDGES`` chat nudges per venture per founder day, nothing in quiet hours (22:00-08:00 founder
  time); events stay derivable from state, so a held nudge goes out on the first sweep after quiet hours.
  Email stays on the existing needs_you paths (these kinds never email).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.db.models import ChatMessage, Job, Person, Venture, VentureDocument, Workspace
from app.state import db
from core.clock import as_utc, utcnow

BRIEF_GAP = timedelta(minutes=30)
DAILY_NUDGES = 4
STUCK = timedelta(minutes=30)
STALE = timedelta(hours=1)
NEWS = timedelta(hours=24)  # older background events are not news any more
OLD_WORK = timedelta(days=7)  # stopped or stuck work older than this is abandoned, not news
QUIET_START, QUIET_END = 22, 8
NUDGE, BRIEF = "proactive_nudge", "proactive_brief"
WATCHED_JOBS = {"idea.validate": "Validation", "research.deep": "Research", "plan.compile": "Planning",
                "gtm.journey.prepare": "Finding customers", "pagebuilder.build": "Building your landing page"}
ACTIVE = ("pending", "scheduled", "retry_wait", "running", "leased")
FAILED = ("failed", "failed_dead", "poison", "cancelled")


# ------------------------------------------------------------------ small state (one venture document)

def _doc_id(vid: str) -> str:
    return f"proactive:{vid}"


def _state(s, vid: str) -> tuple[VentureDocument | None, dict]:
    doc = s.get(VentureDocument, _doc_id(vid))
    try:
        return doc, (json.loads(doc.body_md or "{}") if doc else {})
    except ValueError:
        return doc, {}


def _write(s, vid: str, doc: VentureDocument | None, data: dict) -> None:
    now = utcnow()
    if doc is None:
        doc = VentureDocument(id=_doc_id(vid), venture_id=vid, kind="chat_proactive", title="Charlie check-ins",
                              created_at=now)
        s.add(doc)
    doc.body_md, doc.updated_at = json.dumps(data), now


def _ts(value: Any) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(value)) if isinstance(value, str) and value else None
    except ValueError:
        return None


# ------------------------------------------------------------------ founder time

def _zone(s, vid: str, person_id: str | None = None) -> ZoneInfo | None:
    venture = s.get(Venture, vid)
    workspace = s.get(Workspace, venture.workspace_id) if venture and venture.workspace_id else None
    person = s.get(Person, person_id or workspace.owner_id) if person_id or workspace else None
    try:
        return ZoneInfo(person.timezone) if person and person.timezone else None
    except (KeyError, ValueError):
        return None


def _local(s, vid: str, now: datetime, person_id: str | None = None) -> datetime:
    zone = _zone(s, vid, person_id)
    return as_utc(now).astimezone(zone) if zone else as_utc(now)


def quiet(vid: str, now: datetime | None = None, *, person_id: str | None = None) -> bool:
    with db()() as s:
        hour = _local(s, vid, now or utcnow(), person_id).hour
    return hour >= QUIET_START or hour < QUIET_END


def _day_start(s, vid: str, now: datetime) -> datetime:
    local = _local(s, vid, now)
    return as_utc(local.replace(hour=0, minute=0, second=0, microsecond=0))


# ------------------------------------------------------------------ events from saved state

def _ev(key: str, at: datetime | None, title: str, body: str, actions: list, said: tuple = (), risk: bool = False,
        question: bool = False) -> dict:
    return {"key": key, "at": as_utc(at) if at else None, "title": title, "body": body, "actions": actions,
            "said": said, "risk": risk, "citations": [{"question": True}] if question else []}


def _ask(label: str, text: str, style: str = "primary") -> dict:
    """A one-tap chip that sends the founder's request to Charlie (handled like a typed message)."""
    return {"label": label, "action": "chat.reply", "payload": {"text": text}, "style": style}


def _hours(delta: timedelta) -> str:
    h = int(delta.total_seconds() // 3600)
    return f"{h} hour{'s' if h != 1 else ''}" if h else f"{int(delta.total_seconds() // 60)} minutes"


def _job_events(s, vid: str, now: datetime) -> list[dict]:
    from app.chat import failed_jobs

    out = []
    for kind, label in WATCHED_JOBS.items():
        job = s.scalars(select(Job).where(Job.venture_id == vid, Job.kind == kind)
                        .order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
        if job is None:
            continue
        started = as_utc(job.started_at or job.created_at)
        ended = as_utc(job.finished_at or job.created_at)
        retry = [failed_jobs.retry_button(kind)] if kind in failed_jobs.KINDS else [_ask("Try again", f"Retry {label.lower()}")]
        if job.status in ACTIVE and now - started > STUCK:
            out.append(_ev(f"stuck:{job.id}", started, f"{label} is taking longer than usual",
                f"{label} has been running for {_hours(now - started)}, longer than it normally takes. "
                "Your saved work is safe. Want me to check what it is waiting on?",
                [_ask("Check on it", f"Why is {label.lower()} taking so long?")], risk=True, question=True))
        elif job.status in FAILED:
            acted = s.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == vid, ChatMessage.role == "user",
                                                          ChatMessage.created_at > ended).limit(1))
            what = "stopped" if job.status == "cancelled" else "did not finish"
            if now - ended > STALE and not acted:
                step = {"research.deep": "research", "idea.validate": "validation", "gtm.journey.prepare": "find customers"}.get(kind)
                out.append(_ev(f"stale:{job.id}", ended, f"{label} {what} and is waiting on you",
                    f"{label} {what} {_hours(now - ended)} ago and nothing has happened since. Your saved work is kept. "
                    "Want me to retry it" + (", or skip it and move on?" if step else "?"),
                    retry + ([_ask(f"Skip {step}", f"Skip {step}", "secondary")] if step else []), risk=True, question=True))
            elif job.status != "cancelled" and kind in failed_jobs.KINDS:  # the founder stopped it themselves: no news
                code = failed_jobs.clean_code(job.last_error)
                out.append(_ev(f"failed:{job.id}", ended, f"{label} did not finish", failed_jobs.body(kind, code),
                    failed_jobs.failure_actions(vid, kind, code), said=(f"{failed_jobs.KINDS[kind][2]}_failed",), risk=True))
        elif job.status in ("done", "succeeded") and kind == "research.deep":
            out.append(_ev(f"research_done:{job.id}", ended, "Your research is ready",
                "Research on your idea is done. Let's turn what it found into your next move.",
                [_ask("What did we learn?", "Summarize what the research found and what we should do next")],
                said=("research_ready",)))
    return out


def _gtm_events(s, vid: str, now: datetime) -> list[dict]:
    from app.db.models import GtmTouch
    from app.autopilot.outreach import KIND as DRAFT

    out = []
    journey = s.scalar(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == "gtm_journey"))
    data = json.loads(journey.provenance_json or "{}") if journey else {}
    found = len(data.get("reachable_leads") or [])
    if journey and data.get("state") == "ready" and found:
        out.append(_ev(f"leads:{data.get('job_id') or journey.id}", journey.updated_at, "Your leads are ready",
            f"I found {found} people with reachable contact routes. Next, I can draft short, personal "
            "first messages for the best ones. Nothing is sent without you.",
            [_ask("Draft first messages", "Draft first messages for the best leads you found")]))
    elif journey and data.get("state") in {"ready", "needs_input"} and not found:
        from app.chat.customers import empty_search_reply

        reply = empty_search_reply(data)
        out.append(_ev(f"leads_empty:{data.get('job_id') or journey.id}", journey.updated_at,
            "No reachable leads found yet",
            reply["body_md"], reply["actions"]))
    drafts, blocked = {}, []
    for doc in s.scalars(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == DRAFT,
                                                       VentureDocument.updated_at >= now - NEWS)):
        d = json.loads(doc.provenance_json or "{}")
        if d.get("state") == "draft" and d.get("channel") == "email" and not d.get("error") and d.get("batch_id"):
            drafts.setdefault(d["batch_id"], []).append(doc.updated_at)
        elif d.get("error") and d.get("state") in ("draft", "delivery_unconfirmed"):
            blocked.append((doc, d))
    for batch, stamps in drafts.items():
        from app.autopilot.outreach import send_card
        out.append(_ev(f"drafts:{batch}", max(stamps), "Outreach drafts are ready",
            f"{len(stamps)} outreach email{'s are' if len(stamps) != 1 else ' is'} ready for your approval. "
            "Nothing is sent until you approve.", [send_card(len(stamps), batch)], said=("needs_you",)))
    for doc, d in blocked[:1]:
        from app.chat.blockers import error_copy
        out.append(_ev(f"send_blocked:{doc.id}:{d['error']}", doc.updated_at, "An email could not be sent",
            "An outreach email was held back and not sent. " + (error_copy(d["error"]) or "") + " Your draft is saved.",
            [_ask("Fix it", "Why was my outreach email not sent, and how do we fix it?")], said=("needs_you",), risk=True))
    first = s.scalars(select(GtmTouch).where(GtmTouch.venture_id == vid, GtmTouch.direction == "in",
                                             GtmTouch.simulated == 0).order_by(GtmTouch.created_at).limit(1)).first()
    if first:
        out.append(_ev("first_reply", first.created_at, "Your first reply came in",
            "Someone replied to your outreach. Replies go cold fast, so let's answer today.",
            [_ask("Help me reply", "Help me reply to the first response we got")], said=("outreach_reply", "needs_you")))
    return out


def _idea_mark(vid: str) -> tuple[str | None, dict | None]:
    from app.ventures.idea_page import read

    saved = read(vid)
    if not saved:
        return None, None
    page = saved["page"]
    parts = [page.get(k) or "" for k in ("summary", "problem", "offer")] + list((page.get("customers") or {}).get("current") or [])
    parts = [" ".join(str(part).lower().split()) for part in parts]
    parts = parts[:3] + sorted(set(parts[3:]) - {""})
    return "v2:" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16], saved


def _idea_event(vid: str, known: str | None) -> dict | None:
    from app.authz.workspace import subject_person_id
    from app.chat.conversations import current_conversation
    from app.chat.idea_bridge import split_question_reply
    from app.chat.idea_corrections import parse
    from app.db.supymem_models import ClarifierTurn, IdeaRevision
    from app.ventures.idea_page import UNKNOWN

    mark, saved = _idea_mark(vid)
    if mark is None or known in (None, mark) or not known.startswith("v2:"):  # first sight or unchanged: no news
        return None
    page = saved["page"]
    at = _ts(saved["updated_at"])
    founder_seen = False
    if at is not None:
        with db()() as session:
            revision = session.scalar(select(IdeaRevision).where(IdeaRevision.venture_id == vid,
                IdeaRevision.source == "founder", IdeaRevision.created_at >= at - timedelta(minutes=5),
                IdeaRevision.created_at <= at).order_by(IdeaRevision.created_at.desc()).limit(1))
            venture = session.get(Venture, vid)
            conversation = current_conversation()
            messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == vid,
                ChatMessage.workspace_id == venture.workspace_id if venture else False,
                ChatMessage.role.in_(("user", "founder")),
                ChatMessage.conversation_id == conversation.id if conversation else True,
                ChatMessage.author_person_id == subject_person_id(revision.created_by) if revision and revision.created_by else True,
                ChatMessage.created_at >= at - timedelta(minutes=5), ChatMessage.created_at <= at)
                .order_by(ChatMessage.created_at.desc())).all()
            page_answers = [page.get(field) or "" for field in ("summary", "problem", "offer")]
            page_answers += (page.get("customers") or {}).get("current") or []
            page_answers = {" ".join(str(value).casefold().split()).rstrip(".!?")
                            for value in page_answers if value and value != UNKNOWN}
            # shortcut: five-minute chat correlation can combine overlapping edits, use a page revision receipt when edits overlap.
            for message in messages:
                answer, request = split_question_reply(message.body_md)
                text = answer or request
                founder_seen = " ".join(text.casefold().split()).rstrip(".!?") in page_answers
                if revision and not founder_seen:
                    founder_seen = bool(parse(text) or session.scalar(select(ClarifierTurn.id).where(
                        ClarifierTurn.session_id == revision.session_id, ClarifierTurn.venture_id == vid,
                        ClarifierTurn.workspace_id == revision.workspace_id, ClarifierTurn.role == "founder",
                        ClarifierTurn.text == text, ClarifierTurn.created_at >= at - timedelta(minutes=5),
                        ClarifierTurn.created_at <= at).limit(1)))
                if founder_seen:
                    break
    return {**_ev(f"idea:{mark}", _ts(saved["updated_at"]), "Your Idea page changed",
        f"Your Idea page changed: {page.get('summary') or 'the summary was rewritten.'} Worth a quick look, "
        "since it steers research and outreach.",
        [_ask("What changed?", "What changed on my Idea page, and what should we do about it?")]),
        "mark": mark, "founder_seen": founder_seen}


def events(vid: str, now: datetime | None = None, *, idea_mark: str | None = None) -> list[dict]:
    """Current proactive-worthy events from saved state, newest first; risks first among equals."""
    now = as_utc(now or utcnow())
    with db()() as s:
        found = _job_events(s, vid, now) + _gtm_events(s, vid, now)
    idea = _idea_event(vid, idea_mark)
    found += [idea] if idea else []
    fresh = [e for e in found if e["at"] is None or now - e["at"] <= NEWS
             or e["key"].startswith(("stale:", "stuck:")) and now - e["at"] <= OLD_WORK]
    return sorted(fresh, key=lambda e: (not e["risk"], -(e["at"] or now).timestamp()))


# ------------------------------------------------------------------ nudges

def _notice_kind(vid: str, key: str) -> str:
    return "nudge:" + hashlib.sha256(f"{vid}:{key}".encode()).hexdigest()[:24]


def sweep(vid: str, now: datetime | None = None) -> int:
    """Deliver each new event once: a chat nudge (unless the producer already said it) plus a Mac notice."""
    from app import desktop_notices
    from app.chat.milestones import post_milestone, scrub
    from app.db.supymem_models import Notice

    now = as_utc(now or utcnow())
    if quiet(vid, now):
        return 0  # held, not dropped: the events are re-derived on the first sweep after quiet hours
    with db()() as s:
        doc, data = _state(s, vid)
    found = events(vid, now, idea_mark=data.get("idea"))
    mark = data.get("idea")
    if mark is None or not mark.startswith("v2:"):
        mark = _idea_mark(vid)[0]
    sent = 0
    with db()() as s:
        day = _day_start(s, vid, now)
        used = s.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.venture_id == vid,
            ChatMessage.kind == NUDGE, ChatMessage.created_at >= day)) or 0
        delivered = set(s.scalars(select(Notice.kind).where(Notice.kind.in_([_notice_kind(vid, e["key"]) for e in found] or [""]))))
    for ev in found:
        if ev.get("founder_seen"):
            mark = ev["mark"]
            continue
        if _notice_kind(vid, ev["key"]) in delivered:
            continue
        with db()() as s:
            said = ev["said"] and s.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == vid,
                ChatMessage.role == "assistant", ChatMessage.kind.in_(ev["said"]),
                ChatMessage.created_at >= (ev["at"] or now) - timedelta(minutes=5)).limit(1))
        if not said:
            if used >= DAILY_NUDGES:
                continue  # tomorrow, if it is still news
            if not post_milestone(vid, key="nudge:" + ev["key"], kind=NUDGE, body_md=ev["body"], actions=ev["actions"], citations=ev.get("citations")):
                continue
            used += 1
        try:
            desktop_notices.enqueue(title=ev["title"], body=scrub(ev["body"]), venture_id=vid, kind=_notice_kind(vid, ev["key"]))
        except Exception as exc:  # noqa: BLE001 - the chat nudge already landed
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, vid, "proactive.desktop_notice")
        sent += 1
        mark = ev.get("mark", mark)
    if mark != data.get("idea"):
        with db()() as s:
            doc, fresh = _state(s, vid)
            _write(s, vid, doc, {**fresh, "idea": mark})
            s.commit()
    return sent


def sweep_all(now: datetime | None = None, *, founder_id=None) -> int:
    with db()() as s:
        query = select(Venture.id).where(Venture.archived_at.is_(None), Venture.stage != "purged")
        if founder_id:
            from app.core.scheduled import founder_ventures
            query = query.where(Venture.id.in_(founder_ventures(founder_id)))
        ids = list(s.scalars(query))
    total = 0
    for vid in ids:
        try:
            total += sweep(vid, now)
        except Exception as exc:  # noqa: BLE001 - one venture never stops the others
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, vid, "proactive.sweep")
    return total


# ------------------------------------------------------------------ "since you left" brief

def claim_brief(vid: str, now: datetime | None = None) -> bool:
    """Record that you are looking; True once after more than 30 minutes away."""
    from app.db.session import serialize_writes
    from app.chat.conversations import resolve
    from app.chat.store import _partition

    now = as_utc(now or utcnow())
    scope = resolve(vid)
    with db()() as s:
        serialize_writes(s, key="chat-messages:" + scope.id)
        row = s.scalar(select(ChatMessage).where(ChatMessage.venture_id == vid, _partition(scope),
            ChatMessage.role == "assistant")
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
        if row is None:
            return False
        metadata = json.loads(row.citations_json or "[]")
        data = next((item["_proactive"] for item in metadata if "_proactive" in item), {})
        seen = _ts(data.get("seen_at"))
        if seen and now - seen < timedelta(minutes=5):
            return False
        if seen is None:  # first sight since this shipped: the founder's last message is the best record
            last = s.scalar(select(func.max(ChatMessage.created_at)).where(ChatMessage.venture_id == vid,
                _partition(scope), ChatMessage.role.in_(["user", "founder"])))
            seen = as_utc(last) if last else None
        brief_at = _ts(data.get("brief_at"))
        due = seen is not None and now - seen > BRIEF_GAP and (brief_at is None or now - brief_at > BRIEF_GAP)
        data["seen_at"] = now.isoformat()
        if due:
            data["brief_at"] = now.isoformat()
            data["since"] = seen.isoformat()
        row.citations_json = json.dumps([*[item for item in metadata if "_proactive" not in item], {"_proactive": data}])
        s.commit()
        return due


def _brief_parts(vid: str, since: datetime, now: datetime) -> dict:
    from app.db.models import GtmTouch
    from app.ventures.next_step import founder_next_step

    found = events(vid, now)
    changed = [e for e in found if not e["risk"] and e["at"] and e["at"] >= since]
    risk = next((e for e in found if e["risk"]), None)
    with db()() as s:
        sent = s.scalar(select(func.count()).select_from(GtmTouch).where(GtmTouch.venture_id == vid,
            GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at >= since)) or 0
        replies = s.scalar(select(func.count()).select_from(GtmTouch).where(GtmTouch.venture_id == vid,
            GtmTouch.direction == "in", GtmTouch.simulated == 0, GtmTouch.created_at >= since)) or 0
        journey = s.scalar(select(VentureDocument).where(VentureDocument.venture_id == vid,
                                                        VentureDocument.kind == "gtm_journey"))
        journey_data = json.loads(journey.provenance_json or "{}") if journey else {}
        lead_count = len(journey_data.get("reachable_leads") or [])
    lines = [event["title"] for event in changed if lead_count or not event["key"].startswith("leads:")][:3]
    lines += [f"{sent} outreach email{'s' if sent != 1 else ''} sent"] if sent else []
    lines += [f"{replies} repl{'ies' if replies != 1 else 'y'} came in"] if replies else []
    if lead_count == 0 and journey_data.get("state") in {"ready", "needs_input"}:
        from app.chat.customers import empty_search_reply

        if "No reachable leads found yet" not in lines:
            lines.insert(0, "No reachable leads found yet")
        reply = empty_search_reply(journey_data)
        return {"changed": lines, "next": reply["body_md"], "next_detail": "",
                "actions": reply["actions"], "risk": risk, "reachable_leads": lead_count}
    step = founder_next_step(vid)
    action = ({"label": step.get("cta_label") or "Do it", "action": step["chat_action"], "payload": step.get("payload") or {},
               "style": "primary"} if step.get("chat_action") else
              {"label": step.get("cta_label") or "Open", "href": step["href"], "style": "primary"} if step.get("href") else None)
    return {"changed": lines, "next": step.get("title") or "", "next_detail": step.get("detail") or "",
            "actions": [action] if action else [], "risk": risk, "reachable_leads": lead_count}


def fallback_text(parts: dict) -> str:
    changed = "; ".join(parts["changed"]) if parts["changed"] else "nothing new finished in the background"
    text = f"Welcome back. Since you left: {changed}."
    if parts["next"]:
        text += f" The most important next step: {parts['next'].rstrip('.')}."
    if parts["risk"]:
        text += f" One thing needs attention: {parts['risk']['body']}"
    return text


async def _write_brief(vid: str, parts: dict, brief_text: str) -> str:
    from app.credits import metered

    facts = json.dumps({"changed_since_last_visit": parts["changed"], "next_step": parts["next"],
                        "next_step_detail": parts["next_detail"],
                        "reachable_leads": parts["reachable_leads"],
                        "risk": parts["risk"]["body"] if parts["risk"] else None}, ensure_ascii=False)
    response = await metered.llm([
        {"role": "system", "content": "You are Charlie, the founder's chief of staff. Write ONE short welcome-back "
            "message (at most 4 sentences, plain prose, no lists, no em dashes): what changed since they left, the single "
            "most important next step, and the risk only if one is given. Use ONLY the facts given; never invent numbers, "
            "people or events. Do not write 'let me know'. Do not mention buttons."},
        {"role": "user", "content": f"Facts:\n{facts}\n\nSaved venture state:\n{brief_text[:6000]}"},
    ], purpose="venture.chat.assistant", venture_id=vid, tier="flash", max_tokens=2000)
    choices = getattr(response, "choices", None) or []
    return str(getattr(getattr(choices[0], "message", None), "content", "") or "").strip() if choices else ""


def post_brief(vid: str, since: datetime | None = None, now: datetime | None = None) -> bool:
    import asyncio

    from app.chat.assistant import normalize_assistant_dashes
    from app.chat.milestones import post_milestone
    from app.tenancy import bind
    from app.ventures.state_brief import get_venture_state_brief, render_brief_for_llm

    now = as_utc(now or utcnow())
    with db()() as s:
        venture = s.get(Venture, vid)
        _, data = _state(s, vid)
    if venture is None:
        return False
    since = since or _ts(data.get("since")) or now - BRIEF_GAP
    with bind(workspace_id=venture.workspace_id, venture_id=vid, actor="system"):
        parts = _brief_parts(vid, since, now)
        try:
            text = ("" if parts["reachable_leads"] == 0 else
                    asyncio.run(_write_brief(vid, parts, render_brief_for_llm(get_venture_state_brief(vid)))))
        except Exception as exc:  # noqa: BLE001 - credits, provider or offline: the deterministic brief still lands
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, vid, "proactive.brief")
            text = ""
    actions = (parts["risk"]["actions"] if parts["risk"] else []) + parts["actions"]
    return post_milestone(vid, key=f"since_left:{now.isoformat(timespec='minutes')}", kind=BRIEF,
                          body_md=normalize_assistant_dashes(text) or fallback_text(parts), actions=actions[:3])


def _spawn(fn, *args, **kwargs) -> None:
    context = contextvars.copy_context()  # keeps the open conversation, so the brief lands where the founder is
    threading.Thread(target=lambda: context.run(fn, *args, **kwargs), daemon=True, name="since-you-left").start()


def on_chat_open(vid: str) -> None:
    """GET /chat hook: cheap check inline; the brief itself is written off the request thread. Never raises."""
    try:
        claim_brief(vid)
    except Exception as exc:  # noqa: BLE001 - opening the chat must never fail on a check-in
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, vid, "proactive.chat_open")
