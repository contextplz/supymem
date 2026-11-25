"""Enrichment ladder, web fallback, LinkedIn discovery, 14-day congrats repair.

Port of Email-Supymem research behaviour into Company OS (no Next.js).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.db.models import GtmCompany, GtmContact, GtmResearchJob, GtmResearchSnapshot
from app.gtm.supymem import brightdata, hunter
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

_DATE_RE = re.compile(
    r"\b(20\d{2}|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.I,
)
_LINKEDIN_IN = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?",
    re.I,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

WARM_SOURCES = ("yc", "launch_hn", "hn", "raise", "admit", "accelerator", "producthunt")


def sequence_kind_for(source: str | None, hook: str | None) -> str:
    blob = f"{source or ''} {hook or ''}".lower()
    if any(tok in blob for tok in WARM_SOURCES):
        return "warm"
    return "cold"


def richness_score(evidence: dict[str, Any]) -> int:
    score = 0
    if evidence.get("linkedin_url"):
        score += 2
    if evidence.get("email"):
        score += 2
    if evidence.get("profile"):
        score += 2
    if evidence.get("web_snippets"):
        score += 1
    if evidence.get("secondary_email"):
        score += 1
    if evidence.get("hook"):
        score += 1
    return min(score, 7)


def congrats_is_fresh(hook: str | None, notes: str | None, *, max_days: int = 14) -> bool:
    """True if we can prove the congratulated event is within max_days."""
    text = f"{hook or ''}\n{notes or ''}"
    if not text.strip():
        return False
    now = utcnow()
    # ISO dates
    for m in re.finditer(r"20\d{2}-\d{2}-\d{2}", text):
        try:
            from datetime import date

            d = date.fromisoformat(m.group(0))
            age = (now.date() - d).days
            if 0 <= age <= max_days:
                return True
        except ValueError:
            continue
    lower = text.lower()
    if any(w in lower for w in ("today", "yesterday", "this week", "just announced", "just got into")):
        return True
    if _DATE_RE.search(text) is None:
        return False
    return False


def repair_congrats_hook(hook: str | None, notes: str | None) -> tuple[str, bool]:
    """If congrats isn't fresh, return a non-congratulatory hook and repaired=True."""
    hook = (hook or "").strip() or "what you're building"
    if congrats_is_fresh(hook, notes):
        return hook, False
    return hook.replace("congrats on ", "").replace("Congrats on ", "") or "what you're building", True


def _merge_enrichment(contact: GtmContact, patch: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(contact.enrichment_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update({k: v for k, v in patch.items() if v not in (None, "", [], {})})
    contact.enrichment_json = json.dumps(data)
    return data


def discover_linkedin_url(*, name: str | None, company: str | None, email: str | None) -> str | None:
    query_bits = [p for p in (name, company, "linkedin") if p]
    if not query_bits:
        return None
    try:
        from app.research.openworker_bridge import search_web

        hits = search_web(" ".join(query_bits), max_results=8)
    except Exception as exc:  # noqa: BLE001
        log.info("linkedin discovery search failed: %s", exc)
        return None
    rows = hits if isinstance(hits, list) else (hits.get("results") or hits.get("items") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("href") or row.get("link") or "")
        m = _LINKEDIN_IN.search(url) or _LINKEDIN_IN.search(str(row.get("snippet") or row.get("title") or ""))
        if m:
            return brightdata.normalize_linkedin_url(m.group(0))
    if email and "@" in email:
        domain = email.split("@", 1)[1]
        try:
            from app.research.openworker_bridge import search_web

            hits = search_web(f"site:linkedin.com/in {name or ''} {domain}", max_results=5)
            rows = hits if isinstance(hits, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "")
                m = _LINKEDIN_IN.search(url)
                if m:
                    return brightdata.normalize_linkedin_url(m.group(0))
        except Exception:
            pass
    return None


def web_fallback(*, name: str | None, company: str | None, domain: str | None) -> list[str]:
    snippets: list[str] = []
    urls: list[str] = []
    if domain:
        urls.append(f"https://{domain.removeprefix('www.')}")
    query = " ".join(p for p in (name, company, "founder") if p)
    if query:
        try:
            from app.research.openworker_bridge import search_web

            hits = search_web(query, max_results=5)
            rows = hits if isinstance(hits, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = str(row.get("url") or "")
                if url.startswith("http"):
                    urls.append(url)
                sn = str(row.get("snippet") or row.get("title") or "").strip()
                if sn:
                    snippets.append(sn[:400])
        except Exception as exc:  # noqa: BLE001
            log.info("web fallback search failed: %s", exc)
    from app.research.web_fetch import fetch_page_markdown

    for url in urls[:3]:
        try:
            text = fetch_page_markdown(url, timeout_s=12.0)
            if text and text.strip():
                snippets.append(f"{url}: {text.strip()[:900]}")
        except Exception:
            continue
    return snippets[:8]


def secondary_email_from_domain(domain: str | None, first: str | None, last: str | None) -> str | None:
    if not domain or not first:
        return None
    found = hunter.email_finder(domain=domain, first_name=first, last_name=last)
    if not found:
        return None
    email = found.get("email")
    return email.lower() if isinstance(email, str) else None


def enrich_contact(contact_id: str, *, sequence_day: int | None = None) -> dict[str, Any]:
    """Run the 0–7 ladder. Persist notes + snapshot."""
    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        if contact is None:
            return {"ok": False, "reason": "missing"}
        company = session.get(GtmCompany, contact.company_id) if contact.company_id else None
        name = " ".join(p for p in (contact.first_name, contact.last_name) if p) or None
        domain = company.domain if company else None
        evidence: dict[str, Any] = {}
        notes_parts: list[str] = []

        if not contact.linkedin_url:
            found = discover_linkedin_url(
                name=name, company=company.canonical_name if company else None, email=contact.email
            )
            if found:
                contact.linkedin_url = found
        if contact.linkedin_url:
            evidence["linkedin_url"] = contact.linkedin_url

        if (not contact.email) and hunter.configured():
            lead = {
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "email": contact.email,
                "website": domain,
                "linkedin_url": contact.linkedin_url,
            }
            hunter.enrich_lead(lead)
            if lead.get("email"):
                contact.email = lead["email"]
                contact.email_confidence = lead.get("email_confidence") or contact.email_confidence
        if contact.email:
            evidence["email"] = contact.email

        if contact.linkedin_url and brightdata.configured():
            scraped = brightdata.scrape_profile(contact.linkedin_url)
            profile = scraped.get("profile") or {}
            if profile:
                evidence["profile"] = profile
                notes_parts.append(brightdata.format_research_notes(profile))
                if profile.get("name") and not contact.first_name:
                    parts = str(profile["name"]).split()
                    contact.first_name = parts[0]
                    contact.last_name = " ".join(parts[1:]) or contact.last_name
                contact.role = contact.role or profile.get("position") or profile.get("headline")
            posts = brightdata.scrape_posts(contact.linkedin_url)
            if posts.get("notes"):
                notes_parts.append(str(posts["notes"]))

        snippets = web_fallback(
            name=name,
            company=company.canonical_name if company else None,
            domain=domain,
        )
        if snippets:
            evidence["web_snippets"] = snippets
            notes_parts.append("WEB EVIDENCE:\n" + "\n".join(f"- {s}" for s in snippets))

        if contact.email and not contact.secondary_email and hunter.configured() and domain:
            alt = secondary_email_from_domain(domain, contact.first_name, contact.last_name)
            if alt and alt.lower() != contact.email.lower():
                contact.secondary_email = alt
                evidence["secondary_email"] = alt

        notes = "\n\n".join(notes_parts) or (contact.dossier_json or "")
        hook = contact.dossier_hook
        if not hook:
            org = company.canonical_name if company else "their company"
            hook = f"building {org}"
        kind = sequence_kind_for(contact.source, hook)
        contact.sequence_kind = kind
        if kind == "warm":
            hook, repaired = repair_congrats_hook(hook, notes)
            evidence["congrats_repaired"] = repaired
        contact.dossier_hook = hook
        evidence["hook"] = hook
        level = richness_score({**evidence, "email": contact.email, "linkedin_url": contact.linkedin_url})
        contact.enrichment_level = level
        if contact.email and contact.email_confidence in {"safe", "risky"}:
            if contact.relationship_stage in {"new", "researched"}:
                contact.relationship_stage = "researched"
        _merge_enrichment(contact, {"evidence": evidence, "notes": notes, "level": level})

        snap = GtmResearchSnapshot(
            id=new_id("gtmsn"),
            contact_id=contact.id,
            sequence_day=sequence_day,
            notes=notes[:12000],
            evidence_json=json.dumps(evidence)[:20000],
            richness=level,
            created_at=utcnow(),
        )
        session.add(snap)
        session.commit()
        return {
            "ok": True,
            "contact_id": contact.id,
            "enrichment_level": level,
            "email": contact.email,
            "linkedin_url": contact.linkedin_url,
            "hook": hook,
            "sequence_kind": kind,
        }


def latest_snapshot_notes(contact_id: str, sequence_day: int | None = None) -> str | None:
    with db()() as session:
        q = select(GtmResearchSnapshot).where(GtmResearchSnapshot.contact_id == contact_id)
        if sequence_day is not None:
            q = q.where(GtmResearchSnapshot.sequence_day == sequence_day)
        row = session.scalars(q.order_by(GtmResearchSnapshot.created_at.desc()).limit(1)).first()
        return row.notes if row else None


def queue_research_job(
    session: Any,
    *,
    workspace_id: str,
    contact_id: str,
    scheduled_for: Any,
    kind: str = "pre_send",
    sequence_day: int | None = None,
) -> GtmResearchJob:
    job = GtmResearchJob(
        id=new_id("gtmrj"),
        workspace_id=workspace_id,
        contact_id=contact_id,
        kind=kind,
        status="queued",
        scheduled_for=scheduled_for,
        sequence_day=sequence_day,
        attempts=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    return job


def tick_research_jobs(*, limit: int = 5) -> dict[str, Any]:
    processed: list[str] = []
    now = utcnow()
    with db()() as session:
        jobs = list(
            session.scalars(
                select(GtmResearchJob)
                .where(
                    GtmResearchJob.status == "queued",
                    GtmResearchJob.scheduled_for <= now,
                )
                .order_by(GtmResearchJob.scheduled_for.asc())
                .limit(limit)
            )
        )
        ids = [j.id for j in jobs]
        for job in jobs:
            job.status = "running"
            job.attempts += 1
            job.updated_at = now
        session.commit()
    for jid in ids:
        with db()() as session:
            job = session.get(GtmResearchJob, jid)
            if job is None:
                continue
            try:
                enrich_contact(job.contact_id, sequence_day=job.sequence_day)
                job.status = "done"
                job.updated_at = utcnow()
            except Exception as exc:  # noqa: BLE001
                job.status = "queued"
                job.last_error = str(exc)[:500]
                job.scheduled_for = utcnow() + timedelta(minutes=20)
                job.updated_at = utcnow()
            session.commit()
            processed.append(jid)
    return {"processed": processed}
