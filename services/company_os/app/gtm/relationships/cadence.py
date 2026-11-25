"""OT-44 cadence caps (5 connect / 2 DM / 5 email per week). Single enforcement module.

OT-34 (hosted kill switch) should import from here rather than duplicating copy caps.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.clock import utcnow
from sqlalchemy import func, select

from app.db.models import GtmCompany, GtmContact, GtmEmailSendQueue, GtmEngineState, GtmOutreachSettings, GtmTouch, VentureDocument
from app.state import db

PLATFORM_MAX = {"connect": 5, "dm": 2, "email": 5}


def company_history(session, workspace_id: str, company_ids) -> dict:
    from app.gtm.journey import _json
    touches, uncertain = {}, {}
    for company_id, cid, sent_at in session.execute(select(GtmContact.company_id, GtmTouch.contact_id, GtmTouch.sent_at)
            .join(GtmContact, GtmContact.id == GtmTouch.contact_id).where(GtmContact.workspace_id == workspace_id,
                GtmContact.company_id.in_(company_ids), GtmTouch.workspace_id == workspace_id,
                GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))):
        touches.setdefault(company_id, []).append((cid, sent_at))
    peers = {}
    for cid, company_id, sending in session.execute(select(GtmContact.id, GtmContact.company_id, GtmEmailSendQueue.id)
            .outerjoin(GtmEmailSendQueue, (GtmEmailSendQueue.contact_id == GtmContact.id) & (GtmEmailSendQueue.status == "sending"))
            .where(GtmContact.workspace_id == workspace_id, GtmContact.company_id.in_(company_ids))):
        peers[cid] = company_id
        if sending is not None:
            uncertain.setdefault(company_id, set()).add(cid)
    if peers:
        from app.db.models import Venture
        # ponytail: scan workspace outreach drafts for uncertain siblings; index recipient/company if the corpus grows.
        for raw in session.scalars(select(VentureDocument.provenance_json).join(Venture, Venture.id == VentureDocument.venture_id)
                .where(Venture.workspace_id == workspace_id, VentureDocument.kind == "outreach_draft")):
            document = _json(raw)
            cid = document.get("contact_id")
            if cid in peers and document.get("state") in {"delivery_pending", "delivery_unconfirmed"}:
                uncertain.setdefault(peers[cid], set()).add(cid)
    return {"touches": touches, "uncertain": uncertain}


def company_block(session, workspace_id: str, contact, *, reply: bool = False, history: dict | None = None) -> str | None:
    # logic adapted from ethanplusai/harvey (MIT)
    now = utcnow()
    from app.gtm.journey import _json, _signal_date
    data = _json(contact.enrichment_json)
    due = _signal_date(data.get("follow_up_not_before"))
    if due and now < due:
        return "follow_up_not_due"
    if reply or not contact.company_id:
        return None
    from app.gtm.enrich import _FREE_MAIL
    from app.gtm.relationships.store import normalize_domain
    company = session.get(GtmCompany, contact.company_id)
    if company and (not company.domain or normalize_domain(company.domain) in _FREE_MAIL) and normalize_domain(company.canonical_name) in _FREE_MAIL:
        return None
    history = history if history is not None else company_history(session, workspace_id, [contact.company_id])
    touched = history["touches"].get(contact.company_id, [])
    if history["uncertain"].get(contact.company_id, set()) - {contact.id}:
        return "company_delivery_unconfirmed"
    contacted = {cid for cid, _ in touched}
    if contact.id not in contacted and len(contacted) >= 3:
        return "company_contact_limit"
    for cid, sent_at in touched:
        if cid == contact.id:
            continue
        resume = sent_at.replace(tzinfo=now.tzinfo)
        days = 0
        while days < 5:
            resume += timedelta(days=1)
            days += resume.weekday() < 5
        if now < resume:
            return "company_contact_hold"
    return None


class CadenceError(ValueError):
    def __init__(self, code: str, status: int = 429, payload: dict[str, Any] | None = None) -> None:
        self.code = code
        self.status = status
        self.payload = payload or {}
        super().__init__(code)


def _week_start(now):
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _channel_for_copy(channel: str | None, ask_rung: str | None) -> str:
    ch = (channel or "").lower()
    if "email" in ch:
        return "email"
    if "dm" in ch:
        return "dm"
    if ask_rung == "question":
        return "dm"
    return "connect"


def remaining_caps(workspace_id: str) -> dict[str, Any]:
    now = utcnow()
    start = _week_start(now)
    with db()() as session:
        settings = session.get(GtmOutreachSettings, workspace_id)
        engine = session.get(GtmEngineState, workspace_id)
        max_connect = min(
            PLATFORM_MAX["connect"],
            settings.max_connect_per_week if settings else PLATFORM_MAX["connect"],
        )
        max_dm = min(
            PLATFORM_MAX["dm"],
            settings.max_dm_per_week if settings else PLATFORM_MAX["dm"],
        )
        max_email = min(
            PLATFORM_MAX["email"],
            settings.max_email_per_week if settings else PLATFORM_MAX["email"],
        )
        rows = list(
            session.execute(
                select(GtmTouch.channel, GtmTouch.ask_rung, func.count())
                .where(
                    GtmTouch.workspace_id == workspace_id,
                    GtmTouch.outcome == "copied",
                    GtmTouch.copied_at.is_not(None),
                    GtmTouch.copied_at >= start,
                )
                .group_by(GtmTouch.channel, GtmTouch.ask_rung)
            )
        )
        used = {"connect": 0, "dm": 0, "email": 0}
        for channel, ask_rung, n in rows:
            used[_channel_for_copy(channel, ask_rung)] += int(n)
        remaining = {
            "connect": max(0, max_connect - used["connect"]),
            "dm": max(0, max_dm - used["dm"]),
            "email": max(0, max_email - used["email"]),
        }
        return {
            "outreach_week": engine.outreach_week if engine else 1,
            "week_start": start.isoformat(),
            "reset_at": (start + timedelta(days=7)).isoformat(),
            "max": {"connect": max_connect, "dm": max_dm, "email": max_email},
            "used": used,
            "remaining": remaining,
        }


def assert_copy_allowed(workspace_id: str, *, channel: str | None, ask_rung: str | None) -> None:
    bucket = _channel_for_copy(channel, ask_rung)
    caps = remaining_caps(workspace_id)
    with db()() as session:
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is not None and settings.send_enabled == 0:
            raise CadenceError("send_paused", 403, {"remaining": 0, "reset_at": caps["reset_at"]})
    if caps["remaining"].get(bucket, 0) <= 0:
        raise CadenceError(
            "cadence_cap",
            429,
            {"remaining": 0, "reset_at": caps["reset_at"], "bucket": bucket},
        )
