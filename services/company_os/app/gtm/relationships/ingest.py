"""Paste ingest: Layer A signals and Layer B humans without web harvest (OT-20)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import GtmCompany, GtmContact, GtmPasteBody, GtmSignal
from app.gtm.relationships.scope import gtm_get
from app.gtm.relationships.store import normalize_domain, upsert_company, upsert_contact
from app.state import db

PASTE_SOURCES = frozenset(
    {"profile_paste", "job_post_paste", "thread_url_paste", "founder_note"}
)

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_QUOTE_SPAN = re.compile(r"[\"“]([^\"”]{12,280})[\"”]")
_WE_LINE = re.compile(r"^\s*(I|we)\s.{10,}", re.I | re.M)


def extract_quote(raw_text: str) -> str | None:
    """Heuristic only — quoted spans or first-person lines ≥ 12 chars. No LLM."""

    text = (raw_text or "").strip()
    if not text:
        return None
    m = _QUOTE_SPAN.search(text)
    if m:
        return m.group(1).strip()
    m = _WE_LINE.search(text)
    if m:
        line = m.group(0).strip()
        if len(line) >= 12:
            return line[:280]
    return None


def _parse_name(raw: str) -> tuple[str | None, str | None]:
    m = re.search(r"(?im)^\s*(?:name|person)\s*[:\-]\s*(.+)$", raw)
    if m:
        parts = m.group(1).strip().split()
        return parts[0], " ".join(parts[1:]) or None
    first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if first and "@" not in first and len(first.split()) <= 4 and len(first) < 60:
        parts = first.split()
        if parts and parts[0][0].isalpha():
            return parts[0], " ".join(parts[1:]) or None
    return None, None


def _parse_company(raw: str, company_name: str | None) -> str | None:
    if company_name and company_name.strip():
        return company_name.strip()
    m = re.search(r"(?im)^\s*(?:company|org(?:anization)?)\s*[:\-]\s*(.+)$", raw)
    if m:
        return m.group(1).strip()
    return None


def ingest_paste(
    workspace_id: str,
    *,
    source: str,
    raw_text: str = "",
    url: str | None = None,
    trigger_code: str | None = None,
    company_name: str | None = None,
    enrich: bool = False,
) -> dict[str, Any]:
    """Paste a job, thread, or profile. Never calls ``discover_leads``. Default no enrich."""

    if source not in PASTE_SOURCES:
        raise ValueError("unknown_ingest_source")
    from app.gtm.relationships.hosted import assert_aup_accepted

    assert_aup_accepted(workspace_id)
    _ = enrich  # Bright Data stays off unless a later caller opts in on a pasted domain.
    text = (raw_text or "").strip()
    quote = extract_quote(text)
    email_m = _EMAIL_RE.search(text)
    email = email_m.group(0).lower() if email_m else None
    first, last = _parse_name(text)
    co_name = _parse_company(text, company_name)
    domain = None
    if email:
        domain = normalize_domain(email.split("@", 1)[-1])
    if url:
        domain = domain or normalize_domain(url)

    now = utcnow()
    with db()() as session:
        company: GtmCompany | None = None
        if co_name or domain:
            company = upsert_company(
                session,
                workspace_id=workspace_id,
                name=co_name or (domain or "Unknown"),
                domain=domain,
                evidence_url=url,
            )
        signal_kind = {
            "job_post_paste": "job_post_paste",
            "thread_url_paste": "pasted_thread",
            "profile_paste": "pasted_profile",
            "founder_note": "founder_note",
        }[source]
        extra = hashlib.sha256((text or url or "")[:800].encode()).hexdigest()[:16]
        dedup = hashlib.sha256(
            f"{signal_kind}|{workspace_id}|{url or ''}|{extra}".encode()
        ).hexdigest()[:40]
        existing = session.scalar(
            select(GtmSignal).where(
                GtmSignal.workspace_id == workspace_id,
                GtmSignal.dedup_key == dedup,
            )
        )
        contact: GtmContact | None = None
        layer_b = source == "profile_paste" or bool(email) or bool(first)
        if layer_b and (email or first):
            contact = upsert_contact(
                session,
                workspace_id=workspace_id,
                company_id=company.id if company else None,
                email=email,
                first_name=first,
                last_name=last,
                role="founder" if source == "profile_paste" else None,
                source="paste",
                email_confidence="safe" if email else "none",
                lawful_basis="none",
                lawful_basis_note="tenant_paste",
            )
            contact.layer = "B"
            contact.contact_provenance = source
            if email and url and source != "founder_note":
                evidence = json.loads(contact.enrichment_json or "{}")
                evidence.setdefault("contact_source_url", url)
                contact.enrichment_json = json.dumps(evidence)
            if quote:
                contact.exact_language_json = json.dumps(
                    [{"text": quote, "source": "extracted", "signal_id": None}]
                )
            session.flush()

        if existing is None:
            existing = GtmSignal(
                id=new_id("gtmli"),
                workspace_id=workspace_id,
                company_id=company.id if company else None,
                contact_id=contact.id if contact else None,
                kind=signal_kind,
                evidence_url=url,
                observed_at=now,
                payload_json=json.dumps({"source": source, "chars": len(text)}),
                dedup_key=dedup,
                created_at=now,
                source=source,
                trigger_code=trigger_code,
                raw_text=None,
                confidence="claimed" if source == "founder_note" else "observed",
                layer="B" if contact else "A",
                extracted_quote=quote,
                timing_observed_at=now,
            )
            session.add(existing)
            session.flush()
            if text:
                session.add(
                    GtmPasteBody(
                        signal_id=existing.id,
                        workspace_id=workspace_id,
                        raw_text=text[:8000],
                        created_at=now,
                    )
                )
            if contact is not None and quote:
                contact.exact_language_json = json.dumps(
                    [{"text": quote, "source": "extracted", "signal_id": existing.id}]
                )

        session.commit()
        return {
            "ok": True,
            "source": source,
            "company_id": company.id if company else None,
            "company": company.canonical_name if company else None,
            "signal_id": existing.id,
            "contact_id": contact.id if contact else None,
            "layer": "B" if contact else "A",
            "quote": quote,
            "personalize_ready": bool(quote or trigger_code),
            "enrich": False,
        }


def promote_signal_to_layer_b(
    workspace_id: str,
    signal_id: str,
    *,
    contact_fields: dict[str, Any],
) -> dict[str, Any]:
    with db()() as session:
        signal = gtm_get(session, GtmSignal, signal_id, workspace_id)
        if signal is None:
            raise ValueError("signal_not_found")
        company = (
            gtm_get(session, GtmCompany, signal.company_id, workspace_id)
            if signal.company_id
            else None
        )
        email = (contact_fields.get("email") or "").strip().lower() or None
        first = contact_fields.get("first_name")
        last = contact_fields.get("last_name")
        role = contact_fields.get("role")
        contact = upsert_contact(
            session,
            workspace_id=workspace_id,
            company_id=company.id if company else None,
            email=email,
            first_name=first,
            last_name=last,
            role=role,
            source="paste",
            email_confidence="safe" if email else "none",
            lawful_basis="none",
            lawful_basis_note="tenant_paste",
        )
        contact.layer = "B"
        contact.contact_provenance = signal.source or "job_post_paste"
        if signal.extracted_quote:
            contact.exact_language_json = json.dumps(
                [
                    {
                        "text": signal.extracted_quote,
                        "source": "extracted",
                        "signal_id": signal.id,
                    }
                ]
            )
        session.commit()
        return {"contact_id": contact.id, "signal_id": signal.id, "layer": "B"}
