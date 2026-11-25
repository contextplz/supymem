"""A chat reply that proposes a next action carries the button for it.

The model can say "ready to start when you say go"; the founder must never have to type a command to
follow that. This adds the matching card-less buttons (each opens a confirmation card or a read-only
answer) to free-form replies that offer or were asked for something we can actually do.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

_OFFER_CUE = re.compile(
    r"\b(?:when you say|say go|say the word|ready to (?:start|begin|draft|run|go)|want me to|shall i|should i|"
    r"i can (?:draft|start|run|prepare|set up|write|build)|happy to)\b", re.I)
_USERS_TOPIC = re.compile(
    r"\b(?:outreach|invite|cold e-?mail|e-?mails?|first (?:\d+ )?(?:users|customers|gym owners|owners|signups)|"
    r"(?:get|find|reach|bring|land)(?:ting)? (?:my |our |the )?(?:first )?(?:\d+ )?(?:users|customers|owners|gym owners|signups|people)|"
    r"onto the waitlist|waitlist signups)\b", re.I)
# A request for help (not a lookup like "how many signups?"), after dropping "do not ..." constraints.
_NEGATION = re.compile(r"\b(?:do not|don'?t|never|without)\b[^.?!]*", re.I)
_HELP_ASK = re.compile(
    r"^\W*(?:[\w' ,-]{0,60}?[.!]\s+)?(?:help (?:me|us)|can you help|i (?:want|need) to|let'?s|"
    r"how (?:do|can|should) (?:i|we)|get me|find me)\b", re.I)
_BUILD_TOPIC = re.compile(r"\b(?:build|landing page)\b", re.I)


def draft_outreach_button() -> dict[str, Any]:
    return {"label": "Draft outreach emails", "action": "outreach.draft", "style": "primary"}


def gtm_button() -> dict[str, Any]:
    return {"label": "Run GTM diagnosis", "action": "gtm.diagnosis", "style": "secondary"}


def offer_buttons(question: str, body: str) -> list[dict[str, Any]]:
    """Buttons matching what the founder asked for or what the reply offered; empty when neither applies."""
    from app.chat.intent import _ANSWER_ONLY, find_customers_request
    from app.chat.search_recovery import manual_discovery_request

    if _ANSWER_ONLY.search(question) or manual_discovery_request(question) or find_customers_request(question):
        return []
    from app.chat.intent import strategy_question

    if strategy_question(question):
        # Ideas first; the search is one tap away, never started by the question itself.
        return [{"label": "Find people to talk to now", "action": "chat.reply", "text": "find me people i should talk to", "style": "primary"}]
    asking = _NEGATION.sub(" ", question or "")
    asked = bool(_USERS_TOPIC.search(asking) and _HELP_ASK.search(asking))
    offered = bool(_OFFER_CUE.search(body or "") and _USERS_TOPIC.search(body or ""))
    if asked or offered and re.search(r"\b(?:gtm|outreach|users?|customers?|leads?|waitlist|signups?)\b", asking, re.I):
        return [draft_outreach_button(), gtm_button()]
    if _OFFER_CUE.search(body or "") and _BUILD_TOPIC.search(body or ""):
        return [{"label": "Review and start the build", "action": "build.prepare", "style": "primary"}]
    return []


def with_offers(reply: dict[str, Any], question: str) -> dict[str, Any]:
    """Add the missing offer buttons to a text reply that has no confirmation card of its own."""
    actions = list(reply.get("actions") or [])
    if any(isinstance(a, dict) and a.get("confirmation") for a in actions):
        return reply
    have = {a.get("action") for a in actions if isinstance(a, dict)}
    extra = [b for b in offer_buttons(question, str(reply.get("body_md") or "")) if b["action"] not in have]
    return {**reply, "actions": actions + extra} if extra else reply


def draft_prepare(venture_id: str, *, request: str = "", review_only: bool = False, contact_id: str | None = None) -> dict[str, Any]:
    from app.chat.execute import local_capability_reply

    if blocked := local_capability_reply("content"):
        return blocked
    from app.chat.intent import _OUTREACH_DRAFT_RE, recruitment_draft_request, single_interview_email_request, single_outreach_email_request

    interview_email = single_interview_email_request(request)
    outreach_email = single_outreach_email_request(request)
    noun = "one interview email" if interview_email else "one outreach email" if outreach_email else "recruitment posts" if recruitment_draft_request(request) else "outreach emails"
    state = {"run": True} if request else {"run": True, "batch": True}  # no specific ask: a batch of personal drafts; the single-draft path is its fallback
    if review_only:
        state = {"run": True, "review_only": True}
    if contact_id is not None:
        if not isinstance(contact_id, str) or not contact_id or len(contact_id) > 100:
            raise ValueError("gtm_invalid_input")
        state = {"run": True, "review_only": True, "contact_id": contact_id}
        noun = "one outreach email"
    if request:
        state["recruitment_request"] = request[:4000]
        if _OUTREACH_DRAFT_RE.search(request) and not recruitment_draft_request(request) and contact_id is None and not review_only:
            from app.experiments import recruitment
            # Asking for drafts right after preparing a test means that test's invitations.
            if recruitment.selected(venture_id):
                state["batch"] = True
    card = replace(ConfirmationCard.for_action(
        get_action("outreach.draft"),
        interpretation=f"Draft {noun} for you to review.",
        consequences=["Uses available idea and website context; unknown audience and research stay placeholders." if review_only else f"A teammate writes short {noun} from your idea, audience and saved GTM diagnosis.",
                      "Nothing is sent, posted or scheduled. You choose who gets what, and sending is a separate step."],
        resulting_state=state), confirm_label="Draft the email" if interview_email or outreach_email or contact_id is not None else "Draft the posts" if request else "Draft the emails")
    return {"kind": "confirmation", "citations": [], "actions": [card.as_action()],
            "body_md": "I can have the drafts written now. They appear here for your review, and nothing is sent."}


def _withdraw_stale_draft_gates(venture_id: str) -> None:
    """Draft-only tasks never need posting; close ones an older run parked on a connection, and its alert."""
    from sqlalchemy import select

    from app.chat.steer import is_review_draft
    from app.concierge.human import dismiss_provider_attention_if_unused
    from app.db.models import PlannedTask
    from app.state import db
    from core.clock import utcnow

    providers: set[str] = set()
    with db()() as session:
        for row in session.scalars(select(PlannedTask).where(
                PlannedTask.venture_id == venture_id, PlannedTask.status == "open",
                PlannedTask.last_error.like("needs_you:%"))):
            if is_review_draft(row.title, row.detail or ""):
                if row.last_error.startswith("needs_you:review_draft_"):
                    continue  # An unresolved paid request is not an obsolete posting gate.
                providers.add(row.last_error.split(":", 1)[1])
                row.status, row.runnable, row.updated_at = "superseded", 0, utcnow()
        session.commit()
    for provider in providers:
        dismiss_provider_attention_if_unused(venture_id, provider)


def format_drafts(text: str) -> str:
    """Each draft as its own fenced block (chat gives code blocks a copy button)."""
    text = re.sub(r"(?m)^[ \t]*```[^\n]*\n?", "", text or "")
    parts = [p.strip() for p in re.split(r"(?im)^[ \t]*(?=(?:#{1,6}\s+)?(?:\*\*)?draft\s+\d+\b)", text) if p.strip()]
    return "\n\n".join(f"```text\n{p}\n```" for p in parts)


async def draft_run(venture_id: str, *, request: str = "") -> dict[str, Any]:
    """Queue the draft as Work, run it now, and post the finished drafts in this reply."""
    from app.planning.execute import TaskRunError, execute_planned_task
    from app.chat.execute import local_capability_reply

    if blocked := local_capability_reply("content"):
        return blocked
    from app.planning.store import enqueue_task
    from app.chat.customers import _draft_request_at

    from app.chat.intent import outreach_email_count, recruitment_draft_request, single_interview_email_request

    interview_email = single_interview_email_request(request)
    outreach_email = bool(request) and outreach_email_count(request) == 1 and not recruitment_draft_request(request)
    requested_at = _draft_request_at(venture_id) if outreach_email else None
    noun = "one interview email" if interview_email else "one outreach email" if outreach_email else "recruitment posts" if recruitment_draft_request(request) else "outreach emails"
    _withdraw_stale_draft_gates(venture_id)
    task_spec = {
        "title": f"Draft {noun} for me to review" if request else "Draft the outreach emails for me to review",
        "detail": (("Founder request: " + request[:4000] + f"\nWrite {noun} for review only. Do not send, post or schedule anything.")
                   if request else "Write short outreach email drafts for the founder to review. Do not send anything."),
        "why_now": "You asked for outreach drafts.", "tags": ["chat"], "est_minutes": 10, "runnable": True}
    if request:
        task_spec["fingerprint"] = "recruitment-draft:" + hashlib.sha256(request[:4000].encode()).hexdigest()[:24]
    task = enqueue_task(venture_id, task_spec, source="chat")
    try:
        result = await execute_planned_task(venture_id, task["id"], post_completion=False)
    except TaskRunError as exc:
        result = ({"status": "needs_you", "failure_code": exc.code,
                   "pause_message": "Your draft request is paused. Nothing was sent. "
                                    "You can ask me to try again."}
                  if exc.code.startswith("review_draft_") else {})
    except Exception as exc:  # noqa: BLE001 - draft-only work: report "nothing was sent" with a next step, not an unknown outcome
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "outreach.draft")
        result = {}
    drafts = format_drafts(str(result.get("draft_text") or ""))
    work = [{"label": "Open work", "href": f"/v/{venture_id}/tasks?task={task['id']}", "style": "secondary"}]
    if result.get("status") == "needs_you" and str(result.get("failure_code") or "").startswith("review_draft_"):
        return {"kind": "needs_you", "citations": [], "body_md": result["pause_message"],
                "actions": [{**work[0], "style": "primary"}]}
    if result.get("already_running"):
        return {"kind": "text", "citations": [], "body_md": "Your email draft is still being prepared. Nothing was sent. Open work to check its progress.",
                "actions": [{**work[0], "style": "primary"}]}
    if not drafts:
        from sqlalchemy import func, select

        from app.autopilot import outreach
        from app.chat.customers import _as, _reply
        from app.db.models import GtmContact
        from app.state import db

        addresses = {email.lower() for email in outreach._EMAIL.findall(request)}
        if len(addresses) == 1:
            with _as(venture_id, None) as workspace_id, db()() as session:
                contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == workspace_id,
                    func.lower(GtmContact.email) == addresses.pop()))
                if contact and outreach._suppressed(session, workspace_id, contact):
                    return _reply(outreach.suppression_message(session, workspace_id, contact), [{**work[0], "style": "primary"}])
        return {"kind": "text", "citations": [], "body_md": "Your email draft could not be prepared. Nothing was sent. Tell me the question you want to ask, then ask me to draft again.",
                "actions": [{**work[0], "style": "primary"}]}
    from app.autopilot import receipts
    from app.db.models import PlannedTask
    from app.db.models import VentureDocument
    from app.state import db
    receipts.persisted_record(PlannedTask, task["id"], {"status": "done"})
    with db()() as session:
        note = session.get(VentureDocument, f"task-note:{task['id']}")
        if note and note.venture_id == venture_id and note.kind == "task_note":
            receipts.saved_document(note.id, note.kind, note.body_md, provenance=note.provenance_json)
    if outreach_email:
        return await requested_email_reply(venture_id, request, str(result["draft_text"]), task["id"], work, requested_at)
    from app.gtm.relationships.email_compliance import sender_address
    from app.db.models import Venture

    with db()() as session:
        venture = session.get(Venture, venture_id)
        address = sender_address(session, venture.workspace_id)
    return {"kind": "text", "citations": [], "actions": work,
            "body_md": "Here are the drafts for your review. Nothing was sent. Tell me which ones to change, "
                       "or who each should go to. "
                       + ("Check your recipient's email source before sending. " if address else
                          "Check your recipient's email source and save your sending address in Find customers before sending. ")
                       + "\n\n" + drafts}


async def requested_email_reply(venture_id: str, request: str, text: str, batch_id: str, actions: list, requested_at) -> dict:
    from app.chat.customers import (
        _EDIT_ERRORS,
        BLOCKED,
        _approval_payload,
        _draft_block,
        _draft_copy_refusal,
        _reply,
        _workspace,
    )

    drafts = format_drafts(text)
    if not drafts:
        return _reply("Your email draft could not be prepared. Nothing was sent. Tell me the question you want to ask, then ask me to draft again.", actions)
    try:
        saved = await _save_requested_email(venture_id, request, text, batch_id)
    except Exception as exc:  # noqa: BLE001 - retain the written draft if saving fails
        if isinstance(exc, ValueError) and str(exc) in _EDIT_ERRORS:
            return _draft_copy_refusal(exc)
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "outreach.draft")
        return _reply("Your email was written but could not be saved for sending. Nothing was sent. Ask me to try again to save it.\n\n" + drafts, actions)
    if not saved:
        return _reply("Your draft is ready to review. Nothing was sent. Tell me what to change.\n\n" + drafts, actions)
    if saved.get("blocked_reason"):
        return _reply(saved["blocked_reason"], saved.get("actions"))
    _approval_payload(venture_id, [saved], focus=True, requested_at=requested_at)
    kind = "text"
    next_step = f'Say "send it" to send this email to {saved["to"]}, or tell me what to change.'
    if not saved.get("sender"):
        from app.gtm.journey import _mailbox
        if not _mailbox(_workspace(venture_id)):
            next_step = BLOCKED["gtm_gmail_required"]
            kind = "navigation"
            actions = [{"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"}, *actions]
    return {"kind": kind, "citations": [], "actions": actions,
            "body_md": _draft_block(1, saved).replace("**Draft 1**", "**Draft**") + "\n\nNothing was sent. " + next_step}


async def _save_requested_email(venture_id: str, request: str, text: str, task_id: str) -> dict | None:
    import json
    from sqlalchemy import func, select
    from app.autopilot import outreach
    from app.gtm import journey
    from app.db.models import GtmContact
    from app.db.session import serialize_writes
    from app.gtm.relationships.email_compliance import complete_body
    from app.gtm.supymem.pipeline import _persist_lead
    from app.intake.contacts import _email, check_domains
    from app.state import db
    from core.clock import utcnow

    addresses = {email.lower() for email in outreach._EMAIL.findall(request)}
    if len(addresses) != 1:
        return None
    email = addresses.pop()
    _email(email)
    recipient = re.search(r"\b(?:to|for)\s+(?:(?P<name>[^\n@]{1,100}?)\s+at\s+)?" + re.escape(email) + r"\b", request, re.I)
    from app.chat.intent import single_interview_email_request

    subject = re.search(r"(?im)^Subject:[ \t]*(?P<subject>[^\n]+)\n(?P<body>[\s\S]+)", text)
    supplied_recipient = single_interview_email_request(request) and re.search(
        r"\b(?:to|for)\s+(?:that|this|the)\s+(?:public\s+)?(?:business\s+)?(?:e-?mail\s+)?address\b", request, re.I)
    if not subject or (not recipient and not supplied_recipient):
        return None
    workspace_id = outreach._venture(venture_id)[0]
    actor = journey.authority(workspace_id, venture_id, write=True)
    checks = await check_domains({email.split("@", 1)[1]})
    with db()() as session:
        serialize_writes(session, key="founder-contact-import:" + workspace_id)
        if blocked := journey._processing_block(session, workspace_id):
            raise ValueError("gtm_" + blocked)
        contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == workspace_id, func.lower(GtmContact.email) == email))
        if contact and not (contact.source or "").startswith(f"venture:{venture_id}:"):
            raise ValueError("gtm_contact_already_in_workspace")
        if contact is None:
            name = ((recipient["name"] or "") if recipient else "").strip().split(maxsplit=1)
            contact = _persist_lead(session, workspace_id, {"email": email, "first_name": name[0] if name else None,
                "last_name": name[1] if len(name) > 1 else None, "source": f"venture:{venture_id}:founder_import",
                "email_confidence": "risky" if checks.get(email.split("@", 1)[1]) == "mx" else "guess"}, schedule_research=False)
            contact.lawful_basis, contact.lawful_basis_at = "none", None
            contact.lawful_basis_note = None
            contact.source, contact.contact_provenance = f"venture:{venture_id}:founder_import", "founder_import"
            contact.email_confidence = "risky" if checks.get(email.split("@", 1)[1]) == "mx" else "guess"
            evidence = journey._json(contact.enrichment_json)
            evidence["founder_import"] = (evidence.get("founder_import") or {"imported_at": utcnow().isoformat(),
                "imported_by": actor, "lawful_basis_attested": False, "status": "new"}) | {"email_check": checks.get(email.split("@", 1)[1], "unknown")}
            contact.enrichment_json = json.dumps(evidence)
        if outreach._suppressed(session, workspace_id, contact):
            return {"blocked_reason": outreach.suppression_message(session, workspace_id, contact)}
        body = complete_body(session, workspace_id, contact, subject["body"].strip(), outreach.founder_signature(venture_id))
        contact_id = contact.id
        session.commit()
    saved = {"contact_id": contact_id, "to": email, "subject": subject["subject"].strip(), "body": body,
        "channel": "email", "state": "draft", "day": 1, "batch_id": task_id, "source": "chat",
        "requires_batch_approval": True, "requires_journey_review": True, "review_state": "draft", "suggestion_draft": True,
        "asked_by_name_at": utcnow().isoformat()}
    try:
        doc_id = outreach._save_draft(venture_id, saved)
    except outreach.mailboxes.MailboxUnavailable:
        # shortcut: an unconnected inbox uses the legacy unbound format, until the shared writer supports unbound email drafts.
        saved.pop("channel")
        doc_id = outreach._save_draft(venture_id, saved)
    from app.chat.customers import _check_written_draft

    draft = next(draft for draft in outreach.drafts(venture_id, "draft") if draft["doc_id"] == doc_id)
    return await _check_written_draft(venture_id, workspace_id, draft, request)
