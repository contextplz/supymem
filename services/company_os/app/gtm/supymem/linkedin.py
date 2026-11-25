"""LinkedIn friendship sequence. Human-copy queue only.

Never drives linkedin.com in a browser. Unipile send is structurally
impossible (OT-04) — tick_linkedin never calls _unipile_send.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db.models import GtmContact, GtmLinkedInQueue, GtmTouch
from app.gtm.supymem import factory, learning
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

STEPS = ("connect", "chat1", "chat2", "chat3", "chat4", "offer")
OFFSETS_DAYS = {"connect": 0, "chat1": 2, "chat2": 5, "chat3": 10, "chat4": 16, "offer": 24}
DAILY_HARD_CAP = 25


def configured() -> bool:
    # Prefer full readiness (key + account); keep key-only truthy for legacy callers.
    try:
        from app.gtm.supymem import unipile as unipile_client

        if unipile_client.configured():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(os.environ.get("UNIPILE_API_KEY", "").strip())


def _next_step(step: str) -> str | None:
    try:
        i = STEPS.index(step)
    except ValueError:
        return None
    if i + 1 >= len(STEPS):
        return None
    return STEPS[i + 1]


def _template(
    step: str,
    first: str,
    hook: str,
    thesis_strategy: dict[str, Any] | None = None,
) -> str:
    first = first or "there"
    hook = hook or "what you're building"
    if step == "connect":
        return (
            f"Hey {first}, saw {hook}. Would be good to be in each other's world. No pitch."
        )
    if step == "chat1":
        return f"Thanks for connecting, {first}. Curious what the next 90 days look like on {hook}."
    if step == "chat2":
        return f"Still thinking about {hook}. Anything you're stuck on that a second pair of eyes would help?"
    if step == "chat3":
        angle = str((thesis_strategy or {}).get("message_angle") or "").strip()
        return (
            f"Sharing this because it reminded me of you: {hook}. "
            f"{angle or 'Happy to compare notes on what is working.'}"
        )
    if step == "chat4":
        return f"No agenda, {first}. If useful I can intro you to someone who's been through the same stretch."
    offer = str((thesis_strategy or {}).get("offer") or "").strip()
    return f"{first}, we've been talking a bit. {offer or 'I can send a quiet walkthrough if useful.'} No pressure."


def enroll_linkedin(workspace_id: str, contact_ids: list[str] | None = None) -> dict[str, Any]:
    from app.gtm.localization.experiments import execution_context

    execution = execution_context(workspace_id)
    if not execution["allowed"]:
        return {
            "enrolled": [],
            "skipped": [],
            "provider": "blocked",
            "blocker": execution["reason"],
        }
    thesis_strategy = (
        (execution.get("thesis") or {}).get("strategy") or {}
        if execution.get("mode") == "thesis"
        else {}
    )
    enrolled: list[str] = []
    skipped: list[str] = []
    now = utcnow()
    with db()() as session:
        q = select(GtmContact).where(
            GtmContact.workspace_id == workspace_id,
            GtmContact.do_not_contact == 0,
            GtmContact.linkedin_url.isnot(None),
        )
        if contact_ids:
            q = q.where(GtmContact.id.in_(contact_ids))
        contacts = list(session.scalars(q.order_by(GtmContact.created_at.desc()).limit(80)))
        for contact in contacts:
            if contact.replied_at or contact.bounced_at:
                skipped.append(contact.id)
                continue
            existing = session.scalar(
                select(GtmLinkedInQueue).where(
                    GtmLinkedInQueue.contact_id == contact.id,
                    GtmLinkedInQueue.step == "connect",
                )
            )
            if existing:
                skipped.append(contact.id)
                continue
            enrich = {}
            try:
                from app.gtm.supymem import draft as _draft

                enrich = _draft.parse_enrichment(contact.enrichment_json)
                li_bodies = _draft.linkedin_bodies_from_opengtm(
                    _draft.opengtm_messages_of(enrich)
                )
            except Exception:  # noqa: BLE001
                li_bodies = {}
            body = li_bodies.get("connect") or _template(
                "connect",
                contact.first_name or "there",
                contact.dossier_hook or "",
                thesis_strategy,
            )
            session.add(
                GtmLinkedInQueue(
                    id=new_id("gtmli"),
                    workspace_id=workspace_id,
                    contact_id=contact.id,
                    step="connect",
                    status="queued",
                    scheduled_for=now,
                    body=body,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            contact.linkedin_stage = "queued_connect"
            enrolled.append(contact.id)
        session.commit()
    return {"enrolled": enrolled, "skipped": skipped, "provider": "human_queue"}


def _unipile_send(linkedin_url: str, body: str) -> dict[str, Any]:
    from app.gtm.relationships.compliance import assert_unipile_outreach_forbidden

    assert_unipile_outreach_forbidden()
    raise RuntimeError("unipile_outreach_forbidden")


def tick_linkedin(workspace_id: str, *, limit: int = 3) -> dict[str, Any]:
    from app.gtm.localization.experiments import execution_context

    execution = execution_context(workspace_id)
    if not execution["allowed"]:
        return {
            "processed": [],
            "skipped": ["gtm_thesis_approval_required"],
            "blocker": execution["reason"],
        }
    thesis_strategy = (
        (execution.get("thesis") or {}).get("strategy") or {}
        if execution.get("mode") == "thesis"
        else {}
    )

    # Live Unipile send is structurally forbidden (OT-04), even when
    # CHANNELS_DRY_RUN is false. Queue items are human-copy / simulated only.
    simulated = True
    state = factory.get_or_create_state(workspace_id)
    day_cap = min(DAILY_HARD_CAP, int(state["linkedin_daily_cap"]))
    processed: list[dict[str, Any]] = []
    sent_today = 0
    with db()() as session:
        today = utcnow().date()
        sent_today = sum(
            1
            for r in session.scalars(
                select(GtmLinkedInQueue).where(
                    GtmLinkedInQueue.workspace_id == workspace_id,
                    GtmLinkedInQueue.status == "sent",
                )
            )
            if r.updated_at and r.updated_at.date() == today
        )
    for _ in range(limit):
        if sent_today >= day_cap:
            break
        with db()() as session:
            job = session.scalar(
                select(GtmLinkedInQueue)
                .where(
                    GtmLinkedInQueue.workspace_id == workspace_id,
                    GtmLinkedInQueue.status == "queued",
                    GtmLinkedInQueue.scheduled_for <= utcnow(),
                )
                .order_by(GtmLinkedInQueue.scheduled_for.asc())
                .limit(1)
            )
            if job is None:
                break
            contact = session.get(GtmContact, job.contact_id)
            if contact is None or contact.do_not_contact or contact.replied_at:
                job.status = "skipped"
                session.commit()
                continue
            body = job.body or _template(
                job.step,
                contact.first_name or "there",
                contact.dossier_hook or "",
                thesis_strategy,
            )
            provider_id = None
            err = None
            if simulated:
                provider_id = f"sim_li_{job.id}"
            else:
                # Unreachable: simulated is unconditionally True (OT-04).
                processed.append(
                    {
                        "skipped": True,
                        "error": "unipile_outreach_forbidden",
                        "contact_id": contact.id,
                    }
                )
                continue
            now = utcnow()
            job.status = "sent"
            job.body = body
            job.provider_message_id = provider_id
            job.updated_at = now
            contact.last_linkedin_at = now
            contact.times_linkedin = int(contact.times_linkedin or 0) + 1
            contact.linkedin_stage = job.step
            session.add(
                GtmTouch(
                    id=new_id("gtmtch"),
                    contact_id=contact.id,
                    mailbox_id=None,
                    direction="out",
                    channel="linkedin",
                    message_id=provider_id,
                    subject=job.step,
                    body_text=body,
                    sent_at=now,
                    simulated=1 if simulated else 0,
                    created_at=now,
                )
            )
            nxt = _next_step(job.step)
            if nxt:
                nxt_body = _template(
                    nxt,
                    contact.first_name or "there",
                    contact.dossier_hook or "",
                    thesis_strategy,
                )
                try:
                    from app.gtm.supymem import draft as _draft

                    li_map = _draft.linkedin_bodies_from_opengtm(
                        _draft.opengtm_messages_of(
                            _draft.parse_enrichment(contact.enrichment_json)
                        )
                    )
                    if li_map.get(nxt):
                        nxt_body = li_map[nxt]
                except Exception:  # noqa: BLE001
                    pass
                session.add(
                    GtmLinkedInQueue(
                        id=new_id("gtmli"),
                        workspace_id=workspace_id,
                        contact_id=contact.id,
                        step=nxt,
                        status="queued",
                        scheduled_for=now + timedelta(days=OFFSETS_DAYS.get(nxt, 3)),
                        body=nxt_body,
                        attempts=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
            sent_today += 1
            processed.append(
                {
                    "contact_id": contact.id,
                    "step": job.step,
                    "simulated": simulated,
                    "provider_message_id": provider_id,
                }
            )
            learning.record(
                workspace_id,
                "linkedin_sent",
                contact_id=contact.id,
                channel="linkedin",
                payload={"step": job.step, "simulated": simulated},
            )
    return {
        "processed": processed,
        "dry_run": simulated,
        "daily_cap": day_cap,
        "provider": "human_or_simulated",
    }


def _dry_need() -> bool:
    from app.gtm.supymem.pipeline import _dry_run

    return _dry_run()
