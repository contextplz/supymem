from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select

from app.autopilot import outreach
from app.db.models import GtmCompany, GtmContact, GtmSuppression, GtmTouch, Job, VentureDocument
from app.gtm.relationships.email_compliance import legacy_cleanup
from app.gtm.journey import _json
from app.gtm.contact_work import _notes
from app.gtm import journey
from app.state import db


def draft_public_contact_message(workspace_id: str, venture_id: str, lead: dict, source: str, *, checked_website: str) -> str:
    from app.gtm import contact_work, reachable
    from app.gtm.relationships.email_compliance import no_dashes, sender_address

    draft = dict(lead)
    reachable._call_draft(draft, journey._icp(venture_id).get("problem") or "")
    with db()() as session:
        postal = sender_address(session, workspace_id)
    disclosure = f"I found you through your public page on {urlsplit(source).hostname}."
    message = no_dashes("\n\n".join(filter(None, (draft["draft"], disclosure, outreach.founder_signature(venture_id), outreach.OPT_OUT, postal))))
    contact_work._put(venture_id, lead["id"], "lead_message_draft", "", {"message_draft": message, "checked_website": checked_website})
    return message


def saved_leads(workspace_id: str, venture_id: str) -> dict:
    search = journey.status(workspace_id, venture_id, authorize=False)
    work_notes = {}
    for note in _notes(venture_id):
        work_notes.setdefault(note.get("contact_id"), []).append(note)
    with db()() as session:
        contacts = list(session.scalars(select(GtmContact).where(
            GtmContact.workspace_id == workspace_id,
            GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True),
        ).order_by(GtmContact.created_at.desc(), GtmContact.id.desc())))
        companies = {row.id: row for row in session.scalars(select(GtmCompany).where(
            GtmCompany.workspace_id == workspace_id, GtmCompany.id.in_({contact.company_id for contact in contacts}),
        ))}
        documents = list(session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id, VentureDocument.kind == outreach.KIND,
        ).order_by(VentureDocument.created_at.asc(), VentureDocument.id.asc())))
        saved_drafts = [_json(document.provenance_json) | {"doc_id": document.id} for document in documents]
        scheduled_ids = {(draft.get("scheduled_send") or {}).get("job_id") for draft in saved_drafts} - {None}
        scheduled = set(session.scalars(select(Job.id).where(Job.workspace_id == workspace_id, Job.venture_id == venture_id,
            Job.id.in_(scheduled_ids), Job.status.in_({"scheduled", "pending", "leased", "running", "retry_wait"})))) if scheduled_ids else set()
        drafts, history = {}, {}
        for draft in saved_drafts:
            email_draft = draft.get("channel", "email") == "email" or draft.get("channel") == "manual" and outreach._EMAIL.fullmatch(str(draft.get("to") or ""))
            if email_draft and draft.get("state") not in {"review_abandoned", "review_staging", "review_failed"}:
                if draft.get("body"):
                    draft["body"] = legacy_cleanup(draft["body"])
                draft["truth_state"] = outreach.draft_truth_state(draft, scheduled=(draft.get("scheduled_send") or {}).get("job_id") in scheduled)
                drafts[draft.get("contact_id")] = draft
                history.setdefault(draft.get("contact_id"), []).append(draft)
        suppressed = {email.lower() for email in session.scalars(select(GtmSuppression.email).where(
            GtmSuppression.workspace_id == workspace_id,
        ))}
        touches = {}
        for touch in session.scalars(select(GtmTouch).where(
            GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0,
        ).order_by(GtmTouch.created_at.asc(), GtmTouch.id.asc())):
            flags = touches.setdefault(touch.contact_id, set())
            if touch.direction == "in" and touch.classification != "automated":
                flags.add("reply received")
                flags.discard("positive reply")
                if touch.classification == "human_positive":
                    flags.add("positive reply")
            elif touch.direction == "out" and touch.sent_at:
                flags.add("sent")
        search_row = journey._row(session, venture_id)
        items, by_source = [], {}
        by_email = {contact.email.casefold(): contact.id for contact in contacts if contact.email}
        for contact in contacts:
            evidence = _json(contact.enrichment_json)
            name = evidence.get("person_name") or " ".join(filter(None, [contact.first_name, contact.last_name])) or "Company contact"
            source_url = evidence.get("found_at") or evidence.get("source_url") or evidence.get("contact_source_url")
            by_source[(source_url, name.casefold())] = contact.id
            if evidence.get("skipped"):
                continue
            company = companies.get(contact.company_id)
            imported = evidence.get("founder_import") or {}
            draft = drafts.get(contact.id)
            opted_out = bool(contact.unsubscribed_at or contact.do_not_contact or (contact.email or "").lower() in suppressed)
            flags = touches.get(contact.id, set())
            manual = [_json(note["body"]) for note in work_notes.get(contact.id, []) if note.get("type") == "manual_outreach"]
            verified = bool(evidence.get("email_evidence") == "published" and isinstance(evidence.get("contact_quote"), str)
                and evidence.get("contact_source_url") and contact.email and contact.email.lower() in evidence["contact_quote"].lower())
            status = ("opted out" if opted_out else "bounced" if contact.bounced_at else
                      "meeting booked" if any(item.get("booked_at") for item in manual) else
                      "positive reply" if "positive reply" in flags else
                      "reply received" if "reply received" in flags or imported.get("status") == "replied" or any(item.get("replied_at") for item in manual) else
                      "sent" if contact.last_emailed_at or "sent" in flags or any(item.get("state") == "sent" for item in history.get(contact.id, [])) or any(item.get("sent_at") for item in manual) or imported.get("status") == "contacted" else
                      draft["truth_state"] if draft else "verified" if verified else "suggested")
            reported = status == "meeting booked" or status in {"sent", "reply received"} and not flags and not any(item.get("state") == "sent" for item in history.get(contact.id, []))
            route = evidence.get("email_route") or {}
            email = contact.email if not opted_out and not contact.bounced_at else None
            items.append({
                "id": contact.id, "contact_id": contact.id, "name": name,
                "role": contact.role or evidence.get("person_role"), "company": company.canonical_name if company else evidence.get("business") or "Unknown",
                "source": "library" if ":lead_library:" in (contact.source or "") else "imported" if "founder_import" in (contact.source or "") else "web",
                "source_url": source_url,
                "website": evidence.get("website") or ("https://" + company.domain if company and company.domain else None),
                "email": contact.email, "can_draft": bool(email and contact.email_confidence in {"safe", "risky"}
                    and outreach._EMAIL.fullmatch(email) and not outreach._JUNK_EMAIL.search(email)),
                "company_inbox": route.get("kind") == "company_inbox" or evidence.get("email_evidence") == "company_inbox" or (evidence.get("lead_library") or {}).get("email_tier") == "company_inbox",
                "linkedin_url": contact.linkedin_url, "x_handle": contact.x_handle, "phone": evidence.get("phone"),
                "status": status, "truth_basis": "Recorded in your saved history" if reported and status == "sent" and contact.last_emailed_at
                    and imported.get("status") != "contacted" and not any(item.get("sent_at") for item in manual) else "Recorded by you" if reported else None,
                "interest_note": "You recorded interest. This is not a received reply or a booked meeting." if imported.get("status") == "interested" and "reply received" not in flags and status != "meeting booked" else None,
                "created_at": contact.created_at.isoformat(), "why": evidence.get("why") or contact.dossier_hook,
                "notes": "\n\n".join(filter(None, [imported.get("notes"), evidence.get("notes"), company.notes if company else None,
                    *[note["body"] for note in work_notes.get(contact.id, []) if note.get("type") not in {"manual_outreach", "learnings"}]])) or None,
                "draft": draft, "outreach": history.get(contact.id, []), "saved": {"evidence": evidence, "dossier": _json(contact.dossier_json),
                    "sequence": _json(contact.sequence_state_json), "work_notes": work_notes.get(contact.id, []),
                    "company_description": company.description if company else None,
                    "relationship_stage": contact.relationship_stage, "country": contact.recipient_geo,
                    "lawful_basis": contact.lawful_basis, "lawful_basis_note": contact.lawful_basis_note},
            })
        by_id = {item["id"]: item for item in items}
        for person in (search.get("reachable_leads") or []) + (search.get("industry_experts") or []):
            contact_id = next((by_email[route["email"].casefold()] for route in person.get("routes", [])
                if route.get("email") and route["email"].casefold() in by_email), None)
            contact_id = contact_id or by_source.get((person.get("source_url"), person["name"].casefold()))
            if contact_id:
                if contact_id in by_id:
                    by_id[contact_id]["routes"] = person.get("routes") or []
                continue
            if person["id"] in by_id:
                continue
            item = {
                "id": person["id"], "contact_id": None, "name": person["name"],
                "role": person.get("person_role") or person.get("role"), "company": person.get("company") or "Unknown",
                "source": "web", "source_url": person.get("source_url"), "website": person.get("business_url"),
                "routes": person.get("routes") or [], "email": None, "can_draft": False, "company_inbox": False,
                "status": "suggested", "created_at": (search_row.updated_at or search_row.created_at).isoformat(),
                "why": person.get("why"), "notes": person.get("value_exchange"), "draft": None, "outreach": [],
                "saved": {"evidence": person},
            }
            items.append(item)
            by_id[person["id"]] = item
    # ponytail: one workspace lead list per response, add cursor pagination if lists outgrow the page.
    return {"venture_id": venture_id, "workspace_id": workspace_id, "items": items, "total": len(items),
            "outreach_next_step": search.get("outreach_next_step")}


def linkedin_note(workspace_id: str, venture_id: str, contact_id: str, view: dict) -> dict:
    from app.autopilot import outreach
    from app.gtm import playbooks

    lead = next((item for item in saved_leads(workspace_id, venture_id)["items"] if item["id"] == contact_id), None)
    if not lead or lead["status"] == "opted out":
        raise ValueError("gtm_not_found")
    icp = view.get("icp") or {}
    key = next((item["key"] for item in view.get("playbooks") or [] if item.get("key") in playbooks.PLAYBOOKS), next(iter(playbooks.PLAYBOOKS)))
    from app.db.models import Venture
    from app.state import db
    with db()() as session:
        venture = session.get(Venture, venture_id)
        product = view.get("pitch") or (outreach.venture_description(venture_id) if venture else "") or icp.get("offer") or ""
    messages = playbooks.messages(lead["saved"]["evidence"] | {"person": lead["name"], "company": lead["company"], "source_url": lead["source_url"]},
        key, product, audience=icp.get("who") or "", problem=icp.get("problem") or "", venture_name=venture.name if venture else "")
    return {"name": lead["name"], "note": messages["connection_note"]}
