"""Company / contact / signal upserts for the GTM relationship engine."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GtmCompany, GtmContact, GtmSignal, OpsProspect
from app.gtm.relationships.icp import passes_icp, score_signal
from app.gtm.relationships.state_machine import transition
from core.clock import utcnow
from core.ids import new_id

_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if raw.startswith("http"):
        host = urlparse(raw).hostname or ""
    else:
        host = raw.split("/")[0]
    host = host.removeprefix("www.")
    if not host or not _DOMAIN_RE.match(host):
        return None
    return host


def dedup_key(kind: str, *, domain: str | None, evidence_url: str | None, extra: str = "") -> str:
    base = f"{kind}|{domain or ''}|{evidence_url or ''}|{extra}"
    return hashlib.sha256(base.encode()).hexdigest()[:40]


def upsert_company(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    domain: str | None = None,
    yc_batch: str | None = None,
    accelerator: str | None = None,
    description: str | None = None,
    evidence_url: str | None = None,
) -> GtmCompany:
    from app.gtm.enrich import _FREE_MAIL

    domain_n = normalize_domain(domain)
    if domain_n in _FREE_MAIL:
        domain_n = None
    row: GtmCompany | None = None
    if domain_n:
        row = session.scalar(
            select(GtmCompany).where(
                GtmCompany.workspace_id == workspace_id,
                GtmCompany.domain == domain_n,
            )
        )
    if row is None and (domain_n or normalize_domain(name) not in _FREE_MAIL):
        row = session.scalar(
            select(GtmCompany).where(
                GtmCompany.workspace_id == workspace_id,
                GtmCompany.canonical_name == name.strip(),
            )
        )
    if row is None:
        row = GtmCompany(
            id=new_id("gtmco"),
            workspace_id=workspace_id,
            canonical_name=name.strip() or (domain_n or "unknown"),
            domain=domain_n,
            yc_batch=yc_batch,
            accelerator=accelerator,
            description=description,
            source_urls_json=json.dumps([evidence_url] if evidence_url else []),
            created_at=utcnow(),
        )
        session.add(row)
    else:
        if yc_batch and not row.yc_batch:
            row.yc_batch = yc_batch
        if accelerator and not row.accelerator:
            row.accelerator = accelerator
        if description and not row.description:
            row.description = description
        if domain_n and not row.domain:
            row.domain = domain_n
        if evidence_url:
            urls = json.loads(row.source_urls_json or "[]")
            if evidence_url not in urls:
                urls.append(evidence_url)
                row.source_urls_json = json.dumps(urls[:20])
    session.flush()
    return row


def upsert_contact(
    session: Session,
    *,
    workspace_id: str,
    company_id: str | None,
    email: str | None,
    first_name: str | None = None,
    last_name: str | None = None,
    role: str | None = None,
    source: str | None = None,
    email_confidence: str = "none",
    lawful_basis: str = "legitimate_interest",
    lawful_basis_note: str | None = None,
    linkedin_url: str | None = None,
) -> GtmContact:
    email_n = (email or "").strip().lower() or None
    li = (linkedin_url or "").strip() or None
    row: GtmContact | None = None
    if email_n:
        row = session.scalar(
            select(GtmContact).where(
                GtmContact.workspace_id == workspace_id,
                GtmContact.email == email_n,
            )
        )
    if row is None and li:
        row = session.scalar(
            select(GtmContact).where(
                GtmContact.workspace_id == workspace_id,
                GtmContact.linkedin_url == li,
            )
        )
    if row is None:
        now = utcnow()
        prospect = None
        if email_n:
            prospect = session.scalar(
                select(OpsProspect).where(
                    OpsProspect.workspace_id == workspace_id,
                    OpsProspect.email == email_n,
                )
            )
            if prospect is None:
                prospect = OpsProspect(
                    id=new_id("opspr"),
                    workspace_id=workspace_id,
                    email=email_n,
                    first_name=first_name,
                    last_name=last_name,
                    company=None,
                    title=role,
                    source=source,
                    status="new",
                    created_at=now,
                )
                session.add(prospect)
                session.flush()
        row = GtmContact(
            id=new_id("gtmct"),
            workspace_id=workspace_id,
            company_id=company_id,
            ops_prospect_id=prospect.id if prospect else None,
            email=email_n,
            email_confidence=email_confidence,
            first_name=first_name,
            last_name=last_name,
            role=role,
            linkedin_url=li,
            lawful_basis=lawful_basis,
            lawful_basis_note=lawful_basis_note
            or "B2B public signal; legitimate interest for relationship outreach",
            lawful_basis_at=now if lawful_basis != "none" else None,
            relationship_stage="new",
            stage_changed_at=now,
            source=source,
            created_at=now,
        )
        session.add(row)
    else:
        if company_id and not row.company_id:
            row.company_id = company_id
        if first_name and not row.first_name:
            row.first_name = first_name
        if last_name and not row.last_name:
            row.last_name = last_name
        if role and not row.role:
            row.role = role
        if li and not row.linkedin_url:
            row.linkedin_url = li
        if email_n and not row.email:
            row.email = email_n
            row.email_confidence = email_confidence or row.email_confidence
    session.flush()
    return row


def ingest_signal(
    session: Session,
    *,
    workspace_id: str,
    kind: str,
    company_name: str,
    domain: str | None = None,
    evidence_url: str | None = None,
    payload: dict[str, Any] | None = None,
    yc_batch: str | None = None,
    accelerator: str | None = None,
    contact_email: str | None = None,
    contact_first_name: str | None = None,
    contact_role: str | None = None,
    email_confidence: str = "none",
) -> GtmSignal | None:
    """Create company + optional contact + signal if ICP passes and dedup is clean."""
    if not passes_icp(kind):
        return None
    company = upsert_company(
        session,
        workspace_id=workspace_id,
        name=company_name,
        domain=domain,
        yc_batch=yc_batch,
        accelerator=accelerator,
        description=(payload or {}).get("description") if payload else None,
        evidence_url=evidence_url,
    )
    key = dedup_key(
        kind,
        domain=company.domain,
        evidence_url=evidence_url,
        extra=company.canonical_name.lower(),
    )
    existing = session.scalar(
        select(GtmSignal).where(
            GtmSignal.workspace_id == workspace_id,
            GtmSignal.dedup_key == key,
        )
    )
    if existing:
        return existing

    if contact_email or contact_first_name:
        contact = upsert_contact(
            session,
            workspace_id=workspace_id,
            company_id=company.id,
            email=contact_email,
            first_name=contact_first_name,
            role=contact_role or "Founder",
            source=kind,
            email_confidence=email_confidence,
        )
        if contact.relationship_stage == "new" and contact.dossier_hook:
            transition(contact, "researched")

    now = utcnow()
    sig = GtmSignal(
        id=new_id("gtmsig"),
        workspace_id=workspace_id,
        company_id=company.id,
        kind=kind,
        evidence_url=evidence_url,
        observed_at=now,
        payload_json=json.dumps(payload or {}),
        icp_score=score_signal(kind),
        dedup_key=key,
        created_at=now,
    )
    session.add(sig)
    session.flush()
    return sig
