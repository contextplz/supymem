"""Founder-led outreach service: score, draft, copy ≠ sent, outcomes (OT-21 / OT-26)."""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import (
    GtmArtifactSku,
    GtmCompany,
    GtmContact,
    GtmEvidenceSource,
    GtmLinkedInQueue,
    GtmMarketProfile,
    GtmOutcome,
    GtmOutreachSettings,
    GtmSignal,
    GtmSuppression,
    GtmTouch,
)
from app.gtm.relationships.drafts import render_draft
from app.gtm.relationships.examples import is_labelled_example_contact
from app.gtm.relationships.funnel import funnel_metrics
from app.gtm.relationships.gates import HARVESTED_SOURCES
from app.gtm.relationships.icp_seed import get_segment
from app.gtm.relationships.scope import gtm_get
from app.gtm.relationships.score import ScoreInput, score_contact
from app.gtm.relationships.skus import SkuAttachError, attach_sku, match_sku
from app.state import db

UNIPILE_LIVE = "unipile_live"


class OutreachError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _settings(session, workspace_id: str) -> GtmOutreachSettings:
    row = session.get(GtmOutreachSettings, workspace_id)
    if row is None:
        now = utcnow()
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise RuntimeError("unsupported_outreach_settings_dialect")
        # Only this workspace primary-key conflict is benign. The winning
        # initializer/settings are retained; unrelated integrity errors escape.
        session.execute(insert(GtmOutreachSettings).values(
            workspace_id=workspace_id,
            onboarding_status="{}",
            outbound_mode="human_clipboard",
            created_at=now,
            updated_at=now,
        ).on_conflict_do_nothing(index_elements=[GtmOutreachSettings.workspace_id]))
        row = session.get(GtmOutreachSettings, workspace_id)
        if row is None:
            raise RuntimeError("outreach_settings_initialization_failed")
    return row


def _constraints(workspace_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.scalar(
            select(GtmMarketProfile)
            .where(GtmMarketProfile.workspace_id == workspace_id)
            .order_by(GtmMarketProfile.version.desc())
        )
        if row is None:
            return {}
        try:
            return json.loads(row.constraints_json or "{}")
        except json.JSONDecodeError:
            return {}


def onboarding_status(workspace_id: str) -> dict[str, Any]:
    from app.ventures import known
    if known.enabled():
        from app.gtm.relationships.icp_seed import seed_icp_template
        seed_icp_template(workspace_id)
    segment = get_segment(workspace_id)
    constraints = _constraints(workspace_id)
    voice = constraints.get("voice") or {}
    with db()() as session:
        settings = _settings(session, workspace_id)
        paste_exists = session.scalar(
            select(GtmSignal.id)
            .where(
                GtmSignal.workspace_id == workspace_id,
                GtmSignal.source.in_(
                    ["profile_paste", "job_post_paste", "thread_url_paste", "founder_note"]
                ),
            )
            .limit(1)
        )
        connector = session.scalar(
            select(GtmEvidenceSource.id)
            .where(
                GtmEvidenceSource.workspace_id == workspace_id,
                GtmEvidenceSource.status.in_(["started", "connected"]),
            )
            .limit(1)
        )
        from app.connections import mailboxes
        gmail_refs = mailboxes.list_read_mailboxes(session, workspace_id)
        gmail_acct = gmail_refs[0] if gmail_refs else None
        evidence_rows = list(
            session.scalars(
                select(GtmEvidenceSource).where(
                    GtmEvidenceSource.workspace_id == workspace_id
                )
            )
        )
        evidence_by_provider = {row.provider: row for row in evidence_rows}
        evidence_sources: list[dict[str, Any]] = []
        for provider in ("slack", "github", "gmail"):
            row = evidence_by_provider.get(provider)
            status = row.status if row else "absent"
            connected_account_id = row.connected_account_id if row else None
            if provider == "gmail" and gmail_acct is not None:
                status = "connected"
                connected_account_id = gmail_acct["connected_account_id"]
            evidence_sources.append(
                {
                    "provider": provider,
                    "status": status,
                    "connected_account_id": connected_account_id,
                    "fake": False,
                }
            )
            if provider == "gmail" and mailboxes.enabled():
                evidence_sources[-1]["mailboxes"] = [{"mailbox_id": ref["mailbox_id"], "status": ref["status"]}
                    for ref in gmail_refs]
                if not gmail_refs and status == "connected":
                    evidence_sources[-1].update(status="absent", connected_account_id=None)
        icp_who = (segment.get("who") or segment.get("sentence") or "").strip()
        icp = len(icp_who) >= 8
        excludes = "excludes" in segment
        sender_geo = bool((settings.sender_geo or "").strip())
        send_path = settings.outbound_mode in {"human_clipboard", "gmail_composio"}
        if settings.outbound_mode in {"gmail_composio", "email"} and not (
            settings.sender_postal_address or ""
        ).strip():
            sender_geo = sender_geo and False
        proud = voice.get("proud_emails") or voice.get("write_like") or voice.get("samples")
        voice_ok = bool(proud)
        known_checks = {}
        from app.ventures import known
        if known.enabled():
            from app.db.models import Venture
            from app.tenancy import current_or_none
            scope = current_or_none()
            venture = session.get(Venture, scope.venture_id) if scope and scope.venture_id else None
            if venture is None:
                ventures = session.scalars(select(Venture).where(Venture.workspace_id == workspace_id,
                    Venture.archived_at.is_(None), Venture.stage.notin_(["paused", "purged", "archived"])).limit(2)).all()
                venture = ventures[0] if len(ventures) == 1 else None
            if venture and venture.workspace_id != workspace_id:
                raise PermissionError("known_context_scope_mismatch")
            if venture:
                saved_inputs = {"audience": icp_who or None, "voice": proud or None,
                    "excludes": segment.get("excludes"), "sender_geo": settings.sender_geo or None,
                    "sender_postal_address": settings.sender_postal_address or None,
                    "outbound_mode": settings.outbound_mode if send_path else None}
                for field, value in saved_inputs.items():
                    result = known.known(venture, field, session=session, saved=value)
                    known_checks[field] = result
                    if field == "audience":
                        icp = result["status"] == "known" and isinstance(result["value"], str) and len(result["value"]) >= 8
                        if result["status"] in {"known", "confirm"}:
                            segment = {**segment, "who": result["value"]}
                    elif field == "voice":
                        voice_ok = result["status"] == "known"
                        if result["status"] in {"known", "confirm"}:
                            voice = {**voice, "write_like": result["value"]}
                    elif result["status"] != "missing":
                        candidate = result["value"]
                        valid = (isinstance(candidate, list) and all(isinstance(domain, str) for domain in candidate)
                            if field == "excludes" else isinstance(candidate, str) and (
                                len(candidate) == 2 and candidate.isascii() and candidate.isalpha() if field == "sender_geo"
                                else candidate in {"human_clipboard", "gmail_composio"} if field == "outbound_mode"
                                else bool(candidate.strip()) and len(candidate) <= 500))
                        if result["status"] == "conflict":
                            valid = True
                        if not valid:
                            known_checks[field] = {"status": "missing", "value": None, "source": None}
                            continue
                        if result["status"] == "known":
                            if field == "excludes":
                                segment = {**segment, "excludes": candidate}
                                profile = session.scalar(select(GtmMarketProfile).where(GtmMarketProfile.workspace_id == workspace_id)
                                    .order_by(GtmMarketProfile.version.desc()))
                                if profile:
                                    profile.segment_json = json.dumps(segment)
                                    excludes = True
                            else:
                                setattr(settings, field, candidate.upper() if field == "sender_geo" else candidate)
                sender_geo = bool((settings.sender_geo or "").strip())
                send_path = settings.outbound_mode in {"human_clipboard", "gmail_composio"}
                if settings.outbound_mode in {"gmail_composio", "email"} and not (settings.sender_postal_address or "").strip():
                    sender_geo = False
        if mailboxes.enabled():
            connector = any(source["status"] in {"started", "connected"} for source in evidence_sources)
        evidence = bool(paste_exists) or bool(connector) or bool(gmail_acct)
        flags = {
            "icp": icp,
            "excludes": excludes,
            "sender_geo": sender_geo,
            "send_path": send_path,
            "voice": voice_ok,
            "evidence": evidence,
        }
        ready = all(flags.values())
        blob = {
            **flags,
            "ready": ready,
            "outbound_mode": settings.outbound_mode,
            "sender_geo_code": settings.sender_geo,
            "sender_postal_address": settings.sender_postal_address,
            "segment": segment,
            "voice_config": voice,
            "evidence_sources": evidence_sources,
            **({"known_checks": known_checks} if known_checks else {}),
        }
        settings.onboarding_status = json.dumps(flags)
        session.commit()
        return blob


def patch_onboarding(workspace_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.gtm.relationships.icp_seed import save_segment, seed_icp_template

    seed_icp_template(workspace_id)
    segment = get_segment(workspace_id)
    constraints = _constraints(workspace_id)
    if "who" in body:
        segment["who"] = str(body["who"])
    if "roles" in body:
        segment["roles"] = list(body["roles"] or [])
    if "excludes" in body:
        segment["excludes"] = list(body["excludes"] or [])
    if "voice" in body and isinstance(body["voice"], dict):
        constraints["voice"] = body["voice"]
    save_segment(workspace_id, segment, constraints=constraints)
    with db()() as session:
        settings = _settings(session, workspace_id)
        if body.get("sender_geo"):
            settings.sender_geo = str(body["sender_geo"]).strip().upper()
        if "sender_postal_address" in body:
            settings.sender_postal_address = body.get("sender_postal_address")
        if body.get("outbound_mode"):
            mode = str(body["outbound_mode"])
            if mode == UNIPILE_LIVE:
                raise OutreachError("send_mode_unipile_forbidden", 409)
            settings.outbound_mode = mode
        # Same transaction: a second one would wait on this row's uncommitted changes forever.
        if body.get("aup_accepted"):
            settings.aup_accepted_at = utcnow()
        if body.get("dpa_accepted"):
            settings.dpa_accepted_at = utcnow()
        settings.updated_at = utcnow()
        from app.ventures import known
        if known.enabled() and "who" in body:
            from app.db.models import Venture
            from app.secrets.detect import looks_like_secret
            from app.tenancy import current_or_none
            scope = current_or_none()
            venture = session.get(Venture, scope.venture_id) if scope and scope.venture_id else None
            if venture and venture.workspace_id == workspace_id and isinstance(body["who"], str) and 0 < len(body["who"]) <= 2000 and not looks_like_secret(body["who"]):
                known.founder_facts.save(session, venture,
                    {"audience": known.founder_facts._fact(str(body["who"]), "review", 1)})
        session.commit()
    return onboarding_status(workspace_id)


def _quote_from_contact(contact: GtmContact) -> tuple[str | None, str | None]:
    try:
        items = json.loads(contact.exact_language_json or "[]")
    except json.JSONDecodeError:
        items = []
    if items and isinstance(items, list):
        first = items[0] if items else {}
        if isinstance(first, dict):
            return first.get("text"), first.get("source")
    return None, None


def _suppressed(session, workspace_id: str, contact: GtmContact) -> bool:
    from app.gtm.email_learning import globally_suppressed
    if globally_suppressed(session, contact.email):
        return True
    if contact.do_not_contact or contact.unsubscribed_at:
        return True
    email = (contact.email or "").lower()
    if email:
        row = session.scalar(
            select(GtmSuppression).where(
                GtmSuppression.workspace_id == workspace_id,
                GtmSuppression.email == email,
            )
        )
        if row:
            return True
    return False


def score_and_store(workspace_id: str, contact_id: str) -> dict[str, Any]:
    segment = get_segment(workspace_id)
    roles = tuple(str(r) for r in (segment.get("roles") or []))
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise OutreachError("contact_not_found", 404)
        settings = _settings(session, workspace_id)
        quote, quote_src = _quote_from_contact(contact)
        signal = session.scalar(
            select(GtmSignal)
            .where(
                GtmSignal.workspace_id == workspace_id,
                GtmSignal.contact_id == contact.id,
            )
            .order_by(GtmSignal.observed_at.desc())
        )
        trigger = (signal.trigger_code if signal else None) or None
        trig_conf = (signal.confidence if signal else None) or None
        channel = "linkedin_connect" if settings.outbound_mode == "human_clipboard" else "email"
        why = contact.why_this_week
        result = score_contact(
            ScoreInput(
                role=contact.role,
                icp_roles=roles or ("founder", "ceo", "head of platform"),
                recipient_geo=contact.recipient_geo or settings.sender_geo,
                channel=channel,
                send_mode=settings.outbound_mode or "human_clipboard",
                lawful_basis=contact.lawful_basis or "none",
                sender_postal_address=settings.sender_postal_address,
                legitimate_use_note=contact.lawful_basis_note,
                do_not_contact=bool(contact.do_not_contact),
                unsubscribed=bool(contact.unsubscribed_at),
                suppressed=_suppressed(session, workspace_id, contact),
                why_this_week=why,
                why_this_week_confirmed=bool(contact.why_this_week_confirmed_at),
                source=contact.source,
                named_human=True,
                company_name=None,
                quote_text=quote,
                quote_source=quote_src or ("extracted" if quote else None),
                trigger_code=trigger,
                trigger_confidence=trig_conf,
                mutual_count=contact.mutual_count,
                buyer_tag=contact.buyer_tag,
                recipient_seniority=contact.recipient_seniority,
                ask_rung=contact.ask_rung or "none",
                sku_key=contact.sku_key,
                i_actually_know_them=bool(contact.i_actually_know_them),
            )
        )
        contact.score_void = 1 if result.void else 0
        contact.score_sum = result.sum
        contact.score_band = result.band.value if hasattr(result.band, "value") else str(result.band)
        contact.score_factors_json = json.dumps(result.factors)
        contact.score_explain = result.explain
        contact.personalize_mode = result.personalize
        session.commit()
        return {
            "contact_id": contact.id,
            "void": result.void,
            "sum": result.sum,
            "band": contact.score_band,
            "explain": result.explain,
            "personalize": result.personalize,
            "gates": result.gates,
            "harvested": result.harvested,
        }


def list_queue(workspace_id: str, *, limit: int = 50) -> dict[str, Any]:
    status = onboarding_status(workspace_id)
    if not status.get("ready"):
        return {"items": [], "ready": False, "onboarding": status, "empty_reason": "onboarding_incomplete"}
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmContact)
                .where(
                    GtmContact.workspace_id == workspace_id,
                    GtmContact.layer == "B",
                    GtmContact.do_not_contact == 0,
                )
                .order_by(GtmContact.created_at.desc())
                .limit(limit)
            )
        )
        items = []
        for c in rows:
            src = (c.source or "").lower()
            if src in HARVESTED_SOURCES:
                continue
            if (c.score_band or "") == "blocked" or c.score_void:
                continue
            company = session.get(GtmCompany, c.company_id) if c.company_id else None
            items.append(
                {
                    "id": c.id,
                    "first_name": c.first_name,
                    "email": c.email,
                    "role": c.role,
                    "band": c.score_band,
                    "explain": c.score_explain,
                    "personalize": c.personalize_mode,
                    "company": company.canonical_name if company else None,
                    "why_this_week": c.why_this_week,
                }
            )
        return {"items": items, "ready": True, "onboarding": status}


def get_contact(workspace_id: str, contact_id: str) -> dict[str, Any]:
    with db()() as session:
        c = gtm_get(session, GtmContact, contact_id, workspace_id)
        if c is None:
            raise OutreachError("contact_not_found", 404)
        company = (
            gtm_get(session, GtmCompany, c.company_id, workspace_id) if c.company_id else None
        )
        return {
            "id": c.id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "email": c.email,
            "role": c.role,
            "band": c.score_band,
            "sum": c.score_sum,
            "void": bool(c.score_void),
            "explain": c.score_explain,
            "personalize": c.personalize_mode,
            "why_this_week": c.why_this_week,
            "ask_rung": c.ask_rung,
            "company": company.canonical_name if company else None,
            "source": c.source,
        }


def patch_contact(workspace_id: str, contact_id: str, body: dict[str, Any]) -> dict[str, Any]:
    with db()() as session:
        c = gtm_get(session, GtmContact, contact_id, workspace_id)
        if c is None:
            raise OutreachError("contact_not_found", 404)
        if "why_this_week" in body:
            c.why_this_week = body.get("why_this_week")
            text = (c.why_this_week or "").strip()
            if len(text) >= 12:
                c.why_this_week_confirmed_at = utcnow()
        if "recipient_geo" in body:
            c.recipient_geo = str(body["recipient_geo"]).upper() if body["recipient_geo"] else None
        if "mutual_count" in body and body["mutual_count"] is not None:
            c.mutual_count = int(body["mutual_count"])
        if "role" in body:
            c.role = body.get("role")
        if "score_override" in body:
            c.score_override = body.get("score_override")
        if "score_override_reason" in body:
            c.score_override_reason = body.get("score_override_reason")
        session.commit()
    return score_and_store(workspace_id, contact_id)


def create_draft(workspace_id: str, contact_id: str, kind: str = "connect") -> dict[str, Any]:
    scored = score_and_store(workspace_id, contact_id)
    with db()() as session:
        contact = gtm_get(session, GtmContact, contact_id, workspace_id)
        if contact is None:
            raise OutreachError("contact_not_found", 404)
        if is_labelled_example_contact(
            email=contact.email, first_name=contact.first_name, last_name=contact.last_name
        ):
            raise OutreachError("example_cannot_send", 409)
        from app.gtm.relationships.suppression import email_suppressed
        if kind == "email" and email_suppressed(session, workspace_id, contact):
            raise OutreachError("contact_suppressed", 409)
        settings = _settings(session, workspace_id)
        if settings.outbound_mode == UNIPILE_LIVE:
            raise OutreachError("send_mode_unipile_forbidden", 409)
        if kind == "call_15":
            from app.gtm.relationships.ladder import call_15_allowed

            value_return_sent = session.scalar(
                select(GtmTouch.id)
                .where(
                    GtmTouch.workspace_id == workspace_id,
                    GtmTouch.contact_id == contact.id,
                    GtmTouch.ask_rung == "value_return",
                    GtmTouch.sent_at.is_not(None),
                )
                .limit(1)
            )
            sku_ok = False
            if contact.sku_key:
                sku_ok = (
                    session.scalar(
                        select(GtmArtifactSku.id).where(
                            GtmArtifactSku.workspace_id == workspace_id,
                            GtmArtifactSku.sku_key == contact.sku_key,
                            GtmArtifactSku.status == "approved",
                        )
                    )
                    is not None
                )
            lock = call_15_allowed(
                from_rung=contact.ask_rung or "none",
                has_value_return=bool(value_return_sent),
                sku_approved=bool(sku_ok),
            )
            if not lock.ok:
                raise OutreachError("call_15_locked", 409)
        quote, quote_src = _quote_from_contact(contact)
        company = (
            gtm_get(session, GtmCompany, contact.company_id, workspace_id)
            if contact.company_id
            else None
        )
        rendered = render_draft(
            kind=kind,
            first_name=contact.first_name,
            quote=quote if (quote_src or "extracted") == "extracted" else None,
            trigger_code=None,
            trigger_confidence=None if not quote else "observed",
            company=company.canonical_name if company else None,
            product_name=None,
            ask_rung=contact.ask_rung or "none",
            hook=contact.dossier_hook or quote,
        )
        if kind == "email" and rendered.get("body"):
            from app.gtm.relationships.email_compliance import complete_body
            rendered["body"] = complete_body(session, workspace_id, contact, rendered["body"], "")
        if rendered.get("blocked") and rendered.get("reason") == "premature_calendar":
            raise OutreachError("premature_calendar", 409)
        honesty = rendered.get("honesty") or {}
        if honesty.get("blocked"):
            flags = honesty.get("flags") or []
            if "premature_calendar" in flags:
                raise OutreachError("premature_calendar", 409)
            if "pitch_in_connection_request" in flags:
                raise OutreachError("pitch_in_connection_request", 409)
        now = utcnow()
        job = GtmLinkedInQueue(
            id=new_id("gtmli"),
            workspace_id=workspace_id,
            contact_id=contact.id,
            step=f"{kind}_{now.strftime('%H%M%S%f')}",
            status="ready_to_copy" if rendered.get("body") else "blocked_honesty",
            scheduled_for=now,
            body=rendered.get("body"),
            honesty_json=json.dumps(honesty),
            send_mode=settings.outbound_mode or "human_clipboard",
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.add(
            GtmTouch(
                id=new_id("gtmtch"),
                workspace_id=workspace_id,
                contact_id=contact.id,
                direction="out",
                channel="linkedin_connect" if kind == "connect" else "email",
                body_text=rendered.get("body"),
                sent_at=None,
                simulated=1,
                created_at=now,
                ask_rung=kind,
                send_mode=settings.outbound_mode or "human_clipboard",
                honesty_json=json.dumps(honesty),
                outcome="draft",
            )
        )
        session.commit()
        return {
            "id": job.id,
            "contact_id": contact.id,
            "kind": kind,
            "body": rendered.get("body"),
            "blocked": rendered.get("blocked"),
            "reason": rendered.get("reason"),
            "personalize": rendered.get("personalize") or scored.get("personalize"),
            "honesty": honesty,
            "band": scored.get("band"),
            "explain": scored.get("explain"),
            "copied_at": None,
            "sent_at": None,
        }


def edit_draft(workspace_id: str, draft_id: str, body: str | None) -> dict[str, Any]:
    with db()() as session:
        job = gtm_get(session, GtmLinkedInQueue, draft_id, workspace_id)
        if job is None:
            raise OutreachError("draft_not_found", 404)
        if body is not None:
            if body and (job.step or "").startswith("email_"):
                from app.gtm.relationships.email_compliance import complete_body
                contact = gtm_get(session, GtmContact, job.contact_id, workspace_id)
                if contact is None:
                    raise OutreachError("contact_not_found", 404)
                body = complete_body(session, workspace_id, contact, body, "")
            job.body = body
            job.updated_at = utcnow()
        session.commit()
        return {"id": job.id, "body": job.body}


def mark_copied(workspace_id: str, draft_id: str) -> dict[str, Any]:
    from app.gtm.relationships.abuse import assert_copy_or_send

    assert_copy_or_send(workspace_id, kind="connect")
    with db()() as session:
        job = gtm_get(session, GtmLinkedInQueue, draft_id, workspace_id)
        if job is None:
            raise OutreachError("draft_not_found", 404)
        if not job.body:
            raise OutreachError("nothing_to_copy", 409)
        if (job.step or "").startswith("email_"):
            from app.gtm.relationships.email_compliance import complete_body
            contact = gtm_get(session, GtmContact, job.contact_id, workspace_id)
            if contact is None:
                raise OutreachError("contact_not_found", 404)
            job.body = complete_body(session, workspace_id, contact, job.body, "")
        now = utcnow()
        job.copied_at = now
        job.status = "copied"
        job.updated_at = now
        session.add(
            GtmTouch(
                id=new_id("gtmtch"),
                workspace_id=workspace_id,
                contact_id=job.contact_id,
                direction="out",
                channel="linkedin_connect",
                body_text=job.body,
                sent_at=None,
                copied_at=now,
                simulated=1,
                created_at=now,
                send_mode=job.send_mode or "human_clipboard",
                outcome="copied",
            )
        )
        session.commit()
        from app.gtm.relationships.cadence import remaining_caps

        caps = remaining_caps(workspace_id)
        return {
            "id": job.id,
            "copied_at": job.copied_at.isoformat() if job.copied_at else None,
            "sent_at": job.sent_at.isoformat() if job.sent_at else None,
            "remaining": caps.get("remaining"),
            "reset_at": caps.get("reset_at"),
        }


def mark_sent(workspace_id: str, draft_id: str, *, channel: str = "linkedin_connect") -> dict[str, Any]:
    from app.gtm.relationships.abuse import assert_copy_or_send

    kind = "email" if channel in {"email", "gmail_composio"} else "connect"
    assert_copy_or_send(workspace_id, kind=kind)
    with db()() as session:
        job = gtm_get(session, GtmLinkedInQueue, draft_id, workspace_id)
        if job is None:
            raise OutreachError("draft_not_found", 404)
        contact = gtm_get(session, GtmContact, job.contact_id, workspace_id)
        if contact is None:
            raise OutreachError("contact_not_found", 404)
        from app.gtm.relationships.suppression import email_suppressed
        if kind == "email" and email_suppressed(session, workspace_id, contact):
            raise OutreachError("contact_suppressed", 409)
        if is_labelled_example_contact(
            email=contact.email, first_name=contact.first_name, last_name=contact.last_name
        ):
            raise OutreachError("example_cannot_send", 409)
        src = (contact.source or "").lower()
        if src in HARVESTED_SOURCES or contact.score_void:
            raise OutreachError("harvested_quarantine", 409)
        why = (contact.why_this_week or "").strip()
        if len(why) < 12 or not contact.why_this_week_confirmed_at:
            raise OutreachError("why_this_week_required", 409)
        if job.send_mode == UNIPILE_LIVE:
            raise OutreachError("send_mode_unipile_forbidden", 409)
        now = utcnow()
        job.sent_at = now
        job.status = "sent_by_human"
        job.updated_at = now
        contact.ask_rung = "connect"
        contact.ask_rung_changed_at = now
        session.add(
            GtmTouch(
                id=new_id("gtmtch"),
                workspace_id=workspace_id,
                contact_id=contact.id,
                direction="out",
                channel=channel,
                body_text=job.body,
                sent_at=now,
                simulated=1,
                created_at=now,
                send_mode=job.send_mode or "human_clipboard",
                outcome="sent_by_human",
                logged_by="human",
            )
        )
        if kind == "email":
            from app.gtm.email_learning import record
            record(session, contact, "send", job.id, observed_at=now)
        session.add(
            GtmOutcome(
                id=new_id("gtmout"),
                workspace_id=workspace_id,
                contact_id=contact.id,
                kind="prospect_identified",
                channel=channel,
                metadata_json=json.dumps({"n": 1, "d": "of_identified", "d_value": 1}),
                occurred_at=now,
                created_at=now,
            )
        )
        session.commit()
        return {
            "id": job.id,
            "sent_at": job.sent_at.isoformat() if job.sent_at else None,
            "copied_at": job.copied_at.isoformat() if job.copied_at else None,
        }


def log_outcome(workspace_id: str, touch_id: str, outcome: str) -> dict[str, Any]:
    with db()() as session:
        job = gtm_get(session, GtmLinkedInQueue, touch_id, workspace_id)
        touch = gtm_get(session, GtmTouch, touch_id, workspace_id)
        contact_id = job.contact_id if job is not None else (touch.contact_id if touch is not None else None)
        if not contact_id:
            raise OutreachError("touch_not_found", 404)
        now = utcnow()
        session.add(
            GtmOutcome(
                id=new_id("gtmout"),
                workspace_id=workspace_id,
                contact_id=contact_id,
                kind=outcome,
                channel=touch.channel if touch is not None else "linkedin_connect",
                metadata_json="{}",
                occurred_at=now,
                created_at=now,
            )
        )
        session.commit()
        return {"touch_id": touch_id, "outcome": outcome}


def funnel_stub(workspace_id: str, *, window_days: int = 30) -> dict[str, Any]:
    """Deprecated alias — OT-23 funnel_metrics."""
    from app.gtm.relationships.funnel import funnel_metrics

    return funnel_metrics(workspace_id, window_days=window_days)


def outreach_status(workspace_id: str) -> dict[str, Any]:
    from app.gtm.relationships.cadence import remaining_caps

    onboarding = onboarding_status(workspace_id)
    queue = list_queue(workspace_id)
    caps = remaining_caps(workspace_id)
    return {
        "onboarding": onboarding,
        "ready": bool(onboarding.get("ready")),
        "queue_count": len(queue.get("items") or []),
        "examples": True,
        "linkedin_send": False,
        "cadence": caps,
        "outreach_week": caps.get("outreach_week"),
        "remaining": caps.get("remaining"),
        "funnel": funnel_metrics(workspace_id),
    }


__all__ = [
    "OutreachError",
    "SkuAttachError",
    "attach_sku",
    "create_draft",
    "funnel_metrics",
    "funnel_stub",
    "get_contact",
    "list_queue",
    "log_outcome",
    "mark_copied",
    "mark_sent",
    "match_sku",
    "onboarding_status",
    "outreach_status",
    "patch_contact",
    "patch_onboarding",
    "score_and_store",
]
