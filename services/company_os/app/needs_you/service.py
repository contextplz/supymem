"""request_human: one deduped, batched, escalating way to ask the founder for something only they can do.

Delivery on a new request: a chat card (always), a desktop notice and the God mode list (``list_open``).
The first email rides the chat card (``post_milestone`` -> ``on_milestone``). Until the founder resolves it,
``remind_due`` sends a notice after two hours, then backs off from NEEDS_YOU_REMIND_MINUTES (240).
Reminders respect founder quiet hours, leave the unanswered chat card alone, and email at most every four hours.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Venture, VentureDocument
from app.db.supymem_models import NeedsYouRequest as R
from app.state import db
from core.clock import utcnow
from core.ids import new_id

from app.needs_you.templates import blocker_message, explain

log = logging.getLogger("app.needs_you")
BATCH_WINDOW = timedelta(minutes=10)      # one notification per venture per window; the rest ride a digest
FIRST_REMINDER = timedelta(hours=2)
RESUME: dict[str, Callable[[dict[str, Any]], None]] = {}  # blocking_ref kind -> callback(row dict)


def on_resume(kind: str):
    def deco(fn):
        RESUME[kind] = fn
        return fn
    return deco


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _quiet(now: datetime) -> bool:
    # ponytail: UTC hours from env, per-founder timezone when a profile timezone exists.
    raw = os.environ.get("NEEDS_YOU_QUIET_HOURS", "22-8")
    try:
        a, b = (int(x) for x in raw.split("-"))
    except ValueError:
        return False
    return a != b and ((a <= now.hour or now.hour < b) if a > b else a <= now.hour < b)


def _minutes(name: str, default: float) -> timedelta | None:
    try:
        m = float(os.environ.get(name, default))
    except ValueError:
        m = default
    return timedelta(minutes=m) if m > 0 else None  # 0 switches that channel off


def remind_every() -> timedelta | None:
    interval = _minutes("NEEDS_YOU_REMIND_MINUTES", 240)
    return max(interval, timedelta(hours=2)) if interval else None


def _prefs_id(venture_id: str) -> str:
    return f"needs-you-prefs:{venture_id}"


def reminders_paused(venture_id: str) -> bool:
    with db()() as s:
        doc = s.get(VentureDocument, _prefs_id(venture_id))
        return bool(doc and doc.body_md == "paused")


def set_reminders_paused(venture_id: str, paused: bool) -> bool:
    """Founder's 'pause reminders' (the first notice of each new blocker is still delivered)."""
    now = utcnow()
    with db()() as s:
        doc = s.get(VentureDocument, _prefs_id(venture_id))
        if doc is None:
            doc = VentureDocument(id=_prefs_id(venture_id), venture_id=venture_id, kind="needs_you_prefs",
                                  title="Needs-you reminder preference", created_at=now, updated_at=now)
            s.add(doc)
        doc.body_md, doc.updated_at = ("paused" if paused else "on"), now
        s.commit()
    return paused


def _requirements_copy(text: str) -> str:
    # Old saved requests asserted parallel execution without a receipt. Keep
    # their actual requirements and remove that unsupported activity sentence.
    return re.sub(r"\s*I (?:am carrying|carry) on with everything else(?: meanwhile)?[.!]?", "", text or "", flags=re.I).strip()


def _row(r: R) -> dict[str, Any]:
    return {"id": r.id, "venture_id": r.venture_id, "reason_code": r.reason_code, "title": r.title,
            "what_we_need": _requirements_copy(r.what_we_need), "how_to_resume": _requirements_copy(r.how_to_resume), "blocking_ref": r.blocking_ref,
            "severity": r.severity, "status": r.status, "created_at": r.created_at.isoformat(),
            "deadline": r.deadline.isoformat() if r.deadline else None}


def _emit(name: str, r: R, **props: Any) -> None:
    try:
        from app.analytics.events import emit

        emit(name, workspace_id=r.workspace_id, venture_id=r.venture_id,
             props={"reason_code": r.reason_code, "severity": r.severity, **props})
    except Exception:  # noqa: BLE001
        log.warning("needs_you_emit_failed", exc_info=True)


def _error(r: R, stage: str) -> None:
    try:
        from app.analytics.error_events import log_error_event

        log_error_event("needs_you", feature="needs_you", code=f"{stage}_failed", venture_id=r.venture_id)
    except Exception:  # noqa: BLE001
        pass


def request_human(venture_id: str, *, reason_code: str, title: str, what_we_need: str, how_to_resume: str,
                  blocking_ref: str, severity: str = "normal", deadline: datetime | None = None,
                  chat_card: bool = True, actions: list[dict[str, Any]] | None = None) -> str | None:
    """Create (or return the open duplicate of) a request and deliver it. Never raises.

    ``chat_card=False`` when the caller already posted its own chat message for this blocker."""
    try:
        now, open_key = utcnow(), f"{venture_id}|{blocking_ref}"
        what_we_need, how_to_resume = explain(reason_code, what_we_need, how_to_resume)  # never empty, always numbered
        with db()() as s:
            ven = s.get(Venture, venture_id)
            if ven is None:
                return None
            dup = s.scalar(select(R).where(R.open_key == open_key))
            if dup is not None:
                return dup.id
            r = R(id=new_id("nyr"), workspace_id=ven.workspace_id, venture_id=venture_id, reason_code=reason_code[:60],
                  title=title[:120], what_we_need=what_we_need[:600], how_to_resume=how_to_resume[:900],
                  blocking_ref=blocking_ref[:200], severity="blocking" if severity == "blocking" else "normal",
                  status="open", open_key=open_key, deadline=deadline, created_at=now, reminders_sent=0)
            s.add(r)
            try:
                s.commit()
            except IntegrityError:  # lost a race with a concurrent identical request
                s.rollback()
                dup = s.scalar(select(R).where(R.open_key == open_key))
                return dup.id if dup else None
            s.refresh(r)
            rid, row = r.id, _row(r)
            _emit("needs_you.created", r)
        _deliver(row, chat_card, actions)
        return rid
    except Exception:  # noqa: BLE001 - asking for help must not break the step that needed it
        log.warning("needs_you_request_failed", exc_info=True)
        return None


def _card(row: dict[str, Any]) -> str:
    return f"**Supymem needs you: {row['title']}**\n\n{blocker_message([row])}"


def _mac_review_order(row: dict[str, Any]) -> str | None:
    """None is ordinary; an empty ID is an unconfirmed native request."""
    from app.autopilot.lifecycle import MAC_NATIVE_APPROVAL_REQUIRED
    if row.get("reason_code") != MAC_NATIVE_APPROVAL_REQUIRED:
        return None
    prefix = "godmode:human:mac."
    ref = row.get("blocking_ref") or ""
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return ""
    order_id = ref[len(prefix):]
    return order_id if order_id and order_id == order_id.strip() else ""


def _native_review_move(row: dict[str, Any]):
    from fastapi import HTTPException
    from app.autopilot import lifecycle
    from app.autopilot.authority import founder_scope
    order_id = _mac_review_order(row)
    if not order_id:
        return None
    try:
        with db()() as session:
            saved = session.get(R, row["id"])
            if (saved is None or saved.status != "open" or (saved.venture_id, saved.reason_code, saved.blocking_ref)
                    != (row["venture_id"], row["reason_code"], row["blocking_ref"])
                    or saved.open_key != f"{saved.venture_id}|{saved.blocking_ref}"):
                return None
            workspace_id = saved.workspace_id
        with founder_scope(row["venture_id"], autonomous=True) as scope:
            if scope.workspace_id != workspace_id:
                return None
            return lifecycle.native_mac_review_move(row["venture_id"], order_id)
    except (ValueError, PermissionError, HTTPException):
        return None


def _deliver(row: dict[str, Any], chat_card: bool = True, extra: list[dict[str, Any]] | None = None) -> None:
    from app.chat.milestones import post_milestone

    vid, rid = row["venture_id"], row["id"]
    try:
        link = ("/connections?venture=" + vid) if row["reason_code"] == "gmail_connect" else f"/v/{vid}/now"
        if row["reason_code"] == "gtm_bounce_pause":
            extra = [{"label": "Resume sending", "action": "gtm.resume_sending",
                "payload": {"reviewed_bounced_addresses": True}, "style": "primary"},
                {"label": "Review Find customers", "href": f"/gtm?venture={vid}"}]
        if _mac_review_order(row) is not None:
            review = _native_review_move(row)
            if review is None:
                return
            actions = [{"label": "Review on Mac", "href": review.human_href, "style": "primary"}]
        else:
            actions = [*(extra or []), {"label": "Continue", "action": "needs_you.resolve", "payload": {"request_id": rid},
                "style": "primary"}, {"label": "Open", "href": link, "style": "ghost"}]
        chat_card and post_milestone(vid, key=f"needs_you:{rid}", kind="needs_you",
                       email_reason=row["reason_code"], body_md=_card(row),
                       actions=actions)
    except Exception:  # noqa: BLE001
        log.warning("needs_you_chat_failed", exc_info=True)
    now = utcnow()
    with db()() as s:
        r = s.get(R, rid)
        recent = s.scalar(select(R.id).where(R.venture_id == vid, R.id != rid, R.notified_at.is_not(None),
                                             R.notified_at > now - BATCH_WINDOW))
        if recent or (_quiet(now) and row["severity"] != "blocking"):
            return  # batched into a later digest (remind_due) instead of another ping
        _notify(vid, [r], now)
        s.commit()


def _notify(vid: str, rows: list[R], now: datetime, *, reminder: bool = False) -> bool:
    """One desktop notification for due rows, with interval-limited reminder email."""
    from app import desktop_notices

    try:
        views = [_row(r) for r in rows]
        title = "Supymem has a blocker" if len(rows) == 1 else f"Supymem has {len(rows)} blockers"
        desktop_notices.enqueue(title=("Reminder: " if reminder else "") + title,
                                body=blocker_message(views, flat=True)[:400], venture_id=vid, kind="needs_you")
    except Exception:  # noqa: BLE001
        log.warning("needs_you_notice_failed", exc_info=True)
        _error(rows[0], "notify")
        return False
    for r in rows:
        r.notified_at = now
    if reminder:
        _remind_email(vid, rows, now)
    return True


def _remind_email(vid: str, rows: list[R], now: datetime) -> None:
    email_every = _minutes("NEEDS_YOU_EMAIL_REMIND_MINUTES", 240)
    for r in rows:
        if _mac_review_order(_row(r)) is not None and _native_review_move(_row(r)) is None:
            continue
        age = now - _aware(r.created_at)
        if email_every and age >= email_every:
            try:
                from app.email.triggers import on_needs_you

                # send_email is idempotent per ref, so one email per elapsed email interval, never one per tick.
                on_needs_you(vid, r.title, blocker_message([_row(r)], flat=True), f"{r.id}:remind:{int(age / email_every)}")
            except Exception:  # noqa: BLE001
                log.warning("needs_you_email_failed", exc_info=True)


def list_open(venture_id: str) -> list[dict[str, Any]]:
    with db()() as s:
        return [_row(r) for r in s.scalars(select(R).where(R.venture_id == venture_id, R.status == "open")
                                          .order_by(R.created_at)).all()]


def resolve(request_id: str, *, venture_id: str | None = None, actor: str | None = None) -> dict[str, Any] | None:
    """Mark resolved and run the resume callback for the blocking_ref kind. None if unknown or not open."""
    now = utcnow()
    with db()() as s:
        snapshot = s.get(R, request_id)
        if snapshot is None or snapshot.status != "open" or (venture_id and snapshot.venture_id != venture_id):
            return None
        if (order_id := _mac_review_order(_row(snapshot))) is not None:
            from app.autopilot.lifecycle import native_mac_review_retired
            if (not order_id or snapshot.open_key != f"{snapshot.venture_id}|{snapshot.blocking_ref}"
                    or not native_mac_review_retired(snapshot.venture_id, order_id)):
                return None
        device_resume = None
        if snapshot.blocking_ref.startswith("device:"):
            from app.build import device_resume as bridge
            device_resume = bridge.lock_for_resolution(s, snapshot, actor)
        r = s.scalar(select(R).where(R.id == request_id).with_for_update().execution_options(populate_existing=True))
        if r is None or r.status != "open" or (venture_id and r.venture_id != venture_id):
            return None
        r.status, r.resolved_at, r.resolved_by, r.open_key = "resolved", now, (actor or "founder")[:80], None
        from app.chat.milestones import _scoped, scrub
        from app.db.models import ChatMessage
        from app.org.characters import stamp_speaker
        from app.org.manager import manager_speaker

        card = s.scalar(select(ChatMessage).where(ChatMessage.venture_id == r.venture_id,
                        ChatMessage.idempotency_key == _scoped(r.venture_id, f"needs_you:{r.id}")))
        if card is not None:
            card.kind = "needs_you_resolved"
            card.body_md = stamp_speaker(scrub(f"**Resolved: {r.title}**\n\nThis blocker is cleared. Its reminders have stopped."), manager_speaker(r.venture_id))
            card.actions_json = None
        _emit("needs_you.resolved", r, time_to_resolve_s=int((now - _aware(r.created_at)).total_seconds()))
        row = _row(r)
        if device_resume is not None:
            from app.build import device_resume as bridge
            row["resume_state"] = bridge.queue(s, *device_resume, r, actor)
        if row["blocking_ref"].startswith("godmode:"):
            # Enlist the durable wakeup in the same transaction as resolution. A crash
            # after commit cannot lose the action between acknowledgement and callback.
            from app.core.jobs import enqueue
            enqueue(s, "autopilot.tick", {}, workspace_id=r.workspace_id, venture_id=r.venture_id,
                    dedupe_key=f"needs_you_resume:{r.id}")
        s.commit()
    cb = None if device_resume is not None or row["blocking_ref"].startswith("godmode:") else RESUME.get(row["blocking_ref"].split(":", 1)[0])
    if cb:
        try:
            cb(row)
        except Exception:  # noqa: BLE001 - the founder's action is saved even if the resume hook fails
            log.warning("needs_you_resume_failed", exc_info=True)
    return row


def remind_due(now: datetime | None = None, *, founder_id=None) -> dict[str, int]:
    """Worker tick (5 min): expire past-deadline rows; one batched notice per venture, repeated until resolved."""
    now = now or utcnow()
    out = {"expired": 0, "digests": 0, "reminders": 0}
    with db()() as s:
        query = select(R).where(R.status == "open")
        if founder_id:
            from app.core.scheduled import founder_ventures
            query = query.where(R.venture_id.in_(founder_ventures(founder_id)))
        rows = s.scalars(query).all()
        by_v: dict[str, list[R]] = {}
        for r in rows:
            if (order_id := _mac_review_order(_row(r))) is not None:
                if not order_id or r.open_key != f"{r.venture_id}|{r.blocking_ref}":
                    continue
                from app.autopilot.lifecycle import native_mac_review_retired
                if native_mac_review_retired(r.venture_id, order_id):
                    resolve(r.id, venture_id=r.venture_id, actor="system")
                    continue
                if _native_review_move(_row(r)) is None:
                    continue
            if r.deadline and _aware(r.deadline) <= now:
                r.status, r.open_key = "expired", None
                _emit("needs_you.expired", r, age_s=int((now - _aware(r.created_at)).total_seconds()))
                out["expired"] += 1
            else:
                by_v.setdefault(r.venture_id, []).append(r)
        every = remind_every()
        from app.chat.proactive import _local
        for vid, rs in by_v.items():
            eligible = [] if _quiet(_local(s, vid, now)) else rs
            recent = any(r.notified_at and _aware(r.notified_at) > now - BATCH_WINDOW for r in rs)
            paused = reminders_paused(vid)

            def due(r: R) -> bool:
                if r.notified_at is None:
                    return not recent
                if not every or paused:
                    return False
                gap = FIRST_REMINDER if r.reminders_sent == 0 else min(every * 2 ** min(r.reminders_sent - 1, 10), timedelta(days=1))
                return now - _aware(r.notified_at) >= gap

            due_rows = [r for r in eligible if due(r)]
            if not due_rows:
                continue
            seen = {r.id for r in due_rows if r.notified_at is not None}
            if _notify(vid, due_rows, now, reminder=bool(seen)):
                out["reminders" if seen else "digests"] += 1
                for r in due_rows:
                    r.reminders_sent += r.id in seen
        s.commit()
    return out


@on_resume("godmode")
def _resume_godmode(row: dict[str, Any]) -> None:
    """The next 5-minute God mode tick re-evaluates the venture; queue one now so the founder sees it move."""
    from app.core.jobs import enqueue

    with db()() as s:
        enqueue(s, "autopilot.tick", {}, workspace_id=None, venture_id=row["venture_id"],
                dedupe_key=f"needs_you_resume:{row['id']}")
        s.commit()


def resolve_ref(venture_id: str, blocking_ref: str, actor: str = "system") -> bool:
    """Resolve the open request for a blocker that cleared by itself (e.g. the hosted URL verified)."""
    with db()() as s:
        rid = s.scalar(select(R.id).where(R.open_key == f"{venture_id}|{blocking_ref}"))
    return bool(rid and resolve(rid, venture_id=venture_id, actor=actor))
