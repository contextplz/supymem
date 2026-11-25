"""One deterministic, read-only projection of existing venture records."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from sqlalchemy import select

from app.db.models import GtmContact, GtmSuppression, GtmTouch, MoneyLedgerEntry, SiteVersion, Venture, VentureDocument, Workspace
from app.db.supymem_models import Deployment, FounderApp, IdeaSession, VentureWaitlistEntry
from app.gtm.contact_work import NOTE, _json
from app.ventures import founder_facts, startup_review
from app.ventures.next_step import current_research_report
from app.ventures.next_step_catalogue import aware
from app.ventures.next_step_models import Call, Contact, Evidence, Observations, Outcome, SavedFact, Snapshot

OUTCOME_KIND = "internal_next_step_outcome"
DECISION_KIND = "internal_next_step_decision"
ARTIFACT_KIND = "next_step_artifact"
PRIORITY_LABELS = {"outreach_leads": "finding customers", "market_insights": "understanding your market",
                   "fundraising": "raising funds", "other": "other work"}


def _priority_words(priorities):
    labels = [PRIORITY_LABELS[value] for value in priorities if value in PRIORITY_LABELS and value != "other"]
    return ", ".join(dict.fromkeys(labels)) or "other work"


def _display_text(text):
    text = re.sub(r"\b(?:ids|idv|ven|dwo|task|tsk|gtmnt|nsart)_[a-z0-9]{6,}(?::[a-z_]+)*", "", text)
    return " ".join(text.replace(" — ", ", ").replace("—", ", ").split()).strip()[:2000]


def _date(value):
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00"))) if isinstance(value, str) else aware(value) if isinstance(value, datetime) else None
    except ValueError:
        return None


def venture_snapshot(session, venture_id: str) -> Snapshot:
    venture = session.get(Venture, venture_id)
    if venture is None:
        raise KeyError(venture_id)
    snapshot = Snapshot(venture_id=venture.id, workspace_id=venture.workspace_id)

    def evidence(identifier, kind, summary, contact_id=None, *, display_summary=None):
        snapshot.evidence[identifier] = Evidence(id=identifier, kind=kind, summary=str(summary)[:2000], contact_id=contact_id,
            display_summary=_display_text(display_summary if display_summary is not None else str(summary)) or "Saved evidence supports this step.")

    evidence(venture.id, "venture", f"{venture.name}: {venture.raw_idea or 'No idea recorded'}")
    snapshot.founder_facts = {field: SavedFact.model_validate(fact) for field, fact in founder_facts.read(session, venture).items()}
    for field, fact in snapshot.founder_facts.items():
        if fact.value:
            value = fact.value
            if field == "priorities":
                for code, label in PRIORITY_LABELS.items():
                    value = re.sub(rf"\b{code}\b", label, value)
            evidence(f"{venture.id}:fact:{field}", "founder_fact", f"{field}: {fact.value} ({fact.source}; quote: {fact.quote})",
                display_summary=f"{field.replace('_', ' ').capitalize()}: {value}")
    documents = list(session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind != DECISION_KIND)
        .order_by(VentureDocument.created_at, VentureDocument.id)))
    if startup_review.enabled(session, venture_id):
        review = startup_review.review(session, venture)
        snapshot.review_questions = [review["question"]["field"]] if review["question"] else []
        snapshot.priorities = review["priorities"]
        snapshot.clarified = review["complete"]
        evidence(f"{venture.id}:review", "review", f"Founder priorities: {', '.join(snapshot.priorities)}; unresolved: {', '.join(snapshot.review_questions)}",
            display_summary=f"Your priorities: {_priority_words(snapshot.priorities)}" if snapshot.priorities else "You have not chosen priorities yet.")
    idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
        IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
        .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
    if idea:
        snapshot.idea_revision_id = idea.current_revision_id
        snapshot.clarified = snapshot.clarified or idea.state in {"reviewing", "validating", "confirmed"}
        snapshot.validated = idea.state == "confirmed" and bool(idea.current_revision_id) and idea.current_revision_id == idea.confirmed_revision_id
        idea_summary = "Your idea is confirmed." if snapshot.validated else {
            "reviewing": "Your idea is ready for review.", "validating": "Your idea is being checked.",
            "confirmed": "Your idea has changes to confirm.",
        }.get(idea.state, "Your idea is taking shape.")
        evidence(idea.id, "idea", f"Idea state: {idea.state}; current revision: {idea.current_revision_id}; confirmed revision: {idea.confirmed_revision_id}",
            display_summary=idea_summary)
        dossier = current_research_report(session, idea)
        if dossier:
            from app.research.presentation import display_text
            report = _json(dossier.summary)
            overview = _json_dict(report.get("review")).get("overview") or _json_dict(report.get("verdict")).get("title")
            snapshot.research_id = dossier.id
            evidence(dossier.id, "research", f"Research {dossier.state}: {dossier.summary or ''}",
                display_summary=display_text(overview) or "Your market research is saved.")
    contacts = {}
    suppressed = {email.lower() for email in session.scalars(select(GtmSuppression.email).where(
        GtmSuppression.workspace_id == venture.workspace_id)) if email}
    for row in session.scalars(select(GtmContact).where(GtmContact.workspace_id == venture.workspace_id,
            GtmContact.source.startswith(f"venture:{venture.id}:")).order_by(GtmContact.id)):
        contact = Contact(id=row.id, name=" ".join(value for value in (row.first_name, row.last_name) if value) or "Saved contact",
            blocked=bool(row.do_not_contact or row.unsubscribed_at or (row.email or "").lower() in suppressed), sent_at=_date(row.last_emailed_at))
        contacts[row.id] = contact
        if not contact.blocked:
            snapshot.lead_ids.append(row.id)
        evidence(row.id, "lead", contact.name, row.id)
    sent_contacts = set()
    for touch in session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == venture.workspace_id,
            GtmTouch.venture_id == venture.id, GtmTouch.simulated == 0).order_by(GtmTouch.created_at, GtmTouch.id)):
        if touch.contact_id not in contacts:
            continue
        contact = contacts[touch.contact_id]
        if touch.direction == "out" and touch.sent_at:
            sent_contacts.add(touch.contact_id)
            snapshot.sent += 1
            contact.sent_at = _date(touch.sent_at)
            evidence(touch.id, "sent", f"Message sent at {contact.sent_at.isoformat()}", contact.id)
        elif touch.direction == "in":
            snapshot.replies += (touch.classification or "").startswith("human_")
            contact.positive = touch.classification == "human_positive"
            evidence(touch.id, "reply", f"{touch.classification}: {touch.body_text or ''}", contact.id,
                display_summary=touch.body_text or "You received a reply.")
    notes = {}
    price_signals = {}
    payment_contacts = set()
    for document in documents:
        if document.kind == NOTE:
            metadata = _json(document.provenance_json)
            if isinstance(metadata.get("contact_id"), str) and isinstance(metadata.get("type"), str):
                notes.setdefault(metadata["contact_id"], {})[metadata["type"]] = document
        elif document.kind == "gtm_journey":
            journey = _json(document.provenance_json)
            audience = _json_dict(journey.get("icp")).get("who")
            evidence(document.id, "journey", f"Journey: {journey.get('state', 'unknown')}; audience: {audience or ''}",
                display_summary=f"Audience: {audience}" if audience else "Your customer search is saved.")
            for kind, lead in ((kind, lead) for kind in ("reachable_leads", "industry_experts", "channels", "need_signals") for lead in journey.get(kind, [])):
                if not isinstance(lead, dict):
                    continue
                identifier = lead.get("id") or hashlib.sha256(str(lead.get("url") or lead.get("source_url") or "").encode()).hexdigest()[:24]
                if not identifier or identifier in contacts:
                    continue
                contacts[identifier] = Contact(id=identifier, name=str(lead.get("person") or lead.get("name") or lead.get("author") or "Community conversation"),
                    blocked=bool(lead.get("skipped") or lead.get("blocked_reason") == "suppressed_or_replied"))
                evidence(identifier, "lead", f"{contacts[identifier].name}: {lead.get('why') or lead.get('quote') or ''}", identifier)
                if kind in {"reachable_leads", "industry_experts"} and not contacts[identifier].blocked:
                    snapshot.lead_ids.append(identifier)
        elif document.kind == OUTCOME_KIND:
            snapshot.outcomes.append(Outcome.model_validate_json(document.body_md))
        elif document.kind == "external_site":
            site = _json(document.body_md)
            if site.get("status") in {"verified", "founder_reported"} and site.get("url"):
                snapshot.page_live, snapshot.page_status = True, str(site["status"])
                evidence(document.id, "page", f"Existing page ({site['status']}): {site['url']}",
                    display_summary=f"Your website: {site['url']}")
        elif document.kind == ARTIFACT_KIND:
            artifact = _json(document.body_md)
            kind = artifact.get("type")
            artifact_summary = {
                "manual_follow_up_sent": "You recorded a follow-up message.", "mvp_scope": "Your first product scope is saved.",
                "payment": "You recorded a customer payment.", "learning_deadline": "Your customer learning deadline is saved.",
                "pricing": {"yes": "A customer agreed to your price.", "no": "A customer declined your price.",
                    "maybe": "A customer is considering your price."}.get(artifact.get("price_response"), "Your pricing response is saved."),
                "venture_decision": {"persevere": "You chose to keep going.", "narrow_segment": "You chose to focus on a smaller audience.",
                    "pivot_problem": "You chose to focus on a different problem."}.get(artifact.get("decision"), "Your venture decision is saved."),
            }.get(kind, "Your saved work supports this step.")
            evidence(document.id, "sent" if kind == "manual_follow_up_sent" else str(kind), artifact.get("text") or str(artifact), artifact.get("contact_id"),
                display_summary=artifact_summary)
            if kind == "manual_follow_up_sent" and artifact.get("contact_id") in contacts:
                contacts[artifact["contact_id"]].sent_at = _date(artifact.get("sent_at"))
            if kind == "venture_decision":
                snapshot.venture_decision = artifact.get("decision")
            elif kind == "pricing":
                price_signals[artifact.get("contact_id") or document.id] = (artifact.get("price_response"), artifact.get("commitment"))
            elif kind == "payment" and artifact.get("payment_received"):
                payment_contacts.add(artifact.get("contact_id") or document.id)
            elif kind == "mvp_scope":
                snapshot.mvp_saved = True
            elif kind == "learning_deadline":
                snapshot.learning_deadline = _date(artifact.get("deadline"))
    for contact_id, per_contact in sorted(notes.items()):
        if contact_id not in contacts:
            continue
        contact = contacts[contact_id]
        manual = per_contact.get("manual_outreach")
        record = _json(manual.body_md) if manual else {}
        if record.get("sent_at") and contact_id not in sent_contacts:
            snapshot.sent += 1
            contact.sent_at = _date(record["sent_at"])
            evidence(manual.id, "sent", f"Founder recorded outreach sent at {record['sent_at']}", contact_id)
        booking = per_contact.get("call_schedule") or manual
        scheduled = _json(booking.body_md) if booking else {}
        booked_at = _date(record.get("booked_at")) or _date(scheduled.get("booked_at"))
        call_notes = per_contact.get("call_notes")
        learnings = per_contact.get("learnings")
        if not booked_at and not call_notes:
            continue
        learned = _json(learnings.body_md) if learnings else {}
        observed = Observations.model_validate({key: learned[key] for key in Observations.model_fields if key in learned})
        call = Call(contact_id=contact_id, evidence_id=booking.id if booking else call_notes.id,
            booked_at=booked_at or aware(call_notes.created_at), scheduled_at=_date(scheduled.get("scheduled_at")),
            brief_id=per_contact["brief"].id if "brief" in per_contact else None,
            notes_id=call_notes.id if call_notes else None, learning_id=learnings.id if learnings else None,
            pains=observed.pains_confirmed, contradictions=observed.pains_refuted,
            price_response=observed.willingness_to_pay, commitment=observed.commitment,
            commitment_at=aware(learnings.updated_at) if learnings else None)
        if call.price_response != "unknown" or call.commitment == "loi":
            price_signals.setdefault(contact_id, (call.price_response, call.commitment))
        if contact_id in price_signals:
            response, commitment = price_signals[contact_id]
            call.price_response = response or "unknown"
            call.commitment = commitment or call.commitment
        call.paid = contact_id in payment_contacts
        for document in per_contact.values():
            metadata = _json(document.provenance_json)
            note_summary = {
                "manual_outreach": "Your outreach update is saved.", "call_schedule": "Your call time is saved.",
                "brief": "Your call brief is saved.", "call_notes": "Your call notes are saved.",
                "learnings": f"A customer told you: {observed.pains_confirmed[0]}" if observed.pains_confirmed else "Your call learnings are saved.",
            }.get(metadata["type"], "Your contact update is saved.")
            if document is booking:
                note_summary = f"Your call with {contact.name} is scheduled for {call.scheduled_at.strftime('%B %d, %Y at %H:%M UTC')}." if call.scheduled_at else f"{contact.name} agreed to a call."
            evidence(document.id, "call" if document is booking else metadata["type"], document.body_md or "", contact_id,
                display_summary=note_summary)
            if document is learnings:
                snapshot.evidence[document.id].observations = observed
        for document in documents:
            if document.kind != ARTIFACT_KIND:
                continue
            artifact = _json(document.body_md)
            if artifact.get("contact_id") == contact_id:
                if artifact.get("type") == "pricing":
                    call.commitment = artifact.get("commitment", "none")
                    call.commitment_at = aware(document.updated_at)
                elif artifact.get("type") == "manual_follow_up_sent":
                    contact.sent_at = _date(artifact.get("sent_at"))
        snapshot.calls.append(call)
    for contact in contacts.values():
        contact.paid = contact.id in payment_contacts
    snapshot.contacts = sorted(contacts.values(), key=lambda contact: contact.id)
    snapshot.lead_ids = sorted(set(snapshot.lead_ids))
    snapshot.calls.sort(key=lambda call: (call.scheduled_at or call.booked_at, call.contact_id))
    snapshot.price_responses = sum(response != "unknown" for response, commitment in price_signals.values())
    snapshot.pricing_yeses = sum(response == "yes" for response, commitment in price_signals.values())
    snapshot.lois = sum(commitment == "loi" for response, commitment in price_signals.values())
    snapshot.pricing_supporters = sorted(identifier for identifier, (response, commitment) in price_signals.items() if response == "yes" or commitment == "loi")
    snapshot.paid_customers = len(payment_contacts)
    from app.money.summary import verified_customer_payment
    for payment in session.scalars(select(MoneyLedgerEntry).where(MoneyLedgerEntry.venture_id == venture.id)
            .order_by(MoneyLedgerEntry.created_at, MoneyLedgerEntry.id)):
        if verified_customer_payment(session, payment, venture.workspace_id, venture.id):
            snapshot.paid_customers = max(1, snapshot.paid_customers)
            evidence(payment.id, "payment", f"Verified live customer payment: {payment.amount_minor} minor units {payment.currency}",
                display_summary="A customer payment has been verified.")
    apps = list(session.scalars(select(FounderApp).where(FounderApp.venture_id == venture.id,
        FounderApp.workspace_id == venture.workspace_id, FounderApp.deleted_at.is_(None)).order_by(FounderApp.id)))
    for application in apps:
        snapshot.page_status = snapshot.page_status if snapshot.page_live else application.state
    for page in session.scalars(select(SiteVersion).where(SiteVersion.venture_id == venture.id,
            SiteVersion.workspace_id == venture.workspace_id, SiteVersion.published == 1).order_by(SiteVersion.version)):
        if page.url and page.url.startswith("https://"):
            snapshot.page_live, snapshot.page_status = True, "published"
            evidence(page.id, "page", f"Published landing page: {page.url}")
    for deployment in session.scalars(select(Deployment).where(Deployment.venture_id == venture.id,
            Deployment.workspace_id == venture.workspace_id, Deployment.app_id.in_([application.id for application in apps]),
            Deployment.state == "live", Deployment.live_at.is_not(None)).order_by(Deployment.id)):
        if deployment.url:
            snapshot.page_live, snapshot.page_status = True, "live"
            evidence(deployment.id, "page", f"Live deployment: {deployment.url}")
    signups = list(session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture.id,
        VentureWaitlistEntry.workspace_id == venture.workspace_id, VentureWaitlistEntry.is_test.is_(False)).order_by(VentureWaitlistEntry.id)))
    snapshot.waitlist_signups = len(signups)
    for signup in signups:
        evidence(signup.id, "waitlist", f"Waitlist signup at {aware(signup.created_at).isoformat()} — unverified interest",
            display_summary="Someone joined your waitlist; their interest is not yet verified.")
    from app.credits.accounts import account_for_venture
    workspace = session.get(Workspace, venture.workspace_id)
    account = account_for_venture(venture.id, session=session, acting_person_id=workspace.owner_id if workspace else None)
    if account:
        snapshot.available_micro = account.granted_micro - account.spent_micro - account.reserved_micro
        snapshot.credit_status = account.status
        evidence(account.id, "credits", f"Credits {account.status}: {snapshot.available_micro} micro available",
            display_summary="Your credit balance is recorded.")
    return snapshot


def _json_dict(value):
    return value if isinstance(value, dict) else {}
