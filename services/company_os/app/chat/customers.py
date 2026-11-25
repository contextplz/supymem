"""Find customers in chat: the GTM journey's search, results, review, send and replies as chat messages.

Every step calls the same journey functions the Find customers page uses (prepare, status, patch_draft,
send, update_icp, update_settings, patch_prospect, confirm_prospects), so the same gates apply.
Nothing here links out to that page.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from core.clock import utcnow
from app.chat.recovery import founder_failure, log_founder_exception
from app.chat.actions import question_reply
from app.chat.intent import _EMAIL_COUNTS
from app.core.local_mode import is_local
from app.gtm.discovery import (
    GENERIC_ASK as _GENERIC_ASK,
    REACH_TAIL_RE as _REACH_TAIL,
    SEARCH_REQUEST_RE as _SEARCH_REQUEST,
    request_audience as _plain_audience,
    search_note,
)

_LEAD = r"^\s*(?:(?:ok(?:ay)?|yes|please|now|so|great|cool|go ahead and|can you|could you)[,.!\s]+)*"
_SAVED_DRAFT_QUALIFIER = r"(?:\s+(?:that\s+)?you\s+(?:prepared|drafted|wrote|saved)(?:\s+for\s+(?:this|the|my|our)\s+(?:experiment|test))?)?"
_APPROVE_RE = re.compile(_LEAD + r"approve\s+(?P<what>all(?:\s+(?:of\s+)?(?:them|the\s+(?:drafts|emails)|drafts|emails))?|them|both|everything|"
                         r"(?:the\s+|all\s+the\s+)?(?:drafts|emails)|(?:drafts?|emails?)\s+[\d,\sand]+)\s*[.!]*\s*$", re.I)
_SEND_COUNT_RE = re.compile(_LEAD + r"send\s+(?:all\s+)?(?:the\s+|my\s+)?(?P<count>\d+|"
    + "|".join(word for word in _EMAIL_COUNTS if word not in {"a", "an", "ek"})
    + r")(?:\s+(?:(?:approved|reviewed|saved|displayed|pending)\s+)*(?:emails?|drafts?|(?:(?:interview|e-?mail)\s+)?invitations?))?"
    + _SAVED_DRAFT_QUALIFIER + r"(?:\s+for\s+me)?(?:\s+now)?\s*[?.!]*\s*$", re.IGNORECASE)
_SEND_RE = re.compile(_LEAD + r"(?:send\s+(?:all\s+)?(?:(?:the|my|our)\s+)?"
                      r"(?:(?:approved|reviewed|saved|displayed|pending|prepared)\s+)*(?:emails|drafts|(?:(?:interview|e-?mail)\s+)?invitations)"
                      r"\s+to\s+(?P<recipients>[^?!;\n]+?)|"
                      r"send(?:\s+(?:all\s+(?:of\s+)?(?:them|those|these)(?:\s+(?:emails?|drafts?|invitations?))?|(?:them|those|these)\s+(?:all|both)|both(?:\s+(?:emails?|drafts?|invitations?))?|them|those|these|all|everything|(?:the\s+|my\s+|all\s+(?:the\s+)?)?(?:(?:approved|reviewed|saved|displayed|pending)\s+)*(?:emails?|drafts?|(?:(?:interview|e-?mail)\s+)?invitations?|batch|outreach)))?"
                      r"(?:\s+(?:the\s+)?e-?mails?)?|e-?mail\s+(?:them|(?:these|those)\s+(?!(?:people|persons|contacts)\s*[.!?]*\s*$)[\w '-]+?|(?:the|my|our)\s+[\w '-]+?))"
                      + _SAVED_DRAFT_QUALIFIER + r"(?:\s+shown)?(?:\s+for\s+me)?(?:\s+now)?\s*[?.!]*\s*$", re.IGNORECASE)
_SEND_ONE_RE = re.compile(_LEAD + r"send\s+(?:(?:it|(?:this|that)(?:\s+(?:one|e-?mail|draft))?|"
                          r"(?:the(?:\s+same)?|another|an?)\s+(?:e-?mail|draft)|e-?mail|draft)\s+to\s+(?P<who>[^?!\n]+?)"
                          r"|to\s+(?P<recipient>[^?!\n]+?))(?:\s+again)?(?:\s+for\s+me)?(?:\s+now)?\s*[?.!]*\s*$", re.I)
_OPT_OUT_QUESTION_RE = re.compile(r"^\s*(?:why|what\s+happened|did|has|have|was|is|can\s+you\s+explain)\b"
    r"(?=[\s\S]*\b(?:opt(?:ed)?[ -]?out|unsubscrib\w*|(?:not|didn['’]?t)(?:\s+you)?\s+(?:send|e-?mail(?:ed)?))\b)", re.I)
_SHOW_FOUND_RE = re.compile(_LEAD + r"(?:show|list|see|give)\b[^.!?\n]*\b(?:people|leads|contacts|prospects|customers)\b[^.!?\n]*\b(?:you|we)\s+(?:have\s+|already\s+)?found\b", re.I)
_SAVED_EMAILS_RE = re.compile(_LEAD + r"(?:show(?:\s+me)?|list|see|review)\s+(?:(?:my|our|the)\s+)?"
    r"(?:(?:saved|outreach|e-?mail)\s+)*(?:e-?mails|drafts)\s*[.!?]*\s*$", re.IGNORECASE)
_SAVED_PEOPLE_RE = re.compile(_LEAD + r"(?:show(?:\s+me)?|list|see|review)\s+(?:(?:my|our|the|your)\s+)?"
    r"saved\s+(?:people|leads|contacts|prospects|customers)\s*[.!?]*\s*$", re.IGNORECASE)
_OPT_OUT_HELP_RE = re.compile(r"^\s*(?:how\s+(?:does|do)\s+(?:the\s+)?(?:opt[ -]?outs?|unsubscribe(?:\s+links?)?)\s+work|"
    r"what\s+is\s+(?:an?\s+)?opt[ -]?out)\s*[.!?]*\s*$", re.I)
_FOUND_PEOPLE_RE = re.compile(_LEAD + r"(?:show(?:\s+me)?|list)\s+(?:the\s+|those\s+)?(?:people|prospects|leads|contacts|customers)\s+"
    r"(?:you\s+(?:found|identified)|found|from\s+(?:the\s+)?search)\s*[.!?]*\s*$", re.I)
_SENDER_RE = re.compile(r"\s+from\s+(?P<sender>[^?!\n]+?)\s*[.!?]*\s*$", re.I)
_WRITE_PERSON_RE = re.compile(_LEAD + r"(?:write|draft|prepare)(?:\s+(?:an?\s+)?(?:e-?mail|message|draft))?\s+(?:to|for)\s+(?P<person>.+?)\s*[.!]*\s*$"
    r"|" + _LEAD + r"e-?mail\s+(?P<people>these|those)\s+(?:people|persons|contacts)\s*[.!?]*\s*$", re.I)
_SEND_DRAFT_RE = re.compile(_LEAD + r"send\s+(?:it|(?:this|that)(?:\s+(?:one|e-?mail|draft|message))?|"
    r"(?:the\s+same|another|an?)\s+(?:e-?mail|draft))"
    r"(?:\s+again)?(?:\s+for\s+me)?(?:\s+now)?\s*[.!?]*\s*$", re.I)
_REPLIES_RE = re.compile(_LEAD + r"(?:any\s+(?:new\s+)?(?:replies|responses|answers)(?:\s+yet)?|(?:did|has)\s+any\s*(?:one|body)\s+(?:replied|responded|written back)(?:\s+yet)?|"
                         r"(?:check|show(?:\s+me)?)\s+(?:the\s+|my\s+)?(?:replies|responses|inbox\s+for\s+(?:replies|responses)(?:\s+to\s+(?:(?:my|our|the)\s+)?(?:outreach|e-?mails?|invitations?))?)|who\s+(?:replied|responded)|replies)\s*[?.!]*\s*$", re.I)
_DELIVERY_QUESTION_RE = re.compile(r".*?\b(?:did you send|went out|go out)\b[^\n]*\b(?:replied|responded)\b|"
    + _LEAD + r"(?:what\s+happened\s+(?:to|with)|did\s+you\s+send|have\s+you\s+sent)\s+"
    r"(?:(?:the|that|this|my|our)\s+)?(?:e-?mails?|messages?|send)\s*[.!?]*\s*$|"
    + _LEAD + r"(?:did|has|have)\s+(?:(?:the|that|this|my|our)\s+)?(?:e-?mails?|messages?)\s+"
    r"(?:go|gone|went)\s+out\s*[.!?]*\s*$", re.I)
_SHORTER_RE = re.compile(_LEAD + r"(?:make|keep)\s+(?:it|this|that|the\s+(?:draft|email)|(?:draft|email)\s+(?P<n>\d+))\s+(?:a\s+(?:bit|lot)\s+)?(?:shorter|tighter|more\s+concise)\s*[.!?]*\s*$|"
    + _LEAD + r"(?:shorten|trim)\s+(?:it|this|that|(?:this|that|the|my)\s+(?:e-?mail|draft))(?:\s+down)?\s*[.!?]*\s*$", re.I)
_REVISE_RE = re.compile(_LEAD + r"(?:make|keep|revise|edit|rewrite|shorten|trim)\s+(?:it|(?:e-?mail|draft)\s+(?P<n>\d+)|"
    r"(?:(?:this|that|the|my|saved)\s+)?(?:e-?mail|draft))\s+(?P<changes>\S[\s\S]*)$", re.IGNORECASE)
_FIRST_LINE_RE = re.compile(_LEAD + r"change\s+the\s+(?:first|opening)\s+line(?:\s+(?:of|in)\s+(?:draft|email)\s+(?P<n>\d+))?\s+to\s*[:\-]?\s*(?P<line>.+?)\s*$", re.I | re.S)
_REWRITE_RE = re.compile(_LEAD + r"(?:re-?write|redo|re-?draft|regenerate|recreate|re-?create|write\s+again)\s+(?P<what>"
                         r"(?:all\s+(?:of\s+)?)?(?:the\s+|my\s+|these\s+|those\s+)?(?:outreach\s+|saved\s+)?(?:drafts|emails)|them|both(?:\s+(?:of\s+)?(?:them|the\s+)?(?:drafts|emails)?)?|all|"
                         r"it|this(?:\s+(?:draft|email))?|that(?:\s+(?:draft|email))?|the\s+(?:draft|email)|(?:drafts?|emails?)\s+\d+(?:\s*(?:,|and)\s*\d+)*)"
                         r"(?:\s+(?:from\s+scratch|again|fresh|from\s+the\s+start|in\s+the\s+new\s+format))*(?:\s+please)?\s*[.!]*\s*$", re.I)
_AUDIENCE_RE = re.compile(r"^\s*(?:my\s+|our\s+)?(?:customers|audience|target\s+customers|ideal\s+customers|buyers)\s+(?:are|is)\s*[:\-]?\s*(?P<who>.{3,300}?)\s*[.!]*\s*$", re.I | re.S)
_SENT_COUNT_RE = re.compile(r"^\s*(?=[^.!?]*\bemails?\b)(?:you\s+(?:already\s+|have\s+)?sent|have\s+you\s+sent|"
    r"how\s+many\s+emails?\b[^.!?]*\bsent)\b[^.!?]*[.!?]*\s*$", re.I)
_ADDRESS_RE = re.compile(r"^\s*(?:my\s+|our\s+)?(?:business\s+|postal\s+|mailing\s+|sending\s+|sender\s+)?address\s+is\s*[:\-]?\s*(?P<address>.{8,300}?)\s*$", re.I | re.S)
_FIRST = r"(?:the\s+|my\s+|all\s+)?first\s+(?:outreach\s+)?(?:e-?mails?|messages?)"
_AUTO = r"auto(?:matic(?:ally)?)?[-\s]?(?:send(?:ing)?|approv(?:e|ing|al))\s+(?:(?:of|for)\s+)?"
_TAIL = r"(?:\s+(?:please|now|anymore|any\s+more|from\s+now\s+on))*\s*[.!]*\s*$"
_AUTO_ON_RE = re.compile(_LEAD + r"(?:(?:send|approve)\s+" + _FIRST + r"\s+automatically|(?:(?:turn|switch)\s+on\s+)?" + _AUTO + _FIRST + r")" + _TAIL, re.I)
_AUTO_OFF_RE = re.compile(_LEAD + r"(?:(?:stop|pause|disable|(?:turn|switch)\s+off)\s+" + _AUTO + _FIRST
                          + r"|(?:stop|don['’]?t|do\s+not|never)\s+send(?:ing)?\s+" + _FIRST + r"\s+automatically)" + _TAIL, re.I)
_ALLOW_RE = re.compile(_LEAD + r"(?:(?:turn|switch)\s+on\s+(?:automatic\s+|e-?mail\s+)?sending|(?:turn|switch)\s+sending\s+on|"
                       r"(?:allow|enable|unpause)\s+(?:automatic\s+|e-?mail\s+)?sending)" + _TAIL, re.I)
_AUTOMATE_RE = re.compile(_LEAD + r"(?:automate\s+(?:all\s+)?(?:of\s+)?(?:my\s+|the\s+|our\s+)?(?:e-?mails?|outreach|sending)|(?:turn|switch)\s+on\s+(?:e-?mail\s+)?automation)" + _TAIL, re.I)
_STOP_AUTOMATE_RE = re.compile(_LEAD + r"(?:stop\s+automating(?:\s+(?:my\s+|the\s+)?(?:e-?mails?|outreach))?|(?:turn|switch)\s+off\s+(?:e-?mail\s+)?automation)" + _TAIL, re.I)
_AUTO_SEARCH_RE = re.compile(_LEAD + r"keep\s+finding\s+(?:new\s+)?customers\s+until\s+(?P<target>\d+)" + _TAIL, re.I)
_STOP_SEARCH_RE = re.compile(_LEAD + r"(?:stop|pause)\s+finding\s+(?:new\s+)?customers" + _TAIL, re.I)
_RESUME_SEARCH_RE = re.compile(_LEAD + r"resume\s+(?:finding\s+(?:new\s+)?customers|customer\s+search(?:es)?)" + _TAIL, re.I)
_NOT_NOW_RE = re.compile(r"^\s*not\s+now,\s+i\s+will\s+review\s+(?:the\s+)?emails\s+myself\s*[.!]*\s*$", re.I)
_CADENCE_RE = re.compile(r"^\s*(?:(?:follow\s+up\s+on\s+)?days?\s+(?P<days>\d(?:\s*(?:,|and)\s*\d)*)(?:\s*\([a-z, ]+\))?|(?P<none>no\s+follow[-\s]?ups?(?:\s+please)?))\s*[.!]*\s*$", re.I)
_CHANGE_FU_RE = re.compile(_LEAD + r"(?:follow\s+up\s+(?:less|more)(?:\s+often)?|(?:change|pick|choose|set)\s+(?:the\s+|my\s+)?follow[-\s]?ups?(?:\s+(?:pattern|days|schedule|cadence))?|fewer\s+follow[-\s]?ups)" + _TAIL, re.I)
CADENCES = (("Day 1, 2, 4, 7 (persistent)", (1, 2, 4, 7)), ("Day 2, 4, 7 (balanced, recommended)", (2, 4, 7)),
            ("Day 3, 7 (light)", (3, 7)), ("No follow-ups", ()))
_TERMS = {"aup_required": ("acceptable use terms", "https://supymem.com/terms-of-service", "aup_accepted"),
          "dpa_required": ("data processing agreement", "https://supymem.com/data-processing-agreement", "dpa_accepted")}
_MORE_RE = re.compile(_LEAD + r"(?:show\s+(?:me\s+)?|find\s+(?:me\s+)?)?(?:more|(?:the\s+)?remaining(?:\s+\w+)?)\s+"
    r"(?:leads|customers|people)(?:\s+from\s+(?:this|the)\s+search)?\s*[.!]*\s*$", re.I)
_SEARCH_AGAIN_RE = re.compile(_LEAD + r"(?:search\s+again(?:\s+for)?|find(?:\s+me)?\s+more|look\s+for\s+more)"
                            r"(?:\s+(?P<target>[^.!?;\n]{1,300}?))?\s*[.!]*\s*$", re.I)

BLOCKED = {
    "send_paused": "Sending is paused after too many bounced emails. Check the bounced addresses, then confirm below to resume.",
    "consumer_recruitment_only": "For your audience, email sending stays off. Nothing was sent. You can ask me to draft a research note for a public community you choose.",
    "outbound_execution_disabled": "Real email sending is switched off on this server for now. Your approved drafts stay saved.",
    "gtm_gmail_required": "Emails go out from your own Gmail. Connect it below, then say \"send\".",
    "gtm_allow_sending_required": "Sending is not allowed for this venture yet. Confirm below to allow capped sending from your Gmail.",
    "gtm_sender_address_required": "Every outreach email needs your business postal address in the footer. Reply \"my address is ...\" and I will add it.",
}
_EDIT_ERRORS = {
    "gtm_draft_honesty_or_length": "That version made a claim I cannot back up or went over 120 words",
    "gtm_draft_too_long": "Your email is over 120 words",
    "gtm_draft_unsupported_claim": "Your email contains a claim I cannot back up",
    "gtm_draft_one_ask_and_optout_required": "That version needs exactly one question and the opt-out line",
    "gtm_source_disclosure_required": "Your email needs a disclosure matching this person's saved contact source",
    "gtm_draft_changed": "That draft changed since you saw it",
    "gtm_draft_unavailable": "That draft is no longer waiting for review",
    "gtm_send_in_progress": "A send is in progress",
    "gtm_draft_recipient_required": "That draft has no recipient yet",
}


_ASKED = "gtm_audience_asked:{}"
_DRAFT_REVIEW = "gtm_draft_review:{}:{}:{}"
_ELIGIBILITY_ASKED = "gtm_eligibility_asked:{}"
_ELIGIBILITY_QUESTIONS = ("What country is this person based in?", "Which basis really applies to contacting them?",
                          "What is your one-line reason or evidence?")
_PERMISSION_REASONS = {
    "unknown_jurisdiction": "I need their country to check what outreach is allowed",
    "casl_consent_required": "Canada: needs consent before email",
    "casl_mined_directory_not_implied_consent": "Canada: a directory listing is not consent; needs consent before email",
    "eprivacy_opt_in_required": "Needs opt-in consent before email",
    "dpdp_public_carveout_not_licence": "India: a public email address is not permission; needs consent or a documented legitimate business use",
    "dpdp_consent_or_legitimate_use_required": "India: needs consent or a documented legitimate business use",
    "gtm_recipient_consent_required": "Needs the recipient's consent before email",
}
_SEND_ERRORS = {
    "sample_mode": "Your sample email cannot be sent. Open Settings > Keys and connections > Your AI provider, save your model and API key, then create a real venture and find real people.",
    "gtm_draft_unsupported_claim": 'Your email contains a claim I cannot back up. Say "make it shorter", or tell me what to change.',
    "gtm_actor_required": "Sign in again, then ask to send your saved email.",
    "gtm_scope_mismatch": "Open the startup where you saved this email, then ask to send it.",
    "gtm_venture_unavailable": "Open an active startup, then check your saved emails.",
    "gtm_write_forbidden": "Ask your workspace owner for sending access, then try again.",
    "gtm_not_found": "Open your startup and check that the saved recipient is still available.",
    "gtm_approval_changed": 'Your email or sending setup changed. Choose "Show drafts", then send the version you see.',
    "gtm_gmail_required": 'Connect your Gmail, then say "send it" to try again.',
    "gtm_prospect_blocked": "Check this person's country, permission and opt-out status before asking to send again.",
    "company_contact_hold": "You recently emailed someone else at this company. Choose another company, or wait five business days after that email before asking to send again.",
    "company_contact_limit": "You have already emailed the allowed number of people at this company. Choose another company to contact.",
    "company_delivery_unconfirmed": "Your earlier email to this company has no confirmed delivery. Check your saved send records before asking to send again.",
    "suppressed_or_replied": "This person has replied or cannot receive further emails. Check your saved replies, or choose another person to contact.",
    "email_evidence_required": "Open Find customers, select this person and check their saved email source or your import consent before sending. Guessed addresses cannot be sent.",
    "send_paused": "Your sending is paused. Check your sending settings before asking to send again.",
    "gtm_source_disclosure_required": "In your startup chat, ask to show this draft. Add the disclosure from this person's saved contact source, remove conflicting source lines, then ask to send again.",
    "gtm_sender_address_required": 'Add your business postal address by saying "my address is ...", then ask to send again.',
    "gtm_sender_address_changed": "Show your draft and update its postal address to your saved business address, then ask to send again.",
    "gtm_sender_identity_required": "Show your draft and restore your signature, then ask to send again.",
    "gtm_sender_review_required": 'Your sending inbox changed. Choose "Show drafts", then send the version you see.',
    "gtm_draft_one_ask_and_optout_required": "Show your draft and keep one question, the opt-out line and the unsubscribe link, then ask to send again.",
    "gtm_generated_email_review_required": "Check the recipient's email address and permission before asking to send again.",
    "us_postal_address_required": 'Add your business postal address by saying "my address is ...", then ask to send again.',
    "opted_out": "This person opted out. Choose another person to contact.",
    "art_21_objected": "This person declined further emails. Choose another person to contact.",
}
_INTRO = "I can find potential customers and industry contacts with public contact routes. Your goal is to book a first call. "


def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


def _workspace(venture_id: str) -> str:
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise ValueError("gtm_not_found")
        return venture.workspace_id


@contextmanager
def _as(venture_id: str, actor: str | None):
    """Journey writes check the request scope's actor; bind it when chat runs outside one."""
    from app.tenancy import bind, current_or_none

    wid = _workspace(venture_id)
    scope = current_or_none()
    if scope and scope.workspace_id == wid and scope.actor not in {None, "system"}:
        yield wid
        return
    with bind(workspace_id=wid, venture_id=venture_id, actor=actor or "system"):
        yield wid


def _view(venture_id: str) -> dict[str, Any]:
    from app.gtm import journey

    return journey.status(_workspace(venture_id), venture_id, authorize=False)


def _kind(actions: list[dict]) -> str:
    return "confirmation" if any(a.get("confirmation") for a in actions) else "text"


def _card(name: str, interpretation: str, consequences: list[str], state: dict, label: str) -> dict[str, Any]:
    from dataclasses import replace

    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard

    return replace(ConfirmationCard.for_action(get_action(name), interpretation=interpretation,
                                               consequences=consequences, resulting_state=state), confirm_label=label).as_action()


def _fence(text: str) -> str:
    return "```\n" + str(text).replace("```", "'''").strip() + "\n```"


def _pending(view: dict) -> list[dict]:
    return [d for d in view.get("drafts") or [] if d.get("state") == "draft" and d.get("channel", "email") == "email"
            and not d.get("awaiting_recipient")]


def _draft_review_key(venture_id: str) -> str:
    from app.chat.conversations import current_conversation, resolve
    from app.tenancy import current_or_none

    scope = current_or_none()
    conversation = current_conversation()
    if conversation is None and scope and scope.venture_id == venture_id:
        conversation = resolve(venture_id)
    return _DRAFT_REVIEW.format(venture_id, scope.actor if scope else "system",
                               conversation.id if conversation else "legacy_shared")


def _draft_request_at(venture_id: str) -> str | None:
    from app.chat.store import CLIENT_REQUEST_ID, REPLY_REQUEST_ID, list_messages

    if REPLY_REQUEST_ID.get() == "":
        return None
    nonce = CLIENT_REQUEST_ID.get() or REPLY_REQUEST_ID.get()
    message = next((message for message in reversed(list_messages(venture_id, limit=200))
        if message.get("role") in {"user", "founder"} and (not nonce or message.get("client_request_id") == nonce)), None)
    return message.get("created_at") if message else None if nonce else utcnow().isoformat()


def _approval_payload(venture_id: str, drafts: list[dict], *, focus: bool = False, requested_at: str | None = None) -> dict:
    from app.gtm import journey
    from app.state import MEM

    revisions = {draft["doc_id"]: draft.get("revision") or journey._draft_revision(draft)
                 for draft in drafts if draft.get("doc_id")}
    review = {"doc_ids": list(revisions), "revisions": revisions}
    key = _draft_review_key(venture_id)
    previous = MEM.get(key) or {}
    selected, focus_at = previous.get("focus"), previous.get("focus_at")
    requested_at = requested_at or (_draft_request_at(venture_id) if focus else None)
    if focus and requested_at and (not focus_at or datetime.fromisoformat(requested_at) >= datetime.fromisoformat(focus_at)):
        selected = {"doc_id": drafts[0]["doc_id"], "revision": revisions[drafts[0]["doc_id"]],
                    "at": requested_at} if len(revisions) == 1 else None
        focus_at = requested_at
    bulk_revisions = {**(previous.get("bulk_revisions") or previous.get("revisions") or {}), **revisions} if focus else revisions
    MEM[key] = {**review, "bulk_revisions": bulk_revisions, "focus": selected, "focus_at": focus_at}
    return review


def _forget_draft_focus(venture_id: str) -> None:
    from app.state import MEM

    key = _draft_review_key(venture_id)
    if review := MEM.get(key):
        MEM[key] = {**review, "focus": None, "focus_at": _draft_request_at(venture_id) or utcnow().isoformat()}


def _venture_search_note(query: str, venture_name: str, saved_who: str = "") -> str:
    if is_local() and venture_name and _SEARCH_REQUEST.match(query):
        query = re.sub(rf"\s+for\s+{re.escape(venture_name)}(?=[.!]*\s*$)", "", query, flags=re.I)
    return search_note(query, saved_who)


def saved_search_request(venture_id: str, *, exclude: str = "") -> str:
    from sqlalchemy import select
    from app.chat.conversations import current_conversation
    from app.chat.idea_bridge import split_question_reply
    from app.chat.intent import declines_gtm, find_customers_request, icp_question
    from app.chat.store import _partition, _IDEA_REVISION
    from app.db.models import ChatConversation, ChatMessage, Venture
    from app.state import db
    from app.ventures.current_idea import current_revision
    from core.clock import as_utc

    # shortcut: recover from the last 200 conversation messages, persist a request ledger if longer histories need it.
    with db()() as session:
        conversation = current_conversation() or session.scalar(select(ChatConversation).where(
            ChatConversation.venture_id == venture_id, ChatConversation.visibility == "legacy_shared"))
        if conversation and conversation.venture_id != venture_id:
            raise ValueError("chat_conversation_not_found")
        venture = session.get(Venture, venture_id)
        if venture is None:
            return ""
        partition = _partition(conversation) if conversation else ChatMessage.conversation_id.is_(None)
        revision = current_revision(venture, session)
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id, partition)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(200)).all()
        for message in messages:
            if revision:
                binding = next((item for item in json.loads(message.citations_json or "[]") if _IDEA_REVISION in item), None)
                if binding is not None:
                    if binding[_IDEA_REVISION] != revision.id:
                        continue
                elif as_utc(message.created_at) <= as_utc(revision.created_at):
                    continue
            if message.role == "assistant":
                for action in json.loads(message.actions_json or "[]"):
                    card = action.get("confirmation") or action
                    payload = card.get("resulting_state") or card.get("payload") or {}
                    if card.get("action") == "gtm.find" and payload.get("note"):
                        return str(payload["note"])[:2000]
            elif message.role == "user":
                _, request = split_question_reply(message.body_md or "")
                if request == exclude:
                    continue
                if declines_gtm(request):
                    return ""
                if find_customers_request(request) and not icp_question(request):
                    if note := _venture_search_note(request, venture.name):
                        return note[:2000]
    return ""


def _requested_place(note: str) -> str:
    # shortcut: capitalised place names only, use a structured location from the router if lower-case places matter.
    match = re.search(r"\b(?:in|near|around)\s+([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)*)", note)
    return match[1] if match else ""


def _library_can_serve(note: str, icp: dict, place: str) -> bool:
    """Library filters carry only audience and place, so a request with any other condition stays web-only."""
    def words(text: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[\w&'+#-]+", text)}
    # Words from the saved idea are this venture's own context, not an extra restriction on who to find.
    known = words(" ".join(str(icp.get(key) or "") for key in ("who", "location", "geography", "problem", "offer")) + " " + place)
    return not words(_REACH_TAIL.sub("", note)) - _GENERIC_ASK - {"in", "near", "around"} - known


def empty_search_reply(view: dict) -> dict[str, Any]:
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.secrets.detect import looks_like_secret
    from core.copy import no_dashes

    icp = view.get("icp") or {}
    note = no_dashes(str(view.get("search_request") or view.get("discovery_note") or ""))
    if not _SEARCH_REQUEST.match(note) or looks_like_secret(note):
        note = ""
    changed_request = bool(note and note != no_dashes(str(view.get("discovery_note") or "")))
    note = note.rsplit("\nLatest request: ", 1)[-1]
    audience = no_dashes(str(icp.get("who") or "your audience"))
    location = no_dashes(str(icp.get("location") or icp.get("geography") or "")) or _requested_place(note)
    if location and location.casefold() not in audience.casefold():
        audience += f" in {location}"
    label = (_plain_audience(note) if changed_request else audience) or audience
    # A request that spells out the audience keeps its count and conditions in the search; anything else would garble it.
    request = _REACH_TAIL.sub("", re.sub(r"^\s*(?:please\s+)?(?:find|reach|contact|recruit|identify|locate)(?:\s+me)?\s+", "", note, flags=re.I))
    target = request if changed_request or audience.casefold() in request.casefold() else label
    if request.isdecimal():
        target = f"{request} {label}"
    context = f"Your audience: {label}."
    if note:
        context += f" Your search request: {note}"
    body = journey.empty_search_message(label, journey._consumer(icp))
    if changed_request and is_local():
        body = "Your last search didn't find contacts. I haven't searched your updated request yet. Choose a public search option to try it now. Nothing was posted or sent."
    elif changed_request:
        body = (f"Previous attempt failed; current request not attempted. Here are public routes for {label}. "
                "Nothing is posted or sent.")
    elif view.get("source_access") == "unavailable" or view.get("error_code") == "public_search_blocked":
        body = (f"Public sources could not be opened, so I could not verify reachable contacts for {label}. "
                "You can try public community or LinkedIn search, or add someone you know. Nothing is posted or sent.")
    if is_local() and view.get("error_code") == "public_search_blocked" and (setup := _search_setup_reply()):
        body += "\n\n" + setup["body_md"]
    if note:
        body += f"\n\nYour search request: {note}"
    return _reply(body, [
        {"label": f"Find communities for {label}", "action": "openworker_search", "style": "primary",
         "payload": {"query": f"{target} public subreddits forums groups", "search_context": context}},
        {"label": f"Search public LinkedIn for {label}", "action": "openworker_search", "style": "secondary",
         "payload": {"query": f"site:linkedin.com/in/ {target}", "search_context": context}},
        {"label": "Add someone you know", "action": "contacts.preview", "style": "secondary",
         "payload": {"search_context": context}},
    ])


def _saved_empty_reply(venture_id: str) -> dict[str, Any] | None:
    from sqlalchemy import select
    from app.autopilot import outreach
    from app.db.models import GtmContact, VentureDocument
    from app.gtm import journey
    from app.state import db

    workspace_id = _workspace(venture_id)
    with db()() as session:
        row = journey._row(session, venture_id)
        data = json.loads(row.provenance_json or "{}") if row else {}
        if (data.get("state") not in {"ready", "needs_input"} or not data.get("job_id")
                or any(data.get(field) for field in ("reachable_leads", "industry_experts", "channels", "need_signals"))):
            return None
        if session.scalar(select(GtmContact.id).where(
                GtmContact.workspace_id == workspace_id,
                GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True)).limit(1)):
            return None
        if session.scalar(select(VentureDocument.id).where(VentureDocument.venture_id == venture_id,
                VentureDocument.kind == outreach.KIND).limit(1)):
            return None
    data["search_request"] = saved_search_request(venture_id) or data.get("discovery_note") or ""
    reply = empty_search_reply(data)
    reply["citations"] = [{"gtm_search_progress": {"run_id": data.get("run_id"),
                            "job_id": data.get("job_id"), "state": data["state"]}}]
    return reply


def _search_audience(view: dict) -> str:
    from app.gtm import journey

    icp = view.get("icp") or {}
    saved_audience = str((view.get("search_target") or {}).get("audience") or icp.get("who") or "")
    audience = journey.search_audience(saved_audience,
                                      str(view.get("search_request") or view.get("discovery_note") or ""))
    location = str(icp.get("location") or icp.get("geography") or "")
    return f"{audience} in {location}" if location and audience == saved_audience and location.casefold() not in audience.casefold() else audience


def _search_card(view: dict, *, retry: bool = False) -> dict[str, Any]:
    from app.gtm import journey
    from app.core.local_mode import is_local

    icp = view.get("icp") or {}
    # Consumers are found through communities; requesting this search is the opt-in the old audience form set for them.
    state = {"retry": retry, **({"recruitment_opt_in": True} if icp.get("mode") == "consumer" else {})}
    empty = (retry and view.get("error_code") == "no_public_candidates"
             and not (view.get("search_request") and view["search_request"] != view.get("discovery_note")))
    note = "" if empty else str(view.get("search_request") or view.get("discovery_note") or "")[:2000]  # the same search again keeps its saved request
    who = str(icp.get("who") or "")
    if is_local() and note == f"Find {who} to talk to.":
        note = ""
    if not is_local() and not note and not empty and who and icp.get("mode") != "consumer" and not journey._consumer(who, icp):
        note = f"Find {who} to talk to."
    if note:
        state["note"] = note
    card = {"action": "gtm.find", "payload": state, "style": "primary",
            "label": "Retry" if view.get("state") == "failed" else "Try the same search again" if empty else "Search again" if retry else "Start search"}
    return card | {"style": "ghost"} if empty else card


def _search_setup_reply() -> dict[str, Any] | None:
    from app.chat.execute import local_capability_reply
    from app.core.local_mode import is_local
    from app.research.local_providers import setup_reply

    if is_local():
        from app.core.local_capabilities import capability
        name = "research" if capability("research")["state"] == "off" else "find_customers"
        return local_capability_reply(name) or local_capability_reply("research")
    return setup_reply()


def _search_failure(view: dict, code: str = "", *, retry_status: bool = False) -> dict[str, Any]:
    from app.chat.recovery import button
    from app.gtm.progress import failure_message
    from app.core.local_mode import is_local

    if is_local() and (setup := _search_setup_reply()):
        return setup

    code = code or view.get("error_code") or "gtm_preparation_failed"
    retry_status = retry_status or code == "gtm_preparation_in_progress"
    actions = ([{"label": "Retry", "action": "gtm.status", "style": "primary", "retry": True}] if retry_status else
               [_search_card(view | {"state": "needs_input" if code == "discovery_boundary_blocked" else "failed"}, retry=True)])
    actions.append(button("gtm.journey.prepare", "gtm_search_failed", view.get("job_id") or "", code))
    message = failure_message(code)
    if is_local() and code == "public_search_unavailable":
        from app.research.local_providers import SEARCH_UNAVAILABLE
        message = SEARCH_UNAVAILABLE
    if code in {"gtm_preparation_failed", "public_search_unavailable", "public_search_blocked", "public_sources_unavailable"} and (setup := _search_setup_reply()):
        message += " " + setup["body_md"]
    if (view.get("search_target") or {}).get("target_type") == "person" and view.get("state") in {"ready", "needs_input", "failed"}:
        from app.gtm.discovery import people_search_message
        summary = people_search_message(len(view.get("reachable_leads") or []) + len(view.get("industry_experts") or []),
            view["search_target"], view.get("organisations_found") or [], error_code=code)
        message = summary if code == "no_public_candidates" else message + " " + summary
    return _reply(message, actions, "gtm_search_failed")


def _search_error(venture_id: str, error: Exception, code: str | None = "gtm_status_unavailable", *,
                  view: dict | None = None, safety: str = "") -> dict[str, Any]:
    if code == "gtm_status_unavailable" and isinstance(error, ValueError) and str(error) in {
            "gtm_invalid_auto_search", "gtm_auto_search_invalid"}:
        code = "gtm_invalid_auto_search"
    retry_status = code != "gtm_invalid_input"
    reply = _search_failure(view or {}, code or "job_outcome_uncertain", retry_status=retry_status)
    failure = founder_failure(error, venture_id, "your customer search",
        {"action": "gtm.status", "payload": {}}, safety=safety)
    if retry_status:
        reply["actions"][0] = failure["actions"][0] | {"style": "primary"}
    # shortcut: shared failure copy has a saved-work suffix, expose separate reasons if that format changes.
    reply["body_md"] = failure["body_md"] + (
        " " + reply["body_md"].partition(" Your saved work is kept.")[0] if code else "")
    return reply


def terms_reply(reason: str, note: str = "") -> dict[str, Any] | None:
    """The workspace must accept the acceptable use terms, then the data processing agreement, before outreach. Accept in chat."""
    if reason not in _TERMS:
        return None
    name, url, key = _TERMS[reason]
    card = _card("gtm.accept_terms", f"Accept your workspace's {name}.",
                 [f"Read it first: {url}", "Nothing is searched or sent by accepting. Each batch still needs your approval."],
                 {key: True}, f"Accept {name}")
    return _reply(f"{note}Before I prepare outreach, your workspace needs to accept the [{name}]({url}). Confirm below.", [card], "confirmation")


def _cadence_options() -> list[dict]:
    return [{"label": label, "action": "chat.reply", "text": label, "style": "secondary"} for label, _ in CADENCES]


def _cadence_question(lead: str) -> dict[str, Any]:
    return question_reply(lead + "Which follow-up pattern should I use for people who have not replied? "
                  "Days count from the first email. You can change it any time; say \"change follow-ups\".", _cadence_options())


def _auto_search_choice(settings: dict | None = None) -> tuple[dict, str]:
    from app.gtm.journey import AUTO_SEARCH_MIN_INTERVAL_HOURS, auto_search_settings

    saved = auto_search_settings({"auto_search": settings or {}})
    choice = {key: saved[key] for key in ("target", "interval_days", "ceiling_micro")}
    choice["enabled"] = True
    return choice, (f"Keep finding new customers until you have contacted {choice['target']} businesses, "
        f"searching again when you have less than a day of eligible contacts left, at least {AUTO_SEARCH_MIN_INTERVAL_HOURS} hours apart.")


def auto_search_card(settings: dict | None = None, *, label: str = "Keep finding customers") -> dict:
    choice, description = _auto_search_choice(settings)
    if is_local():
        choice.pop("ceiling_micro")
        return {"action": "gtm.auto_search", "label": label, "style": "primary", "payload": {"auto_search": choice}}
    return _card("gtm.auto_search", description,
        ["Already-seen businesses are excluded. New drafts use your existing email settings.",
         "Searches stop at your target or after two searches find nothing new. Say \"stop finding new customers\" to pause."],
        {"auto_search": choice}, label)


def automate_card(*, daily_cap: int | None = None) -> dict[str, Any]:
    from app.autopilot.guard import DEFAULT_SEND_MAX

    daily_cap = daily_cap or DEFAULT_SEND_MAX
    card = _card("gtm.automate", "Send the approved first emails and all follow-ups from your Gmail automatically.",
                 ["I send your first emails and every follow-up from your own Gmail, without asking you each time.",
                  f"At most {daily_cap} emails a day.",
                  "Every email carries an opt-out line, and the sequence stops as soon as someone replies."],
                 {}, "Automate all emails")
    return question_reply("Your first outreach emails are ready. Want me to handle sending from here? I would send the first emails "
                  f"and all follow-ups from your Gmail automatically, up to {daily_cap} a day. Every email has an opt-out line, "
                  "and I stop emailing anyone who replies. Nothing is sent until you ask me to automate emails.",
                  [card, {"label": "Not now", "action": "chat.reply", "text": "Not now, I will review emails myself", "style": "secondary"}],
                  "confirmation")


def offer_automation(wid: str, vid: str) -> bool:
    """Once per venture: when outreach drafts are ready, Gmail is connected and sending is off, offer to automate. Never raises."""
    try:
        view = _view(vid)
        if (view.get("allow_sending") or not (view.get("gmail") or {}).get("connected") or view.get("processing_blocked_reason")
                or not _pending(view)):
            return False
        from app.chat.milestones import post_milestone

        reply = automate_card(daily_cap=view.get("daily_cap"))
        return post_milestone(vid, key="gtm_automate_offer", body_md=reply["body_md"], actions=reply["actions"], kind="confirmation",
                              citations=reply.get("citations"), requested=True)
    except Exception as exc:  # noqa: BLE001 - an offer must never break drafting
        log_founder_exception(exc, vid, "gtm.offer_automation")
        return False


def post_results(vid: str, run_id: str) -> bool:
    """A finished customer search lands in chat on its own, once per run. Never raises."""
    try:
        from app.chat.milestones import post_milestone
        readable = True
        try:
            reply = render(vid, _view(vid))
        except Exception as exc:
            readable = False
            reply = _search_error(vid, exc)

        posted = post_milestone(vid, key=f"gtm_results:{run_id}" + ("" if readable else ":unavailable"),
                               body_md=reply["body_md"], actions=reply["actions"], kind=reply["kind"], citations=reply.get("citations"), requested=True)
        return posted and readable
    except Exception as exc:  # noqa: BLE001 - "Check progress" still shows the results
        log_founder_exception(exc, vid, "gtm.post_results")
        return False


def _email(lead: dict) -> str:
    from app.gtm.reachable import published_route

    if lead.get("email_status") in {"opted_out", "suppressed"}:
        reason = "opted out" if lead["email_status"] == "opted_out" else "blocked from email"
        return reason + "; you can choose someone else"
    routes = lead.get("routes") or []
    published = published_route(lead)
    if published:  # "(published)" alone promises a first email; an excluded lead says why instead
        why = lead.get("no_draft_reason")
        label = "published company address" if published.get("company_contact") else "published"
        return f"{published['email']} ({label}; no first email: {why})" if why else f"{published['email']} ({label})"
    guessed = next((r for r in routes if r.get("email") and r.get("route_type") in {"guess", "pattern"}), None)
    return f"{guessed['email']} ({guessed.get('confidence_label') or 'guessed, unverified'})" if guessed else "none published"


def _reach_line(lead: dict) -> str:
    """The non-email routes a lead publishes, so a lead without an address still has a next step."""
    links = {"contact_form": "Contact page", "booking": "Booking page", "linkedin": "Public LinkedIn profile", "x": "X profile", "website": "Their website"}
    routes = []
    for route in lead.get("routes") or []:
        kind = route.get("kind")
        if kind == "phone":
            routes.append(f"phone {route.get('value')}")
        elif kind == "forum":
            routes.append(f"[{'Author profile' if route.get('profile') else 'Community thread'}]({route.get('url')})")
        elif kind in links:
            label = "Company contact page" if kind == "website" and route.get("company_contact") else links[kind]
            routes.append(f"[{label}]({route.get('url')})")
    return "Reach: " + ", ".join(routes[:3]) if routes else ""


def _lead_block(index: int, lead: dict, *, include_notes: bool = True) -> str:
    from app.gtm import playbooks
    from app.gtm.relationships.email_compliance import no_dashes

    person = playbooks.real_person(lead)
    if "eligible" in lead:
        person = lead.get("name") or person
    company = lead.get("company") or lead.get("name") or "Unnamed lead"
    title = f"**{index}. {person}, {company}**" if person and person != company else f"**{index}. {company}**"
    lines = [title]
    if lead.get("role"):
        lines.append("Role: " + no_dashes(str(lead["role"])))
    if lead.get("why"):
        lines.append(f"Why: {no_dashes(str(lead['why']))[:200]}")
    if lead.get("evidence_label") == "fit_only" and lead.get("kind") == "person":
        lines.append("Their public role fits your audience, but nothing public shows they have this problem. Ask whether it affects them.")
    if lead.get("contact_provenance") == "founder_import":
        lines.append("You added this person. Their details are not independently verified. Share a public profile to check them.")
    if lead.get("source_url"):
        lines.append(f"{'Reference' if 'eligible' in lead else 'Source'}: {lead['source_url']}")
    if "eligible" in lead:
        lines.append(f"Email: {lead.get('email') or 'none saved'}")
        if lead.get("blocked_reason"):
            lines.append("No first email: " + _PERMISSION_REASONS.get(lead["blocked_reason"], lead["blocked_reason"].replace("_", " ")) + ".")
    else:
        lines.append(f"Email: {_email(lead)}")
        if reach := _reach_line(lead):
            lines.append(reach)
    people = lead.get("people") or []
    if people:
        lines.append("Decision-makers: " + "; ".join(
            f"{p['name']} ({p.get('role') or 'role unknown'}) [LinkedIn search]({p['linkedin_search']})" for p in people[:3]))
    if lead.get("linkedin_search"):
        lines.append(f"[Find {person or company} on LinkedIn]({lead['linkedin_search']})")
    messages = (lead.get("playbook_messages") or [])[:1] if include_notes else []
    for message in messages:
        lines.append(f"LinkedIn connection note ({message['title']}):\n{_fence(message['connection_note'])}")
        lines.append(f"LinkedIn DM after they accept:\n{_fence(message['linkedin_dm'])}")
    return "\n".join(lines)


def _draft_block(index: int, draft: dict) -> str:
    state = {"sent": "sent", "delivery_pending": "sending is still being checked",
        "delivery_unconfirmed": "delivery is not confirmed"}.get(draft.get("state")) or (
        "approved" if draft.get("review_state") == "approved" else "waiting for your review")
    from app.connections import mailboxes
    sender = f"From: {draft['sender']['email']}\n" if mailboxes.enabled() and draft.get("sender") else ""
    return f"**Draft {index}** to {draft.get('to') or 'not selected'} ({state})\n" + _fence(
        f"{sender}Subject: {draft.get('subject') or ''}\n\n{draft.get('body') or ''}")


def _remaining_today(venture_id: str, view: dict) -> int:
    from app.autopilot import guard, outreach

    return max(0, int(view.get("daily_cap") or guard.DEFAULT_SEND_MAX) - outreach.send_slots_used_today(venture_id))


def send_block_message(code: str) -> str:
    if is_local() and code in {"outbound_execution_disabled", "gtm_gmail_required", "gtm_sender_address_required"}:
        from app.chat.execute import local_capability_reply
        if blocked := local_capability_reply("send_email"):
            return blocked["body_md"]
        return "Open Settings > Keys and connections > Your mail account, check your SMTP host, mailbox, app password and postal address, then ask to send your saved emails."
    if is_local() and code == "send_paused":
        return "Choose Show drafts, review the bounced addresses, then choose Resume sending. Nothing is sent until you ask to send again."
    if is_local() and code == "gtm_allow_sending_required":
        return "Open Settings > Capabilities, switch on Send email, then ask to send your saved emails."
    return _SEND_ERRORS.get(code) or BLOCKED.get(code) or "Open Find customers and review your saved drafts and send records before retrying."


def _send_section(venture_id: str, view: dict) -> tuple[str, list[dict]]:
    batch = next((b for b in view.get("batches") or [] if b.get("approved_count")), None)
    if batch is None:
        return "", []
    reason = view.get("send_blocked_reason")
    if reason and reason != "gtm_allow_sending_required":
        actions: list[dict] = []
        if reason == "gtm_gmail_required" and not is_local():
            actions.append({"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"})
        elif reason == "send_paused":
            actions.append(_card("gtm.resume_sending", "I checked the bounced addresses; resume sending.",
                                 ["Nothing is sent now. Each batch still needs your approval."], {"reviewed_bounced_addresses": True}, "Resume sending"))
        return send_block_message(reason), actions
    remaining = _remaining_today(venture_id, view)
    if not remaining:
        from app.autopilot import guard

        resets_at = (guard.day_start() + timedelta(days=1)).isoformat()
        return ("Your daily send limit is reached. Your approved drafts stay saved. "
                f"Your limit resets at {resets_at} UTC. You can ask to send your saved emails after it resets."), []
    n = min(batch["approved_count"], batch["cap"], remaining)
    sender = (view.get("gmail") or {}).get("sender") or "your Gmail"
    if batch.get("senders"):
        sender = ", ".join(dict.fromkeys(box["email"] for box in batch["senders"].values()))
    card = {"label": f"Send {n} email(s)", "action": "gtm.send", "style": "primary",
            "payload": {k: batch[k] for k in ("batch_id", "approval_digest", "draft_ids", "cap")}}
    return f"{n} reviewed email(s) are ready to send from {sender}. Ask to send when you are ready.", [card]


def _results_line(view: dict) -> str:
    results = view.get("results") or {}
    if not results.get("sent"):
        return ""
    return (f"So far: {results['sent']} sent, {results.get('replied', 0)} replied, "
            f"{results.get('interested', 0)} interested.")


def _permission_prospects(view: dict) -> list[dict]:
    from app.gtm.journey import PERMISSION_BLOCKS

    return [p for p in view.get("prospects") or [] if not p.get("skipped") and (p.get("blocked_reason") in PERMISSION_BLOCKS
        or p.get("imported") and p.get("blocked_reason") == "email_evidence_required")]


def _eligibility_key(venture_id: str) -> str:
    from app.chat.conversations import current_conversation

    conversation = current_conversation()
    return _ELIGIBILITY_ASKED.format(conversation.id if conversation else venture_id)


# ponytail: country names and US state names in the saved audience; use verified site addresses for other regions.
_US_STATE_NAMES = re.compile(r"\b(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
    r"Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|"
    r"Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|"
    r"Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|"
    r"Wisconsin|Wyoming)\b")


def _audience_country(venture_id: str) -> str:
    """A 2-letter country the saved audience names, offered as a hint only."""
    from app.gtm import enrich, journey

    icp = (_view(venture_id).get("icp") or {})
    who = " ".join(str(icp.get(key) or "") for key in ("who", "location", "geography"))
    found = {enrich._COUNTRY_NAMES[match[0].lower()] for match in enrich._NAME_RE.finditer(who)}
    if re.search(r"\b(?-i:US|USA)\b|\bUnited States\b", who) or _US_STATE_NAMES.search(who):
        found.add("US")
    return next(iter(found)) if len(found) == 1 else ""


def _request_country(text: str) -> str:
    from app.core.local_mode import is_local
    from app.gtm import enrich

    if not is_local():
        return ""
    found = {enrich._COUNTRY_NAMES[match[0].lower()] for match in enrich._NAME_RE.finditer(text)}
    found.update(match[1] for match in re.finditer(r"\b(?i:in|from|country(?:\s+is|:)?)\s+(?i:the\s+)?([A-Z]{2})\b", text)
        if enrich.supported_country(match[1]))
    return next(iter(found)) if len(found) == 1 else ""


def _eligibility_question(venture_id: str, prospect: dict, pending: dict) -> dict[str, Any]:
    from app.core.local_mode import is_local
    from app.gtm.relationships.geo_policy import policy_for
    from app.state import MEM

    pending = pending | {"at": time.time()}
    rule = policy_for(pending.get("country"), "email") if is_local() else {}
    consent_required = rule.get("framework") and rule.get("consent_required") and not rule.get("legitimate_use_note_required")
    if consent_required and pending.get("lawful_basis") != "consent":
        pending.pop("lawful_basis", None)
    MEM[_eligibility_key(venture_id)] = pending
    skip = {"label": "Skip", "action": "gtm.eligibility", "payload": {"contact_id": prospect["id"], "skip": True}, "style": "ghost"}
    person = prospect.get("name") or prospect.get("email") or "This person"
    if prospect.get("email") and person != prospect["email"]:
        person += f" ({prospect['email']})"
    if not pending.get("country"):
        guess = _audience_country(venture_id)
        chips = [{"label": f"{guess} (from your audience)", "action": "chat.reply", "text": guess, "style": "secondary"}] if guess else []
        example = f"such as {guess}" if guess else "such as IN, or a country name such as India"
        return question_reply(f"{person}: {_ELIGIBILITY_QUESTIONS[0]} Reply with a 2-letter code {example}. "
                      "Use the country you know they are based in; your audience is only a hint.", [*chips, skip])
    if not pending.get("lawful_basis"):
        chips = [{"label": label, "action": "gtm.eligibility", "payload": {"contact_id": prospect["id"], "lawful_basis": basis}, "style": "secondary"}
                 for label, basis in (("They gave consent", "consent"), ("Legitimate business use", "legitimate_interest"))
                 if not consent_required or basis == "consent"]
        if consent_required:
            return question_reply(f"{person} ({pending['country']}): For a first email, you need their consent. "
                f"{_ELIGIBILITY_QUESTIONS[1]} "
                "A public listing or a business reason is not consent. If they have already agreed, "
                "choose They gave consent and record how. Otherwise, choose Skip.", [*chips, skip])
        return question_reply(f"{person} ({pending['country']}): {_ELIGIBILITY_QUESTIONS[1]} "
                      "Choose only a basis you actually have. Your choice does not override the country's rules.", [*chips, skip])
    next_step = ("Once you answer, I will send the email you asked for if it passes the checks." if pending.get("send_review") else
        "Once you answer, I will prepare the email you asked for if it passes the checks. Nothing is sent." if is_local() and pending.get("draft_request") else "Nothing is sent by saving it.")
    return question_reply(f"{person}: {_ELIGIBILITY_QUESTIONS[2]} Describe the actual consent or legitimate use in your own words. "
                  f"I cannot attest for you. {next_step}", [skip])


async def _eligibility(venture_id: str, payload: dict, actor: str | None, *, draft_request: str = "") -> dict[str, Any]:
    from app.core.local_mode import is_local
    from app.gtm import enrich, journey
    from app.state import MEM

    if not is_local():
        draft_request = ""
    key = _eligibility_key(venture_id)
    view = _view(venture_id)
    blocked = _permission_prospects(view)
    cid = payload.get("contact_id")
    if cid and payload.get("skip") is True:
        with _as(venture_id, actor) as wid:
            try:
                view = journey.confirm_prospects(wid, venture_id, [{"contact_id": cid, "skip": True}])
            except ValueError:
                return _reply("I could not remove that person just now. Nothing changed or was sent.")
        MEM.pop(key, None)
        reply = render(venture_id, view)
        lead_in = "Removed from your leads." if payload.get("remove") is True else "Skipped 1 person."
        reply["body_md"] = lead_in + " They will not be emailed. Nothing was sent.\n\n" + reply["body_md"]
        return reply
    prospects = [p for p in blocked if not cid or p["id"] == cid]
    if not prospects:
        MEM.pop(key, None)
        reply = render(venture_id, view)
        reply["body_md"] = "That permission step is no longer pending. Nothing changed.\n\n" + reply["body_md"]
        return reply
    if payload.get("skip") is True:
        with _as(venture_id, actor) as wid:
            try:
                view = journey.confirm_prospects(wid, venture_id, [{"contact_id": p["id"], "skip": True} for p in prospects])
            except ValueError:
                return _reply("I could not skip them just now. Please try again. Nothing was sent.")
        MEM.pop(key, None)
        reply = render(venture_id, view)
        reply["body_md"] = f"Skipped {len(prospects)} {'person' if len(prospects) == 1 else 'people'}. They will not be emailed.\n\n" + reply["body_md"]
        return reply
    if not cid:
        return _reply("Please choose the person whose country and permission you want to record.")
    prospect = prospects[0]
    if is_local() and prospect.get("imported") and prospect.get("blocked_reason") == "email_evidence_required" and prospect.get("email_check") != "mx":
        MEM.pop(key, None)
        return _reply("Your imported email address is not verified. Nothing was sent. "
            "Open Find customers > Import your existing contacts, correct the email address or restore your connection, "
            "then preview it again.")
    pending = MEM.get(key)
    continuing = "text" in payload or "lawful_basis" in payload or "lawful_basis_note" in payload
    if not isinstance(pending, dict) or pending.get("contact_id") != cid or pending.get("actor") != actor or time.time() - pending.get("at", 0) > 1800:
        pending = {}
    if not pending or draft_request:
        country = _request_country(draft_request) or journey.normalize_geo(prospect.get("country"))
        pending = {"contact_id": cid, "actor": actor, "country": country if enrich.supported_country(country) else None}
        if draft_request:
            pending["draft_request"] = draft_request
        return _eligibility_question(venture_id, prospect, pending)
    if not continuing:
        return _eligibility_question(venture_id, prospect, pending)
    answer = payload.get("text", "")
    if not isinstance(answer, str):
        return _eligibility_question(venture_id, prospect, pending)
    answer = answer.strip()
    if answer.lower() == "skip":
        return await _eligibility(venture_id, {"contact_id": cid, "skip": True}, actor)
    if not pending.get("country"):
        country = journey.normalize_geo(enrich._COUNTRY_NAMES.get(answer.lower(), answer))
        if not enrich.supported_country(country):
            reply = _eligibility_question(venture_id, prospect, pending)
            reply["body_md"] = "Please give a supported country name or 2-letter code. " + reply["body_md"]
            return reply
        return _eligibility_question(venture_id, prospect, pending | {"country": country})
    if not pending.get("lawful_basis") or "lawful_basis" in payload:
        basis = payload.get("lawful_basis") or {"they gave consent": "consent", "legitimate business use": "legitimate_interest"}.get(answer.lower())
        if basis not in ("consent", "legitimate_interest"):
            return _eligibility_question(venture_id, prospect, pending)
        return _eligibility_question(venture_id, prospect, pending | {"lawful_basis": basis})
    note = payload.get("lawful_basis_note", answer)
    note = note.strip() if isinstance(note, str) else ""
    if not note or len(note) > 1500 or "\n" in note or "\r" in note:
        reply = _eligibility_question(venture_id, prospect, pending)
        reply["body_md"] = "Please add one line of actual reason or evidence before I save this. " + reply["body_md"]
        return reply
    with _as(venture_id, actor) as wid:
        try:
            view = journey.patch_prospect(wid, venture_id, cid, {"country": pending["country"], "lawful_basis": pending["lawful_basis"],
                                                               "lawful_basis_note": note})
        except ValueError as error:
            reply = _eligibility_question(venture_id, prospect, pending)
            reply["body_md"] = ("Please add your actual reason or evidence before I save this. " if str(error) == "gtm_lawful_basis_note_required" else
                                "I could not finish that update. Please try again. Nothing was sent. ") + reply["body_md"]
            return reply
    MEM.pop(key, None)
    updated = next(p for p in view["prospects"] if p["id"] == cid)
    reason = updated.get("blocked_reason")
    if not reason and pending.get("send_review"):
        return await _send_reviewed(venture_id, pending["send_review"], actor)
    if is_local() and not reason and pending.get("draft_request"):
        return await run_command(venture_id, "draft_named", {"contact_id": cid, "name": prospect.get("name")}, pending["draft_request"], actor)
    ready = sum(d.get("contact_id") == cid for d in _pending(view))
    change = ("Still blocked: " + (_PERMISSION_REASONS.get(reason) or BLOCKED.get(reason) or reason.replace("_", " ")) + "." if reason else
              f"{ready} draft(s) ready for your review." if ready else "Permission check passed, but no draft is ready yet. Check the saved results below.")
    reply = render(venture_id, view)
    reply["body_md"] = f"Saved your country, basis and note for {prospect.get('email')}. {change} Nothing was sent.\n\n" + reply["body_md"]
    return reply


def _library_section(venture_id: str, icp: dict, offset: int = 0) -> tuple[str, list[dict]]:
    """Optional licensed matches alongside the venture-specific web search."""
    from app.lead_library.service import match

    try:
        found = match(venture_id, limit=5, offset=offset, icp=icp)
    except Exception as exc:  # noqa: BLE001 - the library is optional; web search still works without it
        log_founder_exception(exc, venture_id, "gtm.library_match")
        return "", []
    web = {"label": "Search the web", "action": "chat.reply", "text": "Find me customers", "style": "secondary"}
    audience = _search_audience({"icp": icp}) or "your audience"
    if not found["items"]:
        if not icp.get("who"):
            return "", []
        missing = "No more close matches" if offset else "No close matches"
        return (f"{missing} in the library for {audience} yet. You can search the web for matching people. Nothing is sent.", [web])
    lines = [f"{offset + i}. **{item['name']}**, {item['role'] or 'role not listed'} at {item['company'] or 'company not listed'}. "
             f"Why: {item['why'] or 'matches your audience'}. Reach by {', '.join('company inbox email (lower priority)' if channel == 'company inbox email' else channel for channel in item['channels'])}."
             for i, item in enumerate(found["items"], 1)]
    actions = [{"label": "Add these to my leads", "action": "gtm.library_claim",
                "payload": {"entry_ids": [item["id"] for item in found["items"]]}, "style": "primary"}]
    remaining = max(0, found["total"] - offset - len(found["items"]))
    if remaining:
        actions.append({"label": "Show more", "action": "gtm.library_more", "payload": {"offset": offset + len(found["items"])}, "style": "ghost"})
    shortfall = ""
    if not remaining and len(found["items"]) < 5:
        shortfall = f"\n\nOnly {len(found['items'])} matching {'person is' if len(found['items']) == 1 else 'people are'} available in this view. You can search the web for more."
        actions.append(web)
    return (f"**Top matches from Supymem's lead library**\n\nYour audience: {audience}.\n\n" + "\n".join(lines)
            + (f"\n\n{remaining} more similar people." if remaining else "")
            + shortfall
            + "\n\nThey come from a licensed business contact list. Nothing is sent when you add them; every email still waits for your approval."), actions


def render(venture_id: str, view: dict, *, lead_limit: int | None = None, offset: int = 0, show_saved: bool = False) -> dict[str, Any]:
    from app.gtm import journey

    state = view.get("state")
    icp = view.get("icp") or {}
    if state == "failed":
        reply = _search_failure(view)
        if any(view.get(field) for field in ("reachable_leads", "industry_experts", "drafts", "prospects", "channels", "need_signals")):
            partial = render(venture_id, view | {"state": "ready", "error_code": None}, lead_limit=lead_limit, offset=offset, show_saved=show_saved)
            reply["body_md"] += "\n\nYour saved partial results:\n\n" + partial["body_md"]
            reply["actions"] += partial["actions"]
        reply["citations"] = [{"gtm_search_progress": {"job_id": view.get("job_id")}}]
        return reply
    if state in {"queued", "running"} and not show_saved:
        progress = view.get("research_progress") or {}
        step = progress.get("step_label")
        if not step:
            step = ("Your customer search is queued" if is_local() else "Starting soon. Your search is saved but has not started yet.") if state == "queued" else "Searching public sources"
        percent = f" ({progress['percent']}%)" if isinstance(progress.get("percent"), int) and progress.get("state", state) not in {"queued", "retry_wait"} else ""
        reply = _reply(f"Searching public sources for {_search_audience(view).rstrip('.!? ') or 'your venture'}: {step.rstrip('.')}{percent}. "
                       "Nothing is sent. I will post here when your search finishes or stops.",
                       [{"label": "Check progress", "action": "gtm.status", "style": "primary"}], "gtm_search_progress")
        reply["citations"] = [{"gtm_search_progress": {"run_id": progress.get("run_id"), "job_id": view.get("job_id"), "audience": _search_audience(view)}}]
        return reply
    if terms := terms_reply(view.get("processing_blocked_reason") or ""):
        return terms
    target = view.get("search_target") or {}
    people_requested = target.get("target_type") == "person" and not show_saved
    if lead_limit is None:
        lead_limit = journey._keep_limit(0, target.get("requested_count")) if people_requested else 5
    prospects = view.get("prospects") or []
    if show_saved and not any(view.get(key) for key in ("prospects", "drafts", "channels", "need_signals")):
        return _reply("You have no saved leads or drafts yet. Say \"find me customers\" to start a search. Nothing was sent.")
    show_saved = show_saved or bool(not people_requested and prospects and not (view.get("reachable_leads") or view.get("industry_experts")))
    leads = prospects if show_saved else view.get("reachable_leads") or []
    experts = [] if show_saved else view.get("industry_experts") or []
    pending = _pending(view)
    drafts = [d for d in view.get("drafts") or [] if d.get("state") == "draft" or show_saved and d.get("state") == "manual"]
    records = [draft for draft in view.get("drafts") or [] if show_saved and draft.get("state") in {
        "sent", "delivery_pending", "delivery_unconfirmed"}]
    permission = _permission_prospects(view)
    playbooks = view.get("playbooks") or []
    consumer = journey._consumer(icp)
    if people_requested and not show_saved:
        prospects, drafts, permission, playbooks = [], [], [], []
    channels = (view.get("channels") or []) if consumer or show_saved else []
    need_signals = (view.get("need_signals") or []) if consumer or show_saved else []
    # shortcut: library filters cannot verify free-text restrictions, use web search until those restrictions have structured filters.
    request = str(view.get("search_request") or view.get("discovery_note") or "")
    place = "" if icp.get("location") or icp.get("geography") else _requested_place(request)
    library_scope = not is_local() and not people_requested and _library_can_serve(request, icp, place)
    library, library_actions = _library_section(venture_id, icp | {"location": place} if place else icp) if library_scope and offset == 0 and not consumer and not icp.get("needs_input") and not show_saved and not (prospects or view.get("drafts")) else ("", [])
    library_matches = any(action["action"] == "gtm.library_claim" for action in library_actions)
    if not library_matches:
        library = ""
    if view.get("can_prepare") or not library_matches:
        library_actions = [action for action in library_actions if action["action"] != "chat.reply"]
    if not library_matches and view.get("error_code") == "no_public_candidates":
        library = ""
    if state in {"ready", "needs_input"} and (view.get("discovery_started") or view.get("job_id") or view.get("error_code") == "no_public_candidates") and not (
            leads or experts or (drafts if people_requested else view.get("drafts")) or prospects or channels or need_signals):
        reply = (_search_failure(view, "public_sources_unavailable") if view.get("source_access") == "unavailable"
            and view.get("discovery_notice") else empty_search_reply(view))
        if view.get("discovery_notice"):
            reply["body_md"] += "\n\n" + view["discovery_notice"]
        if people_requested and target.get("requested_count"):
            count = f"You found 0 of {target['requested_count']} people with a publicly listed role. "
            if view.get("error_code") not in {None, "no_public_candidates"} or view.get("source_access") == "unavailable":
                reply = _search_failure(view)
                reply["body_md"] = count + reply["body_md"]
            else:
                reply["body_md"] = count + "Try a broader role or place, or add a person you know. Nothing is sent."
        if library_matches:
            reply["body_md"] = library + "\n\n" + reply["body_md"]
            reply["actions"] = library_actions + reply["actions"]
        return reply
    if icp.get("needs_input") and not (leads or drafts or permission or channels or need_signals):
        from app.state import MEM

        field = "problem" if icp.get("who") and not icp.get("problem") else "who"
        asked = _pending_audience(venture_id)
        start_search = bool(view.get("start_search", asked.get("start_search", False)))
        note = view.get("search_request") or asked.get("note") or ""
        MEM[_ASKED.format(venture_id)] = {"field": field, "at": time.time(), "start_search": start_search, "note": note}
        MEM.pop(_SUGGESTED.format(venture_id), None)
        cost = (" Once you answer, I search public sources; nothing is posted or sent." if start_search else
                " Once you answer, your customer search is ready to start; nothing is posted or sent.")
        # One-tap answers for founders who are unsure; a plain typed answer works too.
        unsure = "Who should I reach out to?"  # routes to the three suggested groups (icp_reply)
        chips = ([{"label": f"Use: {icp['who']}", "action": "chat.reply", "text": str(icp["who"]), "style": "secondary"}]
                 if field == "who" and icp.get("who") else [])
        if field == "who":
            chips.append({"label": "Not sure, suggest some", "action": "chat.reply", "text": unsure, "style": "secondary"})
        if field == "problem":
            return question_reply(f"Got it: {icp['who']}. What problem do they have that you solve? One line is enough.{cost}", chips)
        return question_reply(_INTRO + "Who are you trying to reach? Tell me in one line, for example "
                      f"\"a specific role or life situation in a specific place\".{cost}", chips)
    parts: list[str] = [library] if library else []
    actions: list[dict] = list(library_actions)
    company_fallback = any(lead.get("company_contact_fallback") for lead in leads)
    if state in {"ready", "needs_input"} and people_requested and not company_fallback:
        from app.gtm.discovery import people_search_message
        parts.append(people_search_message(len(leads) + len(experts), target, view.get("organisations_found") or []))
    if people_requested and view.get("error_code") not in {None, "no_public_candidates"}:
        failure = _search_failure(view)
        parts.append(failure["body_md"])
        actions.extend(failure["actions"])
    if permission:
        parts.append(f"{len(permission)} people need one more step before I can email them:\n" + "\n".join(
            f"- {p.get('company') or p.get('name') or 'Company contact'}, {p.get('email') or 'email unknown'}, "
            f"{p.get('country') or 'country unknown'}: {_PERMISSION_REASONS.get(p['blocked_reason']) or p['blocked_reason'].replace('_', ' ')}."
            for p in permission))
        actions += [{"label": f"Record permission: {p.get('email') or p.get('company') or p.get('name')}", "action": "gtm.eligibility",
                     "payload": {"contact_id": p["id"]}, "style": "secondary"} for p in permission]
        actions.append({"label": "Skip them", "action": "gtm.eligibility", "payload": {"skip": True}, "style": "ghost"})
    if state == "failed":
        parts.append("The last customer search did not finish. Nothing was sent.")
    if playbooks:
        parts.append("**Outreach playbooks that fit right now**\n" + "\n".join(f"- **{p['title']}**: {p['why']}" for p in playbooks))
    if channels:
        from app.gtm.playbooks import community_post

        communities = []
        for channel in channels:
            draft = (channel.get("draft") or channel.get("post")) if show_saved else None
            from app.autopilot import claims
            draft = draft or community_post(icp, view.get("pitch") or "",
                partner=channel.get("channel_role") == "partner/channel to approach", participation=channel.get("participation") or "", venture_name=view.get("venture_name") or "", founder_words=claims.founder_words(venture_id))
            communities.append(f"- [{channel.get('name') or 'Community'}]({channel['url']}): {channel.get('why_fits') or ''}\n\n"
                               f"[Read the rules before posting]({channel.get('rules_url') or channel['url']}). {channel.get('participation') or ''}\n\n"
                               f"Copyable research draft:\n{_fence(draft)}")
        parts.append("**Communities where they gather**\n\n" + "\n\n".join(communities))
    if need_signals:
        signals = []
        for signal in need_signals:
            quote = " ".join((signal.get("quote") or "").split())
            signals.append(f"- [{signal.get('title') or signal.get('name') or 'Public problem discussion'}]({signal.get('source_url') or signal.get('url') or ''})\n\n"
                           f"> {quote[:300]}{'...' if len(quote) > 300 else ''}")
            if show_saved and signal.get("draft"):
                signals.append("Copyable research draft:\n" + _fence(signal["draft"]))
        parts.append("**People talking about this problem**\n\n" + "\n\n".join(signals))
    if channels or need_signals:
        parts.append("Nothing is posted or sent.")
    shown = (leads + experts)[offset:offset + lead_limit]
    if shown:
        from app.state import MEM

        MEM["people:" + _draft_review_key(venture_id)] = [
            {**person, "display_number": offset + index} for index, person in enumerate(shown, 1)]
        heading = "Your saved leads" if show_saved else "Company contacts" if company_fallback else "People you can reach"
        count = len(leads) + len(experts)
        summary = f"{count} found"
        parts.append(f"**{heading}** ({summary})\n\n"
                     + "\n\n".join(_lead_block(offset + i, lead, include_notes=not people_requested) for i, lead in enumerate(shown, 1)))
        if company_fallback:
            parts.append("I couldn't find someone in the requested role. Use a company contact to ask who handles this work. Nothing is sent.")
        elif people_requested:
            parts.append("Review each public source, then use its public contact route to ask about their experience. Nothing is sent. If someone publishes an email, say \"draft an email to\" and their name.")
        if len(leads) + len(experts) > offset + lead_limit:
            actions.append({"label": "Show more leads", "action": "gtm.status", "payload": {"offset": offset + lead_limit,
                **({"contact_ids": [lead["id"] for lead in prospects]} if show_saved else {})}, "style": "ghost"})
    if drafts:
        reviewed_drafts = drafts[:10] if show_saved else drafts[:5]
        parts.append("**Email drafts**\n\n" + "\n\n".join(_draft_block(i, d) for i, d in enumerate(reviewed_drafts, 1))
                     + "\n\nSay \"approve all\", \"approve draft 2\", \"make it shorter\", \"rewrite the drafts\" or \"change the first line to ...\".")
        approval = _approval_payload(venture_id, reviewed_drafts)
        waiting = [d for d in reviewed_drafts if d in pending and d.get("review_state") != "approved"]
        if waiting:
            actions.append({"label": "Approve this draft" if len(waiting) == 1 else f"Approve all {len(waiting)} drafts", "action": "gtm.approve",
                            "payload": approval | {"doc_ids": [draft["doc_id"] for draft in waiting]}, "style": "primary"})
    if records:
        parts.append("**Your email records**\n\n" + "\n\n".join(_draft_block(index, draft).replace("**Draft", "**Email", 1)
            for index, draft in enumerate(records, 1)))
        uncertain = [draft for draft in records if draft.get("state") in {"delivery_pending", "delivery_unconfirmed"}]
        if uncertain:
            from app.autopilot.outreach import delivery_resolution_actions

            parts.append("Your email may have been delivered. Check your Gmail Sent folder before another send; you will not send these emails automatically again.")
            actions.extend(action for draft in uncertain for action in delivery_resolution_actions(draft))
            if not actions:
                actions.append({"label": "Open Gmail Sent", "href": "https://mail.google.com/mail/u/0/#sent", "style": "primary"})
        if not actions:
            actions.append({"label": "Check replies", "action": "chat.reply", "text": "Any replies?", "style": "primary"})
    emailable = sum("(published)" in _email(lead) for lead in leads)
    if emailable and not people_requested and not drafts and not (view.get("gmail") or {}).get("connected"):
        if is_local():
            setup = ("Open Settings > Keys and connections > Your mail account and save your SMTP host, mailbox address, "
                     "app password and postal address.")
            actions.append({"label": "Open your mail settings", "href": "/settings/local#smtp-title", "style": "primary"})
        else:
            setup = "Connect your Gmail to send any drafts you approve."
            actions.append({"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"})
        parts.append(f"{emailable} of them publish an email address. {setup} Drafts still depend on fit and permission.")
    review = (view.get("outreach_next_step") or journey._next_step(venture_id, view)) if is_local() and show_saved and drafts else {}
    send_text, send_actions = (review["body_md"], []) if review.get("reason") == "approved_drafts" else _send_section(venture_id, view)
    if send_text:
        parts.append(send_text)
        actions += send_actions
    if line := _results_line(view):
        parts.append(line + " Ask \"any replies?\" to read them.")
    if not parts or (not shown and not drafts and not records and not playbooks and not permission and not channels and not need_signals):
        lead_in = library + "\n\n" if library else ""
        if view.get("error_code") or view.get("source_access") == "unavailable" or (view.get("discovery_started") and state != "idle"):
            reply = _search_failure(view, view.get("error_code") or (
                "public_sources_unavailable" if view.get("source_access") == "unavailable" else "no_public_candidates"))
            reply["body_md"] = lead_in + reply["body_md"]
            reply["actions"] = library_actions + reply["actions"]
            return reply
        if view.get("can_prepare"):
            notice = view.get("discovery_notice") or view.get("message") or ""
            return _reply(lead_in + (f"{notice} " if notice and state != "idle" else "")
                          + f"I'll {'also search the web' if library_matches else 'search public sources'} for {_search_audience(view).rstrip('.!? ') or 'your audience'}. "
                          + "Nothing is sent.",
                          library_actions + [_search_card(view, retry=bool(view.get("discovery_started")))])
        if library_matches:
            return _reply(library, library_actions)
        reply = _search_failure(view, retry_status=True)
        reply["body_md"] = lead_in + reply["body_md"]
        return reply
    if view.get("can_prepare") and state != "failed":
        actions.append(_search_card(view, retry=True) | {"style": "ghost"})
    elif state == "failed" and view.get("can_prepare"):
        actions.append(_search_card(view, retry=True))
    if view.get("error_code"):
        failure = _search_failure(view)
        parts.insert(0, failure["body_md"])
        actions = failure["actions"] + actions
    if view.get("discovery_notice"):
        parts.append(view["discovery_notice"])
    return SavedOutreachReply(_reply("\n\n".join(parts), actions, _kind(actions)))


_SUGGESTED = "gtm_audience_suggested:{}"
_CLEAN = "gtm_audience_clean:{}"
_NONE_OF_THESE = "None of these, I'll describe them myself."
_REFINEMENTS = {
    "Narrow to one city or region": "Choose one city or region and describe your audience there. ",
    "Widen your audience": "Include a related group with the same need as {who}. ",
}
_SUGGEST_PROMPT = (
    "You help a first-time founder pick who to reach out to first. From the venture context, propose exactly 3 specific, "
    "different customer groups for THIS idea. Each must be concrete enough to search for (role, life situation, place or "
    "type of organisation), never generic like 'small businesses' or 'everyone'. Use only the context; never invent facts "
    "about the founder, their contacts, traction or location. These are assumptions to test, not findings. "
    "Return JSON only: {\"groups\": [{\"who\": \"under 12 words\", \"problem\": \"the problem this idea solves for them, one line\", "
    "\"why\": \"one line, why they are a good first group\", \"where\": \"communities, places or roles where they can be found\", "
    "\"reach\": \"Easy, Medium or Hard, plus a few words why\"}], \"saved_audience_ok\": true|false}. "
    "Think about who USES or PAYS for the product: if the idea helps a business or person serve their own customers "
    "(for example a business's order management tool), the buyer is that business, not its customers. The first group must "
    "follow the founder's latest chosen angle or differentiation, including their geography and life situation, rather than "
    "defaulting to unrelated local groups. Current idea fields and latest idea answers override older creation text or research. "
    "Treat chosen options as assumptions, not evidence. saved_audience_ok is true only when the saved audience is already a specific, cleanly worded "
    "description of that buyer; false when it is missing, raw or messy phrasing, the buyer's own customers, or too broad to reach "
    "(everyone, anyone, all people, or people who like something). Never use em dashes."
)


def _icp_context(venture_id: str, view: dict) -> str:
    """The saved idea, clarification answers, Idea page facts and research the journey already grounds its ICP in."""
    from app.chat.context import _answers
    from app.db.models import Venture
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    from app.ventures.current_idea import current_idea, current_revision, revision_fields
    from sqlalchemy import select

    latest_answers, fields = [], {}
    with db()() as session:
        venture = session.get(Venture, venture_id)
        idea = f"Venture: {venture.name}\nCurrent idea: {current_idea(venture, session=session)}"[:3000] if venture else ""
        revision = current_revision(venture, session) if venture else None
        fields = revision_fields(revision)
        if revision:
            turns = session.scalars(select(ClarifierTurn).where(
                ClarifierTurn.session_id == revision.session_id, ClarifierTurn.workspace_id == venture.workspace_id,
                ClarifierTurn.venture_id == venture_id, ClarifierTurn.role == "founder",
            ).order_by(ClarifierTurn.turn.desc()).limit(12))
            latest_answers = [f"- {turn.text[:500]}" for turn in turns]
    icp = view.get("icp") or {}
    keep = ("idea", "offer", "problem", "competitors", "business_model", "customer_type", "market_type", "audience_type",
            "location", "geography", "company_size", "pricing_hypothesis", "goal", "workflow", "desired_outcome",
            "first_user_path", "requirements", "constraints")
    facts = {k: icp[k] for k in keep if icp.get(k)}
    parts = (["Founder's latest idea answers (newest first; chosen options are assumptions):\n" + "\n".join(latest_answers)] if latest_answers else [])
    parts += ["Current idea fields: " + json.dumps({key: value for key, value in fields.items() if not key.startswith("_")}, ensure_ascii=False, default=str)[:3000],
              idea, "Saved venture facts (idea page, clarification, research): " + json.dumps(facts, ensure_ascii=False, default=str)[:2000]]
    answers = [f"- {a['question']}: {a['chosen']}" for a in _answers(venture_id) if a.get("question") and a.get("chosen")]
    if answers:
        parts.append("Founder's clarification answers:\n" + "\n".join(answers[:12]))
    return "\n\n".join(p for p in parts if p)[:8000]


def _parse(content: str) -> tuple[bool, list[dict[str, str]]]:
    """(saved audience is already a clean buyer ICP, up to 3 groups)."""
    from app.gtm.relationships.email_compliance import no_dashes

    text = str(content or "")
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return False, []
    data = data if isinstance(data, dict) else {}
    out, seen = [], set()
    for item in data.get("groups") or []:
        if not isinstance(item, dict):
            continue
        group = {k: no_dashes(" ".join(str(item.get(k) or "").split()))[:limit]
                 for k, limit in (("who", 120), ("problem", 300), ("why", 240), ("where", 240), ("reach", 120))}
        if len(group["who"]) >= 3 and group["who"].lower() not in seen:
            seen.add(group["who"].lower())
            out.append(group)
    return data.get("saved_audience_ok") is True, out[:3]


async def _suggest(venture_id: str, view: dict, saved_who: str = "") -> tuple[bool, list[dict[str, str]]]:
    from app.credits import metered
    from app.idea.agent import broad_audience

    saved = f"\n\nSaved audience (founder's words, may be raw): {saved_who}" if saved_who else "\n\nSaved audience: none"
    if view.get("search_request"):
        saved += "\n\nYour explicit search constraints, preserve these in every suggested audience: " + view["search_request"]
    response = await metered.llm(
        [{"role": "system", "content": _SUGGEST_PROMPT},
         {"role": "user", "content": _icp_context(venture_id, view) + saved}],
        purpose="venture.chat.assistant", workspace_id=_workspace(venture_id), venture_id=venture_id, tier="flash", max_tokens=2000)
    ok, groups = _parse(response.choices[0].message.content)
    return ok and not broad_audience(saved_who), groups


def _pending_audience(venture_id: str) -> dict:
    from app.state import MEM

    asked = MEM.get(_ASKED.format(venture_id))
    return asked if isinstance(asked, dict) and time.time() - asked.get("at", 0) <= 1800 else {}


def _describe_reply(venture_id: str, lead: str = "", *, pending: dict | None = None) -> dict[str, Any]:
    """Ask for the audience in the founder's own words; the next plain answer saves it."""
    from app.state import MEM

    pending = pending or {}
    start_search = bool(pending.get("start_search"))
    MEM[_ASKED.format(venture_id)] = {"field": "who", "at": time.time(), "start_search": start_search,
                                      "note": pending.get("note") or ""}
    MEM.pop(_SUGGESTED.format(venture_id), None)
    unsure = "I'm not sure yet. Suggest three specific groups I could reach for this idea, and why."
    return question_reply(lead + "Who are you trying to reach? Tell me in one line, for example a specific role or life situation and where to find them. "
                  + ("I'll save your audience and start your search. " if start_search else "I save it as your audience. ")
                  + "Nothing is sent.",
                  [{"label": "Not sure, suggest some", "action": "chat.reply", "text": unsure, "style": "secondary"}])


async def icp_reply(venture_id: str, view: dict | None = None, *, gate: bool = False) -> dict[str, Any]:
    """Three specific customer groups to test, each a one-tap answer; picking one saves the audience.

    gate=True is the check before the first search card: a saved audience that is already a clean buyer ICP
    goes straight to the card; raw phrasing or the buyer's own customers get the three options first.
    """
    from app.idea.agent import broad_audience
    from app.state import MEM

    view = view or _view(venture_id)
    icp = view.get("icp") or {}
    suggestion_error = None
    try:
        from app.core.local_mode import is_offline
        if is_offline():
            raise RuntimeError("llm_offline")  # no paid call (and no credit hold) when the model is off
        ok, groups = await _suggest(venture_id, view, str(icp.get("who") or ""))
    except Exception as error:  # noqa: BLE001 - no suggestions: the founder can still name the audience
        suggestion_error = error
        if gate:
            log_founder_exception(error, venture_id, "gtm.suggest_audience")
        ok, groups = gate, []
    if gate and (ok or len(groups) < 3):
        if ok:
            MEM[_CLEAN.format(venture_id)] = icp.get("who")  # checked once per saved audience
        return render(venture_id, view)
    asked = _pending_audience(venture_id)
    pending = {"start_search": bool(view.get("start_search", asked.get("start_search"))),
               "note": view.get("search_request") or asked.get("note") or ""}
    if len(groups) < 3:
        from app.chat.recovery import button
        description = _describe_reply(venture_id, pending=pending)
        reply = founder_failure(suggestion_error or ValueError("gtm_customer_suggestions_unavailable"), venture_id,
            "customer group suggestions", {"action": "chat.reply", "payload": {"text": description["actions"][0]["text"]}})
        reply["body_md"] += "\n\n" + description["body_md"]
        reply["citations"] = [*(reply.get("citations") or []), {"question": True}]
        reply["actions"].append(button("gtm.journey.prepare", "gtm_search_failed", code="gtm_customer_suggestions_unavailable"))
        return reply
    current = icp.get("who") if icp.get("chat_confirmed") else ""  # never echo raw, unconfirmed phrasing
    MEM[_ASKED.format(venture_id)] = {"field": "who", "at": time.time(), **pending}
    MEM[_SUGGESTED.format(venture_id)] = {g["who"].lower(): g["problem"] for g in groups}
    blocks = [f"**{i}. {g['who']}** (assumption to test)\nWhy: {g['why']}\nWhere to find them: {g['where']}\nHow reachable: {g['reach']}"
              for i, g in enumerate(groups, 1)]
    lead = (f"Your saved audience is **{current}**. Here are three groups worth testing" if current else
            "Before I search, let's pin down who to talk to first. Here are three groups worth testing, most likely first,")
    if broad_audience(str(icp.get("who") or "")):
        lead = "That audience is too broad to reach. " + lead
    body = (lead + " for this idea, based on what you have told me so far. None of them is proven yet; "
            "your first conversations will tell you which one is real.\n\n" + "\n\n".join(blocks)
            + "\n\nTap one or type your own. I'll save it as your audience."
            + (" Then I'll start your search." if pending["start_search"] else "") + " Nothing is sent.")
    chips = [{"label": f"Keep: {current}"[:80], "action": "chat.reply", "text": f"My customers are {current}.", "style": "secondary"}] if current else []
    chips += [{"label": g["who"][:80], "action": "chat.reply", "text": f"My customers are {g['who']}.", "style": "secondary"} for g in groups]
    chips.append({"label": "None of these, I'll describe them", "action": "chat.reply", "text": _NONE_OF_THESE, "style": "ghost"})
    return question_reply(body, chips)


def _unchecked_audience(venture_id: str, view: dict) -> bool:
    """A search card is about to be offered for an audience nobody turned into a clean ICP yet."""
    from app.state import MEM
    from app.core.local_mode import is_local

    icp = view.get("icp") or {}
    if is_local() and ((icp.get("known_checks") or {}).get("audience") or {}).get("status") == "known":
        return False
    return bool(view.get("can_prepare") and icp.get("who") and not icp.get("chat_confirmed") and not view.get("search_request")
                and not (view.get("reachable_leads") or view.get("industry_experts") or view.get("drafts") or _permission_prospects(view))
                and MEM.get(_CLEAN.format(venture_id)) != icp.get("who"))


async def _shown(venture_id: str, view: dict, *, offset: int = 0) -> dict[str, Any]:
    if view.get("discovery_started"):
        reply = render(venture_id, view, offset=offset)
        if view.get("state") not in {"queued", "running", "failed"}:
            reply["citations"] = [{"gtm_search_progress": {"run_id": (view.get("research_progress") or {}).get("run_id"),
                "job_id": view.get("job_id"), "state": view.get("state")}}]
        return reply
    if (view.get("icp") or {}).get("needs_input"):
        return render(venture_id, view, offset=offset)
    if _unchecked_audience(venture_id, view):
        return await icp_reply(venture_id, view, gate=True)
    return render(venture_id, view, offset=offset)


def _with_asked_count(target: dict, query: str) -> dict:
    """ "Find 10" after a 20-person search reports against 10; the saved search itself is reused, not rerun."""
    # shortcut: numerals right after the verb, the search plan's model extraction handles the first request.
    asked = re.search(r"\b(?:find|get|show|give)(?:\s+me)?\s+(\d{1,3})\b", query, re.I)
    return {**target, "requested_count": int(asked[1])} if asked and int(asked[1]) and target.get("requested_count") else target


async def find_reply(venture_id: str, query: str = "", *, explicit: bool = False) -> dict[str, Any]:
    from app.chat.intent import declines_gtm, icp_question, requested_customer_search

    if declines_gtm(query):
        from app.state import MEM
        MEM.pop(_ASKED.format(venture_id), None)
        MEM.pop(_SUGGESTED.format(venture_id), None)
        return _reply("Nothing was started. Say \"find me customers\" whenever you are ready.",
                      [{"label": "Find customers", "action": "gtm.status", "style": "ghost"}])
    requested = explicit or requested_customer_search(query)
    if requested:
        from app.chat.execute import local_capability_reply
        if blocked := local_capability_reply("find_customers"):
            return blocked
    try:
        view = _view(venture_id)
        if view.get("can_prepare") and not view.get("discovery_started") and requested:
            if setup := _search_setup_reply():
                return setup
        previous = saved_search_request(venture_id, exclude=query) or view.get("discovery_note") or ""
        if _SHOW_FOUND_RE.match(query):
            note = ""
            view["can_prepare"] = False
        else:
            note = _venture_search_note(query, view.get("venture_name") or "", (view.get("icp") or {}).get("who") or "")
        if note and previous and note not in previous:
            latest = "\nLatest request: " + note
            # shortcut: 2000-character card notes, use structured constraints if longer requests need merging.
            if len(latest) <= 2000:
                note = previous[:2000 - len(latest)] + latest
        view["search_request"] = note or previous
        if icp_question(query):
            return await icp_reply(venture_id, view)
        icp = view.get("icp") or {}
        if (is_local() and view.get("can_prepare") and requested
                and not (view.get("discovery_started") or view["search_request"])
                and ((icp.get("known_checks") or {}).get("audience") or {}).get("status") == "confirm"
                and (who := icp.get("who"))):
            # shortcut: catch English possessive references; use grounded buyer-role extraction for other phrasing.
            if re.match(r"(?:his|her|their)\s", who, re.I):
                return await icp_reply(venture_id, {**view, "start_search": True})
            from app.state import MEM

            MEM[_ASKED.format(venture_id)] = {"field": "who", "at": time.time(), "start_search": True}
            MEM.pop(_SUGGESTED.format(venture_id), None)
            return question_reply(f"Use **{who}** as your audience, or type a different group. I'll search public sources. Nothing is sent.",
                [{"label": f"Use {who}", "action": "chat.reply", "text": f"My customers are {who}.", "style": "primary"}])
        repeat_requested = is_local() and requested and view.get("discovery_started") and view.get("state") not in {"queued", "running"}
        if view.get("can_prepare") and (
                repeat_requested or requested and not view.get("discovery_started") and not _unchecked_audience(venture_id, view)
                or note and view.get("discovery_started") and (
                    requested or not str(view.get("discovery_note") or "").endswith(note))):
            card = _search_card(view, retry=bool(view.get("discovery_started")))
            if repeat_requested:
                card["style"] = "primary"
            reply = _reply(f"I'll look for public contacts for {_search_audience(view).rstrip('.!? ') or 'your audience'}. Nothing is sent.", [card])
        else:
            view["start_search"] = (requested or not query) and not view.get("discovery_started")
            if view.get("discovery_started"):
                view["search_target"] = _with_asked_count(view.get("search_target") or {}, query)
            reply = await _shown(venture_id, view)
        if re.search(r"\b(?:who|whom)\s+should\s+(?:i|we)\s+send\b", query, re.I):
            from app.autopilot.outreach import review_notes

            if view.get("drafts") or review_notes(venture_id):
                reply["body_md"] = "You can find recipients for your outreach drafts here. Nothing is sent.\n\n" + reply["body_md"]
        return reply
    except Exception as error:  # noqa: BLE001 - an unreadable journey still leaves the founder a next step
        if isinstance(error, ValueError) and str(error) == "gtm_not_found":
            log_founder_exception(error, venture_id, "gtm.find_status")
            return _reply(_INTRO, [{"label": "Find customers", "action": "gtm.status", "style": "primary"}])
        return _search_error(venture_id, error)


class SavedOutreachReply(dict):
    """Server-rendered outreach records, never a model or client marker."""


def replies_reply(venture_id: str, *, view: dict | None = None) -> dict[str, Any]:
    view = _view(venture_id) if view is None else view
    results = view.get("results") or {}
    actions = [{"label": "Open leads and replies", "action": "gtm.status", "style": "primary"}]
    if results.get("sent") is None:
        return SavedOutreachReply(_reply("Your saved records do not include a sent count, so I cannot verify your outreach progress.", actions))
    replies = [r for r in results.get("replies") or [] if r.get("classification") != "automated"]
    body = ("Your saved email records: " + _results_line(view) if results["sent"]
            and all(results.get(key) is not None for key in ("replied", "interested"))
            else f"You have sent {results['sent']} outreach emails so far.")
    contacted = (view.get("auto_search") or {}).get("contacted")
    if contacted is not None:
        body += f" Your records show emails sent to {contacted} business{'es' if contacted != 1 else ''}."
    if results.get("replied"):
        body += f" You have {results['replied']} human repl{'y' if results['replied'] == 1 else 'ies'}"
        if results.get("interested") is not None and results.get("negative") is not None:
            body += f", {results['interested']} interested and {results['negative']} negative"
        body += "."
    if replies:
        body += "\n\nLatest replies:\n" + "\n".join(f"- **{r.get('from') or 'Someone'}** ({(r.get('classification') or 'reply').replace('human_', '').replace('_', ' ')}): "
                                   f"{' '.join(str(r.get('body') or '').split())[:160]}" for r in replies[:3])
    elif results.get("replied") == 0:
        body += " No human replies yet."
    from app.autopilot import outreach

    bounced = [contact for contact in outreach.contacts(venture_id) if contact.bounced_at]
    if bounced:
        body += "\n\nBounced emails:\n" + "\n".join(
            f"- **{' '.join(part for part in (contact.first_name, contact.last_name) if part) or contact.email}** ({contact.email}): "
            "Your email bounced. You will not email this address again."
            for contact in bounced[:3])
    sequences: dict[str, dict[int, dict]] = {}
    for draft in view.get("drafts") or []:
        if draft.get("contact_id") and type(draft.get("day")) is int and draft.get("channel", "email") == "email":
            sequences.setdefault(draft["contact_id"], {})[draft["day"]] = draft
    prospects = {prospect["id"]: prospect for prospect in view.get("prospects") or []}
    replied = {reply.get("contact_id") for reply in results.get("replies") or []}
    due = []
    for contact_id, sequence in sequences.items():
        first, prior = sequence.get(1), sequence[max(sequence)]
        prospect = prospects.get(contact_id) or {}
        if prior.get("state") == "draft":
            prior = max((draft for draft in sequence.values() if draft.get("state") == "sent"), key=lambda draft: draft["day"], default={})
        if not first or first.get("state") != "sent" or prior.get("state") != "sent" or contact_id in replied or not prospect.get("email") or prospect.get("skipped"):
            continue
        after = next((day for day in view.get("followup_days") or [] if type(day) is int and day + 1 > prior["day"]), None)
        if after is None:
            continue
        try:
            first_sent = datetime.fromisoformat(first["sent_at"])
            prior_sent = datetime.fromisoformat(prior["sent_at"])
            first_sent = first_sent.replace(tzinfo=timezone.utc) if first_sent.tzinfo is None else first_sent.astimezone(timezone.utc)
            prior_sent = prior_sent.replace(tzinfo=timezone.utc) if prior_sent.tzinfo is None else prior_sent.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        due.append(max(first_sent + timedelta(days=after), prior_sent + timedelta(hours=20)))
    if due:
        body += f" Next follow-up is due no earlier than {min(due).strftime('%Y-%m-%d %H:%M UTC')} under your saved cadence, subject to sending eligibility and approval."
    elif results["sent"]:
        body += " No next follow-up due time is available in your saved records."
    body += " You can review your leads below or ask me to check your saved replies."
    sent = [draft for draft in view.get("drafts") or [] if draft.get("state") == "sent" and draft.get("channel", "email") == "email" and draft.get("to")]
    if sent:
        body += "\n\nYour saved deliveries:\n" + "\n".join(f"- {draft['to']}" for draft in sent)
    return SavedOutreachReply(_reply(body.replace("\u2014", ",").replace("\u2013", "-"), actions))


def last_reply_asked(venture_id: str, markers: tuple[str, ...]) -> bool:
    """True only when Charlie's latest message in this chat is that question, so a later message is never captured."""
    from app.chat.store import list_messages

    try:
        last = next((message for message in reversed(list_messages(venture_id, limit=3))
                     if message.get("kind") != "next_moves"), None)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "gtm.last_reply")
        return False
    body = str(last.get("body_md") or "").casefold() if last and last.get("role") == "assistant" else ""
    return any(marker.casefold() in body for marker in markers)


def _answers_audience_question(venture_id: str, raw: str) -> bool:
    """A plain one-line answer right after Charlie asked who to reach."""
    from app.chat.intent import classify_intent

    if not _pending_audience(venture_id):
        return False
    if not last_reply_asked(venture_id, ("who are you trying to reach", "as your audience", "What problem do they have")):
        return False
    from app.chat.journey import asks_for_guidance

    return (2 <= len(raw.split()) <= 30 and not raw.endswith("?") and not asks_for_guidance(raw)
            and classify_intent(raw) in {"general", "assistant", "change"})


def command(venture_id: str, text: str) -> tuple[str, re.Match | None] | None:
    """A founder chat command for the customer journey, or None (the text keeps its normal routing)."""
    raw = (text or "").strip()
    if not raw:
        return None
    from app.chat.intent import single_outreach_email_request
    from app.core.local_mode import is_local

    if single_outreach_email_request(raw) and not _REVISE_RE.match(raw) and re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw):
        return ("draft_address", None)
    if len(raw) > 1500:
        return None
    if saved := _SAVED_PEOPLE_RE.match(raw):
        return ("status", saved)
    from app.state import MEM

    if re.fullmatch(r"what\s+happened\s*[.!?]*", raw, re.I):
        review_key = _draft_review_key(venture_id)
        if MEM.get("send:" + review_key):
            return ("send_status", None)
        if (MEM.get(review_key) or {}).get("doc_ids"):
            return ("replies", None)
    if _OPT_OUT_HELP_RE.match(raw):
        return ("opt_out_help", None)
    if _OPT_OUT_QUESTION_RE.match(raw):
        return ("contact_status", None)
    from app.chat.guard import _outreach_requests
    from app.chat.intent import requested_customer_search, linkedin_people_request
    from app.chat.search_recovery import search_forbidden

    if linkedin_people_request(raw):
        return None
    if (re.match(_LEAD + r"(?:find|identify|locate|recruit)\b", raw, re.I) and _SEARCH_REQUEST.match(raw)
            and re.match(r"(?:people|customers|users|leads|prospects|contacts)\b", _plain_audience(raw), re.I)
            and not requested_customer_search(raw) and not search_forbidden(raw) and not _outreach_requests(raw)):
        return ("find", None)
    sender = _SENDER_RE.search(raw)
    if sender and re.match(_LEAD + r"send\b", raw, re.I):
        selected = command(venture_id, raw[:sender.start()])
        if selected and selected[0] == "send_named":
            draft = selected[1]
            return ("send_named", draft | {"requested_sender": sender["sender"]} if draft else None)
    matched = next(((name, m) for name, rx in (("status", _SAVED_EMAILS_RE), ("status", _SHOW_FOUND_RE), ("status", _FOUND_PEOPLE_RE), ("draft_people", _WRITE_PERSON_RE), ("send_draft", _SEND_DRAFT_RE), ("sent_count", _SENT_COUNT_RE), ("replies", _REPLIES_RE), ("replies", _DELIVERY_QUESTION_RE), ("approve", _APPROVE_RE), ("send", _SEND_COUNT_RE), ("send", _SEND_RE),
                                                 ("shorter", _SHORTER_RE), ("first_line", _FIRST_LINE_RE), ("rewrite", _REWRITE_RE),
                                                 ("revise", _REVISE_RE),
                                                 ("audience", _AUDIENCE_RE), ("address", _ADDRESS_RE), ("search_again", _SEARCH_AGAIN_RE), ("more", _MORE_RE),
                                                 ("auto_on", _AUTO_ON_RE), ("auto_off", _AUTO_OFF_RE), ("allow", _ALLOW_RE),
                                                 ("automate", _AUTOMATE_RE), ("stop_automate", _STOP_AUTOMATE_RE), ("not_now", _NOT_NOW_RE),
                                                 ("auto_search", _AUTO_SEARCH_RE), ("stop_search", _STOP_SEARCH_RE), ("resume_search", _RESUME_SEARCH_RE),
                                                 ("cadence", _CADENCE_RE), ("change_followups", _CHANGE_FU_RE))
                    if (m := rx.match(raw))), None)
    from app.chat.intent import outreach_email_request

    send_recipients = matched and matched[0] == "send" and matched[1].groupdict().get("recipients")
    send_one = _SEND_ONE_RE.match(raw)
    write_person = matched and matched[0] == "draft_people" and matched[1].group("person")
    if not send_recipients and (send_one or write_person or outreach_email_request(raw)) and (matched is None or matched[0] == "send" or write_person):
        try:
            view = _view(venture_id)
            if send_one:
                who = send_one.group("who") or send_one.group("recipient")
                draft = _asked_draft(venture_id, view, who)
                return ("send_named", draft)
            elif re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw):
                return None
            elif len(people := _named_people(venture_id, view, raw)) == 1:
                return ("draft_named", people[0])
        except Exception as exc:  # noqa: BLE001 - no journey: leave the text to normal chat
            log_founder_exception(exc, venture_id, "gtm.command_status")
            return None
    if matched and matched[0] == "send_draft":
        try:
            return ("send_named", _asked_draft(venture_id, _view(venture_id), None))
        except Exception as exc:
            log_founder_exception(exc, venture_id, "gtm.command_status")
            return ("send_named", None)
    if matched and matched[0] == "send":
        from app.chat.guard import _bulk_outreach

        if _bulk_outreach(raw) and (not re.match(_LEAD + r"send\b", raw, re.I)
                or not (_SEND_COUNT_RE.match(raw) or send_recipients or re.search(r"\b(?:all|everything|both|saved|reviewed|approved|displayed|pending|prepared|\d+)\b", raw, re.I))):
            return None
    eligibility_key = _eligibility_key(venture_id)
    asked = MEM.get(eligibility_key)
    if (matched is None and isinstance(asked, dict) and time.time() - asked.get("at", 0) <= 1800 and not raw.endswith("?")):
        from app.chat.intent import classify_intent, looks_like_instruction
        from app.chat.journey import asks_for_guidance

        intent = classify_intent(raw)
        if is_local() and raw.lower() == "cancel" or intent in {"pause", "cancel", "send_off"}:
            cancelled_fields = {"send_review", "draft_request"} if is_local() else {"send_review"}
            if cancelled_fields.intersection(asked):
                MEM[eligibility_key] = {field: value for field, value in asked.items() if field not in cancelled_fields}
            return None
        if raw.lower() == "skip" or not asks_for_guidance(raw) and not looks_like_instruction(raw) and (
                intent in {"general", "assistant", "change"} or asked.get("lawful_basis") and intent in {"task", "research"}):
            return ("eligibility", None)
    if matched is None and raw == _NONE_OF_THESE:
        return ("describe", None)
    if matched is None and raw in _REFINEMENTS:
        return ("refine", None)
    if matched is None and _answers_audience_question(venture_id, raw):
        return ("audience", None)
    if matched is None:
        return None
    if matched[0] == "revise" and _outreach_requests(matched[1].group("changes")):
        return None
    try:
        view = _view(venture_id)
    except Exception as exc:  # noqa: BLE001 - no journey: leave the text to normal chat
        log_founder_exception(exc, venture_id, "gtm.command_status")
        return matched if matched[0] in {"draft_people", "send_draft"} else None
    has_journey = bool(view.get("drafts") or view.get("reachable_leads") or view.get("discovery_started") or (view.get("results") or {}).get("sent"))
    if matched[0] == "search_again":
        from app.chat.search_recovery import search_forbidden

        if not view.get("discovery_started") or search_forbidden(raw):
            return None
        target = " ".join((matched[1].group("target") or "").casefold().split())
        who = " ".join(str((view.get("icp") or {}).get("who") or "").casefold().split())
        # ponytail: literal saved-audience aliases only; use the audience-change card for a different segment.
        if target and target not in {"leads", "customers", "people", "them"} and target not in who:
            return None
    if matched[0] in {"approve", "shorter", "first_line", "rewrite"} and not _pending(view):
        return None
    if matched[0] == "send" and not (view.get("drafts") or view.get("reachable_leads") or view.get("prospects")
                                     or any(b.get("approved_count") for b in view.get("batches") or [])) and not (
            is_local() and re.search(r"\b(?:saved|reviewed|approved|displayed|pending|prepared)\b", raw, re.I)):
        return None
    if matched[0] == "describe":
        return matched
    if matched[0] == "audience" and not has_journey:
        from app.state import MEM

        if not MEM.get(_ASKED.format(venture_id)):
            return None  # "my customers are ..." during idea clarification answers that question instead
    if matched[0] in {"address", "more", "replies"} and not has_journey and not (
            is_local() and matched[0] == "replies" and re.search(r"\binbox\s+for\s+(?:replies|responses)\b", raw, re.I)):
        return None
    return matched


def _numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", text or "")]


def _names_person(text: str, name: str, email: str = "", *, exact: bool = False) -> bool:
    """The full name or address, or "to <first name>"; a bare first name is too often an ordinary word."""
    text, name = text.casefold(), " ".join((name or "").casefold().split())
    return bool(email and re.search(rf"(?<![\w.+-]){re.escape(email.casefold())}(?![\w.-])", text) or name and (
        re.search(rf"\b{re.escape(name)}\b", text) or not exact and re.search(rf"\bto\s+{re.escape(name.split()[0])}\b", text)))


def _named_people(venture_id: str, view: dict, text: str) -> list[dict]:
    """Everyone found so far that this message names: saved contacts, then listed people not saved as contacts yet."""
    from app.autopilot import outreach
    from app.gtm import journey, playbooks
    from app.gtm.reachable import published_route

    contacts = outreach.contacts(venture_id)
    addresses = {address.casefold() for address in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)}
    named = [{"contact_id": contact.id, "name": " ".join(filter(None, (contact.first_name, contact.last_name)))}
             for contact in contacts if contact.email and (contact.email.casefold() in addresses if addresses else
                 _names_person(text, " ".join(filter(None, (contact.first_name, contact.last_name))), contact.email))]
    with journey.db()() as session:
        row = journey._row(session, venture_id)
        saved_leads = journey._json(row.provenance_json).get("reachable_leads") or [] if row else []
    listed = []
    for lead in [*(view.get("reachable_leads") or []), *saved_leads]:
        name = playbooks.real_person(lead)
        email = str((published_route(lead) or {}).get("email") or "").casefold()
        if name and (email in addresses if addresses else _names_person(text, name)):
            contact = next((contact for contact in contacts if (contact.email or "").casefold() == email), None) if email else None
            listed.append({"lead": lead, "name": name, **({"contact_id": contact.id} if contact else {})})
    resolved_leads = {(person["lead"]["id"], person["name"].casefold()) for person in listed
        if person.get("contact_id") and person["lead"].get("id")}
    listed = [person for person in listed if person.get("contact_id")
        or (person["lead"].get("id"), person["name"].casefold()) not in resolved_leads]
    candidates = named + listed
    if not addresses:
        candidates = [person for person in candidates if _names_person(text, person["name"], exact=True)] or candidates
    matches = {person.get("contact_id") or (person["lead"].get("id") or person["name"]): person for person in candidates}
    return list(matches.values())


def _asked_draft(venture_id: str, view: dict, who: str | None, *, editing: bool = False) -> dict | None:
    """Resolve this conversation's requested or edited email, never an unrelated pending draft."""
    from app.gtm import journey
    from app.state import MEM

    pending = _pending(view)
    selected = (MEM.get(_draft_review_key(venture_id)) or {}).get("focus") or {}
    focused = next((draft for draft in pending if draft["doc_id"] == selected.get("doc_id")
        and ((draft.get("revision") or journey._draft_revision(draft)) == selected.get("revision")
             or journey._review_revision_matches(draft, selected.get("revision")))), None)
    if who:
        from app.gtm.reachable import published_route

        people = _named_people(venture_id, view, f"to {who}")
        emails = {email.casefold() for email in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", who)}
        recipient = " ".join(who.strip(" .!?\"'").casefold().split())
        if recipient not in emails and not editing:
            people = [person for person in people if any(recipient == label.casefold()
                or any(recipient == f"{label.casefold()} at {email}" for email in emails)
                for label in (person["name"], *person["name"].split()[:1]))]
            if not people:
                return None
        ids = {person.get("contact_id") for person in people} - {None}
        emails.update(route["email"].casefold() for person in people if person.get("lead") and (route := published_route(person["lead"])))
        if not editing and (len(people) > 1 or len(ids) > 1 or len(emails) > 1):
            return None
        matches = [d for d in pending if d.get("contact_id") in ids or (d.get("to") or "").casefold() in emails]
        if len(matches) > 1:
            return focused if not editing and focused in matches else None
    else:
        from app.chat.intent import outreach_email_request
        from app.chat.store import list_messages
        from core.clock import as_utc

        if focused is None:
            return None
        # shortcut: inspect 200 conversation messages and ask if the selection predates them, persist requests for longer histories.
        messages = list_messages(venture_id, limit=200)
        current_edit = editing
        for message in reversed(messages):
            if message.get("created_at") and as_utc(datetime.fromisoformat(message["created_at"])) <= as_utc(datetime.fromisoformat(selected["at"])):
                return focused
            if message.get("role") in {"user", "founder"}:
                text = message.get("body_md") or ""
                if any(pattern.match(text) for pattern in (_SHORTER_RE, _REVISE_RE, _FIRST_LINE_RE, _REWRITE_RE)):
                    if not current_edit:
                        return None
                    current_edit = False
                else:
                    current_edit = False
                if rewrite := _REWRITE_RE.match(text):
                    if re.search(r"\b(?:all|both|them|drafts|emails)\b", rewrite["what"], re.I) and len(_numbers(rewrite["what"])) != 1:
                        return None
                    continue
                if outreach_email_request(text) or _WRITE_PERSON_RE.match(text):
                    return None
        return focused if len(messages) < 200 else None
    return matches[0] if len(matches) == 1 else None


def _target(venture_id: str, view: dict, number: int) -> dict | None:
    from app.gtm import journey
    from app.state import MEM

    review = MEM.get(_draft_review_key(venture_id)) or {}
    ids = review.get("doc_ids") or []
    if not 0 < number <= len(ids):
        return None
    draft = next((draft for draft in _pending(view) if draft["doc_id"] == ids[number - 1]), None)
    revision = (review.get("revisions") or {}).get(ids[number - 1])
    return draft if draft and ((draft.get("revision") or journey._draft_revision(draft)) == revision
        or journey._review_revision_matches(draft, revision)) else None


def _error_text(error: Exception) -> str:
    code = str(error)
    return _EDIT_ERRORS.get(code, "That change could not be saved") + ". Nothing was sent."


def _send_refusal(venture_id: str, error: Exception) -> dict[str, Any]:
    from app.connections.mailboxes import MailboxUnavailable

    log_founder_exception(error, venture_id, "gtm.send")
    if isinstance(error, MailboxUnavailable):
        from app.autopilot.outreach import mailbox_failure
        reply = mailbox_failure(venture_id)
        return reply | {"body_md": reply["body_md"].replace("Nothing was sent.", "Nothing was sent by this request.", 1), "action_started": False}
    code = str(error) if isinstance(error, ValueError) else getattr(error, "code", "")
    if code == "gtm_send_in_progress":
        body = "Your earlier send is still being checked. Check your saved emails before sending again."
    else:
        reason = (_PERMISSION_REASONS[code] + ". Record the person's country and actual permission, then ask to send again."
                  if code in _PERMISSION_REASONS else send_block_message(code))
        body = "Nothing was sent by this request. " + reason
    return _reply(body, [{"label": "Show drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}], "needs_you") | {"action_started": False}


def _grounded_draft(venture_id: str, draft: dict, request: str = "") -> str:
    from app.autopilot import claims, outreach
    from app.db.models import Venture
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.state import db

    paragraphs = draft["body"].split("\n\n")
    identity = outreach.founder_signature(venture_id)
    stop = next((index for index, paragraph in enumerate(paragraphs) if index and
        (paragraph.startswith(("Source:", "Product:", "You can see what I'm building here:", "Your email address is listed publicly on",
            "Your work email is listed", "I have your address from", "From:", "Business outreach", "Stop these emails:"))
         or identity in paragraph.splitlines() or OPT_OUT in paragraph)), len(paragraphs))
    words = claims.founder_words(venture_id)
    with db()() as session:
        name = session.get(Venture, venture_id).name
    description = outreach.venture_description(venture_id)
    footer = [paragraph for paragraph in paragraphs[stop:] if paragraph.strip() != identity]
    signature = [] if footer and identity in footer[0].splitlines() else [f"Thanks,\n{identity}"]
    if re.search(r"\bPlan:", request, re.IGNORECASE):
        _, template = outreach._template(description, name, "", venture_name=name, request=request,
            word_budget=120 - len("\n\n".join([*signature, *footer]).split()))
        return "\n\n".join([template, *signature, *footer])
    _, template = outreach._template(description, name, "", venture_name=name, request=request)
    question = template.split("\n\n")[2]
    intro = f"I'm building {outreach.product_phrase(description, name)}."
    greeting = paragraphs[0] if re.match(r"^(?:Hi|Hello|Dear)\b", paragraphs[0]) and len(paragraphs[0].split()) <= 10 and not claims.violations(paragraphs[0], founder_words=words) else "Hi,"
    if not outreach.request_question(request):
        question = next((sentence.strip() for paragraph in paragraphs[1:stop]
            for sentence in re.split(r"(?<=[.!?])\s+", paragraph) if sentence.strip().endswith("?")
            and not claims.violations(sentence, founder_words=words)), question)
    return "\n\n".join([greeting, intro, question, *signature, *footer])


async def _shorter(venture_id: str, wid: str, draft: dict, request: str = "", *, retry: bool = True) -> str:
    """Check the metered edit, retry once, then keep a grounded draft with the saved footer."""
    from app.credits import metered

    from app.gtm.relationships.email_compliance import OPT_OUT, no_dashes
    from app.autopilot.outreach import founder_signature
    from app.autopilot import claims
    from app.gtm import journey

    paras = draft["body"].split("\n\n")
    identity = founder_signature(venture_id)
    stop = next((i for i, p in enumerate(paras) if i and (p.startswith(("Source:", "Product:", "You can see what I'm building here:", "Your email address is listed publicly on", "Your work email is listed", "I have your address from", "From:", "Business outreach", "Stop these emails:")) or identity in p.splitlines() or OPT_OUT in p)), len(paras))
    intro = [p for p in paras[1:stop] if re.match(r"I'm (?:.*?, )?building ", p) and "?" not in p] if stop > 2 else []
    core = "\n\n".join(p for p in paras[1:stop] if p not in intro)
    max_words = max(1, 120 - len("\n\n".join([paras[0], *intro, *paras[stop:]]).split()))
    reason = ""
    for _attempt in range(2 if retry else 1):
        response = await metered.llm([
            {"role": "system", "content": ("Revise this outreach email paragraph using all the requested constraints. " if request else
                "Shorten this outreach email paragraph to about half its length. ") + f"Use at most {max_words} words. "
                "Keep the sender introduction and specific question when they are in the paragraph. Keep exactly one question and one ask, "
                "not two requests joined by and or whether. " + claims.HONESTY_RULES +
                " Never use em dashes. Return only the revised paragraph."},
            {"role": "user", "content": json.dumps({"paragraph": core, "request": request,
                "founder_words": claims.founder_proof(venture_id), "correction": reason})}],
            purpose="venture.chat.assistant", workspace_id=wid, venture_id=venture_id, tier="flash", max_tokens=2000)
        short = no_dashes(" ".join(str(response.choices[0].message.content or "").split()).strip('"'))
        body = "\n\n".join([paras[0], *intro, short, *paras[stop:]])
        try:
            journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
            if (not short or re.sub(r"https?://\S+", "", body).count("?") != 1
                    or re.search(r"\b(?:and|or)\s+(?:whether|how|what|why|when|where|who|share|describe|explain|tell|"
                        r"(?:have|did|do|can|could|would|will|is|are)\s+(?:you|your|it|they))\b", short, re.IGNORECASE)):
                raise ValueError("gtm_draft_one_ask_and_optout_required")
            if ((not request or re.search(r"\b(?:shorter|shorten)\b", request, re.IGNORECASE))
                    and body != draft["body"] and (len(short.split()), len(short)) >= (len(core.split()), len(core))):
                reason = "Make the paragraph shorter than the original."
                continue
            return body
        except ValueError as error:
            reason = _draft_copy_refusal(error, {"body": body})["body_md"]
    body = _grounded_draft(venture_id, draft, request)
    journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
    return body


def _draft_copy_refusal(error: ValueError, draft: dict | None = None) -> dict[str, Any]:
    code = str(error)
    if code == "gtm_draft_honesty_or_length" and draft:
        code = "gtm_draft_too_long" if len(draft["body"].split()) > 120 else "gtm_draft_unsupported_claim"
    return _reply(_EDIT_ERRORS.get(code, "Your email could not be checked")
        + '. Nothing was sent. Say "make it shorter", or tell me what to change.')


async def _check_written_draft(venture_id: str, wid: str, draft: dict, request: str) -> dict:
    from llm_gateway.errors import ProviderCallError

    from app.credits.errors import CreditError
    from app.gtm import journey

    try:
        journey.validate_draft_copy(draft["body"], venture_id=venture_id, generated=True)
        return draft
    except ValueError as error:
        reason = _draft_copy_refusal(error, draft)["body_md"]
    _approval_payload(venture_id, [draft], focus=True)
    try:
        body = await _shorter(venture_id, wid, draft, request=f"{request}\nYour draft needs a correction: {reason} Keep the same grounded ask.", retry=False)
    except (CreditError, ProviderCallError, ValueError) as error:
        if getattr(error, "code", None) == "insufficient_credits":
            from app.chat.blockers import explain
            log_founder_exception(error, venture_id, "outreach.draft")
            failure = explain(venture_id, {"code": "insufficient_credits"})
        else:
            failure = founder_failure(error, venture_id, "your email draft", {"label": "Show drafts", "action": "gtm.status", "payload": {"show_saved": True}})
        return {"blocked_reason": reason + "\n\n" + failure["body_md"], "actions": failure.get("actions") or []}
    try:
        try:
            journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
        except ValueError:
            body = _grounded_draft(venture_id, draft, request)
        view = journey.patch_draft(wid, venture_id, draft["doc_id"], {"body": body, "copy_origin": "model", "expected_revision": journey._draft_revision(draft)})
        checked = next(item for item in view["drafts"] if item["doc_id"] == draft["doc_id"])
        body = checked["body"]
        journey.validate_draft_copy(body, venture_id=venture_id, generated=True)
    except ValueError as error:
        if str(error) == "gtm_draft_honesty_or_length":
            raise ValueError("gtm_draft_too_long" if len(body.split()) > 120 else "gtm_draft_unsupported_claim") from error
        raise
    return checked


async def _draft_people(venture_id: str, match: re.Match, actor: str | None) -> dict[str, Any]:
    from app.autopilot import outreach
    from app.chat.execute import execute_action
    from app.core.local_mode import is_local
    from app.state import MEM

    requested_at = _draft_request_at(venture_id)

    show = {"label": "Show people", "action": "gtm.status", "style": "primary"}
    shown = MEM.get("people:" + _draft_review_key(venture_id)) or []
    selected = shown
    if match.group("person"):
        target = re.sub(r"^(?:the\s+)?|\s+(?:one|person)$", "", match.group("person").casefold()).strip()
        position = {"first": 0, "second": 1, "third": 2, "last": len(shown) - 1}.get(target)
        if target.isdecimal():
            position = next((index for index, person in enumerate(shown) if person.get("display_number") == int(target)), None)
        selected = shown[position:position + 1] if position is not None and 0 <= position < len(shown) else [
            person for person in shown if target in {str(person.get("name") or "").casefold(),
                str(person.get("person") or "").casefold(), str(person.get("email") or "").casefold()}]
        if len(selected) != 1:
            return _reply("You need to choose one person from the people shown in this conversation. Choose \"Show people\", then tell me their name or number.", [show])
    if not selected:
        return _reply("You have no people shown in this conversation yet. Choose \"Show people\", then tell me who to write to.", [show])
    contacts = outreach.contacts(venture_id)
    drafts, notes, actions = [], [], []
    for person in selected:
        email = person.get("email") or next((route.get("email") for route in person.get("routes") or []
            if route.get("route_type") == "published"), None)
        contact = next((contact for contact in contacts if contact.id == person.get("id")
            or email and (contact.email or "").casefold() == email.casefold()), None)
        name = person.get("person") or person.get("name") or "this person"
        if contact is None:
            from app.gtm import journey
            from app.gtm.reachable import contact_seed

            if seed := contact_seed(person):
                with _as(venture_id, actor) as wid:
                    journey.authority(wid, venture_id, write=True)
                    await asyncio.to_thread(journey._ingest, wid, venture_id, [seed])
                contacts = outreach.contacts(venture_id)
                contact = next((contact for contact in contacts if contact.email == seed.get("email")), None)
        if contact is None:
            notes.append(f"For {name}, you need a usable email address and permission before you can draft an email. Choose \"Show people\" to use their public contact route or choose someone else. Nothing was sent.")
            continue
        if is_local() and len(selected) == 1:
            return await _draft_named(venture_id, {"contact_id": contact.id, "name": name}, match.string, actor)
        try:
            result = await execute_action(venture_id, "outreach.draft", payload={"run": True, "contact_id": contact.id}, actor_subject=actor)
        except Exception as error:
            return founder_failure(error, venture_id, "your email draft", show,
                safety="Review your saved drafts before trying again. Nothing was sent.")
        saved = [draft for draft in outreach.drafts(venture_id, "draft")
            if draft.get("contact_id") == contact.id and draft.get("day", 1) == 1]
        if result.get("kind") == "needs_you" or not saved:
            notes.append(f"For {name}: {result['body_md']}")
            actions.extend(result.get("actions") or [])
        else:
            for draft in saved:
                named = {**draft, "recipient_name": name}
                outreach._save_draft(venture_id, named, draft["doc_id"])
                drafts.append(named)
    if not drafts:
        return _reply("\n\n".join(notes), actions or [show], "needs_you")
    review = _approval_payload(venture_id, drafts, focus=True, requested_at=requested_at)
    body = "Your email draft" + ("s are" if len(drafts) > 1 else " is") + " saved. Nothing was sent.\n\n"
    body += "\n\n".join(f"**{draft['recipient_name']}**\n" + _draft_block(index, draft) for index, draft in enumerate(drafts, 1))
    body += ('\n\nSay "send all of them" when you want to send these emails.' if len(drafts) > 1 else
        '\n\nSay "send it" when you want to send this email.')
    return _reply(body + ("\n\n" + "\n\n".join(notes) if notes else ""), [
        {"label": f"Send {len(drafts)} emails" if len(drafts) > 1 else "Send this email", "action": "gtm.send", "payload": review, "style": "primary"}])


def _suppression_reply(venture_id: str, wid: str, drafts: list[dict], actor: str | None) -> dict | None:
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.state import db

    with db()() as session:
        for draft in drafts:
            contact = session.get(GtmContact, draft.get("contact_id"))
            if not contact or contact.workspace_id != wid:
                raise ValueError("gtm_not_found")
            if outreach._suppressed(session, wid, contact, replied_ok=draft.get("kind") == "reply"):
                return _contact_status(venture_id, f"to {draft['to']}", actor)
    return None


def _recipient_label(draft: dict, name: str | None = None) -> str:
    address = draft.get("to") or ""
    label = draft.get("recipient_name") or name or ""
    return f"{label} ({address})" if label and label.strip().casefold() != address.casefold() else address


async def _send_permission(venture_id: str, drafts: list[dict], review: dict, actor: str | None) -> dict | None:
    from app.state import MEM

    blocked = {person["id"] for person in _permission_prospects(_view(venture_id))}
    for draft in drafts:
        if draft.get("contact_id") in blocked:
            reply = await execute(venture_id, "gtm.eligibility", {"contact_id": draft["contact_id"]}, actor)
            key = _eligibility_key(venture_id)
            if pending := MEM.get(key):
                MEM[key] = {**pending, "send_review": review}
            return reply
    return None


async def _send_reviewed(venture_id: str, payload: dict, actor: str | None) -> dict[str, Any]:
    from app.gtm import journey
    from app.state import MEM

    try:
        with _as(venture_id, actor) as wid:
            ids, revisions = payload.get("doc_ids") or [], payload.get("revisions") or {}
            saved = MEM.get(_draft_review_key(venture_id)) or {}
            saved_revisions = saved.get("bulk_revisions") or saved.get("revisions") or {}
            drafts = [draft for draft in journey.outreach.drafts(venture_id) if draft.get("doc_id") in ids] if isinstance(ids, list) else []
            if not isinstance(ids, list) or not ids or any(not isinstance(document_id, str) for document_id in ids) or not isinstance(revisions, dict) or any(
                    revisions.get(document_id) != saved_revisions.get(document_id) for document_id in ids) or {draft["doc_id"] for draft in drafts} != set(ids) or any(
                    not journey._review_revision_matches(draft, revisions.get(draft["doc_id"])) and not (
                        draft.get("state") in {"sent", "delivery_pending", "delivery_unconfirmed"}
                        and journey._review_revision_matches(draft | {"state": "draft"}, revisions.get(draft["doc_id"]))) for draft in drafts):
                return _reply('Your saved draft changed or is no longer ready to send. Nothing was sent. Choose "Show drafts", then send the version you see.',
                    [{"label": "Show drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}])
            if any(draft.get("state") != "draft" for draft in drafts):
                return _reviewed_send_result(venture_id, ids, drafts, _reply("You already requested these emails. "
                    "Check your saved send records before sending any remaining drafts."))
            from app.core.local_mode import is_local
            if is_local():
                from app.db.models import GtmContact
                from app.state import db
                eligible = []
                with db()() as session:
                    for draft in drafts:
                        contact = session.get(GtmContact, draft.get("contact_id"))
                        if not contact or contact.workspace_id != wid:
                            raise ValueError("gtm_not_found")
                        if not journey.outreach._suppressed(session, wid, contact, replied_ok=draft.get("kind") == "reply"):
                            eligible.append(draft)
                if not eligible and (blocked := _suppression_reply(venture_id, wid, drafts, actor)):
                    return blocked
                drafts = eligible
                if not drafts:
                    return _reply('None of your selected contacts can be emailed. Nothing was sent. Open Find customers and choose eligible contacts before asking for new drafts.')
                ids = [draft["doc_id"] for draft in drafts]
                revisions = {document_id: revisions[document_id] for document_id in ids}
                payload = {**payload, "doc_ids": ids, "revisions": revisions}
            _approval_payload(venture_id, [draft | {"revision": revisions[draft["doc_id"]]} for draft in drafts], focus=True)
            if blocked := _suppression_reply(venture_id, wid, drafts, actor):
                return blocked
            if question := await _send_permission(venture_id, drafts, payload, actor):
                return question
        reviewed = await execute(venture_id, "gtm.approve", payload, actor)
        with _as(venture_id, actor) as wid:
            view = journey.status(wid, venture_id, authorize=False)
            chosen = [draft for draft in view.get("drafts") or [] if draft.get("doc_id") in ids
                and draft.get("state") == "draft" and draft.get("review_state") == "approved"]
            if {draft["doc_id"] for draft in chosen} != set(ids):
                saved = [draft for draft in journey.outreach.drafts(venture_id) if draft.get("doc_id") in ids]
                return _reviewed_send_result(venture_id, ids, saved, reviewed)
    except ValueError as error:
        return _send_refusal(venture_id, error)
    with _as(venture_id, actor) as wid:
        reply = reviewed
        stopped = False
        try:
            for batch_id in dict.fromkeys(draft.get("batch_id") for draft in chosen):
                batch_ids = [draft["doc_id"] for draft in chosen if draft.get("batch_id") == batch_id]
                try:
                    cap = journey._batch_review(venture_id, batch_id, draft_ids=batch_ids)["cap"]
                except ValueError as error:
                    reply = _send_refusal(venture_id, error)
                    break
                if cap < 1:
                    break
                for offset in range(0, len(batch_ids), cap):
                    try:
                        request = journey._batch_review(venture_id, batch_id, draft_ids=batch_ids[offset:offset + cap])
                    except ValueError as error:
                        reply = _send_refusal(venture_id, error)
                        stopped = True
                        break
                    send_payload = {key: request[key] for key in ("batch_id", "approval_digest", "draft_ids", "cap")}
                    send_payload["review_revisions"] = {document_id: revisions[document_id] for document_id in request["draft_ids"]}
                    reply = await execute(venture_id, "gtm.send", send_payload, actor)
                    delivered = {draft["doc_id"] for draft in journey.outreach.drafts(venture_id) if draft.get("state") == "sent"}
                    if reply.get("kind") != "text" or not set(request["draft_ids"]) <= delivered:
                        stopped = True
                        break
                if stopped:
                    break
        except Exception as error:
            reply = founder_failure(error, venture_id, "your selected emails", {"action": "gtm.status", "payload": {"show_saved": True}},
                safety="Check your saved send records before trying again.")
        saved = [draft for draft in journey.outreach.drafts(venture_id) if draft.get("doc_id") in ids]
        return _reviewed_send_result(venture_id, ids, saved, reply)


def _reviewed_send_result(venture_id: str, ids: list[str], saved: list[dict], reply: dict) -> dict[str, Any]:
    from core.copy import no_dashes

    from app.state import MEM

    MEM["send:" + _draft_review_key(venture_id)] = {"doc_ids": ids, "reply": reply}
    sent = [draft for draft in saved if draft.get("state") == "sent"]
    remaining = [draft for draft in saved if draft.get("state") != "sent"]
    deferred = bool(remaining) or reply.get("kind") != "text"
    body = (f"Your saved records confirm {len(sent)} email{' was' if len(sent) == 1 else 's were'} sent." if len(ids) == 1 else
        f"Your saved records confirm {len(sent)} of your {len(ids)} selected emails were sent.")
    if sent:
        body += "\n\nSent to:\n" + "\n".join(f"- {_recipient_label(draft)}" for draft in sent)
    if remaining:
        body += "\n\nYour remaining selected emails:\n" + "\n".join(f"- {_recipient_label(draft)}: "
            + ("not sent; your draft is saved" if draft.get("state") == "draft" else "delivery is not confirmed") for draft in remaining)
    if len(saved) != len(ids):
        body += "\n\nSome selected emails are no longer available in your saved records."
    if deferred or reply.get("action_started") is False:
        note = reply["body_md"].replace("Nothing was sent by this request.", "Your remaining emails did not start sending.", 1) if sent else reply["body_md"]
        body += "\n\n" + note
    if not deferred:
        body += "\n\nCheck your saved send records before retrying, or ask for replies."
    actions = [action for action in reply.get("actions") or [] if not action.get("confirmation")] or [
        {"label": "Check send records", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}]
    return SavedOutreachReply(_reply(no_dashes(body), actions, reply.get("kind", "text") if deferred else "text")
        | {key: reply[key] for key in ("code", "action_started") if key in reply})


async def _draft_named(venture_id: str, person: dict, text: str, actor: str | None) -> dict[str, Any]:
    from app.autopilot import outreach
    from app.core.local_mode import is_local
    from app.gtm import journey
    from app.gtm.reachable import contact_seed

    requested_at = _draft_request_at(venture_id)
    who = person["name"] or "this person"
    contact_id = person.get("contact_id")
    with _as(venture_id, actor) as wid:
        if not contact_id:
            lead = person["lead"]
            seed = contact_seed(lead)
            if not seed:
                if lead.get("email_status") in {"opted_out", "suppressed"}:
                    return _reply(f"{who}: {_email(lead)}. Nothing was sent.",
                        [{"label": "Show people", "action": "gtm.status", "style": "primary"}])
                source = lead.get("source_url")
                return _reply(f"{who} has no published email, so I can't write to them from here. Nothing was sent. "
                              + (f"You can reach them through [their public page]({source})." if source else "Ask me to find more people with a public address."))
            journey._ingest(wid, venture_id, [seed])
            contact_id = next((c.id for c in outreach.contacts(venture_id) if (c.email or "").lower() == seed["email"].lower()), None)
            if not contact_id:
                return _reply(f"I could not save {who} as a contact, so no email was written. Nothing was sent. Ask me to try again.")
        result = await asyncio.to_thread(journey.draft_for_review, venture_id, contact_id=contact_id, founder_request=text)
    draft = next((d for d in reversed(outreach.drafts(venture_id, "draft")) if d.get("contact_id") == contact_id), None)
    blocked = next((p.get("blocked_reason") for p in _view(venture_id).get("prospects") or [] if p.get("id") == contact_id), None)
    if draft and (result.get("kind") != "needs_you" or blocked in journey.PERMISSION_BLOCKS):
        try:
            with _as(venture_id, actor) as wid:
                draft = await _check_written_draft(venture_id, wid, draft, text)
                if draft.get("blocked_reason"):
                    return _reply(draft["blocked_reason"], draft.get("actions"))
        except ValueError as error:
            return _draft_copy_refusal(error, draft)
    if result.get("kind") == "needs_you" and blocked in journey.PERMISSION_BLOCKS:
        if is_local():
            return await _eligibility(venture_id, {"contact_id": contact_id}, actor, draft_request=text)
        return await execute(venture_id, "gtm.eligibility", {"contact_id": contact_id}, actor)
    if not draft or result.get("kind") == "needs_you":
        return {**_reply(result.get("body_md") or f"I could not write an email to {who}. Nothing was sent. Ask me to try again."), "actions": result.get("actions") or []}
    if is_local() and not draft.get("sample"):
        draft = {**draft, "recipient_name": who}
        outreach._save_draft(venture_id, draft, draft["doc_id"])
    review = _approval_payload(venture_id, [draft], focus=True, requested_at=requested_at)
    if is_local() and draft.get("sample"):
        return _reply(_draft_block(1, draft).replace("**Draft 1**", "**SAMPLE draft**")
            + "\n\nYour sample email cannot be sent. Tell me what to change, or open Settings > Keys and connections > Your AI provider, save your model, then create a real venture and find real people. Nothing was sent.")
    return _reply((f"**{who}**\n" if is_local() else "") + _draft_block(1, draft).replace("**Draft 1**", "**Draft**")
        + f"\n\nNothing was sent. Say \"send it\" and it goes to {draft['to']}, or tell me what to change.",
        [{"label": "Send this email", "action": "gtm.send", "payload": review, "style": "primary"}] if is_local() else [])


async def _send_named(venture_id: str, draft: dict, actor: str | None) -> dict[str, Any]:
    """The founder asked for this one email to go: approve exactly it and send it; every server check still applies."""
    from app.chat.confirmations import start_requested_work
    from app.gtm import journey
    from app.chat.execute import local_capability_reply
    from app.core.local_mode import is_local

    if blocked := local_capability_reply("send_email"):
        return blocked

    with _as(venture_id, actor) as wid:
        view = _view(venture_id)
        _approval_payload(venture_id, [draft], focus=True)
        if blocked := _suppression_reply(venture_id, wid, [draft], actor):
            return blocked
        try:
            journey.validate_draft_copy(draft["body"])
        except ValueError as error:
            return _draft_copy_refusal(error, draft)
        if not is_local() and not (view.get("gmail") or {}).get("connected"):
            return _reply(BLOCKED["gtm_gmail_required"], [{"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"}], "navigation")
        initial_revision = draft.get("revision") or journey._draft_revision(draft)
        if "channel" not in draft or journey.outreach.mailboxes.enabled() and not draft.get("sender"):
            journey.outreach._save_draft(venture_id, draft | {"channel": "email"}, draft["doc_id"])
            draft = next(saved for saved in journey.outreach.drafts(venture_id, "draft") if saved["doc_id"] == draft["doc_id"])
            draft["revision"] = journey._draft_revision(draft)
        revision = draft.get("revision") or journey._draft_revision(draft)
        if sender := draft.get("requested_sender"):
            from app.connections import mailboxes
            from app.state import db

            try:
                with db()() as session:
                    mailbox_id = draft.get("mailbox_id")
                    if not re.fullmatch(r"(?:my\s+|the\s+)?(?:connected\s+)?(?:mailbox|gmail|inbox|e-?mail)(?:\s+account)?", sender, re.I):
                        matches = [box for box in mailboxes.list_read_mailboxes(session, wid) if box["email"].casefold() == sender.casefold()]
                        if len(matches) != 1:
                            raise mailboxes.MailboxUnavailable("mailbox_unavailable")
                        mailbox_id = matches[0]["mailbox_id"]
                    mailbox = mailboxes.resolve_send_mailbox(session, wid, venture_id=venture_id, mailbox_id=mailbox_id)
                if mailboxes.enabled() and draft.get("mailbox_id") != mailbox["mailbox_id"]:
                    view = journey.patch_draft(wid, venture_id, draft["doc_id"],
                        {"mailbox_id": mailbox["mailbox_id"], "expected_revision": revision})
                    draft = next(item for item in view["drafts"] if item["doc_id"] == draft["doc_id"])
                    revision = draft["revision"]
            except mailboxes.MailboxUnavailable:
                return _reply("You do not have one available connected inbox matching that sender. Nothing was sent. "
                    "Connect that inbox below, or tell me the email address of another connected inbox.",
                    [{"label": "Connect your inbox", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"}])
        if reply := await _send_permission(venture_id, [draft], _approval_payload(venture_id, [draft], focus=True), actor):
            if revision != initial_revision:
                reply["body_md"] = _draft_block(1, draft).replace("**Draft 1**", "**Draft**") + "\n\n" + reply["body_md"]
            return reply
        if draft.get("review_state") != "approved":
            journey.patch_draft(wid, venture_id, draft["doc_id"], {"decision": "approved", "expected_revision": revision})
        view = _view(venture_id)
        batch = [journey._batch_review(venture_id, draft["batch_id"], draft_ids=[draft["doc_id"]])]
        if view.get("send_blocked_reason") == "consumer_recruitment_only" and draft.get("founder_import"):
            view = view | {"send_blocked_reason": None}
    body, actions = _send_section(venture_id, view | {"batches": batch})
    for action in actions:
        if action.get("action") == "gtm.send":
            action["payload"]["review_revisions"] = {draft["doc_id"]: revision}
    return await start_requested_work(venture_id, _reply(body or f"This email to {draft['to']} is saved but cannot go out yet. Nothing was sent. You can ask to see your saved emails to check what is missing.", actions, _kind(actions)), actor, actions={"gtm.send"})


def _contact_status(venture_id: str, text: str, actor: str | None) -> dict[str, Any]:
    from app.autopilot import outreach
    from app.db.models import GtmContact
    from app.gtm import journey
    from app.state import db

    actions = [{"label": "Show your saved emails", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}]
    with _as(venture_id, actor) as wid:
        journey.authority(wid, venture_id, actor=actor)
        people = _named_people(venture_id, _view(venture_id), text)
        if len(people) != 1 or not people[0].get("contact_id"):
            return _reply("You can give me the contact's full name or email address so I can check your saved records. Nothing was sent.", actions)
        with db()() as session:
            contact = session.get(GtmContact, people[0]["contact_id"])
            if not contact or contact.workspace_id != wid:
                raise ValueError("gtm_not_found")
            name = people[0]["name"] or "This contact"
            count = contact.times_emailed or 0
            body = f"Your saved records show {count} email{'s' if count != 1 else ''} sent to {name}. "
            if contact.unsubscribed_at or outreach._suppressed(session, wid, contact):
                return _reply(body + outreach.suppression_message(session, wid, contact, name=name), actions)
            else:
                body += f"You have no saved opt-out for {name}. Sending still needs a contact you are allowed to email and an email you asked to send. "
    return _reply(body + "Nothing was sent by this request. You can view your saved emails below, ask me to draft one, or ask me to find someone else.", actions)


async def run_command(venture_id: str, name: str, match: Any, text: str, actor: str | None) -> dict[str, Any]:
    from app.gtm import journey
    from app.chat.execute import local_capability_reply
    from app.core.local_mode import is_local, is_sample_mode

    requested_capability = ("content" if name in {"draft_address", "draft_named", "draft_people", "revise", "rewrite", "shorter", "first_line"}
                            else "send_email" if name in {"send_named", "send_draft", "send", "allow", "auto_on"}
                            else "background_work" if name in {"auto_search", "resume_search", "automate"} else None)
    if requested_capability and (blocked := local_capability_reply(requested_capability)):
        return blocked
    if requested_capability == "send_email" and is_local():
        with _as(venture_id, actor):
            if is_sample_mode():
                return _reply(_SEND_ERRORS["sample_mode"])
    # A raised error here would end as "outcome unknown" (409); these requests have a known outcome to report.
    show_drafts = {"action": "gtm.status", "payload": {"show_saved": True}}
    if name == "draft_address":
        from core.ids import new_id

        from app.chat.guard import guard_reply
        from app.chat.offers import requested_email_reply
        from app.planning.execute import _review_drafts

        if guarded := guard_reply(venture_id, text):
            return guarded
        try:
            with _as(venture_id, actor):
                notes = await _review_drafts(venture_id, "Draft one outreach email for me to review", "Founder request: " + text)
                return await requested_email_reply(venture_id, text, notes, new_id("ob"), [], _draft_request_at(venture_id))
        except Exception as error:  # noqa: BLE001 - preserve the known draft-only outcome with a next step
            if getattr(error, "code", None) == "insufficient_credits":
                from app.chat.blockers import explain
                return explain(venture_id, {"code": "insufficient_credits"})
            return founder_failure(error, venture_id, "your email draft", show_drafts,
                safety="Nothing was sent. Tell me what to change, or ask me to draft again.")
    if name == "find":
        from app.chat.confirmations import start_requested_work
        return await start_requested_work(venture_id, await find_reply(venture_id, text), actor, actions={"gtm.find"})
    if name == "status":
        if _SAVED_EMAILS_RE.match(text):
            from app.chat.execute import _saved_outreach_drafts

            try:
                return await asyncio.to_thread(_saved_outreach_drafts, venture_id)
            except Exception as error:  # noqa: BLE001 - a failed lookup still has a known unsent outcome
                return _search_error(venture_id, error)
        reply = await execute(venture_id, "gtm.status", {"show_saved": True} if _SAVED_PEOPLE_RE.match(text) else {}, actor)
        if reply["kind"] != "gtm_search_failed":
            reply["body_md"] += '\n\nYou can ask me to draft an email to someone listed here, or say "find more people".'
        return reply
    if name == "opt_out_help":
        return _reply('Your outreach emails include a "no thanks" reply option and an unsubscribe link. '
            "When someone uses the unsubscribe link, their opt-out is saved and future emails to that address are blocked. "
            'When your connected inbox is checked and an opt-out reply such as "no thanks" is recorded, future emails and follow-ups stop. '
            "You do not need to keep a separate list or check recipients yourself. I have not checked your inbox by answering this question. "
            "You can ask whether a named contact opted out to check your saved records, or view your saved emails below.",
            [{**show_drafts, "label": "Show your saved emails", "style": "primary"}])
    if name == "revise":
        from app.chat.guard import guard_reply

        if guarded := guard_reply(venture_id, text):
            return guarded
        try:
            from app.state import MEM

            view = _view(venture_id)
            review = MEM.get(_draft_review_key(venture_id)) or {}
            pending = _pending(view)
            selected = [draft for draft in pending if draft.get("doc_id") in review.get("doc_ids", [])]
            number = match.group("n")
            recipient = re.match(r"(?:to|for)\s+(.+)", match["changes"], re.IGNORECASE)
            draft = _target(venture_id, view, int(number)) if number else _asked_draft(venture_id, view, recipient[1] if recipient else None, editing=True)
            if draft is None:
                _forget_draft_focus(venture_id)
                return _reply("You have more than one possible email, or no email ready to edit. Nothing changed or sent. "
                    "Show your saved emails below, then tell me which email to revise.",
                    [{**show_drafts, "label": "Show your saved emails", "style": "primary"}])
            revision = (review.get("revisions") or {}).get(draft["doc_id"]) if draft in selected else draft.get("revision") or journey._draft_revision(draft)
            return await execute(venture_id, "gtm.draft_edit",
                {"doc_id": draft["doc_id"], "expected_revision": revision, "edit": "revise", "request": text}, actor)
        except Exception as error:
            return founder_failure(error, venture_id, "your draft update", show_drafts,
                safety="Nothing was sent. Show your saved emails, then tell me which email to revise.")
    if name == "contact_status":
        try:
            return _contact_status(venture_id, text, actor)
        except Exception as error:
            return founder_failure(error, venture_id, "your saved email records", show_drafts,
                safety="Nothing was sent. You can give me the contact's full name or email address to check again.")
    if name == "draft_named":
        try:
            return await _draft_named(venture_id, match, text, actor)
        except Exception as error:  # noqa: BLE001
            return founder_failure(error, venture_id, "your email", show_drafts, safety="Nothing was sent. Ask me to write it again.")
    if name == "send_named":
        try:
            if match is None:
                _forget_draft_focus(venture_id)
                view = _view(venture_id)
                people = _named_people(venture_id, view, text)
                if len(people) == 1 and people[0].get("contact_id") and not any(
                        draft.get("contact_id") == people[0]["contact_id"] for draft in _pending(view)):
                    return _contact_status(venture_id, text, actor)
                return _reply("Which email do you want to send? Nothing was sent. Show your saved emails, then name the recipient or ask me to draft the email you want.",
                    [{**show_drafts, "label": "Show your saved emails", "style": "primary"}])
            return await _send_named(venture_id, match, actor)
        except ValueError as error:
            return _reply(f"{_error_text(error)} Say \"send it\" to try again.", [{**show_drafts, "label": "Show drafts", "style": "primary"}])
        except Exception as error:  # noqa: BLE001
            return founder_failure(error, venture_id, "your email", show_drafts,
                safety="I could not confirm whether it went out. Check your sent mail before asking me to send it again.")
    if name == "draft_people":
        try:
            return await _draft_people(venture_id, match, actor)
        except Exception as error:
            return founder_failure(error, venture_id, "your email draft", {"action": "gtm.status", "payload": {"show_saved": True}},
                safety="Review your saved drafts before trying again. Nothing was sent.")
    if name == "send_draft":
        return await run_command(venture_id, "send_named", _asked_draft(venture_id, _view(venture_id), None), text, actor)
    if name in {"refine", "more", "search_again", "auto_search", "resume_search"}:
        try:
            view = _view(venture_id)
        except Exception as error:
            return _search_error(venture_id, error)
    if name == "eligibility":
        from app.state import MEM

        pending = MEM.get(_eligibility_key(venture_id)) or {}
        from app.chat.execute import execute_action

        return await execute_action(venture_id, "gtm.eligibility", payload={"contact_id": pending.get("contact_id"), "text": text}, actor_subject=actor)
    if name == "describe":
        return _describe_reply(venture_id, "No problem. ", pending=_pending_audience(venture_id))
    if name == "refine":
        who = (view.get("icp") or {}).get("who") or "your current audience"
        return _describe_reply(venture_id, _REFINEMENTS[text].format(who=who))
    if name == "send_status":
        from app.state import MEM

        with _as(venture_id, actor):
            receipt = MEM.get("send:" + _draft_review_key(venture_id)) or {}
            if receipt.get("doc_ids"):
                ids = receipt["doc_ids"]
                saved = [draft for draft in journey.outreach.drafts(venture_id) if draft.get("doc_id") in ids]
                return _reviewed_send_result(venture_id, ids, saved, receipt["reply"])
        return replies_reply(venture_id)
    if name in {"replies", "sent_count"}:
        if name == "replies" and is_local() and (_REPLIES_RE.match(text) or (
                _DELIVERY_QUESTION_RE.match(text) and re.search(r"\b(?:replied|responded)\b", text, re.I))):
            from app.gtm.local_mail import enabled, blocked_message
            if blocked := local_capability_reply("read_replies"):
                return blocked
            with _as(venture_id, actor):
                if is_sample_mode():
                    return _reply("Your sample inbox cannot be checked. Open Settings > Keys and connections > Your AI provider, save your model and API key, then create a real venture and check replies to your outreach.")
            if not enabled("read_replies"):
                return _reply(blocked_message("read_replies"))
            try:
                with _as(venture_id, actor) as wid:
                    journey.authority(wid, venture_id, actor=actor)
                    await journey.outreach.poll_replies(venture_id, force=True)
            except Exception as error:
                return founder_failure(error, venture_id, "your email replies",
                    {"action": "chat.reply", "payload": {"text": text}, "retry": True},
                    safety="Check your IMAP host and port in Settings > Keys and connections, then retry. Nothing was sent.")
        return replies_reply(venture_id)
    if name == "more":
        return render(venture_id, view, offset=5)
    if name == "search_again":
        from app.chat.confirmations import start_requested_work
        return await start_requested_work(venture_id,
            _reply("Your next search is ready.", [_search_card(view, retry=True) | {"style": "primary"}], "confirmation"),
            actor, actions={"gtm.find"})
    if name == "allow":
        from app.chat.execute import execute_action
        return await execute_action(venture_id, "autopilot.allow_send", payload={}, actor_subject=actor)
    if name in {"auto_search", "resume_search", "stop_search"}:
        if name == "stop_search":
            return await execute(venture_id, "gtm.auto_search", {"auto_search": {"enabled": False}}, actor)
        settings, _ = _auto_search_choice(view.get("auto_search"))
        if name == "auto_search":
            target = int(match.group("target"))
            if target < 1:
                return _search_failure(view, "gtm_invalid_auto_search", retry_status=True)
            settings["target"] = target
        return await execute(venture_id, "gtm.auto_search", {"auto_search": settings}, actor)
    if name == "automate":
        view = _view(venture_id)
        if blocked := local_capability_reply("send_email"):
            return blocked
        if not is_local() and not (view.get("gmail") or {}).get("connected"):
            return _reply("Emails go out from your own Gmail. Connect it below, then say \"automate my emails\". Nothing changed.",
                          [{"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"}])
        from app.chat.confirmations import start_requested_work

        return await start_requested_work(venture_id,
            automate_card(daily_cap=view.get("daily_cap")), actor, actions={"gtm.automate"})
    if name in {"not_now", "stop_automate"}:
        from app.autopilot import service

        service.update_settings(venture_id, allow_send=False, send_max_per_day=0)
        with _as(venture_id, actor) as wid:
            journey.update_settings(wid, venture_id, {"auto_approve_first_emails": False, "auto_search": {"enabled": False}})
        return _reply("Automation is off: sending is paused and first emails wait for your approval. Nothing new will be sent "
                      "until you say \"automate my emails\" or \"turn on sending\".")
    if name == "change_followups":
        return _cadence_question("")
    if name == "cadence":
        days = () if match.group("none") else tuple(int(n) for n in re.findall(r"\d", match.group("days")))
        if days not in dict((v, k) for k, v in CADENCES):
            return question_reply("I can follow up on day 1, 2, 4, 7, on day 2, 4, 7, on day 3, 7, or not at all. Pick one below. Nothing changed.",
                          _cadence_options())
        with _as(venture_id, actor) as wid:
            view = journey.update_settings(wid, venture_id, {"followup_days": list(days)})
        if tuple(view.get("followup_days") or ()) != days:
            return _reply("Start a customer search first (say \"find me customers\"); then I can set follow-ups. Nothing changed.")
        label = next(k for k, v in CADENCES if v == days)
        return _reply("No follow-ups: each person gets only the first email." if not days else
                      f"Follow-ups set to {label.split(' (')[0].replace('Day', 'day')} after the first email. "
                      "They stop as soon as someone replies or opts out.")
    if name == "auto_off":
        return await execute(venture_id, "gtm.auto_first", {"on": False}, actor)
    if name == "auto_on":
        view = _view(venture_id)
        if not (view.get("drafts") or view.get("reachable_leads") or view.get("prospects") or view.get("discovery_started")):
            return _reply("Start a customer search first (say \"find me customers\"); then I can send first emails automatically. Nothing changed.")
        return await execute(venture_id, "gtm.auto_first", {"on": True}, actor)
    if name == "send":
        from app.state import MEM

        reviewed = MEM.get(_draft_review_key(venture_id)) or {}
        counted = _SEND_COUNT_RE.match(text)
        send_match = counted or _SEND_RE.match(text)
        if counted or re.search(r"\b(?:all|everything|both|them|those|these|drafts|emails|invitations?|batch|\d+)\b", text, re.I):
            view = _view(venture_id)
            pending = _pending(view)
            pending_ids = {draft["doc_id"] for draft in pending}
            if not pending_ids:
                _forget_draft_focus(venture_id)
                return _reply('You have no unsent emails. Nothing was sent. Ask me to draft another email, or show your saved emails.',
                    [{**show_drafts, "label": "Show your saved emails", "style": "primary"}])
            revisions = {document_id: revision for document_id, revision in (reviewed.get("bulk_revisions") or reviewed.get("revisions") or {}).items()
                         if document_id in pending_ids}
            recipients = send_match.groupdict().get("recipients") if send_match else None
            if not send_match:
                revisions = {}
            if recipients:
                group = re.fullmatch(r"(?:(?:the|my|our)\s+)?(?P<eligible>eligible\s+)?(?P<business>business\s+)?(?:contacts|recipients|people|leads)", recipients, re.IGNORECASE)
                if group:
                    contacts = {person["id"] for person in view.get("prospects") or [] if not person.get("skipped")
                        and (not group["eligible"] or person.get("eligible")) and (not group["business"] or person.get("company"))}
                    selected_ids = {draft["doc_id"] for draft in pending if draft.get("contact_id") in contacts}
                else:
                    shown = view | {"drafts": [draft for draft in pending if draft["doc_id"] in revisions]}
                    selected = [recipient.strip() and _asked_draft(venture_id, shown, recipient.strip())
                        for recipient in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", recipients, flags=re.IGNORECASE)]
                    selected_ids = {draft["doc_id"] for draft in selected} if all(selected) else set()
                revisions = {document_id: revision for document_id, revision in revisions.items() if document_id in selected_ids}
            reviewed = {"doc_ids": list(revisions), "revisions": revisions}
            value = counted["count"].casefold() if counted else ""
            selection_text = text[:send_match.start("recipients")] if recipients else text
            counts = ([int(value) if value.isdecimal() else _EMAIL_COUNTS[value]] if counted else
                _numbers(selection_text) or ([2] if re.search(r"\bboth\b", selection_text, re.I) else []))
            if not reviewed.get("doc_ids") or counts and (len(counts) != 1 or counts[0] != len(reviewed["doc_ids"])):
                _forget_draft_focus(venture_id)
                return _reply("Which emails do you want to send? Nothing was sent. Show your saved emails, then name the recipients or ask to send all the emails shown.",
                    [{**show_drafts, "label": "Show your saved emails", "style": "primary"}])
            return await execute(venture_id, "gtm.send", {"doc_ids": reviewed["doc_ids"], "revisions": reviewed.get("revisions") or {}}, actor)
        return await run_command(venture_id, "send_named", _asked_draft(venture_id, _view(venture_id), None), text, actor)
    if name == "approve":
        what = match.group("what") if match else ""
        numbers = _numbers(what)
        return await execute(venture_id, "gtm.approve", {"numbers": numbers} if numbers else {"all": True}, actor)
    if name == "rewrite":
        what = match.group("what").lower() if match else ""
        numbers = _numbers(what)
        single = re.fullmatch(r"it|this(?:\s+\w+)?|that(?:\s+\w+)?|the\s+(?:draft|email)", what)
        return await execute(venture_id, "gtm.draft_rewrite", {"numbers": numbers} if numbers else {"single": bool(single)}, actor)
    if name in {"shorter", "first_line"}:
        n = match.group("n") if match else None
        payload = {"number": int(n)} if n else {}
        payload.update({"edit": "shorter"} if name == "shorter" else {"edit": "first_line", "line": match.group("line")[:300]})
        return await execute(venture_id, "gtm.draft_edit", payload, actor)
    with _as(venture_id, actor) as wid:
        try:
            if name == "audience":
                from app.state import MEM

                asked = _pending_audience(venture_id)
                suggested = MEM.get(_SUGGESTED.format(venture_id)) or {}
                answer = (match.group("who") if match else text).strip()[:300]
                from app.chat import experiments
                from app.experiments import autonomous
                if asked.get("experiment_request") and (is_local() or autonomous.autopilot_enabled()):
                    return await experiments.answer_audience(venture_id, answer, asked)
                if asked.get("field") == "problem":
                    current = journey.status(wid, venture_id, authorize=False)["icp"]
                    view = journey.update_icp(wid, venture_id, current.get("who") or answer, problem=answer, start=False)
                else:
                    # The picked group's problem line fills the gap only when no problem is saved yet.
                    known = (journey.status(wid, venture_id, authorize=False).get("icp") or {}).get("problem")
                    view = journey.update_icp(wid, venture_id, answer, start=False,
                                              problem=None if known else suggested.get(answer.lower()) or None)
                MEM.pop(_ASKED.format(venture_id), None)
                MEM.pop(_SUGGESTED.format(venture_id), None)
                view["start_search"] = bool(asked.get("start_search"))
                view["search_request"] = asked.get("note") or ""
                if view["start_search"] and view.get("can_prepare"):
                    reply = await execute(venture_id, "gtm.find", {"retry": True,
                        "note": view["search_request"] or None,
                        "recruitment_opt_in": (view.get("icp") or {}).get("mode") == "consumer"}, actor)
                else:
                    reply = render(venture_id, view)
                if view.get("can_prepare"):
                    reply["body_md"] = "Saved your audience. " + reply["body_md"]
                return reply
            if name == "address":
                view = journey.update_settings(wid, venture_id, {"sender_postal_address": match.group("address")})
                body, actions = _send_section(venture_id, view)
                return _reply("Saved your business address; it is added to every outreach email footer. " + body, actions, _kind(actions))
        except Exception as error:
            return founder_failure(error, venture_id, "your customer details",
                {"action": "gtm.status", "payload": {}},
                safety="Review the current details and search progress before making another change.")
    return await find_reply(venture_id, text)


async def execute(venture_id: str, action: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    """Customer chat actions, all through the journey's own gates."""
    from app.gtm import journey
    from app.chat.execute import _action_capability, local_capability_reply
    from app.core.local_mode import is_local

    if capability_name := _action_capability(action, payload):
        if blocked := local_capability_reply(capability_name):
            return blocked

    if action == "gtm.eligibility":
        return await _eligibility(venture_id, payload, actor)
    if action == "gtm.status":
        try:
            journey.authority(_workspace(venture_id), venture_id, actor=actor)
            if not any(key in payload for key in ("linkedin_note", "contact_ids", "show_saved", "offset")):
                if empty := await asyncio.to_thread(_saved_empty_reply, venture_id):
                    return empty
            if payload.get("linkedin_note") is True:
                from app.gtm.leads import linkedin_note

                with _as(venture_id, actor) as wid:
                    journey.authority(wid, venture_id)
                    try:
                        note = linkedin_note(wid, venture_id, payload.get("contact_id"), _view(venture_id))
                    except ValueError:
                        return _reply("That lead is unavailable or has opted out. No note was written or sent.")
                return _reply(f"LinkedIn connection note for {note['name']}:\n\n" + _fence(note["note"])
                    + "\n\nYou open LinkedIn and send this yourself. Nothing was sent.")
            view = await asyncio.to_thread(_view, venture_id)
            ids = payload.get("contact_ids")
            if isinstance(ids, list) and all(isinstance(cid, str) for cid in ids):
                order = {cid: index for index, cid in enumerate(ids)}
                view["prospects"] = sorted(view.get("prospects") or [], key=lambda lead: order.get(lead["id"], len(order)))
                view["drafts"] = sorted(view.get("drafts") or [], key=lambda draft: order.get(draft.get("contact_id"), len(order)))
                return render(venture_id, view, offset=max(0, int(payload.get("offset") or 0)), show_saved=True)
            if payload.get("show_saved") is True or (
                    not (view.get("discovery_started") or view.get("job_id") or view.get("reachable_leads") or view.get("industry_experts"))
                    and any(d.get("state") == "draft" for d in view.get("drafts") or [])):
                return render(venture_id, view, offset=max(0, int(payload.get("offset") or 0)), show_saved=True)
            return await _shown(venture_id, view, offset=max(0, int(payload.get("offset") or 0)))
        except Exception as error:
            return _search_error(venture_id, error)
    if action == "gtm.accept_terms":
        keys = {k: True for k in ("aup_accepted", "dpa_accepted") if payload.get(k) is True}
        with _as(venture_id, actor) as wid:
            view = journey.update_settings(wid, venture_id, keys) if keys else _view(venture_id)
        done = "Accepted. " if keys else ""
        return terms_reply(view.get("processing_blocked_reason") or "", done) or _reply(
            done + "Outreach can go ahead. Say \"find me customers\" or \"send\" when you are ready. Nothing was sent.")
    if action == "gtm.auto_search":
        if not isinstance(payload.get("auto_search"), dict):
            return _search_failure({}, "gtm_invalid_auto_search", retry_status=True)
        with _as(venture_id, actor) as wid:
            try:
                view = journey.update_settings(wid, venture_id, {"auto_search": payload.get("auto_search")})
            except Exception as error:
                return _search_error(venture_id, error,
                    safety="Review the current settings before making another change.")
        settings = view["auto_search"]
        if not settings["enabled"]:
            return _reply("New customer searches are paused. Your existing email settings are unchanged. Say \"resume finding customers\" to continue.")
        _, description = _auto_search_choice(settings)
        return _reply("Automatic customer searches are on. " + description)
    if action == "gtm.automate":
        try:
            view = _view(venture_id)
        except Exception as exc:
            return _search_error(venture_id, exc)
        if blocked := local_capability_reply("send_email"):
            return blocked
        if not is_local() and not (view.get("gmail") or {}).get("connected"):
            return _reply("Gmail is not connected, so nothing changed. Connect it below, then say \"automate my emails\".",
                          [{"label": "Connect Gmail", "action": "connections.connect", "payload": {"query": "connect gmail"}, "style": "primary"}])
        with _as(venture_id, actor) as wid:
            changes = {"auto_approve_first_emails": True, "allow_sending": True}
            try:
                view = journey.update_settings(wid, venture_id, changes)
            except Exception as error:
                return _search_error(venture_id, error, view=view,
                    safety="Review the current settings before making another change.")
        search = _auto_search_choice(view["auto_search"])[1] + " " if view.get("auto_search", {}).get("enabled") else ""
        return _cadence_question(f"Automation is on. First emails and follow-ups go out from your Gmail, up to {view.get('daily_cap')} a day. " + search)
    if action == "gtm.auto_first":
        on = payload.get("on") is True
        with _as(venture_id, actor) as wid:
            view = journey.update_settings(wid, venture_id, {"auto_approve_first_emails": on})
        if bool(view.get("auto_approve_first_emails")) != on:
            return _reply("Start a customer search first (say \"find me customers\"); then I can change this. Nothing changed.")
        if not on:
            return _reply("First emails wait for your approval again. Follow-ups to people you already emailed still go out on schedule. Nothing was sent.")
        off = "" if view.get("allow_sending") else " Sending is not allowed yet; say \"turn on sending\" to start."
        return _reply("First emails now go out automatically once drafted, within your daily limit." + off)
    if action == "gtm.library_more":
        text, actions = _library_section(venture_id, _view(venture_id).get("icp") or {}, max(0, int(payload.get("offset") or 0)))
        return _reply(text or "No more matching people in Supymem's lead library. Nothing was added or sent.", actions)
    if action == "gtm.library_notes":
        from sqlalchemy import select
        from app.autopilot import outreach
        from app.gtm import playbooks
        from app.lead_library.models import LeadLibraryEntry
        from app.lead_library.service import _entry_for, _visible
        from app.state import db
        from core.clock import utcnow

        ids = payload.get("contact_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids):
            return _reply("Pick saved people first. No notes were written or sent.")
        with _as(venture_id, actor) as wid:
            view = _view(venture_id)
            if terms := terms_reply(view.get("processing_blocked_reason") or ""):
                return terms
            chosen = view.get("playbooks") or playbooks.fallback(playbooks.evidence(venture_id), (view.get("icp") or {}).get("who") or "")
            parts = []
            with db()() as session:
                for contact in outreach.contacts(venture_id):
                    if contact.id not in ids or contact.email or not (contact.linkedin_url or contact.x_handle):
                        continue
                    if outreach._suppressed(session, wid, contact) or contact.unsubscribed_at or contact.bounced_at:
                        continue
                    entry = _entry_for(session, contact)
                    if contact.contact_provenance == "lead_library" and (entry is None or not session.scalar(
                            select(LeadLibraryEntry.id).where(LeadLibraryEntry.id == entry.id, *_visible(wid, utcnow())))):
                        continue
                    lead = journey._json(contact.enrichment_json)
                    if lead.get("skipped"):
                        continue
                    lead.update(person=" ".join(part for part in (contact.first_name, contact.last_name) if part), company=lead.get("business"))
                    message = playbooks.messages(lead, chosen[0]["key"], view.get("pitch") or (view.get("icp") or {}).get("offer") or "",
                        audience=(view.get("icp") or {}).get("who") or "", problem=(view.get("icp") or {}).get("problem") or "", venture_name=view.get("venture_name") or "")
                    url = contact.linkedin_url or f"https://x.com/{contact.x_handle}"
                    channel = "LinkedIn connection note" if contact.linkedin_url else "X message"
                    parts.append(f"**{lead['person']}**\n{url}\n{channel}:\n{_fence(message['connection_note'])}")
        return _reply("Copy these notes and send them yourself. Nothing was sent.\n\n" + "\n\n".join(parts) if parts
                      else "No selected people are available for LinkedIn or X notes. Nothing was sent.")
    if action == "gtm.library_claim":
        from app.lead_library.service import claim
        ids = payload.get("entry_ids")
        if not isinstance(ids, list) or not ids:
            return _reply("Pick people from the lead library first. Nothing was added.")
        with _as(venture_id, actor) as wid:
            try:
                result = claim(wid, venture_id, ids, actor)
            except ValueError as error:
                return founder_failure(error, venture_id, "your lead library selection",
                    {"action": "gtm.status", "payload": {}},
                    safety="Review the current leads and drafts before adding them again.")
        skipped = [f"{result['already']} already in your leads" if result["already"] else "",
                   f"{result['unavailable']} no longer available" if result["unavailable"] else ""]
        note = " (" + ", ".join(s for s in skipped if s) + ")" if any(skipped) else ""
        channels = result.get("channel_contact_ids") or {}
        email_count = len(channels.get("email") or [])
        drafted = result.get("email_drafts")
        details = (f" {drafted} first email{'s' if drafted != 1 else ''} drafted for your review." if drafted
                   else " No first emails were drafted." if drafted == 0 else " I could not verify the saved email drafts.")
        if drafted is not None and email_count > drafted:
            remaining = email_count - drafted
            details += f" {remaining} {'people have' if remaining != 1 else 'person has'} an email address but no first email draft yet."
        for channel, label in (("linkedin", "LinkedIn"), ("x", "X"), ("phone", "phone")):
            if count := len(channels.get(channel) or []):
                details += f" {count} {'people are' if count != 1 else 'person is'} reachable by {label}."
        actions = [{"label": "Show customers and drafts", "action": "gtm.status",
                    "payload": {"contact_ids": result["contact_ids"]}, "style": "primary"}]
        social_ids = [cid for cid in result["contact_ids"] if cid not in (channels.get("email") or [])
                      and cid in (channels.get("linkedin") or []) + (channels.get("x") or [])]
        if social_ids:
            details += " You can write notes to copy into LinkedIn or X."
            actions.append({"label": "Write LinkedIn notes for them", "action": "gtm.library_notes",
                            "payload": {"contact_ids": social_ids}, "style": "secondary"})
        return _reply(f"Added {result['claimed']} people from Supymem's lead library to your leads{note}.{details} Nothing was sent.", actions)
    with _as(venture_id, actor) as wid:
        if action == "gtm.find":
            if setup := _search_setup_reply():
                journey.authority(wid, venture_id, write=True)
                return {**setup, "action_started": False}
            try:
                view = journey.prepare(wid, venture_id, retry=payload.get("retry") is True,
                                       note=payload.get("note"),
                                       recruitment_opt_in=payload.get("recruitment_opt_in") is True)
            except Exception as error:
                if isinstance(error, ValueError) and str(error) in {"gtm_invalid_input", "gtm_preparation_in_progress"}:
                    view = {}
                    if str(error) == "gtm_invalid_input":
                        try:
                            view = _view(venture_id)
                        except Exception as read_error:
                            return _search_error(venture_id, read_error)
                    return _search_error(venture_id, error, str(error), view=view)
                return _search_error(venture_id, error, None,
                    safety="I could not confirm whether the search started. Check its progress before starting another search.")
            return render(venture_id, view | {"start_search": True})
        if action == "gtm.approve":
            from app.state import MEM

            show_drafts = {"label": "Show drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}
            view = journey.status(wid, venture_id, authorize=False)
            drafts = [d for d in view.get("drafts") or [] if d.get("state") == "draft"]
            review = MEM.get(_draft_review_key(venture_id)) or {}
            review = review if isinstance(review, dict) else {}
            shown_ids = review.get("doc_ids", [])
            reviewed = review.get("revisions", {})
            reviewed = reviewed if isinstance(reviewed, dict) else {}
            expected = payload.get("revisions", reviewed)
            expected = expected if isinstance(expected, dict) else {}
            if payload.get("numbers"):
                selected = {shown_ids[number - 1] for number in payload["numbers"]
                            if type(number) is int and 0 < number <= len(shown_ids)}
                if any(type(number) is not int or not 0 < number <= len(shown_ids) for number in payload["numbers"]):
                    selected = set()
            elif payload.get("doc_ids"):
                selected = set(payload["doc_ids"])
            else:
                selected = set(shown_ids)
            chosen = [draft for draft in drafts if draft.get("doc_id") in selected]
            if (not selected or {draft["doc_id"] for draft in chosen} != selected
                    or any(draft["doc_id"] not in reviewed or expected.get(draft["doc_id"]) != reviewed[draft["doc_id"]]
                           or payload.get("numbers") and (draft.get("revision") or journey._draft_revision(draft)) != reviewed[draft["doc_id"]]
                           for draft in chosen)):
                return _reply("Those drafts no longer match what you reviewed in this conversation. Nothing was approved or sent. "
                              "Choose \"Show drafts\", then approve the drafts you see.",
                              [show_drafts])
            chosen = [draft for draft in chosen if draft in _pending(view) and draft.get("review_state") != "approved"]
            if not chosen:
                return render(venture_id, view)
            failed = []
            for draft in chosen:
                try:
                    view = journey.patch_draft(wid, venture_id, draft["doc_id"],
                        {"decision": "approved", "expected_revision": reviewed[draft["doc_id"]]})
                except ValueError as error:
                    failed.append(f"{draft.get('to')}: {_error_text(error)}")
            body, actions = _send_section(venture_id, view)
            note = f"Approved {len(chosen) - len(failed)} draft(s). Nothing was sent. "
            if failed:
                note += "Not approved: " + "; ".join(failed).rstrip(".") + '. Choose "Show drafts", then review those drafts before approving them. '
                actions.append(show_drafts)
            return _reply(note + body, actions, _kind(actions))
        if action == "gtm.send":
            if "doc_ids" in payload:
                return await _send_reviewed(venture_id, payload, actor)
            from app.approvals.external import OutboundExecutionError
            from app.approvals.service import ApprovalError

            request = {k: payload.get(k) for k in ("batch_id", "approval_digest", "draft_ids", "cap")}
            if "review_revisions" in payload:
                request["review_revisions"] = payload["review_revisions"]
            try:
                view = await journey.send(wid, venture_id, request)
            except (ValueError, OutboundExecutionError, ApprovalError) as error:
                from app.connections.mailboxes import MailboxUnavailable
                if isinstance(error, MailboxUnavailable):
                    return _send_refusal(venture_id, error)
                code = str(error) if isinstance(error, ValueError) else getattr(error, "code", "")
                if code == "gtm_approval_changed" or isinstance(error, ValueError) and (code in _SEND_ERRORS or code in _PERMISSION_REASONS or code in BLOCKED or code == "gtm_send_in_progress"):
                    return _send_refusal(venture_id, error)
                return founder_failure(error, venture_id, "your email batch", {"action": "gtm.status", "payload": {}},
                    safety="Review the current send records before sending again.") | {"kind": "needs_you"}
            except Exception as error:
                return founder_failure(error, venture_id, "your email batch", {"action": "gtm.status", "payload": {}},
                    safety="I could not confirm which emails were sent. Check the current send records before sending again.") | {"kind": "needs_you"}
            receipt = view.get("receipt") or {}
            sent = [draft for draft in view.get("drafts") or [] if draft.get("doc_id") in (request.get("draft_ids") or []) and draft.get("state") == "sent"]
            unsent = [draft for draft in view.get("drafts") or [] if draft.get("doc_id") in (request.get("draft_ids") or [])
                and draft.get("state") in {"draft", "delivery_pending", "delivery_unconfirmed"}]
            people = {person["id"]: person.get("name") for person in view.get("prospects") or []}
            deferred = receipt.get("code") == "composio_rate_limited"
            body = f"Sent {len(sent)} email{'s' if len(sent) != 1 else ''} from your Gmail." if sent and not unsent else receipt.get("body_md") or (
                "I cannot confirm whether these emails were sent yet. Check your saved send records before trying again.")
            if sent:
                body += "\n\nYour email was sent to:\n" + "\n".join(
                    f"- {_recipient_label(draft, people.get(draft.get('contact_id')))}" for draft in sent)
            if unsent:
                body += ("\n\nYour emails waiting to retry:\n" if deferred else "\n\nYou still need to check these saved emails:\n") + "\n".join(
                    f"- {_recipient_label(draft, people.get(draft.get('contact_id')))}: "
                    + ("not sent; your draft is saved" if draft.get("state") == "draft" else "delivery is not confirmed") for draft in unsent)
            actions = receipt.get("actions") or [{"label": "Check replies", "action": "chat.reply", "text": "Any replies?", "style": "primary"}
                if sent and not unsent else {"label": "Check send records", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}]
            return SavedOutreachReply(_reply(body + ("" if deferred else '\n\nAsk "any replies?" to check for a response or bounce.'), actions,
                receipt.get("kind") or ("text" if receipt.get("body_md") or sent and not unsent else "needs_you"))
                | ({"code": "composio_rate_limited"} if deferred else {}))
        if action == "gtm.draft_rewrite":
            requested_at = _draft_request_at(venture_id)
            view = journey.status(wid, venture_id, authorize=False)
            if payload.get("numbers"):
                chosen = [draft for number in payload["numbers"] if type(number) is int
                    and (draft := _target(venture_id, view, number))]
            elif payload.get("single"):
                draft = _asked_draft(venture_id, view, None, editing=True)
                chosen = [draft] if draft else []
            else:
                chosen = _pending(view)
            chosen = [d for d in chosen if d in _pending(view)]
            kept = [d for d in chosen if d.get("review_state") == "approved"]
            chosen = [d for d in chosen if d not in kept and d.get("contact_id") and d.get("day", 1) == 1]
            if not chosen:
                note = ("Approved drafts stay as you approved them, so nothing changed. Nothing was sent." if kept else
                        "I could not find a first-email draft waiting for review. Nothing changed.")
                return _reply(note + ' Choose "Show drafts", then tell me which unapproved email to rewrite.')
            try:
                # The writer may call the model; off the event loop so it runs the same as the scheduled drafting.
                done = await asyncio.to_thread(journey.rewrite_drafts, wid, venture_id, [d["doc_id"] for d in chosen])
            except ValueError as error:
                return founder_failure(error, venture_id, "your rewritten drafts",
                    {"action": "gtm.status", "payload": {"show_saved": True}},
                    safety="Some drafts may have been updated. Review the current drafts before editing or approving them again.")
            except Exception as error:
                return founder_failure(error, venture_id, "your rewritten drafts",
                    {"action": "gtm.status", "payload": {"show_saved": True}},
                    safety="Some drafts may have been updated. Review the current drafts before editing or approving them again.")
            view = journey.status(wid, venture_id, authorize=False)
            fresh = [d for d in view.get("drafts") or [] if d.get("doc_id") in done.values()]
            if not fresh:
                return _reply("I could not rewrite those drafts just now (the contact may have opted out or been skipped), "
                              "so they are unchanged. Nothing was sent.")
            missed = len(chosen) - len(fresh)
            note = f"Rewrote {len(fresh)} draft(s) from scratch in the current format. They need your approval before sending. Nothing was sent."
            if missed or kept:
                note += f" Kept {missed + len(kept)} draft(s) as they were" + (" (approved drafts are not rewritten)." if kept else ".")
            return _reply(note + "\n\n" + "\n\n".join(_draft_block(i, d).replace(f"**Draft {i}**", "**New draft**") for i, d in enumerate(fresh, 1)),
                          [{"label": f"Approve {'these' if len(fresh) > 1 else 'this draft'}", "action": "gtm.approve",
                            "payload": _approval_payload(venture_id, fresh, focus=True, requested_at=requested_at), "style": "primary"}])
        if action == "gtm.draft_edit":
            requested_at = _draft_request_at(venture_id)
            view = journey.status(wid, venture_id, authorize=False)
            draft = (next((draft for draft in _pending(view) if draft.get("doc_id") == payload["doc_id"]), None)
                if "doc_id" in payload else _target(venture_id, view, payload["number"]) if payload.get("number") else _asked_draft(venture_id, view, None, editing=True))
            if draft is None:
                _forget_draft_focus(venture_id)
                return _reply('You do not have that email waiting for review. Nothing changed or sent. Choose "Show drafts", then tell me which email to edit.',
                    [{"label": "Show drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}])
            try:
                from app.connections import mailboxes
                if "expected_revision" in payload and payload["expected_revision"] != draft["revision"]:
                    raise ValueError("gtm_draft_changed")
                change_sender = mailboxes.enabled() and "mailbox_id" in payload
                if change_sender:
                    view = journey.patch_draft(wid, venture_id, draft["doc_id"],
                        {"mailbox_id": payload["mailbox_id"], "expected_revision": draft["revision"]})
                elif payload.get("edit") in {"shorter", "revise"}:
                    body = await _shorter(venture_id, wid, draft, str(payload.get("request") or ""))
                else:
                    line = " ".join(str(payload.get("line") or "").split()).strip("\"'")
                    lines = draft["body"].split("\n")
                    first = next(i for i, l in enumerate(lines) if l.strip())
                    body = "\n".join([*lines[:first], line, *lines[first + 1:]])
                if not change_sender:
                    view = journey.patch_draft(wid, venture_id, draft["doc_id"], {"body": body,
                        "copy_origin": "model" if payload.get("edit") in {"shorter", "revise"} else "founder", "expected_revision": draft["revision"]})
            except ValueError as error:
                if isinstance(error, mailboxes.MailboxUnavailable):
                    from app.autopilot.outreach import mailbox_failure
                    log_founder_exception(error, venture_id, "gtm.draft_edit")
                    return mailbox_failure(venture_id)
                return founder_failure(error, venture_id, "your draft update",
                    {"action": "gtm.status", "payload": {"show_saved": True}},
                    safety=(_EDIT_ERRORS.get(str(error), "Review the current draft before editing or approving it again") + "."))
            except Exception as error:  # noqa: BLE001
                return founder_failure(error, venture_id, "your draft update",
                    {"action": "gtm.status", "payload": {"show_saved": True}},
                    safety="Review the current draft before editing or approving it again.")
            updated = next((d for d in view.get("drafts") or [] if d.get("doc_id") == draft["doc_id"]), None) or draft
            return _reply("Updated. Nothing was sent. You can ask me to send this email, or tell me what to change.\n\n" + _draft_block(1, updated).replace("**Draft 1**", "**Draft**"),
                          [{"label": "Approve this draft", "action": "gtm.approve", "payload": _approval_payload(venture_id, [updated], focus=True, requested_at=requested_at), "style": "primary"}])
    raise ValueError(f"unknown chat action: {action}")
