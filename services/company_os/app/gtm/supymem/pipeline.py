"""Lead ingest → Hunter/Bright Data enrich → Gmail sequence (Email-Supymem)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlparse

from sqlalchemy import select

from app.autopilot.guard import DEFAULT_SEND_MAX, mailbox_send_max
from app.core.local_mode import is_local
from app.db.models import (
    GtmCompany,
    GtmContact,
    GtmEmailAccount,
    GtmEmailEvent,
    GtmEmailSendQueue,
    GtmTouch,
    Workspace,
)
from app.gtm.relationships.dossier import build_dossier_from_signal
from app.gtm.relationships.icp import email_confidence_allowed
from app.gtm.relationships.suppression import email_suppressed
from app.gtm.relationships.store import upsert_company, upsert_contact
from app.gtm.supymem import brightdata, composio_gmail, draft, hunter
from app.gtm.supymem import factory as gtm_factory
from app.gtm.supymem import learning
from app.gtm.supymem.research import (
    latest_snapshot_notes,
    queue_research_job,
    repair_congrats_hook,
    sequence_kind_for,
)
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SEQUENCE_DAYS = (1, 2, 4, 7)
DAY_OFFSETS = {1: 0, 2: 1, 4: 3, 7: 6}
DEFAULT_DAILY_CAP = DEFAULT_SEND_MAX
MAX_TICK = 3


def _dry_run() -> bool:
    if is_local():
        from app.gtm.local_mail import enabled
        return not enabled("send_email")
    # A connector and CHANNELS_DRY_RUN=false are insufficient to make a real
    # message possible. Deployment must separately opt into outbound execution.
    from app.approvals.external import execution_enabled

    if not execution_enabled():
        return True
    return os.environ.get("CHANNELS_DRY_RUN", "true").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _confidence(lead: dict[str, Any]) -> str:
    raw = (lead.get("email_confidence") or lead.get("email_tier") or "").lower()
    if raw in {"safe", "risky", "guess", "none"}:
        return raw
    if raw in {"published", "unverified"}:
        # Existing schema calls this guess; it is excluded by the send policy.
        # Published evidence is retained separately and never proves delivery.
        return "guess"
    if raw in {"valid", "verified"}:
        return "safe"
    if raw in {"accept_all", "risky"}:
        return "risky"
    if lead.get("email"):
        return "risky"
    return "none"


def _domain_of(lead: dict[str, Any]) -> str | None:
    website = (lead.get("website") or lead.get("domain") or "").strip()
    if website:
        host = urlparse(website if "://" in website else f"https://{website}").hostname
        if host:
            return host.removeprefix("www.").lower()
    email = (lead.get("email") or "").strip()
    if "@" in email:
        from app.gtm.enrich import _FREE_MAIL
        domain = email.split("@", 1)[1].lower()
        return domain if domain not in _FREE_MAIL else None
    return None


def _llm_status() -> dict[str, Any]:
    """Redacted platform Foundry status; never probes a vendor-specific key."""
    try:
        from app.llm.founder_provider import platform_offer

        offer = platform_offer()
        return {
            "ready": bool(os.environ.get("LLM_API_KEY", "").strip()),
            "provider": offer.get("provider"),
            "model": offer.get("model"),
            "label": offer.get("label"),
        }
    except Exception:
        return {"ready": False, "provider": "azure_foundry", "model": "DeepSeek-V4-Pro"}


def enrich_status() -> dict[str, Any]:
    gmail_ok = composio_gmail.configured()
    return {
        "hunter": {"ready": hunter.configured()},
        "brightdata": {"ready": brightdata.configured()},
        "gmail": {
            "ready": gmail_ok,
            "configured": gmail_ok,
            "hint": None
            if gmail_ok
            else "Supymem's Gmail connection service needs operator setup. This version does not accept a personal Composio API key.",
        },
        "llm": _llm_status(),
        "dry_run": _dry_run(),
        "linkedin_dm": {
            "supported": True,
            "mode": "unipile_or_human_queue",
            "reason": (
                "LinkedIn messages go through a connected inbox (Unipile) or a "
                "human send queue. We do not automate linkedin.com in a browser."
            ),
        },
        "unipile": {"ready": bool(os.environ.get("UNIPILE_API_KEY", "").strip())},
    }


def _contact_payload(c: GtmContact, company: GtmCompany | None = None) -> dict[str, Any]:
    seq = {}
    try:
        seq = json.loads(c.sequence_state_json or "{}")
    except json.JSONDecodeError:
        seq = {}
    enrich = draft.parse_enrichment(c.enrichment_json)
    return {
        "id": c.id,
        "email": c.email or "",
        "first_name": c.first_name,
        "last_name": c.last_name,
        "company": company.canonical_name if company else None,
        "domain": company.domain if company else None,
        "role": c.role,
        "linkedin_url": c.linkedin_url,
        "email_confidence": c.email_confidence,
        "email_verification": enrich.get("email_verification"),
        "source_url": enrich.get("source_url"),
        "evidence": enrich.get("contact_evidence"),
        "target_match": enrich.get("target_match"),
        "status": c.relationship_stage,
        "source": c.source,
        "hook": c.dossier_hook,
        "gmail_thread_id": c.gmail_thread_id,
        "times_emailed": c.times_emailed,
        "times_linkedin": c.times_linkedin,
        "last_emailed_at": c.last_emailed_at.isoformat() if c.last_emailed_at else None,
        "replied_at": c.replied_at.isoformat() if c.replied_at else None,
        "bounced_at": c.bounced_at.isoformat() if c.bounced_at else None,
        "sequence_kind": c.sequence_kind,
        "preferred_channel": c.preferred_channel,
        "enrichment_level": c.enrichment_level,
        "linkedin_stage": c.linkedin_stage,
        "secondary_email": c.secondary_email,
        "icp_score": getattr(c, "icp_score", None) if getattr(c, "icp_score", None) is not None else enrich.get("icp_score"),
        "icp_tier": getattr(c, "icp_tier", None) or enrich.get("icp_tier"),
        "outreach_pattern": getattr(c, "outreach_pattern", None) or enrich.get("outreach_pattern"),
        "opengtm_messages": draft.opengtm_messages_of(enrich) or None,
        "sequence": seq,
        "created_at": c.created_at.isoformat() if c.created_at else "",
    }


def list_contacts(workspace_id: str, *, limit: int = 200, contact_ids: list[str] | None = None) -> list[dict[str, Any]]:
    with db()() as session:
        query = select(GtmContact).where(GtmContact.workspace_id == workspace_id)
        if contact_ids is not None:
            query = query.where(GtmContact.id.in_(contact_ids))
        rows = session.scalars(
            query
            .order_by(GtmContact.created_at.desc())
            .limit(limit)
        ).all()
        companies = {
            co.id: co
            for co in session.scalars(
                select(GtmCompany).where(
                    GtmCompany.id.in_([c.company_id for c in rows if c.company_id] or ["__none__"])
                )
            )
        }
        return [_contact_payload(c, companies.get(c.company_id or "")) for c in rows]


def _persist_lead(session: Any, workspace_id: str, lead: dict[str, Any], *, schedule_research: bool = True) -> GtmContact:
    domain = _domain_of(lead)
    company = upsert_company(
        session,
        workspace_id=workspace_id,
        name=(lead.get("organization") or lead.get("company") or domain or "Unknown"),
        domain=domain,
        description=lead.get("industry"),
        evidence_url=lead.get("source_url") or lead.get("website") or lead.get("linkedin_url"),
    )
    contact = upsert_contact(
        session,
        workspace_id=workspace_id,
        company_id=company.id,
        email=lead.get("email"),
        first_name=lead.get("first_name") or None,
        last_name=lead.get("last_name") or None,
        role=lead.get("title"),
        source=lead.get("source") or "ai_gtm",
        email_confidence=_confidence(lead),
        lawful_basis="none" if lead.get("source") == "public_web" else "legitimate_interest",
        lawful_basis_note="Published contact evidence; outreach authorization has not been established." if lead.get("source") == "public_web" else None,
        linkedin_url=brightdata.normalize_linkedin_url(lead.get("linkedin_url"))
        or lead.get("linkedin_url"),
    )
    if lead.get("x_handle") and not contact.x_handle:
        contact.x_handle = str(lead["x_handle"])
    msgs = lead.get("opengtm_messages") if isinstance(lead.get("opengtm_messages"), dict) else None
    finding = ""
    if msgs:
        finding = str(msgs.get("best_finding") or msgs.get("pattern_reason") or "").strip()
    hook_bits = [
        finding or None,
        lead.get("title"),
        lead.get("organization") or lead.get("company"),
        lead.get("source"),
    ]
    hook = contact.dossier_hook
    org = lead.get("organization") or company.canonical_name
    title = lead.get("title")
    if finding:
        hook = finding[:180]
        contact.dossier_hook = hook
    elif not hook:
        hook = f"building {org}" if org else f"working as {title}" if title else ""
        contact.dossier_hook = hook
    if not contact.dossier_json or finding:
        dossier = build_dossier_from_signal(
            company_name=org or "their company",
            signal_kind="opengtm_audit" if msgs else "web_icp",
            evidence_url=lead.get("source_url") or lead.get("website") or lead.get("linkedin_url"),
            description=" ".join(str(b) for b in hook_bits if b),
            person_name=lead.get("first_name"),
            role=title,
        )
        dossier["hook"] = contact.dossier_hook
        if lead.get("opengtm_qualification"):
            dossier["qualification"] = lead["opengtm_qualification"]
        if lead.get("opengtm_audit"):
            dossier["audit"] = lead["opengtm_audit"]
        contact.dossier_json = json.dumps(dossier)

    # Publication, verification and targeting evidence are distinct facts.
    enrich = draft.parse_enrichment(contact.enrichment_json)
    if lead.get("source") == "public_web":
        contact.email_confidence = "guess"  # never retain an earlier unsafe promotion
        enrich["email_verification"] = "unverified"
        enrich["source_url"] = str(lead.get("source_url") or "")[:2048]
        evidence = lead.get("evidence")
        if isinstance(evidence, dict):
            enrich["contact_evidence"] = {key: str(evidence.get(key) or "")[:2048]
                                          for key in ("kind", "url", "observed_at")}
        target = lead.get("target_match")
        if isinstance(target, dict):
            enrich["target_match"] = {"basis": "company_page_text",
                                      "terms": [str(t)[:120] for t in (target.get("terms") or [])[:20]],
                                      "requested_region": str(target.get("requested_region") or "")[:120]}

    for key in (
        "outreach_hint",
        "found_at",
        "source_url",
        "icp_score",
        "icp_tier",
        "outreach_pattern",
        "opengtm_messages",
        "opengtm_qualification",
        "opengtm_audit",
    ):
        val = lead.get(key)
        if val is not None and val != "":
            enrich[key] = val
    if msgs:
        enrich["opengtm_messages"] = msgs
    if enrich:
        contact.enrichment_json = json.dumps(enrich)
    if lead.get("icp_score") is not None and hasattr(contact, "icp_score"):
        try:
            contact.icp_score = float(lead["icp_score"])
        except (TypeError, ValueError):
            pass
    if lead.get("icp_tier") and hasattr(contact, "icp_tier"):
        contact.icp_tier = str(lead["icp_tier"])[:32]
    if lead.get("outreach_pattern") and hasattr(contact, "outreach_pattern"):
        contact.outreach_pattern = str(lead["outreach_pattern"])[:8]

    if msgs:
        seeded = draft.seed_sequence_from_opengtm(
            msgs,
            first=contact.first_name,
            company=org,
            hook=contact.dossier_hook,
        )
        if seeded:
            existing: dict[str, Any] = {}
            try:
                existing = json.loads(contact.sequence_state_json or "{}")
            except json.JSONDecodeError:
                existing = {}
            # Prefer OpenGTM copy for unsent days.
            for day_key, blob in seeded.items():
                cur = existing.get(day_key)
                if isinstance(cur, dict) and cur.get("sent_at"):
                    continue
                existing[day_key] = blob
            contact.sequence_state_json = json.dumps(existing)
            contact.sequence_kind = "cold"
            contact.enrichment_level = max(int(contact.enrichment_level or 0), 2)
        else:
            contact.sequence_kind = sequence_kind_for(contact.source, contact.dossier_hook)
    else:
        contact.sequence_kind = sequence_kind_for(contact.source, contact.dossier_hook)

    if schedule_research:
        queue_research_job(
            session,
            workspace_id=workspace_id,
            contact_id=contact.id,
            scheduled_for=utcnow(),
            kind="enrich",
        )
    return contact


def ingest_leads(
    workspace_id: str,
    leads: list[dict[str, Any]],
    *,
    enrich: bool = True,
    enrich_limit: int = 20,
) -> dict[str, Any]:
    """Persist found leads, optionally fill emails via Hunter."""
    created: list[str] = []
    enriched_n = 0
    working = list(leads)
    if enrich and hunter.configured():
        budget = enrich_limit
        for lead in working:
            if budget <= 0:
                break
            before = (lead.get("email") or "").strip()
            if before and lead.get("linkedin_url") and lead.get("first_name"):
                continue
            try:
                hunter.enrich_lead(lead)
                budget -= 1
                if (lead.get("email") or "").strip() and (lead.get("email") or "").strip() != before:
                    enriched_n += 1
                elif not before and (lead.get("email") or "").strip():
                    enriched_n += 1
            except Exception as exc:  # noqa: BLE001
                log.info("hunter enrich skipped: %s", exc)

    with db()() as session:
        for lead in working:
            contact = _persist_lead(session, workspace_id, lead, schedule_research=enrich)
            created.append(contact.id)
        session.commit()

    contacts = list_contacts(workspace_id, contact_ids=created)
    return {
        "ingested": len(created),
        "hunter_filled": enriched_n,
        "contacts": contacts,
        "leads": working,
    }


def import_identities(
    workspace_id: str,
    *,
    emails: str = "",
    linkedin_urls: str = "",
    enrich: bool = True,
    raw_text: str = "",
    source: str | None = None,
) -> dict[str, Any]:
    """Paste box: one email or LinkedIn URL per line.

    Optional ``raw_text`` / ``source`` forward to founder-led ingest without
    changing existing callers.
    """
    if source and raw_text:
        from app.gtm.relationships.ingest import ingest_paste

        return ingest_paste(
            workspace_id, source=source, raw_text=raw_text, enrich=bool(enrich)
        )

    leads: list[dict[str, Any]] = []
    for raw in (emails or "").splitlines():
        found = _EMAIL_RE.findall(raw)
        for email in found:
            leads.append({"email": email.lower(), "source": "manual"})
    for raw in (linkedin_urls or "").splitlines():
        url = brightdata.normalize_linkedin_url(raw.strip()) or (
            raw.strip() if "linkedin.com/" in raw.lower() else None
        )
        if not url:
            continue
        lead: dict[str, Any] = {"linkedin_url": url, "source": "manual_linkedin"}
        if enrich and brightdata.configured():
            scraped = brightdata.scrape_profile(url)
            profile = scraped.get("profile") or {}
            if profile.get("name"):
                parts = str(profile["name"]).split()
                lead["first_name"] = parts[0]
                lead["last_name"] = " ".join(parts[1:]) or None
            lead["title"] = profile.get("position") or profile.get("headline")
            lead["organization"] = profile.get("company")
            lead["research_notes"] = brightdata.format_research_notes(profile)
        leads.append(lead)

    result = ingest_leads(workspace_id, leads, enrich=enrich, enrich_limit=max(len(leads), 8))
    result["imported"] = len(leads)
    return result


def _gmail_workspace_ids(session: Any, workspace_id: str) -> list[str]:
    owner = session.scalar(select(Workspace.owner_id).where(Workspace.id == workspace_id))
    if not owner:
        return [workspace_id]
    query = select(Workspace.id).where(Workspace.owner_id == owner)
    from app.connections.mailboxes import enabled
    if enabled():
        from app.api.ops_auth import bound_ops_person
        from app.authz.workspace import subject_person_id
        from app.db.models import WorkspaceMember
        from app.tenancy import current_or_none
        scope = current_or_none()
        actor = bound_ops_person()
        if actor is None and scope is not None and scope.actor != "system":
            actor = subject_person_id(scope.actor)
        if actor is not None and actor != owner:
            memberships = select(WorkspaceMember.workspace_id).where(WorkspaceMember.person_id == actor)
            query = query.where(Workspace.id.in_(memberships))
    return list(session.scalars(query))


def _mailboxes_query(session: Any, workspace_id: str):
    return select(GtmEmailAccount).where(
        GtmEmailAccount.workspace_id.in_(_gmail_workspace_ids(session, workspace_id)),
        GtmEmailAccount.provider == ("smtp" if is_local() else "gmail"),
    ).order_by((GtmEmailAccount.workspace_id == workspace_id).desc(), GtmEmailAccount.updated_at.desc())


def _legacy_active_mailbox(session: Any, workspace_id: str) -> GtmEmailAccount | None:
    if is_local():
        from app.gtm.local_mail import configured, ensure_account
        return ensure_account(session, workspace_id) if configured() else None
    return session.scalar(_mailboxes_query(session, workspace_id).where(GtmEmailAccount.status == "active"))


def _active_mailbox(session: Any, workspace_id: str) -> GtmEmailAccount | None:
    from app.connections.mailboxes import MailboxUnavailable, enabled, resolve_send_mailbox
    if not enabled():
        return _legacy_active_mailbox(session, workspace_id)
    from app.tenancy import current_or_none
    scope = current_or_none()
    venture_id = (scope.venture_id or None) if scope is not None and scope.workspace_id == workspace_id else None
    try:
        ref = resolve_send_mailbox(session, workspace_id, venture_id=venture_id)
    except MailboxUnavailable:
        return None
    return session.get(GtmEmailAccount, ref["mailbox_id"])


def list_mailboxes(workspace_id: str, *, include_usage: bool = True) -> list[dict[str, Any]]:
    """Connection pages use saved counters; send enforcement still counts reserved slots."""
    from app.connections.composio_migration import reconcile_gmail_project
    if not is_local():
        reconcile_gmail_project(workspace_id)
    elif not composio_gmail.configured():
        return []
    today = utcnow().date().isoformat()
    with db()() as session:
        from app.connections.mailboxes import _workspace_default_mailbox_id, enabled
        multi = enabled()
        if is_local():
            from app.gtm.local_mail import ensure_account
            ensure_account(session, workspace_id)
            session.commit()
        workspace = session.get(Workspace, workspace_id) if multi else None
        if workspace is not None and workspace.default_gmail_mailbox_id is None:
            _workspace_default_mailbox_id(session, workspace_id)
            session.commit()
        rows = session.scalars(
            _mailboxes_query(session, workspace_id)
        ).all()
        return [
            {
                "id": a.id,
                "email": a.email,
                "provider": a.provider,
                "status": a.status,
                "daily_send_limit": mailbox_send_max(session, a),
                "sent_today": (composio_gmail.mailbox_slots_used_today(session, a) if a.email else a.sent_today)
                    if include_usage else (a.sent_today if a.sent_count_date == today else 0),
                **({"is_default": bool(workspace and workspace.default_gmail_mailbox_id == a.id),
                    "needs_reconnect": a.status == "needs_reconnect", "updated_at": a.updated_at.isoformat()}
                   if multi else {"connected_account_id": a.connected_account_id}),
            }
            for a in rows
        ]


def start_gmail_connect(
    workspace_id: str,
    app_user_id: str | None = None,
) -> dict[str, Any]:
    # OT-19: client-supplied Composio user_id is ignored.
    _ = app_user_id
    return composio_gmail.create_gmail_connection(workspace_id=workspace_id)


def mail_readiness(workspace_id: str) -> dict[str, Any]:
    """Presence-only Gmail readiness for lab / Connect mail UX (no secrets)."""
    configured = composio_gmail.configured()
    accounts = list_mailboxes(workspace_id, include_usage=False)
    # An active mailbox counts even when its profile lookup failed once; sync backfills the address later.
    active = [a for a in accounts if str(a.get("status") or "") == "active"]
    needs_reconnect = not active and any(a.get("status") == "needs_reconnect" for a in accounts)
    primary = active[0] if active else (accounts[0] if accounts else None)
    from app.connections.mailboxes import enabled
    multi = enabled()
    if multi:
        needs_reconnect = any(a.get("needs_reconnect") for a in accounts)
        primary = next((a for a in accounts if a.get("is_default")), None)
    dry = _dry_run()
    if is_local():
        from app.gtm.local_mail import blocked_message
    state = gtm_factory.get_or_create_state(workspace_id)
    blockers: list[dict[str, str]] = []
    if not configured:
        blockers.append(
            {
                "code": "gmail_env",
                "severity": "NeedsOperator",
                "message": ("Open Keys and connections > Email and add your SMTP server, mailbox address and app password, then try again." if is_local()
                            else "Supymem's Gmail connection service needs operator setup. This version does not accept a personal Composio API key."),
            }
        )
    elif not active:
        blockers.append(
            {
                "code": "gmail_reconnect_required" if needs_reconnect else "gmail_not_connected",
                "severity": "NeedsYou",
                "message": "Reconnect your Gmail to keep your emails going" if needs_reconnect else "Click Connect mail to authorize Gmail via Composio.",
            }
        )
    if dry:
        blockers.append(
            {
                "code": "channels_dry_run",
                "severity": "warning",
                "message": (blocked_message("send_email") if is_local()
                            else "CHANNELS_DRY_RUN is on, so sends are simulated until it is false."),
            }
        )
    if not is_local() and not state.get("send_enabled"):
        blockers.append(
            {
                "code": "send_disabled",
                "severity": "info",
                "message": "Engine send_enabled=false. Cold-mail ticks stay stopped; lab test-send still works.",
            }
        )
    return {
        "configured": configured,
        "connected": bool(active),
        "needs_reconnect": needs_reconnect,
        "ready": bool(configured and active and not dry and (not multi or primary and primary.get("status") == "active")),
        "dry_run": dry,
        "send_enabled": not dry if is_local() else bool(state.get("send_enabled")),
        "account_email": (primary or {}).get("email") if primary else None,
        "account_id": (primary or {}).get("id") if primary else None,
        "accounts": [
            {
                "id": a.get("id"),
                "email": a.get("email"),
                "status": a.get("status"),
                "sent_today": a.get("sent_today"),
                "daily_send_limit": a.get("daily_send_limit"),
                **({"is_default": a.get("is_default", False), "needs_reconnect": a.get("needs_reconnect", False),
                    "updated_at": a.get("updated_at")} if multi else {}),
            }
            for a in accounts
        ],
        "blockers": blockers,
        **({"multi_gmail_v1": True} if multi else {}),
    }


def verify_gmail_connection(workspace_id: str, *, mailbox_id: str | None = None) -> dict[str, Any]:
    """Check the saved workspace mailbox against Gmail without reading messages."""
    with db()() as session:
        from app.connections.mailboxes import MailboxUnavailable, _owned_account, enabled, resolve_send_mailbox
        if enabled():
            try:
                if mailbox_id is not None:
                    account = _owned_account(session, workspace_id, mailbox_id)
                else:
                    ref = resolve_send_mailbox(session, workspace_id)
                    account = session.get(GtmEmailAccount, ref["mailbox_id"])
            except MailboxUnavailable:
                return {"ok": False, "status": "not_connected", "email": None, "log_id": None, "account_id": None}
            session.commit()
        else:
            account = _active_mailbox(session, workspace_id)
            if is_local():
                session.commit()
        if account is not None:
            connected_account_id = account.connected_account_id
            saved_email = account.email
            provider_user_id = account.composio_user_id
            native_smtp = is_local() and account.provider == "smtp"
        else:
            connected_account_id = None
            saved_email = None
    if not connected_account_id:
        return {"ok": False, "status": "not_connected", "email": None, "log_id": None, "account_id": None}
    try:
        if enabled() or is_local():
            from app.credits import metered
            with metered.charge_sync(workspace_id=workspace_id, purpose="connections.verify", provider="smtp" if native_smtp else "composio", estimate_micro=0, payer="founder") as meter:
                proof = composio_gmail.verify_gmail_profile(provider_user_id, str(connected_account_id))
                meter.record_micro(0)
        else:
            proof = composio_gmail.verify_gmail_profile(provider_user_id, str(connected_account_id))
    except composio_gmail.ReconnectRequired:
        return {"ok": False, "status": "needs_reconnect", "email": None, "log_id": None, "account_id": connected_account_id}
    except Exception:  # provider errors may contain sensitive details
        log.info("Gmail profile verification failed for workspace %s", workspace_id)
        return {"ok": False, "status": "provider_error", "email": None, "log_id": None, "account_id": connected_account_id}
    email = proof.get("email")
    log_id = proof.get("log_id")
    if not proof.get("successful"):
        return {"ok": False, "status": "provider_error", "email": None, "log_id": log_id, "account_id": connected_account_id}
    if not email or not log_id and not native_smtp:
        return {"ok": False, "status": "unconfirmed", "email": email, "log_id": log_id, "account_id": connected_account_id}
    if saved_email and str(saved_email).lower() != email.lower():
        return {"ok": False, "status": "account_mismatch", "email": email, "log_id": log_id, "account_id": connected_account_id}
    return {"ok": True, "status": "verified", "email": email, "log_id": log_id, "account_id": connected_account_id}


def send_test_email(
    workspace_id: str,
    *,
    to: str,
    subject: str,
    body: str,
    confirm: bool = False,
    approval_id: str | None = None,
    mailbox_id: str | None = None,
) -> dict[str, Any]:
    """Send one arbitrary test email via the connected Composio Gmail mailbox.

    Skips the cold-mail prospect queue. ``confirm`` only confirms intent in the
    UI; a live message still needs one exact, approved external-comms action.
    """
    to_addr = (to or "").strip()
    subj = (subject or "").strip()
    text = (body or "").strip()
    if not to_addr or not _EMAIL_RE.fullmatch(to_addr):
        return {"ok": False, "error": "invalid_to", "block_reason": "Provide a valid recipient email."}
    if not subj:
        return {"ok": False, "error": "subject_required", "block_reason": "Subject is required."}
    if not text:
        return {"ok": False, "error": "body_required", "block_reason": "Body is required."}
    if not confirm:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": "NeedsYou: confirm=true required. Click Send test email in Marketing Lab.",
            "error": "confirm_required",
        }

    readiness = mail_readiness(workspace_id)
    if not readiness["configured"]:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": (readiness["blockers"][0]["message"] if is_local()
                else "Supymem's Gmail connection service needs operator setup. This version does not accept a personal Composio API key."),
            "error": "gmail_not_configured",
            "mail": readiness,
        }
    if not readiness["connected"]:
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": ("Open Keys and connections > Email and save your mailbox settings, then try your reviewed email again."
                if is_local() else "NeedsYou: Connect mail first (Composio Gmail OAuth)."),
            "error": "gmail_not_connected",
            "mail": readiness,
        }

    simulated = _dry_run()
    from app.gtm.email_learning import globally_suppressed
    if globally_suppressed(None, to_addr):
        return {"ok": False, "status": "blocked", "error": "contact_suppressed",
            "block_reason": "This address opted out. Choose another recipient; nothing was sent."}
    with db()() as session:
        from app.connections.mailboxes import enabled, resolve_send_mailbox
        if enabled():
            from app.tenancy import current_or_none
            scope = current_or_none()
            venture_id = (scope.venture_id or None) if scope is not None and scope.workspace_id == workspace_id else None
            ref = resolve_send_mailbox(session, workspace_id, venture_id=venture_id, mailbox_id=mailbox_id)
            mailbox = session.get(GtmEmailAccount, ref["mailbox_id"])
            session.commit()
        else:
            mailbox = _active_mailbox(session, workspace_id)
        if mailbox is None or not mailbox.connected_account_id:
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "NeedsYou: no active Gmail mailbox. Connect mail.",
                "error": "no_mailbox",
            }
        account_id = mailbox.id
        from_email = mailbox.email
        composio_user_id = mailbox.composio_user_id or composio_gmail.default_user_id(
            workspace_id
        )
        connected_account_id = mailbox.connected_account_id
        # bump daily counter after success
        _reset_daily_if_needed(mailbox)
        if composio_gmail.mailbox_slots_used_today(session, mailbox) >= mailbox_send_max(session, mailbox):
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "Daily send cap reached for this mailbox.",
                "error": "daily_cap",
                "mail": readiness,
            }

    provider_id: str | None = None
    thread_id: str | None = None
    if simulated or str(connected_account_id).startswith("sim_"):
        provider_id = f"sim_test_{new_id('mail')}"
    elif is_local():
        from app.approvals.external import OutboundExecutionError, execute_metered_external_action
        try:
            outcome = execute_metered_external_action(workspace_id=workspace_id, approval_id=approval_id,
                action="gtm_test_email", provider="smtp", target={"mailbox_id": account_id,
                    "to": to_addr, "subject": subj, "body": text},
                invoke=lambda: composio_gmail.send_gmail_email(composio_user_id=composio_user_id,
                    connected_account_id=connected_account_id, to=to_addr, subject=subj, body=text,
                    workspace_id=workspace_id))
        except OutboundExecutionError as exc:
            return {"ok": False, "status": "blocked", "error": exc.code,
                "block_reason": str(exc), "dry_run": False}
        if outcome.status != "succeeded":
            return {"ok": False, "status": "failed", "error": outcome.code,
                "block_reason": ("Open your Sent mailbox and check this email's delivery before trying again."
                    if outcome.status == "recovery_required" else "Open Keys and connections > Email and check your app password and recipient address, then review the email again in Find customers."),
                "dry_run": False}
        sent = outcome.provider_result if isinstance(outcome.provider_result, dict) else {}
        provider_id, thread_id = sent.get("provider_message_id"), sent.get("thread_id")
    else:
        from app.approvals.external import consume_external_approval

        allowed, reason = consume_external_approval(
            workspace_id=workspace_id,
            approval_id=approval_id,
            action="gtm_test_email",
            target={
                "mailbox_id": account_id,
                "to": to_addr,
                "subject": subj,
                "body": text,
            },
        )
        if not allowed:
            return {
                "ok": False,
                "status": "blocked",
                "error": reason,
                "block_reason": "NeedsYou: approve this exact test email before it can be sent.",
                "dry_run": False,
            }
        try:
            sent = composio_gmail.send_gmail_email(
                composio_user_id=composio_user_id,
                connected_account_id=connected_account_id,
                to=to_addr,
                subject=subj,
                body=text,
                workspace_id=workspace_id,
            )
            provider_id = sent.get("provider_message_id")
            thread_id = sent.get("thread_id")
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": "failed",
                "error": str(exc)[:400],
                "block_reason": str(exc)[:400],
                "from": from_email,
                "to": to_addr,
                "dry_run": False,
            }

    with db()() as session:
        mailbox = session.get(GtmEmailAccount, account_id)
        if mailbox is not None:
            _reset_daily_if_needed(mailbox)
            if simulated or str(connected_account_id).startswith("sim_"):
                mailbox.sent_today = int(mailbox.sent_today or 0) + 1
            mailbox.updated_at = utcnow()
            session.commit()

    return {
        "ok": True,
        "status": "sent",
        "dry_run": simulated,
        "simulated": simulated,
        "from": from_email,
        "to": to_addr,
        "subject": subj,
        "provider_message_id": provider_id,
        "thread_id": thread_id,
        "account_id": account_id,
        "floor": "external_comms",
        "floor_note": "A live test email consumed one exact external-comms approval.",
    }


def _multi_gmail_inventory(workspace_id: str, identities: dict[str, str], known_ids: dict[str, tuple[str, str | None]]) -> list[dict[str, Any]]:
    from app.credits import metered

    out = {}
    blocked = set()
    pending_ids = iter(sorted(known_ids))
    pending_users = iter(sorted(identities)) if not known_ids else iter(())
    auth_id = os.environ.get(composio_gmail.AUTH_CONFIG_ENV, "").strip() or composio_gmail._read_auth_config_cache()
    while True:
        requested_id = next(pending_ids, None)
        user = None if requested_id else next(pending_users, None)
        if requested_id is None and user is None:
            return list(out.values())
        params = {"user_ids": user, "toolkit_slugs": "gmail", "limit": 100} if user else None
        path = "/connected_accounts/" + quote(requested_id, safe="") if requested_id else "/connected_accounts"
        with metered.charge_sync(workspace_id=workspace_id, purpose="connections.status", provider="composio", estimate_micro=0, payer="founder") as meter:
            try:
                data = composio_gmail._request("GET", path, params=params)
            except composio_gmail.DefiniteFailure as exc:
                if exc.status_code != 404 or not requested_id:
                    raise
                from app.connections.composio_migration import mark_gmail_reconnect
                mark_gmail_reconnect(requested_id)
                continue
            meter.record_micro(0)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        items = [payload] if requested_id else data.get("items") or payload.get("items") or data.get("data") or []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            user = str(item.get("user_id") or item.get("entity_id") or item.get("userId") or "")
            account_id = str(item.get("id") or item.get("nanoid") or "")
            if requested_id and account_id != requested_id:
                from app.connections.composio_migration import mark_gmail_reconnect
                mark_gmail_reconnect(requested_id)
                continue
            if account_id in known_ids and user and user != known_ids[account_id][0]:
                from app.connections.composio_migration import mark_gmail_reconnect
                mark_gmail_reconnect(account_id)
                blocked.add(account_id)
                out.pop(account_id, None)
                continue
            if user not in identities or not account_id:
                if account_id in out:
                    blocked.add(account_id)
                    out.pop(account_id)
                continue
            if user != composio_gmail.user_id_for(identities[user]) and account_id not in known_ids:
                continue
            toolkit = item.get("toolkit") or {}
            auth = item.get("auth_config") or {}
            expected_user, expected_auth = known_ids.get(account_id, (user, auth_id))
            if (not isinstance(toolkit, dict) or toolkit.get("slug") != "gmail"
                    or not isinstance(auth, dict) or auth.get("is_disabled")
                    or str((item.get("experimental") or {}).get("account_type", "PRIVATE")).upper() != "PRIVATE"
                    or user != expected_user or (expected_auth and auth.get("id") != expected_auth)):
                if account_id in known_ids:
                    from app.connections.composio_migration import mark_gmail_reconnect
                    mark_gmail_reconnect(account_id)
                blocked.add(account_id)
                out.pop(account_id, None)
                continue
            if not expected_auth or account_id in blocked:
                continue
            out[account_id] = {**item, "user_id": user}


def sync_gmail_accounts(workspace_id: str, app_user_id: str | None = None, *, connections: list[dict[str, Any]] | None = None,
                       intent: str | None = None, mailbox_id: str | None = None) -> dict[str, Any]:
    from app.connections.composio_migration import gmail_notice, mark_gmail_reconnect, reconcile_gmail_project, resume_gmail_work
    from app.connections.mailboxes import MailboxUnavailable, _workspace_default_mailbox_id, enabled
    multi = enabled()
    if multi and (intent not in {None, "add", "reconnect"} or (mailbox_id is not None and intent != "reconnect")
                  or (intent == "reconnect" and (not mailbox_id or connections is None or len(connections) != 1))):
        raise MailboxUnavailable("gmail_intent_invalid")
    user_id = composio_gmail.user_id_for(workspace_id)
    if app_user_id is not None and app_user_id != user_id:
        raise ValueError("Gmail identity must belong to the selected workspace.")
    if is_local():
        from app.gtm.local_mail import ensure_account
        with db()() as session:
            ensure_account(session, workspace_id)
            session.commit()
        return {"accounts": list_mailboxes(workspace_id), "synced": 1}
    reconcile_gmail_project(workspace_id)
    requested_workspace_id = workspace_id
    identities = {user_id: workspace_id}
    known_ids = {}
    if multi:
        with db()() as session:
            owned_workspaces = _gmail_workspace_ids(session, workspace_id)
            identities = {composio_gmail.user_id_for(wid): wid for wid in owned_workspaces}
            for row in session.scalars(_mailboxes_query(session, workspace_id)):
                identities.setdefault(row.composio_user_id, row.workspace_id)
                marker = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == row.id,
                    GtmEmailEvent.event_type == "composio_project").order_by(GtmEmailEvent.created_at.desc()).limit(1))
                auth_id = (json.loads(marker.metadata_json or "{}").get("auth_config_id") if marker else None)
                known_ids[row.connected_account_id] = (row.composio_user_id, auth_id or os.environ.get(composio_gmail.AUTH_CONFIG_ENV)
                    or composio_gmail._read_auth_config_cache())
            from app.db.models import ContentOAuthState
            for receipt in session.scalars(select(ContentOAuthState).where(
                    ContentOAuthState.workspace_id.in_(owned_workspaces), ContentOAuthState.platform == "gmail",
                    ContentOAuthState.outcome == "active")):
                if receipt.provider_user_id:
                    identities.setdefault(receipt.provider_user_id, receipt.workspace_id)
                    known_ids[receipt.connected_account_id] = (receipt.provider_user_id, receipt.auth_config_id)
            for wid in owned_workspaces:
                _workspace_default_mailbox_id(session, wid)
            session.commit()
    if connections is None and not multi:
        with db()() as session:
            mailbox = _active_mailbox(session, workspace_id)
            if mailbox is None:
                mailbox = session.scalar(_mailboxes_query(session, workspace_id).where(GtmEmailAccount.status == "needs_reconnect"))
            if mailbox is not None:
                workspace_id, user_id = mailbox.workspace_id, mailbox.composio_user_id
    connected = connections if connections is not None else (
        _multi_gmail_inventory(requested_workspace_id, identities, known_ids) if multi
        else composio_gmail.list_gmail_connections(user_id))
    if connections is None and not multi:
        from app.content.oauth.callbacks import known_connections
        with db()() as session:
            owned_workspaces = _gmail_workspace_ids(session, requested_workspace_id)
        for owned_workspace in owned_workspaces:
            connected.extend(item for item in known_connections(owned_workspace, platform="gmail", active_only=False,
                exclude={str(item.get("id") or "") for item in connected}) if multi or item.get("user_id") == user_id)
    now = utcnow()
    saved: list[str] = []
    newly_connected: list[str] = []
    reconnect: list[str] = []
    with db()() as session:
        retired = set()
        for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.workspace_id.in_(
                _gmail_workspace_ids(session, requested_workspace_id)), GtmEmailEvent.event_type == "composio_project")):
            migration = json.loads(event.metadata_json or "{}")
            if migration.get("previous_connected_account_id") != migration.get("connected_account_id"):
                retired.add(migration.get("previous_connected_account_id"))
        for account in connected:
            ca_id = str(account.get("id") or account.get("nanoid") or "")
            if not ca_id or ca_id in retired:
                continue
            owner = str(
                account.get("userId")
                or account.get("user_id")
                or ""
            )
            if multi:
                if owner not in identities:
                    continue
                workspace_id, user_id = identities[owner], owner
                stored = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == ca_id))
                if stored is not None and stored.workspace_id in _gmail_workspace_ids(session, requested_workspace_id):
                    workspace_id = stored.workspace_id
            if owner != user_id:
                continue
            if str(account.get("status") or "").upper() != "ACTIVE":
                # Revoked or expired at Google/Composio: stop offering this mailbox until the founder reconnects.
                gone = session.scalar(select(GtmEmailAccount).where(
                    GtmEmailAccount.connected_account_id == ca_id, GtmEmailAccount.workspace_id == workspace_id))
                if gone is not None and (not multi or gone.status != "revoked") and str(account.get("status")).upper() in {"EXPIRED", "REVOKED", "FAILED", "INACTIVE", "DELETED", "MISSING"}:
                    reconnect.append(ca_id)
                continue
            existing = session.scalar(select(GtmEmailAccount).where(GtmEmailAccount.connected_account_id == ca_id))
            if existing is not None and existing.workspace_id != workspace_id:
                raise ValueError("Connected account belongs to another workspace.")
            row = existing
            if row is not None and row.status == "revoked" and (multi or connections is None):
                continue
            recovering = row is not None and row.status != "active"
            if row is not None and row.status == "needs_reconnect" and connections is None:
                marker = session.scalar(select(GtmEmailEvent).where(GtmEmailEvent.email_account_id == row.id,
                    GtmEmailEvent.event_type == "composio_project").order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
                if (json.loads(marker.metadata_json or "{}").get("project") if marker else "v1") != (os.environ.get("COMPOSIO_PROJECT", "v1").strip() or "v1"):
                    continue
            previous_connected_account_id = row.connected_account_id if row else None
            previous_composio_user_id = row.composio_user_id if row else None
            email = row.email if row is not None else None
            if not email or recovering:
                try:
                    if multi:
                        from app.credits import metered
                        with metered.charge_sync(workspace_id=requested_workspace_id, purpose="connections.status", provider="composio", estimate_micro=0, payer="founder") as meter:
                            email = composio_gmail.get_gmail_profile(owner, ca_id).get("email")
                            meter.record_micro(0)
                    else:
                        email = composio_gmail.get_gmail_profile(owner, ca_id).get("email")
                except composio_gmail.ReconnectRequired:
                    reconnect.append(ca_id)
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.info("gmail profile skipped: %s", exc)
                    if recovering:
                        continue
            if multi and intent == "reconnect":
                target = session.get(GtmEmailAccount, mailbox_id) if mailbox_id else None
                if (target is None or target.workspace_id not in _gmail_workspace_ids(session, requested_workspace_id)
                        or target.provider != "gmail" or target.status == "revoked"
                        or not email or not target.email or target.email.casefold() != email.casefold()
                        or (row is not None and row.id != target.id)):
                    raise MailboxUnavailable("mailbox_reconnect_mismatch")
                row = target
                workspace_id = row.workspace_id
                previous_connected_account_id = row.connected_account_id
                previous_composio_user_id = row.composio_user_id
                row.connected_account_id = ca_id
                recovering = True
            if row is None and not multi:
                paused = list(session.scalars(_mailboxes_query(session, requested_workspace_id)
                    .where(GtmEmailAccount.status == "needs_reconnect")))
                if paused:
                    row = next((item for item in paused if email and (item.email or "").casefold() == email.casefold()), None)
                    if row is None and email and len(paused) == 1 and not paused[0].email:
                        row = paused[0]
                    if row is None:
                        continue
                    previous_connected_account_id = row.connected_account_id
                    previous_composio_user_id = row.composio_user_id
                    row.connected_account_id = ca_id
            fresh = row is None
            if fresh:
                row = GtmEmailAccount(
                    id=new_id("gtmba"),
                    workspace_id=workspace_id,
                    app_user_id=user_id,
                    provider="gmail",
                    email=email,
                    composio_user_id=owner,
                    connected_account_id=ca_id,
                    daily_send_limit=DEFAULT_DAILY_CAP,
                    sent_today=0,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                newly_connected.append(row.id)
            else:
                if row.workspace_id not in _gmail_workspace_ids(session, requested_workspace_id):
                    continue
                if row.status != "active":
                    newly_connected.append(row.id)
                # A fresh connection becomes the sending mailbox; a plain refresh never reorders existing ones.
                if connections is not None or row.status != "active":
                    row.updated_at = now
                row.status = "active"
                row.email = email or row.email
                row.composio_user_id = owner
                row.app_user_id = user_id
            if connections is not None or fresh or recovering or previous_connected_account_id != ca_id:
                project = os.environ.get("COMPOSIO_PROJECT", "v1").strip() or "v1"
                marker_key = f"composio-project:{row.id}:{ca_id}:{project}"
                if not session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == marker_key)):
                    session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=row.workspace_id, email_account_id=row.id,
                        event_type="composio_project", idempotency_key=marker_key,
                        metadata_json=json.dumps({"project": project, "auth_config_id": account.get("auth_config_id") or (account.get("auth_config") or {}).get("id") or os.environ.get(composio_gmail.AUTH_CONFIG_ENV),
                            "composio_user_id": owner, "previous_composio_user_id": previous_composio_user_id,
                            "previous_connected_account_id": previous_connected_account_id, "connected_account_id": ca_id}), created_at=now))
                for job in session.scalars(select(GtmEmailSendQueue).where(GtmEmailSendQueue.email_account_id == row.id,
                        GtmEmailSendQueue.status == "queued", GtmEmailSendQueue.last_error == "gmail_reconnect_required")):
                    job.last_error = None
                resume_gmail_work(session, row)
                gmail_notice(session, row, reconnected=True)
            saved.append(row.id)
            if multi:
                for wid in _gmail_workspace_ids(session, requested_workspace_id):
                    workspace = session.get(Workspace, wid)
                    if workspace is not None and workspace.default_gmail_mailbox_id is None:
                        workspace.default_gmail_mailbox_id = row.id
        session.commit()
    for ca_id in reconnect:
        mark_gmail_reconnect(ca_id)
    from app.analytics.funnel import emit_for_venture
    for account_id in newly_connected:  # once per mailbox, however often the connection is re-synced
        emit_for_venture("gmail.connected", None, key=f"gmail:{account_id}", workspace_id=workspace_id)
    return {"accounts": list_mailboxes(requested_workspace_id), "synced": len(saved)}


def _reset_daily_if_needed(account: GtmEmailAccount) -> None:
    today = utcnow().date().isoformat()
    if account.sent_count_date != today:
        account.sent_count_date = today
        account.sent_today = 0


def _record_event(
    session: Any,
    *,
    contact_id: str,
    account_id: str | None,
    day: int,
    event_type: str,
    idempotency_key: str | None = None,
    provider_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        GtmEmailEvent(
            id=new_id("gtmev"),
            contact_id=contact_id,
            email_account_id=account_id,
            sequence_day=day,
            event_type=event_type,
            provider_message_id=provider_message_id,
            idempotency_key=idempotency_key,
            metadata_json=json.dumps(metadata or {}),
            created_at=utcnow(),
        )
    )


def _queue_day(
    session: Any,
    *,
    contact: GtmContact,
    account: GtmEmailAccount,
    day: int,
    scheduled_for: Any,
) -> GtmEmailSendQueue | None:
    key = f"{contact.id}:{day}"
    existing = session.scalar(
        select(GtmEmailSendQueue).where(GtmEmailSendQueue.idempotency_key == key)
    )
    if existing:
        return None
    job = GtmEmailSendQueue(
        id=new_id("gtmjq"),
        workspace_id=contact.workspace_id,
        contact_id=contact.id,
        email_account_id=account.id,
        sequence_day=day,
        status="queued",
        scheduled_for=scheduled_for,
        attempts=0,
        idempotency_key=key,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(job)
    if day != 1:
        queue_research_job(
            session,
            workspace_id=contact.workspace_id,
            contact_id=contact.id,
            scheduled_for=scheduled_for - timedelta(hours=6),
            kind="pre_send",
            sequence_day=day,
        )
    _record_event(
        session,
        contact_id=contact.id,
        account_id=account.id,
        day=day,
        event_type="SEND_QUEUED",
        idempotency_key=f"queue:{key}",
    )
    return job


def start_outreach(
    workspace_id: str,
    *,
    contact_ids: list[str] | None = None,
    auto_send: bool = False,
    approval_id: str | None = None,
    product_name: str | None = None,
    mailbox_id: str | None = None,
) -> dict[str, Any]:
    """Enroll contacts with sendable emails into Day 1. Tick immediately."""
    from app.gtm.localization.experiments import execution_context

    strategy_context = execution_context(workspace_id)
    if not strategy_context["allowed"]:
        return {
            "enrolled": [],
            "refused": [{"id": "*", "reason": "gtm_thesis_approval_required"}],
            "queued": 0,
            "sent": [],
            "blocker": strategy_context["reason"],
            "dry_run": _dry_run(),
        }
    thesis_strategy = (
        (strategy_context.get("thesis") or {}).get("strategy") or {}
        if strategy_context.get("mode") == "thesis"
        else {}
    )
    refused: list[dict[str, str]] = []
    enrolled: list[str] = []
    queue_ids: list[str] = []
    simulated = _dry_run()

    with db()() as session:
        from app.connections.mailboxes import enabled, resolve_send_mailbox
        if enabled():
            from app.tenancy import current_or_none
            scope = current_or_none()
            venture_id = (scope.venture_id or None) if scope is not None and scope.workspace_id == workspace_id else None
            ref = resolve_send_mailbox(session, workspace_id, venture_id=venture_id, mailbox_id=mailbox_id)
            mailbox = session.get(GtmEmailAccount, ref["mailbox_id"])
        else:
            mailbox = _active_mailbox(session, workspace_id)
        if mailbox is None and not simulated:
            return {
                "enrolled": [],
                "refused": [{"id": "*", "reason": "gmail_not_connected"}],
                "queued": 0,
                "sent": [],
                "blocker": "Connect Gmail before live sending. Drafts still wait in the contact list.",
                "dry_run": False,
            }
        if mailbox is None:
            mailbox = GtmEmailAccount(
                id=new_id("gtmba"),
                workspace_id=workspace_id,
                app_user_id=composio_gmail.default_user_id(workspace_id),
                provider="gmail",
                email="simulated@local",
                composio_user_id="simulated",
                connected_account_id=f"sim_{workspace_id}",
                daily_send_limit=DEFAULT_DAILY_CAP,
                sent_today=0,
                status="active",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(mailbox)
            session.flush()

        q = select(GtmContact).where(
            GtmContact.workspace_id == workspace_id,
            GtmContact.do_not_contact == 0,
        )
        if contact_ids:
            q = q.where(GtmContact.id.in_(contact_ids))
        contacts = list(session.scalars(q.order_by(GtmContact.created_at.desc()).limit(200)))
        now = utcnow()
        for contact in contacts:
            if not (contact.email or "").strip():
                refused.append({"id": contact.id, "reason": "no_email"})
                continue
            if not email_confidence_allowed(contact.email_confidence or "none"):
                refused.append({"id": contact.id, "reason": f"email_{contact.email_confidence}"})
                continue
            if email_suppressed(session, workspace_id, contact):
                refused.append({"id": contact.id, "reason": "suppressed"})
                continue
            already = session.scalar(
                select(GtmEmailEvent).where(
                    GtmEmailEvent.contact_id == contact.id,
                    GtmEmailEvent.event_type == "SENT",
                )
            )
            if already:
                refused.append({"id": contact.id, "reason": "already_contacted"})
                continue
            if not contact.dossier_hook:
                contact.dossier_hook = f"building {contact.role or 'their company'}"
            kind = contact.sequence_kind or sequence_kind_for(contact.source, contact.dossier_hook)
            hook = contact.dossier_hook
            repaired = False
            if kind == "warm":
                hook, repaired = repair_congrats_hook(hook, contact.dossier_json)
                contact.dossier_hook = hook
            notes = latest_snapshot_notes(contact.id, 1) or contact.dossier_json
            enrich = draft.parse_enrichment(contact.enrichment_json)
            msgs = draft.opengtm_messages_of(enrich)
            state: dict[str, Any] = {}
            try:
                state = json.loads(contact.sequence_state_json or "{}")
            except json.JSONDecodeError:
                state = {}
            stored = state.get("1") if isinstance(state.get("1"), dict) else None
            if stored and stored.get("subject") and stored.get("body"):
                step = {
                    "subject": stored["subject"],
                    "body": stored["body"],
                    "hook": stored.get("hook") or hook,
                    "label": stored.get("label") or "congrats",
                    "source": stored.get("source") or "stored",
                }
            else:
                company_name = None
                if contact.company_id:
                    co = session.get(GtmCompany, contact.company_id)
                    company_name = co.canonical_name if co else None
                step = draft.draft_step(
                    first_name=contact.first_name,
                    email=contact.email,
                    company=company_name,
                    title=contact.role,
                    hook=hook,
                    linkedin_url=contact.linkedin_url,
                    research_notes=notes,
                    day=1,
                    product_name=product_name,
                    sequence_kind=kind,
                    congrats_repaired=repaired,
                    opengtm_messages=msgs or None,
                    thesis_context=thesis_strategy or None,
                    workspace_id=workspace_id,
                    contact_evidence=enrich.get("contact_evidence"),
                )
                state["1"] = {
                    "subject": step["subject"],
                    "body": step["body"],
                    "hook": step["hook"],
                    "label": step["label"],
                    "source": step.get("source"),
                    "pattern": step.get("pattern"),
                    "llm_status": step.get("llm_status"),
                    "llm_provider": step.get("llm_provider"),
                    "llm_model": step.get("llm_model"),
                    "llm_usage_provenance": step.get("llm_usage_provenance"),
                }
                # Prefill remaining days from OpenGTM when available.
                if msgs:
                    seeded = draft.seed_sequence_from_opengtm(
                        msgs,
                        first=contact.first_name,
                        company=company_name,
                        hook=hook,
                    )
                    for day_key, blob in seeded.items():
                        if day_key == "1":
                            continue
                        cur = state.get(day_key)
                        if isinstance(cur, dict) and cur.get("sent_at"):
                            continue
                        state[day_key] = blob
                contact.sequence_state_json = json.dumps(state)
            if not contact.sequence_state_json:
                contact.sequence_state_json = json.dumps(state)
            elif "1" in state and not (stored and stored.get("body")):
                contact.sequence_state_json = json.dumps(state)
            contact.owner_mailbox_id = mailbox.id
            job = _queue_day(
                session, contact=contact, account=mailbox, day=1, scheduled_for=now
            )
            if job:
                enrolled.append(contact.id)
                queue_ids.append(job.id)
            else:
                enrolled.append(contact.id)
        session.commit()

    tick: dict[str, Any] = {"processed": []}
    if auto_send:
        tick = tick_send_queue(workspace_id, limit=MAX_TICK, approval_id=approval_id,
            **({"mailbox_id": ref["mailbox_id"]} if enabled() else {}))
    return {
        "enrolled": enrolled,
        "queue_ids": queue_ids,
        "refused": refused,
        "queued": len(enrolled),
        "sent": tick.get("processed") or [],
        "dry_run": simulated,
        "blocker": None,
        "linkedin_dm": False,
    }


def _next_day(day: int) -> int | None:
    return {1: 2, 2: 4, 4: 7}.get(day)


def tick_send_queue(
    workspace_id: str,
    *,
    limit: int = MAX_TICK,
    respect_stop: bool = False,
    approval_id: str | None = None,
    mailbox_id: str | None = None,
) -> dict[str, Any]:
    """Process due send jobs with the mailbox daily cap (anti-flag)."""
    from app.connections.composio_migration import reconcile_gmail_project
    if not is_local():
        reconcile_gmail_project(workspace_id)
    processed: list[dict[str, Any]] = []
    skipped: list[str] = []
    simulated = _dry_run()
    from app.gtm.localization.experiments import execution_context
    from app.connections.mailboxes import MailboxUnavailable, enabled, list_read_mailboxes, resolve_send_mailbox
    multi = enabled()
    if is_local():
        from app.gtm import local_mail
        if not local_mail.enabled("send_email"):
            return {"processed": [], "skipped": ["send_email_disabled"], "dry_run": True, "remaining": _queue_remaining(workspace_id),
                "message": local_mail.blocked_message("send_email")}

    strategy_context = execution_context(workspace_id)
    if not strategy_context["allowed"]:
        return {
            "processed": [],
            "skipped": ["gtm_thesis_approval_required"],
            "blocker": strategy_context["reason"],
            "dry_run": simulated,
            "remaining": _queue_remaining(workspace_id),
        }
    if respect_stop and not is_local() and not gtm_factory.get_or_create_state(workspace_id)["send_enabled"]:
        return {"processed": [], "skipped": ["send_stopped"], "dry_run": simulated, "remaining": _queue_remaining(workspace_id)}

    for _ in range(max(1, min(limit, MAX_TICK))):
        with db()() as session:
            mailboxes = []
            if multi:
                refs = ([resolve_send_mailbox(session, workspace_id, mailbox_id=mailbox_id)]
                        if mailbox_id is not None else list_read_mailboxes(session, workspace_id))
                for ref in refs:
                    try:
                        resolve_send_mailbox(session, workspace_id, mailbox_id=ref["mailbox_id"])
                    except MailboxUnavailable:
                        continue
                    mailboxes.append(session.get(GtmEmailAccount, ref["mailbox_id"]))
            else:
                mailbox = _active_mailbox(session, workspace_id)
                if mailbox is not None:
                    mailboxes.append(mailbox)
            if not mailboxes:
                return {
                    "processed": processed,
                    "skipped": skipped + ["gmail_reconnect_required" if session.scalar(
                        _mailboxes_query(session, workspace_id).where(GtmEmailAccount.status == "needs_reconnect")) else "no_mailbox"],
                    "dry_run": simulated,
                    "remaining": _queue_remaining(workspace_id),
                }
            eligible_ids = []
            for mailbox in mailboxes:
                _reset_daily_if_needed(mailbox)
                if composio_gmail.mailbox_slots_used_today(session, mailbox) < mailbox_send_max(session, mailbox):
                    eligible_ids.append(mailbox.id)
            if not eligible_ids:
                skipped.append("daily_cap")
                session.commit()
                break
            now = utcnow()
            job = session.scalar(
                select(GtmEmailSendQueue)
                .where(
                    GtmEmailSendQueue.status == "queued",
                    GtmEmailSendQueue.scheduled_for <= now,
                    GtmEmailSendQueue.email_account_id.in_(eligible_ids) if multi else GtmEmailSendQueue.email_account_id == mailbox.id,
                    GtmEmailSendQueue.workspace_id == workspace_id,
                )
                .order_by(GtmEmailSendQueue.scheduled_for.asc())
                .limit(1)
            )
            if job is None:
                break
            job.status = "sending"
            job.locked_at = now
            job.attempts += 1
            job.updated_at = now
            session.commit()
            job_id = job.id

        result = _send_one(job_id, simulated=simulated, approval_id=approval_id)
        processed.append(result)
        if result.get("error") or result.get("skipped"):
            skipped.append(str(result.get("reason") or result.get("error") or "skipped"))
            # An approval is one-time and bound to this send. Never continue a
            # batch after it was absent, invalid, or consumed by another job.
            if result.get("error") == "gmail_reconnect_required" or (not simulated and str(result.get("reason") or result.get("error") or "").startswith("external_approval")):
                break

    return {
        "processed": processed,
        "skipped": skipped,
        "dry_run": simulated,
        "remaining": _queue_remaining(workspace_id),
    }


def _queue_remaining(workspace_id: str) -> int:
    with db()() as session:
        rows = session.scalars(
            select(GtmEmailSendQueue).where(
                GtmEmailSendQueue.workspace_id == workspace_id,
                GtmEmailSendQueue.status == "queued",
            )
        ).all()
        return len(list(rows))


def _send_one(
    job_id: str, *, simulated: bool, approval_id: str | None = None
) -> dict[str, Any]:
    if is_local() and not simulated:
        from app.gtm import local_mail
        if not local_mail.enabled("send_email"):
            return {"skipped": True, "reason": "send_email_disabled", "message": local_mail.blocked_message("send_email")}
    from app.autopilot.send_admission import try_send_admission
    from app.gtm.relationships.cadence import company_block
    from app.db.models import GtmOutreachSettings
    with db()() as session:
        job = session.get(GtmEmailSendQueue, job_id)
        contact = session.get(GtmContact, job.contact_id) if job else None
        if contact is None:
            return {"skipped": True, "reason": "missing"}
        workspace_id, company_id = contact.workspace_id, contact.company_id or contact.id
    with try_send_admission(f"company:{workspace_id}:{company_id}") as admitted:
        if not admitted:
            return {"skipped": True, "reason": "company_contact_hold", "contact_id": contact.id}
        with db()() as session:
            job, contact = session.get(GtmEmailSendQueue, job_id), session.get(GtmContact, contact.id)
            settings = session.get(GtmOutreachSettings, workspace_id)
            blocked = "send_paused" if settings and not settings.send_enabled else company_block(session, workspace_id, contact)
            if blocked:
                job.status, job.last_error, job.locked_at, job.updated_at = "queued", blocked, None, utcnow()
                session.commit()
                return {"skipped": True, "reason": blocked, "contact_id": contact.id}
        return _send_one_admitted(job_id, simulated=simulated, approval_id=approval_id)


def _send_one_admitted(job_id: str, *, simulated: bool, approval_id: str | None = None) -> dict[str, Any]:
    with db()() as session:
        job = session.get(GtmEmailSendQueue, job_id)
        if job is None:
            return {"skipped": True, "reason": "job_missing"}
        contact = session.get(GtmContact, job.contact_id)
        account = session.get(GtmEmailAccount, job.email_account_id)
        if contact is not None and account is not None and account.status == "needs_reconnect":
            job.status, job.last_error, job.locked_at = "queued", "gmail_reconnect_required", None
            session.commit()
            return {"skipped": True, "reason": "gmail_reconnect_required", "contact_id": contact.id}
        if contact is None or account is None:
            job.status = "failed"
            job.last_error = "Contact or mailbox missing"
            session.commit()
            return {"skipped": True, "reason": "missing"}
        from app.connections.mailboxes import MailboxUnavailable, enabled, mailbox_for_conversation, resolve_send_mailbox
        multi = enabled()
        if multi:
            try:
                if job.workspace_id != contact.workspace_id:
                    raise MailboxUnavailable("mailbox_scope_mismatch")
                resolve_send_mailbox(session, contact.workspace_id, mailbox_id=job.email_account_id)
                if job.sequence_day > 1:
                    original = mailbox_for_conversation(session, contact_id=contact.id, thread_id=contact.gmail_thread_id)
                    if original is None or original["mailbox_id"] != job.email_account_id:
                        raise MailboxUnavailable("conversation_unavailable")
            except MailboxUnavailable:
                job.status, job.last_error, job.locked_at, job.updated_at = "queued", "mailbox_unavailable", None, utcnow()
                session.commit()
                return {"skipped": True, "reason": "mailbox_unavailable", "contact_id": contact.id}
        if contact.workspace_id not in _gmail_workspace_ids(session, account.workspace_id):
            return {"skipped": True, "reason": "mailbox_scope_mismatch"}
        if email_suppressed(session, contact.workspace_id, contact):
            job.status = "skipped"
            session.commit()
            return {"skipped": True, "reason": "stopped", "contact_id": contact.id}

        # This worker owns a job, not the request-scoped strategy context used
        # when it was queued. Resolve the current approved thesis for the
        # mailbox workspace before creating a missing follow-up draft.
        from app.gtm.localization.experiments import execution_context

        execution = execution_context(contact.workspace_id)
        thesis_strategy = (
            (execution.get("thesis") or {}).get("strategy") or {}
            if execution.get("mode") == "thesis"
            else {}
        )

        day = job.sequence_day
        state = {}
        try:
            state = json.loads(contact.sequence_state_json or "{}")
        except json.JSONDecodeError:
            state = {}
        stored = state.get(str(day)) if isinstance(state.get(str(day)), dict) else None
        notes = latest_snapshot_notes(contact.id, day) or contact.dossier_json
        kind = contact.sequence_kind or "cold"
        hook = contact.dossier_hook
        repaired = False
        if kind == "warm" and day == 1:
            hook, repaired = repair_congrats_hook(hook, notes)
        enrich = draft.parse_enrichment(contact.enrichment_json)
        msgs = draft.opengtm_messages_of(enrich)
        company_name = None
        if contact.company_id:
            co = session.get(GtmCompany, contact.company_id)
            company_name = co.canonical_name if co else None
        # Prefer any pre-seeded OpenGTM / saved body for this day (not only day 1).
        if stored and stored.get("subject") and stored.get("body"):
            step = {
                "subject": stored["subject"],
                "body": stored["body"],
                "hook": stored.get("hook") or hook,
            }
        else:
            drafted = draft.draft_step(
                first_name=contact.first_name,
                email=contact.email,
                company=company_name,
                title=contact.role,
                hook=hook,
                linkedin_url=contact.linkedin_url,
                research_notes=notes,
                day=day,
                sequence_kind=kind,
                congrats_repaired=repaired,
                opengtm_messages=msgs or None,
                thesis_context=thesis_strategy or None,
                workspace_id=contact.workspace_id,
                contact_evidence=enrich.get("contact_evidence"),
            )
            step = drafted
            state[str(day)] = {
                "subject": drafted["subject"],
                "body": drafted["body"],
                "hook": drafted["hook"],
                "label": drafted["label"],
                "source": drafted.get("source"),
                "pattern": drafted.get("pattern"),
                "llm_status": drafted.get("llm_status"),
                "llm_provider": drafted.get("llm_provider"),
                "llm_model": drafted.get("llm_model"),
                "llm_usage_provenance": drafted.get("llm_usage_provenance"),
            }
            contact.sequence_state_json = json.dumps(state)

        provider_id = None
        sent = {}
        thread_id = contact.gmail_thread_id
        from app.gtm.relationships.email_compliance import complete_body
        step["body"] = complete_body(session, contact.workspace_id, contact, step["body"], account.email or "")
        if simulated or account.connected_account_id.startswith("sim_"):
            provider_id = f"sim_{job.id}"
        else:
            from app.approvals.external import DefinitiveProviderFailure, ProviderReconnectRequired, OutboundExecutionError, execute_metered_external_action

            if not composio_gmail.configured():
                job.status = "queued"
                job.last_error = "outbound_provider_credentials_unavailable"
                job.locked_at = None
                job.updated_at = utcnow()
                session.commit()
                return {
                    "skipped": True,
                    "reason": "outbound_provider_credentials_unavailable",
                    "contact_id": contact.id,
                    "day": day,
                }

            target = {
                "queue_id": job.id,
                "recipient": contact.email or "",
                "sequence_day": day,
                "subject": step["subject"],
                "body": step["body"],
            }
            rejected_reason = None

            def deliver():
                nonlocal rejected_reason
                try:
                    return composio_gmail.send_gmail_email(composio_user_id=account.composio_user_id,
                        connected_account_id=account.connected_account_id, to=contact.email or "",
                        subject=step["subject"], body=step["body"], thread_id=thread_id if day == 2 else None,
                        workspace_id=contact.workspace_id)
                except composio_gmail.DefiniteFailure as exc:
                    rejected_reason = str(exc)
                    raise (ProviderReconnectRequired if isinstance(exc, composio_gmail.ReconnectRequired) else DefinitiveProviderFailure)(rejected_reason) from exc

            try:
                session.commit()
                outcome = execute_metered_external_action(
                    workspace_id=contact.workspace_id,
                    approval_id=approval_id,
                    action="gtm_email_send",
                    provider="smtp" if is_local() else "composio_gmail",
                    target=target,
                    invoke=deliver,
                )
            except OutboundExecutionError as exc:
                job.status = "queued"
                job.last_error = exc.code
                job.locked_at = None
                job.updated_at = utcnow()
                session.commit()
                return {"skipped": True, "reason": exc.code, "contact_id": contact.id, "day": day,
                    **({"message": str(exc)} if is_local() else {})}
            if outcome.status != "succeeded":
                job.status = "queued" if outcome.status == "paused" or (rejected_reason and outcome.status == "failed_definitive") else "failed"
                job.locked_at = None
                job.last_error = rejected_reason or outcome.code
                job.updated_at = utcnow()
                session.commit()
                return {
                    "skipped": True,
                    "error": rejected_reason or outcome.code,
                    "contact_id": contact.id,
                    "day": day,
                    **({"message": "Open your Sent mailbox and check this email's delivery before trying again."
                        if outcome.status == "recovery_required" else "Open Keys and connections > Email and check your app password and recipient address, then review the email again in Find customers."} if is_local() else {}),
                }
            sent = outcome.provider_result if isinstance(outcome.provider_result, dict) else {}
            provider_id = sent.get("provider_message_id")
            thread_id = sent.get("thread_id") or thread_id

        now = utcnow()
        job.status = "sent"
        job.updated_at = now
        _reset_daily_if_needed(account)
        if simulated:
            account.sent_today = int(account.sent_today or 0) + 1
        contact.last_emailed_at = now
        contact.times_emailed = int(contact.times_emailed or 0) + 1
        contact.gmail_thread_id = thread_id
        state[str(day)] = {
            **(state.get(str(day)) or {}),
            "subject": step["subject"],
            "body": step["body"],
            "sent_at": now.isoformat(),
            "simulated": bool(simulated or str(provider_id or "").startswith("sim_")),
        }
        contact.sequence_state_json = json.dumps(state)
        _record_event(
            session,
            contact_id=contact.id,
            account_id=account.id,
            day=day,
            event_type="SENT",
            idempotency_key=f"sent:{contact.id}:{day}",
            provider_message_id=provider_id,
            metadata={"thread_id": thread_id, "rfc_message_id": sent.get("rfc_message_id") if not simulated else None, "simulated": simulated},
        )
        session.add(
            GtmTouch(
                id=new_id("gtmtch"),
                workspace_id=contact.workspace_id,
                contact_id=contact.id,
                mailbox_id=None,
                direction="out",
                email_account_id=account.id if multi else None,
                channel="email",
                message_id=(sent.get("rfc_message_id") if not simulated else None) or provider_id,
                provider_thread_id=thread_id,
                subject=step["subject"],
                body_text=step["body"],
                sent_at=now,
                simulated=1 if simulated else 0,
                send_mode="founder_gmail" if not simulated else None,
                created_at=now,
            )
        )
        nxt = _next_day(day)
        if nxt:
            day1_sent = now if day == 1 else contact.last_emailed_at or now
            scheduled = day1_sent + timedelta(days=DAY_OFFSETS[nxt])
            _queue_day(
                session,
                contact=contact,
                account=account,
                day=nxt,
                scheduled_for=scheduled,
            )
        if not simulated and not str(provider_id or "").startswith("sim_"):
            from app.gtm.email_learning import record
            record(session, contact, "send", provider_id, observed_at=now)
        session.commit()
        learning.record(
            contact.workspace_id,
            "sent",
            contact_id=contact.id,
            channel="email",
            payload={"day": day, "simulated": simulated},
        )
        return {
            "skipped": False,
            "contact_id": contact.id,
            "email": contact.email,
            "day": day,
            "subject": step["subject"],
            "simulated": simulated or str(provider_id or "").startswith("sim_"),
            "provider_message_id": provider_id,
        }


def execute_approved_queue(
    workspace_id: str, queue_id: str, *, approval_id: str | None
) -> dict[str, Any]:
    """Run exactly one reviewed queue item; never sweep a neighbouring draft."""

    from app.approvals.external import execution_enabled

    if is_local():
        from app.gtm import local_mail
        if not local_mail.enabled("send_email"):
            return {"skipped": True, "reason": "send_email_disabled", "message": local_mail.blocked_message("send_email")}
    elif not execution_enabled():
        return {"skipped": True, "reason": "external_execution_disabled"}
    if _dry_run():
        return {"skipped": True, "reason": "outbound_channel_dry_run"}

    with db()() as session:
        job = session.get(GtmEmailSendQueue, queue_id)
        account = session.get(GtmEmailAccount, job.email_account_id) if job else None
        if (job is None or account is None or job.workspace_id != workspace_id
                or account.workspace_id not in _gmail_workspace_ids(session, workspace_id)):
            return {"skipped": True, "reason": "outbound_queue_not_found"}
        if job.status != "queued":
            return {"skipped": True, "reason": "outbound_queue_not_reviewable"}
        job.status = "sending"
        job.locked_at = utcnow()
        job.attempts += 1
        job.updated_at = utcnow()
        session.commit()
    return _send_one(queue_id, simulated=_dry_run(), approval_id=approval_id)


def get_contact(workspace_id: str, contact_id: str) -> dict[str, Any] | None:
    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        if contact is None or contact.workspace_id != workspace_id:
            return None
        company = session.get(GtmCompany, contact.company_id) if contact.company_id else None
        payload = _contact_payload(contact, company)
        from app.db.models import GtmResearchSnapshot, GtmLinkedInQueue

        snaps = list(
            session.scalars(
                select(GtmResearchSnapshot)
                .where(GtmResearchSnapshot.contact_id == contact_id)
                .order_by(GtmResearchSnapshot.created_at.desc())
                .limit(3)
            )
        )
        payload["snapshots"] = [
            {
                "id": s.id,
                "day": s.sequence_day,
                "richness": s.richness,
                "notes": s.notes[:4000],
                "created_at": s.created_at.isoformat() if s.created_at else "",
            }
            for s in snaps
        ]
        li = list(
            session.scalars(
                select(GtmLinkedInQueue)
                .where(GtmLinkedInQueue.contact_id == contact_id)
                .order_by(GtmLinkedInQueue.scheduled_for.asc())
            )
        )
        payload["linkedin_jobs"] = [
            {
                "id": j.id,
                "step": j.step,
                "status": j.status,
                "body": j.body,
                "scheduled_for": j.scheduled_for.isoformat() if j.scheduled_for else "",
            }
            for j in li
        ]
        return payload


def update_contact(workspace_id: str, contact_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        if contact is None or contact.workspace_id != workspace_id:
            return None
        if "hook" in patch and patch["hook"] is not None:
            contact.dossier_hook = str(patch["hook"])[:400]
        if "preferred_channel" in patch and patch["preferred_channel"] in {
            "auto",
            "email",
            "linkedin",
            "both",
        }:
            contact.preferred_channel = patch["preferred_channel"]
        if "sequence_kind" in patch and patch["sequence_kind"] in {"warm", "cold"}:
            contact.sequence_kind = patch["sequence_kind"]
        if "do_not_contact" in patch:
            contact.do_not_contact = 1 if patch["do_not_contact"] else 0
        seq_patch = patch.get("sequence")
        if isinstance(seq_patch, dict):
            try:
                state = json.loads(contact.sequence_state_json or "{}")
            except json.JSONDecodeError:
                state = {}
            for day, body in seq_patch.items():
                if not isinstance(body, dict):
                    continue
                cur = state.get(str(day)) if isinstance(state.get(str(day)), dict) else {}
                if body.get("subject"):
                    cur["subject"] = str(body["subject"])[:200]
                if body.get("body"):
                    cur["body"] = str(body["body"])[:8000]
                state[str(day)] = cur
            contact.sequence_state_json = json.dumps(state)
        session.commit()
    return get_contact(workspace_id, contact_id)


def regenerate_contact_draft(workspace_id: str, contact_id: str, day: int = 1) -> dict[str, Any] | None:
    from app.gtm.localization.experiments import execution_context

    execution = execution_context(workspace_id)
    thesis_strategy = (
        (execution.get("thesis") or {}).get("strategy") or {}
        if execution.get("mode") == "thesis"
        else {}
    )
    with db()() as session:
        contact = session.get(GtmContact, contact_id)
        if contact is None or contact.workspace_id != workspace_id:
            return None
        notes = latest_snapshot_notes(contact.id, day) or contact.dossier_json
        kind = contact.sequence_kind or "cold"
        hook, repaired = contact.dossier_hook, False
        if kind == "warm":
            hook, repaired = repair_congrats_hook(hook, notes)
        company_name = None
        if contact.company_id:
            co = session.get(GtmCompany, contact.company_id)
            company_name = co.canonical_name if co else None
        msgs = draft.opengtm_messages_of(draft.parse_enrichment(contact.enrichment_json))
        step = draft.draft_step(
            first_name=contact.first_name,
            email=contact.email,
            company=company_name,
            title=contact.role,
            hook=hook,
            linkedin_url=contact.linkedin_url,
            research_notes=notes,
            day=day,
            sequence_kind=kind,
            congrats_repaired=repaired,
            opengtm_messages=msgs or None,
            thesis_context=thesis_strategy or None,
            workspace_id=workspace_id,
            contact_evidence=draft.parse_enrichment(contact.enrichment_json).get("contact_evidence"),
        )
        try:
            state = json.loads(contact.sequence_state_json or "{}")
        except json.JSONDecodeError:
            state = {}
        state[str(day)] = {
            "subject": step["subject"],
            "body": step["body"],
            "hook": step["hook"],
            "label": step["label"],
            "source": step.get("source"),
            "pattern": step.get("pattern"),
            "llm_status": step.get("llm_status"),
            "llm_provider": step.get("llm_provider"),
            "llm_model": step.get("llm_model"),
            "llm_usage_provenance": step.get("llm_usage_provenance"),
        }
        contact.sequence_state_json = json.dumps(state)
        session.commit()
    return get_contact(workspace_id, contact_id)


def apply_reply(contact_id: str, *, body: str | None = None, classification: str = "human_neutral",
                venture_id: str | None = None, provider_thread_id: str | None = None,
                provider_message_id: str | None = None, reply_outcome: str | None = None,
                in_reply_to: str | None = None, references: str | None = None,
                email_account_id: str | None = None) -> bool:
    from app.gtm.supymem import workers
    from app.connections import mailboxes
    account_id = email_account_id if mailboxes.enabled() else None
    intent = workers._reply_intent(body or "")
    outcome = intent if intent in {"unsubscribe", "escalate"} else reply_outcome or intent or classification
    if classification == "automated" and outcome not in {"unsubscribe", "escalate"}:
        outcome = "automated"
    # Keep stored human classifications compatible with demand/report consumers; add the specific outcome.
    classification = {"wrong_person": "human_neutral", "question": "human_neutral", "not_now": "human_neutral", "escalate": "human_negative"}.get(classification, classification)
    if outcome in {"unsubscribe", "escalate", "opted_out"} and classification != "automated":
        classification = "human_negative"
    with db()() as session:
        if venture_id is not None:
            from app.db.session import serialize_writes
            serialize_writes(session, key=f"venture-reply:{venture_id}:{contact_id}:{provider_message_id}")
        contact = session.get(GtmContact, contact_id)
        if contact is None:
            return False
        dedupe_ids = [provider_message_id]
        legacy_message_id = None
        if account_id is None and provider_message_id:
            legacy_account = _legacy_active_mailbox(session, contact.workspace_id)
            if legacy_account is not None:
                legacy_message_id = workers._mailbox_message_key(legacy_account.id, provider_message_id)
                dedupe_ids.append(legacy_message_id)
        account_touch_ids = None
        if account_id is not None:
            from app.db.session import serialize_writes
            serialize_writes(session, key=f"mailbox-reply:{account_id}:{provider_message_id}")
            refs = mailboxes.list_read_mailboxes(session, contact.workspace_id)
            if not any(ref["mailbox_id"] == account_id for ref in refs):
                raise mailboxes.MailboxUnavailable("mailbox_unavailable")
            if not isinstance(provider_message_id, str) or not provider_message_id:
                raise ValueError("reply_scope_unconfirmed")
            legacy_message_id = workers._mailbox_message_key(account_id, provider_message_id)
            receipt_key = "gmail_reply_attribution:" + legacy_message_id
            if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == receipt_key)):
                return False
            from sqlalchemy import or_
            if session.scalar(select(GtmTouch.id).where(GtmTouch.contact_id == contact_id, GtmTouch.direction == "in",
                    or_(GtmTouch.message_id == provider_message_id, GtmTouch.message_id == legacy_message_id),
                    or_(GtmTouch.email_account_id == account_id,
                        GtmTouch.email_account_id.is_(None) & (GtmTouch.message_id == legacy_message_id)))):
                return False
            outgoing = session.scalars(select(GtmTouch).where(GtmTouch.contact_id == contact_id,
                GtmTouch.workspace_id == contact.workspace_id, GtmTouch.direction == "out",
                GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None))).all()
            account_touch_ids = [touch.id for touch in workers._touches_for_mailbox(session, contact.workspace_id, account_id, outgoing)]
            legacy_replies = session.scalars(select(GtmTouch).where(GtmTouch.contact_id == contact_id,
                GtmTouch.direction == "in", GtmTouch.message_id == provider_message_id, GtmTouch.email_account_id.is_(None)))
            for legacy_reply in legacy_replies:
                previous = [touch for touch in outgoing if touch.venture_id == legacy_reply.venture_id
                    and (not legacy_reply.provider_thread_id or touch.provider_thread_id == legacy_reply.provider_thread_id)]
                if previous and len(workers._touches_for_mailbox(session, contact.workspace_id, account_id, previous)) == len(previous):
                    return False
        attributed = None
        if venture_id is not None:
            from app.db.models import Venture
            venture = session.get(Venture, venture_id)
            if venture is None or venture.workspace_id != contact.workspace_id or not provider_message_id:
                raise ValueError("reply_venture_scope_unconfirmed")
            references_ids = re.findall(r"<[^<>\s]+>", (in_reply_to or "") + " " + (references or ""))
            from sqlalchemy import or_
            metadata_ids = []
            for event in session.scalars(select(GtmEmailEvent).where(GtmEmailEvent.workspace_id == venture.workspace_id,
                    GtmEmailEvent.contact_id == contact_id, GtmEmailEvent.event_type == "sent_metadata")):
                try:
                    metadata = json.loads(event.metadata_json or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(metadata, dict) or metadata.get("rfc_message_id") not in references_ids or not event.idempotency_key:
                    continue
                if account_id is None and event.metadata_json != json.dumps({"rfc_message_id": metadata["rfc_message_id"]}):
                    continue
                touch_id = event.idempotency_key.removeprefix("sent-metadata:")
                metadata_ids.append(touch_id.removeprefix("conversation-mailbox:") if account_id is not None else touch_id)
            matched = or_(GtmTouch.provider_thread_id == provider_thread_id if provider_thread_id else False,
                GtmTouch.message_id.in_(references_ids), GtmTouch.id.in_(metadata_ids))
            if metadata_ids and account_id is not None:
                matched = or_(GtmTouch.message_id.in_(references_ids), GtmTouch.id.in_(metadata_ids))
            if account_touch_ids is not None:
                from sqlalchemy import and_
                matched = and_(matched, GtmTouch.id.in_(account_touch_ids))
            sent = session.scalar(select(GtmTouch.id).where(
                GtmTouch.venture_id == venture_id, GtmTouch.workspace_id == venture.workspace_id,
                GtmTouch.contact_id == contact_id, GtmTouch.direction == "out", GtmTouch.simulated == 0,
                GtmTouch.sent_at.is_not(None), matched))
            if sent is None:
                raise ValueError("reply_thread_scope_unconfirmed")
            thread_owners = set(session.scalars(select(GtmTouch.venture_id).where(
                GtmTouch.workspace_id == venture.workspace_id, GtmTouch.contact_id == contact_id,
                GtmTouch.direction == "out", GtmTouch.simulated == 0, GtmTouch.sent_at.is_not(None),
                matched)))
            if thread_owners != {venture_id}:
                raise ValueError("reply_thread_scope_ambiguous")
            if session.scalar(select(GtmTouch.id).where(GtmTouch.venture_id == venture_id,
                    GtmTouch.contact_id == contact_id, GtmTouch.direction == "in",
                    GtmTouch.message_id.in_(dedupe_ids),
                    or_(GtmTouch.provider_thread_id == provider_thread_id,
                        GtmTouch.message_id == legacy_message_id if legacy_message_id is not None else False),
                    GtmTouch.email_account_id == account_id if account_id is not None else True)):
                return False
            attributed = venture_id
        if venture_id is None and provider_message_id and session.scalar(select(GtmTouch.id).where(
                GtmTouch.contact_id == contact_id, GtmTouch.direction == "in", GtmTouch.message_id.in_(dedupe_ids),
                GtmTouch.email_account_id == account_id if account_id is not None else True)):
            return False
        now = utcnow()
        contact.replied_at = now
        contact.relationship_stage = (
            "cooling" if classification in {"human_negative", "automated"} or outcome == "not_now" else "conversing"
        )
        for job in session.scalars(
            select(GtmEmailSendQueue).where(
                GtmEmailSendQueue.contact_id == contact_id,
                GtmEmailSendQueue.status.in_(("queued", "sending")),
            )
        ):
            job.status = "skipped"
            job.updated_at = now
        from app.db.models import GtmLinkedInQueue

        for job in session.scalars(
            select(GtmLinkedInQueue).where(
                GtmLinkedInQueue.contact_id == contact_id,
                GtmLinkedInQueue.status == "queued",
            )
        ):
            job.status = "skipped"
            job.updated_at = now
        touch = GtmTouch(
                id=new_id("gtmtch"),
                workspace_id=contact.workspace_id,
                venture_id=attributed,
                provider_thread_id=provider_thread_id,
                message_id=provider_message_id,
                in_reply_to=in_reply_to,
                contact_id=contact.id,
                mailbox_id=None,
                direction="in",
                email_account_id=account_id,
                channel="email",
                body_text=(body or "")[:4000],
                sent_at=now,
                classification=classification,
                outcome=outcome,
                simulated=0,
                created_at=now,
            )
        session.add(touch)
        if account_id is not None:
            workers._record_read_event(session, contact.workspace_id, account_id, "gmail_reply_attribution", provider_message_id,
                contact_id=contact_id, metadata={"reply_id": touch.id, "thread_id": provider_thread_id})
        if attributed and classification != "automated":
            from app.tasks.service import emit_domain_event
            emit_domain_event(session, workspace_id=contact.workspace_id, venture_id=attributed,
                              event_type="outreach.reply", event_id=touch.id, data={"classification": classification})
        evidence_event_key = f"gtm.reply:{touch.id}" if attributed and classification in {
            "human_positive", "human_neutral", "human_negative"} else None
        if classification != "automated":
            from app.gtm.email_learning import record
            record(session, contact, "reply", touch.id, observed_at=now)
        if outcome in {"unsubscribe", "opted_out", "escalate"}:
            from app.gtm.email_learning import suppress
            suppress(session, contact.email)
        session.commit()
        if outcome in {"unsubscribe", "opted_out", "escalate"}:
            from app.autopilot import outreach
            outreach.record_opt_out(contact.workspace_id, contact.id, contact.email or "")
        if attributed:
            from app.gtm import contact_work
            if outcome == "wrong_person":
                contact_work._put(attributed, contact.id, "referral_ask", contact_work.REFERRAL_ASK)
            elif outcome == "not_now":
                due = contact_work.follow_up_date(body or "", now)
                data = draft.parse_enrichment(contact.enrichment_json)
                data["follow_up_not_before"] = due.isoformat() if due else None
                contact.enrichment_json = json.dumps(data)
                session.commit()
                contact_work._put(attributed, contact.id, "follow_up", f"Follow up no earlier than {due.date().isoformat()}; review and send it yourself." if due else
                    "No clear follow-up date was given. Ask permission in Gmail, then log their reply with a date such as YYYY-MM-DD. Nothing is scheduled or sent.")
            elif outcome == "escalate":
                contact_work._put(attributed, contact.id, "reply_attention", "Contact stopped after an angry or legal reply. Review the conversation in Gmail and consult your adviser; do not send another outreach message.")
                from app.needs_you.service import request_human
                request_human(attributed, reason_code="gtm_reply_escalated", title="Review a sensitive outreach reply",
                    what_we_need="This person raised a legal or angry objection. Contact is stopped; no reply will be sent.",
                    how_to_resume="1. Review the conversation in Gmail. 2. Consult your adviser if needed. 3. Skip this contact and choose another prospect.",
                    blocking_ref="gtm-reply:" + contact.id, actions=[{"label": "Review in Gmail", "href": "https://mail.google.com"}])
        if evidence_event_key:
            try:
                from app.ventures.stage import after_evidence
                after_evidence(attributed, evidence_event_key)
            except Exception:
                log.warning("founder_stage_reply_hook_failed")
        learning.record(
            contact.workspace_id,
            "reply_automated" if classification == "automated" else "reply_positive" if classification == "human_positive" else "reply_human",
            contact_id=contact.id,
            channel="email",
            payload={"classification": classification},
        )
        return True


def apply_bounce(contact_id: str, *, reason: str | None = None, provider_message_id: str | None = None, hard_bounce: bool | None = None) -> bool:
    with db()() as session:
        contact = session.get(GtmContact, contact_id, with_for_update=True)
        if contact is None:
            return False
        now = utcnow()
        if provider_message_id:
            from app.db.models import GtmEmailEvent
            from app.db.session import serialize_writes
            import hashlib

            key = "bounce:" + hashlib.sha256(json.dumps([contact.workspace_id, provider_message_id, (contact.email or "").strip().casefold()]).encode()).hexdigest()
            serialize_writes(session, key=key)
            if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
                return False
            session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=contact.workspace_id, contact_id=contact.id,
                                     event_type="bounce", provider_message_id=provider_message_id,
                                     idempotency_key=key, created_at=now))
        contact.bounced_at = contact.bounced_at or now
        contact.do_not_contact = 1
        contact.relationship_stage = "dormant"
        for job in session.scalars(
            select(GtmEmailSendQueue).where(
                GtmEmailSendQueue.contact_id == contact_id,
                GtmEmailSendQueue.status.in_(("queued", "sending")),
            )
        ):
            job.status = "skipped"
            job.updated_at = now
        from app.gtm.email_learning import record
        hard = hard_bounce if hard_bounce is not None else bool(re.search(r"\b(?:5\.1\.1|mailbox (?:does not|doesn't) exist|user unknown|no such (?:user|mailbox)|recipient (?:does not|doesn't) exist)\b", reason or "", re.I))
        record(session, contact, "hard_bounce" if hard else "soft_bounce", "bounce:" + contact.id, observed_at=contact.bounced_at)
        session.commit()
        # logic adapted from ethanplusai/harvey (MIT)
        from app.gtm.relationships.abuse import bounce_counts
        counts = bounce_counts(session, contact.workspace_id)
        sent, bounced = counts["sent"], counts["bounced"]
        if sent >= 10 and bounced / sent > 0.05:
            from app.gtm.relationships.abuse import pause_sends
            if pause_sends(contact.workspace_id, reason="bounce_rate")["reason"] != "bounce_rate":
                return True
            from app.db.models import Venture
            from app.needs_you.service import request_human
            for vid in session.scalars(select(Venture.id).where(Venture.workspace_id == contact.workspace_id)):
                request_human(vid, reason_code="gtm_bounce_pause", title="Email sending paused after bounces",
                    what_we_need=f"{bounced} contacts bounced across {sent} confirmed sends, above the 5% limit. Sending is paused across your workspace; drafts stay saved.",
                    how_to_resume="1. Check the bounced addresses first and skip invalid contacts. 2. Click Resume sending here or in Find customers. 3. Review and approve a fresh batch; nothing is sent by resuming.",
                    blocking_ref="gtm-bounce-pause", actions=[{"label": "Review Find customers", "href": f"/gtm?venture={vid}"}])
        return True
        learning.record(
            contact.workspace_id,
            "bounce",
            contact_id=contact.id,
            channel="email",
            payload={"reason": reason},
        )
