"""email.sequence — governed multi-step outbound (doc 115 §7 / 74).

Live SMTP send lands later; this tool always respects CHANNELS_DRY_RUN and the
dossier + tone gates. Never registers LinkedIn/X DM tools.
"""

from __future__ import annotations

import os

from app.actuation.contract import ActuationMode, ActuationResult
from app.actuation.registry import ToolSpec, register
from app.db.models import GtmContact, GtmEnrollment, GtmSequence, GtmTouch
from app.gtm.relationships.scope import gtm_get
from app.gtm.relationships.state_machine import transition
from app.gtm.relationships.tone import lint_draft
from app.state import db
from core.clock import utcnow
from core.ids import new_id


async def email_sequence(venture_id: str, payload: dict[str, object]) -> ActuationResult:
    contact_id = str(payload.get("contact_id") or "")
    body = str(payload.get("body") or "")
    subject = str(payload.get("subject") or "")
    step = str(payload.get("step") or "congrats")
    enrollment_id = str(payload.get("enrollment_id") or "") or None

    if not contact_id:
        return ActuationResult(
            mode=ActuationMode.FAILED,
            tool_id="email.sequence",
            venture_id=venture_id,
            ok=False,
            reason="missing_contact_id",
        )

    workspace_id: str | None = None
    try:
        from app.workspaces.resolve import workspace_for_venture

        ws = workspace_for_venture(venture_id)
        if ws is not None:
            workspace_id = ws.id
    except Exception:  # noqa: BLE001
        workspace_id = None
    if workspace_id is None:
        workspace_id = venture_id

    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.sequence",
                venture_id=venture_id,
                ok=False,
                reason="contact_not_found",
            )
        if contact.do_not_contact or contact.unsubscribed_at:
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.sequence",
                venture_id=venture_id,
                ok=False,
                reason="suppressed",
            )
        if contact.lawful_basis == "none":
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.sequence",
                venture_id=venture_id,
                ok=False,
                reason="no_lawful_basis",
            )
        if not contact.dossier_hook:
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.sequence",
                venture_id=venture_id,
                ok=False,
                reason="no_dossier_hook",
            )

        product_name = None
        if enrollment_id:
            enr = session.get(GtmEnrollment, enrollment_id)
            if enr:
                seq = session.get(GtmSequence, enr.sequence_id)
                product_name = seq.product_name if seq else None

        lint = lint_draft(
            body, hook=contact.dossier_hook, step=step, product_name=product_name
        )
        if not lint.ok:
            return ActuationResult(
                mode=ActuationMode.FAILED,
                tool_id="email.sequence",
                venture_id=venture_id,
                ok=False,
                reason=f"lint:{','.join(lint.reasons)}",
            )

        dry = os.environ.get("CHANNELS_DRY_RUN", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        # SMTP live path is Phase 3 — for now always simulated unless explicitly off
        # AND a future mailbox SMTP tool is wired. Honest label.
        simulated = 1 if dry else 1
        mode = ActuationMode.SIMULATED

        touch = GtmTouch(
            id=new_id("gtmtch"),
            workspace_id=workspace_id,
            enrollment_id=enrollment_id,
            contact_id=contact_id,
            direction="out",
            channel="email",
            subject=subject,
            body_text=body,
            sent_at=utcnow(),
            simulated=simulated,
            created_at=utcnow(),
        )
        session.add(touch)
        if step in {"congrats", "0"} and contact.relationship_stage == "researched":
            transition(contact, "congratulated")
        elif step in {"nudge", "1"} and contact.relationship_stage == "congratulated":
            transition(contact, "nudged")
        session.commit()
        touch_id = touch.id

    return ActuationResult(
        mode=mode,
        tool_id="email.sequence",
        venture_id=venture_id,
        ok=True,
        reason="simulated" if simulated else "sent",
        external_ref=touch_id,
        provider_response={"step": step, "subject": subject, "simulated": bool(simulated)},
    )


register(
    ToolSpec(
        tool_id="email.sequence",
        handler=email_sequence,
        description="Send a governed relationship-sequence email step",
    )
)
