"""OT-42: interview capture + Mom-Test hypothetical flags (heuristic, no LLM)."""

from __future__ import annotations

import json
import re
from typing import Any

from core.clock import utcnow
from core.ids import new_id

from app.db.models import GtmContact, GtmInterview
from app.gtm.relationships.scope import gtm_get
from app.state import db

HYPOTHETICAL_HINTS = (
    "would you",
    "would this",
    "if we built",
    "hypothetically",
    "in theory",
    "imagine if",
    "do you think people",
)

DEFAULT_SCRIPT = {
    "context_min": 2,
    "investigate_min": 12,
    "reflect_min": 4,
    "next_min": 2,
}


class InterviewError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def extract_mom_test_flags(notes_md: str) -> dict[str, Any]:
    text = notes_md or ""
    low = text.lower()
    flagged = [h for h in HYPOTHETICAL_HINTS if h in low]
    incident = None
    m = re.search(r"(?im)^.*last(?:\s+time)?(?:\s+this)?\s+happen[^\n]{8,200}", text)
    if m:
        incident = m.group(0).strip()[:280]
    quotes = re.findall(r"[\"“]([^\"”]{12,280})[\"”]", text)
    return {
        "incident": incident,
        "cost": None,
        "workaround": None,
        "trigger": None,
        "owner": None,
        "exact_quotes": quotes[:8],
        "hypotheticals_flagged": flagged,
    }


def capture_interview(
    workspace_id: str,
    *,
    contact_id: str,
    notes_md: str,
    kind: str = "first_meeting",
    mom_test_ok: bool = False,
    next_step: str | None = None,
) -> dict[str, Any]:
    extraction = extract_mom_test_flags(notes_md)
    now = utcnow()
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise InterviewError("contact_not_found", 404)
        if mom_test_ok and extraction["hypotheticals_flagged"] and not extraction["incident"]:
            # Human may still confirm, but extractor never auto-sets mom_test_ok.
            pass
        row = GtmInterview(
            id=new_id("gtmin"),
            workspace_id=workspace_id,
            contact_id=contact.id,
            company_id=contact.company_id,
            kind=kind if kind in {"first_meeting", "followup", "roundtable", "async_note"} else "first_meeting",
            occurred_at=now,
            duration_min=20,
            script_json=json.dumps(DEFAULT_SCRIPT),
            notes_md=notes_md,
            extraction_json=json.dumps(extraction),
            mom_test_ok=1 if mom_test_ok else 0,
            next_step=next_step,
            created_at=now,
        )
        session.add(row)
        session.commit()
        return {
            "id": row.id,
            "contact_id": contact.id,
            "extraction": extraction,
            "mom_test_ok": bool(row.mom_test_ok),
            "hypotheticals_flagged": extraction["hypotheticals_flagged"],
        }


def list_interviews(workspace_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmInterview)
                .where(GtmInterview.workspace_id == workspace_id)
                .order_by(GtmInterview.created_at.desc())
            )
        )
        return [
            {
                "id": r.id,
                "contact_id": r.contact_id,
                "kind": r.kind,
                "notes_md": r.notes_md,
                "extraction": json.loads(r.extraction_json or "{}"),
                "mom_test_ok": bool(r.mom_test_ok),
            }
            for r in rows
        ]
