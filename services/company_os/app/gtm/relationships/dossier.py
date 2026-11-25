"""Contact dossiers remain in the scoped GTM store (doc 115 § Phase 2)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import GtmCompany, GtmContact, GtmSignal
from app.gtm.relationships.state_machine import transition
from app.state import db


def build_dossier_from_signal(
    *,
    company_name: str,
    signal_kind: str,
    evidence_url: str | None,
    description: str | None = None,
    yc_batch: str | None = None,
    person_name: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Deterministic dossier stub — no LLM required for the gate.

    lead_researcher may enrich later via GPT Researcher / Crawl4AI; the hook
    must always be concrete enough for the tone linter.
    """
    if yc_batch:
        hook = f"got into YC {yc_batch}"
    elif signal_kind == "launch_hn":
        hook = f"launched on HN: {(description or company_name)[:80]}"
    elif signal_kind in {"web_icp", "rss"}:
        hook = f"building {company_name}" + (
            f": {(description or '')[:70]}" if description else ""
        )
    elif signal_kind == "ef_admit":
        hook = f"joined {(description and 'an accelerator') or 'Entrepreneur First'}"
    elif evidence_url:
        hook = f"public signal at {evidence_url[:60]}"
    else:
        hook = f"building {company_name}"

    return {
        "who": person_name or "Founder",
        "role": role,
        "company": company_name,
        "what_they_built": description or company_name,
        "why_we_care": f"Warm signal ({signal_kind}): timing is right for a congrats.",
        "hook": hook,
        "evidence_url": evidence_url,
        "signal_kind": signal_kind,
        "yc_batch": yc_batch,
    }


def attach_dossier(
    contact_id: str,
    dossier: dict[str, Any],
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
) -> GtmContact:
    """Persist a dossier on its scoped contact and advance to researched."""
    from app.gtm.relationships.scope import gtm_get

    hook = (dossier.get("hook") or "").strip()
    if not hook:
        raise ValueError("dossier_missing_hook")
    if not workspace_id:
        raise ValueError("contact_not_found")

    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise ValueError("contact_not_found")
        contact.dossier_hook = hook
        contact.dossier_json = json.dumps(dossier)
        if contact.relationship_stage == "new":
            transition(contact, "researched")
        session.commit()
        workspace_id = contact.workspace_id
    # A URL does not prove generated dossier prose. Keep third-party personal
    # data in GTM, rather than starting an unawaited, invalid MemoryPort write.
    with db()() as session:
        return gtm_get(session, GtmContact, contact_id, workspace_id)  # type: ignore[return-value]


def research_contact_from_latest_signal(
    contact_id: str,
    *,
    venture_id: str | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Build + attach a dossier from the contact's company latest signal."""
    from app.gtm.relationships.scope import gtm_get

    if not workspace_id:
        raise ValueError("contact_not_found")
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise ValueError("contact_not_found")
        company = session.get(GtmCompany, contact.company_id) if contact.company_id else None
        signal = None
        if company:
            signal = session.scalar(
                select(GtmSignal)
                .where(GtmSignal.company_id == company.id)
                .order_by(GtmSignal.observed_at.desc())
                .limit(1)
            )
        company_name = company.canonical_name if company else (contact.first_name or "Company")
        dossier = build_dossier_from_signal(
            company_name=company_name,
            signal_kind=signal.kind if signal else (contact.source or "rss"),
            evidence_url=signal.evidence_url if signal else None,
            description=company.description if company else None,
            yc_batch=company.yc_batch if company else None,
            person_name=contact.first_name,
            role=contact.role,
        )
    attach_dossier(
        contact_id, dossier, venture_id=venture_id, workspace_id=workspace_id
    )
    return dossier
