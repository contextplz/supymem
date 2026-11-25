"""Outreach board, first-5 queue, sequence arming, Today header (docs 116–117)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select

from app.approvals.service import request_approval
from app.db.models import (
    GtmCompany,
    GtmContact,
    GtmEnrollment,
    GtmSequence,
    GtmSignal,
    GtmTouch,
)
from app.gtm.relationships.dossier import research_contact_from_latest_signal
from app.gtm.relationships.state_machine import transition
from app.gtm.relationships.tone import lint_draft
from app.state import db
from core.clock import utcnow
from core.ids import new_id

DEFAULT_STEPS = [
    {"name": "congrats", "delay_days": 0},
    {"name": "nudge", "delay_days": 6},
    {"name": "value", "delay_days": 14},
    {"name": "offer", "delay_days": 21, "requires_approval": True},
]


def list_prospects(workspace_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(GtmContact)
            .where(GtmContact.workspace_id == workspace_id)
            .order_by(GtmContact.created_at.desc())
            .limit(limit)
        ).all()
        out: list[dict[str, Any]] = []
        for c in rows:
            company = session.get(GtmCompany, c.company_id) if c.company_id else None
            out.append(
                {
                    "id": c.id,
                    "email": c.email or "",
                    "first_name": c.first_name,
                    "company": company.canonical_name if company else None,
                    "status": c.relationship_stage,
                    "created_at": c.created_at.isoformat() if c.created_at else "",
                    "hook": c.dossier_hook,
                    "role": c.role,
                    "email_confidence": c.email_confidence,
                    "linkedin_url": c.linkedin_url,
                    "domain": company.domain if company else None,
                }
            )
        return out


def today_board(workspace_id: str) -> dict[str, Any]:
    """Doc 117 §7 Today header — L1/L2/L3 snapshot."""
    with db()() as session:
        signals = session.scalar(
            select(func.count()).select_from(GtmSignal).where(
                GtmSignal.workspace_id == workspace_id
            )
        ) or 0
        contacts = session.scalars(
            select(GtmContact).where(GtmContact.workspace_id == workspace_id)
        ).all()
        researched = sum(1 for c in contacts if c.dossier_hook)
        ready = [
            c
            for c in contacts
            if c.dossier_hook
            and c.relationship_stage in {"researched", "new"}
            and not c.do_not_contact
        ][:5]
        replied = session.scalar(
            select(func.count())
            .select_from(GtmTouch)
            .where(
                GtmTouch.direction == "in",
                GtmTouch.contact_id.in_([c.id for c in contacts] or ["__none__"]),
            )
        ) or 0
        conversing = sum(
            1 for c in contacts if c.relationship_stage in {"conversing", "friend"}
        )
        converted = sum(1 for c in contacts if c.relationship_stage == "converted")
        pending_approvals = sum(
            1 for c in contacts if c.relationship_stage == "researched" and c.dossier_hook
        )

    return {
        "l1": {
            "signals_found": signals,
            "dossiers_completed": researched,
            "ready_for_approval": pending_approvals,
            "human_replies": replied,
        },
        "l2": {
            "conversations": conversing,
            "offers": sum(1 for c in contacts if c.relationship_stage == "offered"),
        },
        "l3": {"customers": converted},
        "first_five": [
            {
                "id": c.id,
                "first_name": c.first_name,
                "email": c.email,
                "hook": c.dossier_hook,
                "stage": c.relationship_stage,
            }
            for c in ready
        ],
        "recommended_next": (
            f"Approve the {len(ready)} congrats for today's cohort."
            if ready
            else "Run a signal sweep to find warm opportunities."
        ),
    }


def ensure_default_sequence(
    workspace_id: str, *, product_name: str | None = None
) -> GtmSequence:
    with db()() as session:
        row = session.scalar(
            select(GtmSequence).where(
                GtmSequence.workspace_id == workspace_id,
                GtmSequence.track == "relationship",
            )
        )
        if row:
            return row
        row = GtmSequence(
            id=new_id("gtmseq"),
            workspace_id=workspace_id,
            name="Relationships",
            track="relationship",
            steps_json=json.dumps(DEFAULT_STEPS),
            armed=0,
            hypothesis="Congrats-first outreach converts warm signals to conversations",
            metric="human_reply_rate",
            threshold=0.05,
            window_days=14,
            product_name=product_name,
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def draft_congrats(
    contact_id: str,
    *,
    workspace_id: str | None = None,
    product_name: str | None = None,
) -> dict[str, Any]:
    from app.gtm.relationships.scope import gtm_get

    if not workspace_id:
        raise ValueError("contact_not_found")
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise ValueError("contact_not_found")
        if not contact.dossier_hook:
            raise ValueError("no_dossier_hook")
        company = session.get(GtmCompany, contact.company_id) if contact.company_id else None
        first = contact.first_name or "there"
        hook = contact.dossier_hook
        body = (
            f"Hey {first},\n\n"
            f"Saw you {hook}. That's a real milestone. "
            f"Congrats from one builder to another.\n\n"
            f"No ask, just wanted to say well done.\n"
        )
        lint = lint_draft(
            body, hook=hook, step="congrats", product_name=product_name
        )
        if not lint.ok:
            raise ValueError(f"lint_refused:{','.join(lint.reasons)}")
        return {
            "contact_id": contact.id,
            "to": contact.email,
            "subject": f"Congrats on {hook[:60]}",
            "body": body,
            "hook": hook,
            "company": company.canonical_name if company else None,
            "lint": {"ok": True, "reasons": []},
        }


def prepare_first_five(workspace_id: str, *, venture_id: str | None = None) -> list[dict[str, Any]]:
    """Ensure top contacts have dossiers; return draftable first five."""
    with db()() as session:
        contacts = list(
            session.scalars(
                select(GtmContact)
                .where(
                    GtmContact.workspace_id == workspace_id,
                    GtmContact.do_not_contact == 0,
                )
                .order_by(GtmContact.created_at.desc())
                .limit(20)
            )
        )
    drafts: list[dict[str, Any]] = []
    for c in contacts:
        if not c.dossier_hook:
            try:
                research_contact_from_latest_signal(
                    c.id, venture_id=venture_id, workspace_id=workspace_id
                )
            except Exception:
                continue
        try:
            drafts.append(draft_congrats(c.id, workspace_id=workspace_id))
        except ValueError:
            continue
        if len(drafts) >= 5:
            break
    return drafts


def arm_sequence(
    workspace_id: str,
    *,
    contact_ids: list[str],
    armed_by: str = "founder",
    product_name: str | None = None,
) -> dict[str, Any]:
    """Create sequence_arm approval + enroll contacts that pass dossier gate."""
    seq = ensure_default_sequence(workspace_id, product_name=product_name)
    enrolled: list[str] = []
    refused: list[dict[str, str]] = []

    with db()() as session:
        sequence = session.get(GtmSequence, seq.id)
        assert sequence is not None
        from app.gtm.relationships.scope import gtm_get

        for cid in contact_ids:
            contact = gtm_get(session, GtmContact, cid, workspace_id)
            if contact is None:
                refused.append({"id": cid, "reason": "not_found"})
                continue
            if not contact.dossier_hook:
                refused.append({"id": cid, "reason": "no_dossier_hook"})
                continue
            if contact.do_not_contact or contact.unsubscribed_at:
                refused.append({"id": cid, "reason": "suppressed"})
                continue
            try:
                draft_congrats(
                    cid,
                    workspace_id=workspace_id,
                    product_name=product_name or sequence.product_name,
                )
            except ValueError as exc:
                refused.append({"id": cid, "reason": str(exc)})
                continue
            existing = session.scalar(
                select(GtmEnrollment).where(
                    GtmEnrollment.contact_id == cid,
                    GtmEnrollment.sequence_id == sequence.id,
                    GtmEnrollment.status == "active",
                )
            )
            if existing:
                enrolled.append(cid)
                continue
            session.add(
                GtmEnrollment(
                    id=new_id("gtmenr"),
                    workspace_id=workspace_id,
                    contact_id=cid,
                    sequence_id=sequence.id,
                    step_idx=0,
                    next_at=utcnow(),
                    status="active",
                    created_at=utcnow(),
                )
            )
            if contact.relationship_stage == "researched":
                # Stay at researched until first touch actually sends.
                pass
            enrolled.append(cid)
        session.commit()

    approval = request_approval(
        workspace_id=workspace_id,
        kind="external_comms",
        subject_ref=f"gtm_sequence:{seq.id}",
        summary=f"Arm Relationships sequence for {len(enrolled)} contacts (congrats + nudge + value)",
        detail={
            "sequence_id": seq.id,
            "contact_ids": enrolled,
            "steps": ["congrats", "nudge", "value"],
            "note": "offer step still requires per-message approval",
        },
        risk="high",
        requested_by=armed_by,
    )

    with db()() as session:
        sequence = session.get(GtmSequence, seq.id)
        if sequence:
            sequence.armed = 1
            sequence.armed_by = armed_by
            sequence.armed_at = utcnow()
            sequence.approval_id = approval.id
            session.commit()

    return {
        "sequence_id": seq.id,
        "approval_id": approval.id,
        "enrolled": enrolled,
        "refused": refused,
        "armed": True,
    }


def record_inbound_reply(
    workspace_id: str,
    contact_id: str,
    *,
    body: str,
    classification: str = "human_positive",
    message_id: str | None = None,
    in_reply_to: str | None = None,
) -> dict[str, Any]:
    """Reply path: create inbound touch, move enrollment + contact stage."""
    from app.gtm.relationships.scope import gtm_get

    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise ValueError("contact_not_found")
        enrollment = session.scalar(
            select(GtmEnrollment)
            .where(
                GtmEnrollment.contact_id == contact_id,
                GtmEnrollment.status.in_(["active", "paused"]),
            )
            .order_by(GtmEnrollment.created_at.desc())
            .limit(1)
        )
        touch = GtmTouch(
            id=new_id("gtmtch"),
            workspace_id=workspace_id,
            enrollment_id=enrollment.id if enrollment else None,
            contact_id=contact_id,
            mailbox_id=enrollment.mailbox_id if enrollment else None,
            direction="in",
            channel="email",
            message_id=message_id,
            in_reply_to=in_reply_to,
            body_text=body,
            sent_at=utcnow(),
            classification=classification,
            simulated=0,
            created_at=utcnow(),
        )
        session.add(touch)
        if enrollment:
            enrollment.status = "replied"
            enrollment.next_at = None
        if classification.startswith("human_"):
            if contact.relationship_stage in {"congratulated", "nudged", "researched"}:
                transition(contact, "conversing")
            elif contact.relationship_stage == "conversing" and classification == "human_positive":
                transition(contact, "friend")
        if classification == "unsubscribe":
            transition(contact, "unsubscribed")
            contact.unsubscribed_at = utcnow()
        session.commit()
        return {
            "touch_id": touch.id,
            "stage": contact.relationship_stage,
            "enrollment_status": enrollment.status if enrollment else None,
        }
