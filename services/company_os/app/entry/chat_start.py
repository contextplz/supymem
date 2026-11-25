"""Durable, subject-bound chat interpretation before a venture exists."""

from __future__ import annotations

import json
import os
import re
import asyncio
import logging
from urllib.parse import quote
from weakref import WeakValueDictionary
from datetime import timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.authz.workspace import accessible_workspace_ids, assert_venture_access
from app.analytics.entry_chat_usage import record_confirm, record_preview
from app.chat.store import append_message
from app.core.local_mode import is_local
from app.db.models import Venture
from app.db.supymem_models import EntryChatProposal
from app.entry import service
from app.entry.route import route as entry_route
from app.secrets.detect import looks_like_secret
from app.state import db
from app.workspaces import service as workspace_service
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

IDEA_MAX_CHARS = 200000  # keep in sync with apps/console/src/app/new/page.tsx
# Replayed previews share a worker lock; cross-worker provider calls are fenced
# by the persisted classification claim.
_PREVIEW_LOCKS: WeakValueDictionary = WeakValueDictionary()

_GTM = re.compile(
    r"\b(?:gtm|go[ -]?to[ -]?market|"
    r"(?:want|need|do|start|improve|help with|focus on)\s+marketing|"
    r"market(?:ing)?\s+(?:my|our|the)\s+(?:app|product|business)|"
    r"(?:find|get|attract|reach|acquire|needs?|wants?)\s+(?:our |my |the |more |first )*"
    r"(?:(?:paying|paid)\s+)?(?:users|customers|buyers|revenue|sales)|"
    r"sell\s+(?:my|our|the)\s+(?:app|product)|"
    r"(?:grow|increase|improve|boost)\s+(?:my |our |the |more )*(?:users|customers|audience|sales|revenue|traction)|"
    r"(?:no|zero|not getting|struggling to get|struggling to find)\s+"
    r"(?:users|customers|signups|sales|traction)|"
    r"(?:nobody|no one)\s+(?:is\s+)?(?:signing up|using|buying|knows about)\b)",
    re.I,
)
_NEW_PRODUCT_REQUEST = re.compile(
    r"^\s*(?:i\s+(?:want|need|plan|would like)\s+to\s+)?"
    r"(?:build|make|create|develop|code)\s+(?:me\s+)?(?:an?\s+|the\s+|my\s+)?"
    r"(?:app|product|website|tool|platform|startup|business)\b",
    re.I,
)
# shortcut: explicit English stage phrases only, use grounded stage extraction for other languages.
_PRE_PRODUCT = re.compile(r"\b(?:no\s+(?:working|live|launched)\s+product|no\s+product(?=\s*(?:(?:yet|and|but|or)\b|[.,;!?]|$))|"
                          r"(?:product\s+is\s+not|not\s+(?:yet\s+)?)\s*(?:built|launched)|"
                          r"nothing\s+(?:is\s+)?built(?:\s+yet)?|"
                          r"(?:prototype|mock-?ups?|wireframes?)\b[^.!?\n]{0,40}\b(?:no|zero)\s+(?:users|customers|sales|traction)|"
                          r"(?:no|zero)\s+(?:users|customers|sales|traction)\b[^.!?\n]{0,40}\b(?:prototype|mock-?ups?|wireframes?))\b", re.I)
_PERSONAL = re.compile(r"\b(hobby|personal (?:project|use|app|tool|thing|site)|for myself|for my own use|just for me|side project|weekend project)\b", re.I)
_BUILD = re.compile(r"\b(build|make|create|code|develop|app|website|tool|project)\b", re.I)
_STARTUP = re.compile(r"\b(startup|business|company|venture|founder|marketplace|saas)\b", re.I)
_NEGATED_STARTUP = re.compile(r"\b(?:not|isn.t|is not|no)\s+(?:a\s+|an\s+)?(?:startup|business|company|venture)\b", re.I)
_PRODUCT_CONTEXT = re.compile(r"\b(?:app|product|mvp|website|tool|saas|startup|business|company|marketplace|dashboard)\s+(?:(?:idea|concept)\s+)?(?:that|which|for|to|called|named)\s+\S+", re.I)
_PRODUCT_URL = re.compile(r"https?://\S+", re.I)
_PRODUCT_FUNCTION = re.compile(
    r"\b(?:it|(?:this|my|our|the)\s+(?:live\s+)?(?:app|product|tool))\s+"
    r"(?:(?:will|would|could|is going to)\s+)?"
    r"(?:helps?|manages?|does|lets?|tracks?|books?)\b", re.I,
)
_PRODUCT_AUDIENCE = re.compile(r"\b(?:users|customers|audience)\s+(?:(?:will|would)\s+be|are)\b", re.I)
_VENTURE_ID = re.compile(r"\bven_[a-z0-9]+\b", re.I)
_IN_THIS_ACCOUNT = re.compile(r"\b(?:in|on)\s+(?:my|this|our)\s+(?:Supymem\s+)?account\b|\bin\s+Supymem\b", re.I)
_EXPLICIT_EXISTING_TARGET = re.compile(
    r"\b(?:my|our|this|the)\s+existing\s+(?:venture|company|startup)\b|"
    r"\bexisting\s+(?:Supymem\s+)?(?:venture|company|startup)\b|"
    r"\balready\s+have\s+(?!(?:(?:a|an|the|my|our)\s+)?(?:app|product|website|tool|mvp)\b)"
    r"(?:an?\s+|the\s+|my\s+|our\s+)?[\w-]{3,}\b",
    re.I,
)
_NAMED_GTM_TARGET = re.compile(
    r"\b(?:GTM|go[ -]?to[ -]?market|marketing)\s+(?:for|with)\s+"
    r"(?:(?:my|our|the)\s+)?(?:(?:venture|company|product|app)\s+)?"
    r"[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,}){0,3}(?=\W|$)"
)
# ponytail: explicit leading navigation cues, add intent parsing for indirect requests.
_VENTURE_SWITCH = re.compile(
    r"^\s*(?:please\s+)?(?:(?:switch\s+to|open|go\s+to|show\s+me)\s+(?P<name>.+)|"
    r"how\s+is\s+(?P<status_name>.+?)\s+doing\b)", re.I,
)


SKIP_CHOICE_WORDS = "I'm not sure, continue with what I shared"


class ChatStartError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def guard_entry_input(*values: str) -> None:
    if any(looks_like_secret(value) for value in values):
        raise ChatStartError("credential_not_allowed",
            "Remove the key or password and send your message again. Save credentials in Settings > Keys and connections.")


def guard_saved_input(row: EntryChatProposal) -> None:
    guard_entry_input(row.text, row.message, row.receipt_json or "")


def _assert_active_local_proposal(session, row: EntryChatProposal) -> None:
    if is_local() and row.target_venture_id:
        venture = session.get(Venture, row.target_venture_id)
        if venture is None or venture.archived_at is not None:
            raise ChatStartError("venture_not_found", "Open All ventures and restore this venture from Recently deleted, or start a new venture.", 404)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _resume_pending(session, row: EntryChatProposal) -> None:
    now = utcnow()
    cutoff = now - timedelta(days=30)
    if row.state in {"pending", "clarify"} and _utc(row.updated_at) > cutoff:
        session.execute(update(EntryChatProposal).where(
            EntryChatProposal.id == row.id, EntryChatProposal.actor_id == row.actor_id,
            EntryChatProposal.state.in_(("pending", "clarify")),
            EntryChatProposal.updated_at > cutoff,
        ).values(expires_at=now + timedelta(days=30), updated_at=now)
            .execution_options(synchronize_session="fetch"))


def _pre_product(text: str) -> bool:
    from app.intake.smart import no_idea_intent

    return bool(_PRE_PRODUCT.search(text) or no_idea_intent(text))


def _entry_customer_search(text: str, target: str | None) -> str:
    from app.chat.intent import icp_question, requested_customer_search
    from app.chat.search_recovery import search_forbidden

    parts = re.split(r"\n(?:Offer|Audience|Clarification|Context):\s*", text, flags=re.I)
    request = parts[-1].strip()
    if (target or len(parts) > 1 and parts[0].strip()) and not search_forbidden(text):
        if requested_customer_search(request) and not icp_question(request):
            return request
    return ""


def _gtm_request(text: str) -> bool:
    from app.chat.intent import find_customers_request
    from app.chat.llm_router import _command_evidence

    if find_customers_request(text):
        return True
    # shortcut: English clause-level negation only, use grounded intent spans for indirect or multilingual requests.
    for clause in re.split(r"[.!?;,\n]|\b(?:and|but|instead|however)\b", _command_evidence(text), flags=re.IGNORECASE):
        match = _GTM.search(clause)
        if match and not re.search(r"\b(?:no|zero|not|never|without|nobody|struggling)\b|\b\w+n['’]t\b",
                                   clause[:match.end()], re.IGNORECASE):
            return True
    return False


def _classify(text: str, target_venture_id: str | None) -> tuple[str, list[str], str, str] | None:
    """High-impact skips require explicit cues; uncertain messages ask one question."""

    # /entry needs a short sentence for a new idea. Ask for detail now, before
    # issuing a proposal whose confirmation could never commit.
    pre_product = not target_venture_id and _pre_product(text)  # asking about an existing venture stays a go-to-market request
    from app.intake.smart import _existing_product

    requested_gtm = _gtm_request(text)
    live_customer_problem = _existing_product(text) and any(
        re.match(r"(?:no|zero|not getting|struggling|nobody|no one)\b", match.group(), re.IGNORECASE)
        for match in _GTM.finditer(text))
    wants_gtm = (requested_gtm or live_customer_problem) and not (
        _NEW_PRODUCT_REQUEST.search(text) or pre_product)
    if len(text) < 12 and not wants_gtm:
        return None
    from app.intake.smart import classify_fallback
    decision = classify_fallback(text)
    if decision.path == "clarify" and not decision.extracted.idea and not target_venture_id and not wants_gtm:
        return None
    personal = bool(_PERSONAL.search(text))
    if personal and _BUILD.search(text) and (not _STARTUP.search(text) or _NEGATED_STARTUP.search(text)):
        return ("personal_build", ["startup research", "market validation"], "build",
                "This sounds like a personal project. Skip startup research and go straight to a reviewed build?")
    if wants_gtm and not personal:
        skipped = ["idea validation", "build setup"] if requested_gtm else []
        return ("gtm", skipped, "gtm",
                "You want to work on getting users. Skip idea and build setup and open go-to-market review?" if requested_gtm else
                "I'll keep your setup details and review what you need next for your live product.")
    if personal and _STARTUP.search(text):
        return None
    if decision.path == "startup" or pre_product and decision.extracted.idea:
        return ("startup", [], "idea",
                "I’ll use this as your idea and ask what I need next in chat. "
                "If it's a personal project or an existing product, choose Change direction.")
    return None


def _public_receipt(receipt: dict) -> dict:
    return {key: value for key, value in receipt.items() if key not in ("_intake_model_outputs", "_parent_proposal_id")}


def _stated_name(text: str | None) -> str | None:
    """Read the venture name from an opening like "Paper Otter: swim lesson notes"."""
    from app.intake.naming import is_valid_name
    from app.intake.smart import _founder_data, _is_reference

    for sentence in reversed(re.split(r"(?<=[.!?])\s+|\n", _founder_data(text or ""))):
        sentence = re.sub(r"^(?:Offer|Audience|Clarification|Context):\s*", "", sentence.strip(), flags=re.IGNORECASE)
        explicit = (_NAME_SENTENCE.fullmatch(sentence) or re.search(
            r"\b(?i:(?:(?:the|my|our)\s+)?(?:venture|product|business|project)\s+name\s+(?:is|will be))\s+[\"“'‘]?(.+?)[\"”'’]?[.!]*$", sentence))
        keep = re.fullmatch(r"(?i:(?:please\s+)?(?:keep|use)\s+)(.+?)\s+(?i:as\s+(?:the|my|our)\s+(?:venture|product|business|project)\s+name)[.!]*", sentence)
        keep_prefix = re.match(r"(?i:(?:please\s+)?(?:keep|use)\s+(?:the|my|our)\s+(?:venture|product|business|project)\s+name\s+)([A-Z][\w&'-]{1,30}(?:\s+[A-Z][\w&'-]{1,30}){0,2})(?=\s*(?:[.,;!?]|$)|\s+(?:and|for|not)\b)", sentence)
        match = explicit or keep or keep_prefix
        name = match.group(1).strip("\"“'‘”’") if match else ""
        if is_valid_name(name):
            return _clean_name(name)
    # shortcut: English declarations and owned domains only, use quoted model names for indirect introductions.
    declaration = re.match(r"\s*(?:(?:hi|hello|hey)[,!.]?\s+)?(?:(?:i\s+am|i['’]m|we\s+are|we['’]re)\s+(?:building|creating|launching|running)|(?:i|we)\s+(?:run|own|operate))\s+", text or "", re.I)
    source = (text or "")[declaration.end():] if declaration else text or ""
    match = re.search(r"(?:^|:[ \t]+)[ \t]*([A-Z][\w&'-]{1,30}(?:[ \t]+[A-Z][\w&'-]{1,30}){0,2})(?:[ \t]*(?::|[—–-])[ \t]+\S|[ \t]+(?i:is[ \t]+an?[ \t]+(?:(?:Mac|iOS|Android|web|desktop|mobile)[ \t]+)?(?:app|application|tool|platform|product|service|website|SaaS))\b)", source)
    if match and _is_reference(source[:match.start(1)]):
        match = None
    if declaration and not match:
        match = re.match(r"([A-Z][\w&'-]{1,30}(?:[ \t]+[A-Z][\w&'-]{1,30}){0,2})(?=[ \t]*(?:[,.:;!?]|$)|[ \t]+(?i:for|that|which|to|is|and)\b)", source)
    labels = {"hi", "hello", "hey", "note", "idea", "problem", "audience", "update", "context", "summary",
              "background", "goal", "question", "product", "startup", "business", "project", "app",
              "website", "homepage", "site", "url", "i", "we", "it", "this", "that", "my", "our", "the"}
    name = match.group(1) if match else ""
    if name and len(name) <= 48 and name.casefold() not in labels and name.split()[0].casefold() not in {"my", "our", "the"} and is_valid_name(name):
        from app.intake.smart import _URL
        if not _URL.match(source.strip()):
            return name
    from app.intake.smart import _owned_urls
    from app.intake.website import extract_website
    urls = _owned_urls(text or "")
    return extract_website("", urls[0])["name"] if urls else None


def _proposal(row: EntryChatProposal) -> dict:
    saved = json.loads(row.receipt_json) if row.receipt_json else {}
    receipt = _public_receipt(saved) if row.state == "committed" else None
    intake = saved.get("intake") or {}
    successful_names = [value.get("name") for value in saved.get("name_requests", {}).values() if value.get("name")]
    venture_name = (saved.get("name") or saved.get("chosen_name") or (successful_names[-1] if successful_names else None)
                    or _stated_name(row.text) or _stated_name(saved.get("original_source"))
                    or intake.get("extracted", {}).get("venture_name"))
    result = {
        "id": row.id, "route": row.route, "message": row.message,
        "skipped_steps": json.loads(row.skipped_steps_json), "next_step": row.next_step,
        "target_venture_id": row.target_venture_id, "expires_at": _utc(row.expires_at).isoformat(),
        "state": row.state, "receipt": receipt,
        "intake_path": intake.get("path"), "button_label": intake.get("button_label"),
        "venture_name": venture_name,
        "website_answers": intake.get("website_answers", {}),
    }
    if saved.get("evidence_ids") and row.state != "committed":
        from app.intake.evidence import entry_files
        files = entry_files(saved["evidence_ids"], row.actor_id, row.target_venture_id)
        if files:
            result["files"] = files
    if intake:
        from app.intake.assets import intake_questions
        from app.ventures.stage import preview_stage
        result.update({key: saved[key] for key in ("asset_questions", "asset_prefill")}
            if "asset_questions" in saved and "asset_prefill" in saved else intake_questions(intake, saved.get("original_source") or row.text))
        stage = saved.get("founder_stage") or preview_stage(intake)
        if not saved.get("founder_stage"):
            stage["updated_at"] = _utc(row.created_at).isoformat()
            for evidence in stage["evidence"]:
                evidence["timestamp"] = stage["updated_at"]
        result["founder_stage"] = stage
        if row.route in {"startup", "clarify"} and row.state in {"pending", "clarify"} and not row.target_venture_id:
            onboarding = saved["onboarding"] if "onboarding" in saved else _onboarding_progress(intake, row.text, saved.get("original_source") or "")
            from app.intake.smart import has_idea_content, no_idea_intent
            if not intake.get("next_step") and no_idea_intent(row.text) and not has_idea_content(row.text):
                onboarding = _onboarding_progress(intake, row.text, saved.get("original_source") or "")
            result["onboarding_question"] = onboarding.get("question")
            result["onboarding_field"] = onboarding.get("field")
            result["onboarding_choices"] = onboarding.get("choices", [])
            if (row.route == "startup" and row.state == "pending" and not venture_name
                    and not (no_idea_intent(row.text) and not has_idea_content(row.text))):
                result.update(message="What should we call it?", onboarding_question=None,
                              onboarding_field=None, onboarding_choices=[])
    if row.route == "clarify" and not result.get("onboarding_question"):
        result["choices"] = (intake.get("choices") or _clarify_choices(row.message)) if intake else []
    return result


def _preview_result(row: EntryChatProposal) -> dict:
    if row.route == "clarify":
        proposal = _proposal(row)
        return {"status": "clarify", "id": row.id, "question": row.message,
                "choices": proposal.get("choices", []), "intake_path": proposal.get("intake_path"),
                "founder_stage": proposal.get("founder_stage"),
                "venture_name": proposal.get("venture_name"),
                "website_answers": proposal.get("website_answers", {}),
                "onboarding_question": proposal.get("onboarding_question"),
                "onboarding_field": proposal.get("onboarding_field"),
                "onboarding_choices": proposal.get("onboarding_choices", [])}
    return {"status": "proposal", "proposal": _proposal(row)}


_GENERIC_QUESTION = "What does the product do, and who are you trying to reach?"
_GENERIC_CHOICES = [
    "It's a startup or business idea",
    "It's a personal project for me",
    "I already have a product and want users",
]


def _clarify_choices(question: str) -> list[str]:
    return list(_GENERIC_CHOICES) if question == _GENERIC_QUESTION else []


def _onboarding_question(intake: dict, text: str = "") -> str | None:
    return _onboarding_progress(intake, text).get("question")


def _onboarding_progress(intake: dict, text: str = "", original_source: str = "", *, accepted_answers: dict | None = None) -> dict:
    text = re.sub(r"\nOffer:", "\nClarification:", text, flags=re.IGNORECASE)
    from app.intake.smart import (DISCOVERY_EXAMPLES, DISCOVERY_PAUSED, DISCOVERY_QUESTION, Extracted, _URL, _describes_direction, _describes_offer, _discovery_progress, _exploring, _founder_data, _idea_candidate,
                                  _prefill, audience_phrase, discovery_idea_text, has_idea_content, no_idea_intent)
    from app.gstack.office_hours import is_skip
    from app.idea.agent import ASSUMED, broad_audience, core_answer_status, core_followup, is_unsure_answer
    from core.copy import no_dashes

    def suggested_answers(field):
        return [no_dashes(option.removeprefix(ASSUMED)) for option in intake.get("onboarding_options", {}).get(field, [])]

    extracted = intake.get("extracted") or {}
    source = _founder_data(original_source + "\n" + text)
    grounded = {field: fact["value"] for field, fact in intake.get("prefill_facts", {}).items()
        if fact.get("source") == "founder message" and fact.get("confidence", 0) >= .9
        and fact.get("quote") and fact["quote"] in source}
    known = {**intake.get("website_answers", {}), **_prefill(original_source), **_prefill(text), **{field: value for field, value in intake.get("prefill_answers", {}).items()
        if value and (value in source or grounded.get(field) == value) and (field not in {"offer", "customer"}
            or core_answer_status(value, 0, field=field, assessed=True) == (True, False))}}
    if intake.get("path") == "startup" and no_idea_intent(text):
        choice = _idea_candidate(text)
        if choice.startswith(ASSUMED):
            return {"completed": ["offer", "customer"], "assumptions": [choice]}
    decision = intake.get("next_step")
    if (intake.get("path") == "clarify" and _URL.search(text)
            and not known.get("offer") and not decision
            and not re.search(r"\n(?:Audience|Clarification):\s*", text, re.I)):
        return {"completed": [], "assumptions": [], "field": "offer",
                "question": intake["founder_line"], "choices": intake.get("choices", [])}
    last_turn = re.split(r"\n(Audience|Clarification|Context):\s*", text, flags=re.I)[-2:]
    skipped_context = (len(last_turn) == 2 and last_turn[0].casefold() == "context"
                       and any(is_skip(line) for line in last_turn[1].splitlines()))
    if (intake.get("path") in {"startup", "research", "clarify"} and no_idea_intent(text)
            and not has_idea_content(text) and (not decision or decision.get("action") != "discover"
                and (skipped_context or decision.get("situation") != "no_idea"))):
        step = _exploring(text, Extracted())
        if step.founder_line == DISCOVERY_PAUSED:
            return {"completed": [], "skipped": ["context"], "assumptions": [], "question": step.founder_line, "choices": []}
        return {"completed": [], "assumptions": [], "field": "context", "question": step.founder_line,
                "choices": (intake.get("choices") if step.founder_line == DISCOVERY_QUESTION else None) or step.choices}
    if decision:
        if (decision["action"] == "ask" and decision["field"] not in known
                and (decision.get("situation") == "no_idea" or decision["field"] == "context"
                     or extracted.get("language", "en") != "en"
                     or not re.search(r"\n(?:Audience|Clarification):\s*", text, re.I))):
            return {"completed": [], "assumptions": [], "question": decision["question"],
                    "field": decision["field"], "choices": decision["examples"]}
        if decision["action"] == "discover":
            return {"completed": [], "assumptions": [], "question": intake["founder_line"],
                    "field": "context", "choices": intake.get("choices", [])}
        if decision["action"] != "ask":
            return {}
    if (intake.get("path") == "clarify" and not extracted.get("idea") and not has_idea_content(text)
            and not (_BUILD.search(text) or _has_product_context(text))):
        return {"completed": [], "assumptions": [], "question": intake.get("founder_line") or DISCOVERY_QUESTION,
                "field": "context", "choices": intake.get("choices") or DISCOVERY_EXAMPLES}
    if intake.get("path") in {"startup", "research", "clarify"} and no_idea_intent(text):
        if not has_idea_content(text):
            return {"completed": [], "assumptions": [], "field": "context",
                    "question": intake.get("founder_line") if not extracted.get("idea") else DISCOVERY_QUESTION,
                    "choices": intake.get("choices") or DISCOVERY_EXAMPLES}
        candidate = _idea_candidate(text)
        if (not decision and _describes_direction(candidate) and not _describes_offer(candidate)
                and not _discovery_progress(text)[3]):
            return {"completed": [], "assumptions": [], "field": "offer",
                    "question": core_followup("offer", 0), "choices": suggested_answers("offer")}
        text = discovery_idea_text(text)
    goal_entry = bool(_GTM.search(text) and intake.get("path") in {"gtm", "landing_gtm"})
    if (intake.get("path") not in {"startup", "research", "clarify"} and not goal_entry
            or not goal_entry and (extracted.get("existing_site_url") or extracted.get("has_customers"))
            or extracted.get("repo_url")):
        return {}
    parts = re.split(r"\n(Audience|Offer|Clarification|Context):\s*", text, flags=re.I)
    idea = [known.get("offer") or original_source or parts[0] or extracted.get("idea", "")]
    initial_audience = (known.get("customer") or
                        audience_phrase(intake.get("prefill_answers", {}).get("customer") or "") or audience_phrase(extracted.get("audience") or ""))
    if initial_audience and "audience" in {label.casefold() for label in parts[1::2]} and initial_audience.casefold() not in (original_source or parts[0]).casefold():
        initial_audience = _prefill(original_source or parts[0]).get("customer", "")
    audience = [initial_audience] if initial_audience else []
    from app.gstack.office_hours import is_skip
    from app.idea.agent import answer_metadata, answer_prefill
    for label, answer in zip(parts[1::2], parts[2::2]):
        if label.casefold() == "context":
            continue
        field = "customer" if label.casefold() == "audience" else "offer"
        if not answer.startswith(ASSUMED):
            skip = answer.splitlines()[-1] if answer.splitlines() and is_skip(answer.splitlines()[-1]) else ""
            answer = "Skip" if skip else answer_metadata(answer)["founder_text"] or answer
        if field == "customer" and not answer.startswith(ASSUMED) and not is_unsure_answer(answer):
            answer = _prefill("Audience: " + answer).get("customer", "")
            if not answer:
                continue
        (audience if field == "customer" else idea).append(answer)
        if (field == "offer" and not answer.startswith(ASSUMED) and not is_unsure_answer(answer)
                and (customer := answer_prefill(answer, field).get("customer"))):
            audience.append(customer)
    for field, answers in (("offer", idea), ("customer", audience)):
        if any(not is_skip(answer) and core_answer_status(answer, 0, field=field) == (True, False) for answer in answers):
            answers[:] = [answer for answer in answers if not is_skip(answer)]
    progress = {"completed": [], "assumptions": []}
    accepted_answers = accepted_answers if accepted_answers is not None else {}
    from app.ventures.founder_facts import ALIASES, confirmation, literal_facts, policy
    facts = literal_facts(parts[0])
    for field, answers in (("offer", idea), ("customer", audience)):
        if (extracted.get("language", "en") != "en" and known.get(field)
                and known[field] == intake.get("prefill_answers", {}).get(field)
                and core_answer_status(known[field], 0, field=field, assessed=True) == (True, False)):
            progress["completed"].append(field)
            accepted_answers[field] = known[field]
            continue
        website_answer = intake.get("website_answers", {}).get(field)
        if website_answer and (field != "customer" or audience_phrase(website_answer)):
            progress["completed"].append(field)
            accepted_answers[field] = website_answer
            continue
        prefill = intake.get("prefill_facts", {}).get(field)
        interpreted = {ALIASES.get(field, field): prefill} if prefill else {}
        if (prefill and prefill.get("source") != "website" and len(answers) <= 1
                and policy(interpreted, field) == "confirm" and policy(facts, field) != "skip"
                and not (field == "offer" and core_answer_status(parts[0], 0, field="offer") == (True, False))):
            return {**progress, "question": no_dashes(confirmation(interpreted, field)), "field": field,
                "prefill": prefill["value"], "choices": suggested_answers(field)}
        # ponytail: short audience phrases stand in for broad segments; use typed specificity if misses grow.
        if field == "customer" and parts[0] and initial_audience and len(initial_audience.split()) <= 3 and len(audience) == len(idea) == 1 and progress["assumptions"]:
            # ponytail: audience-first English clauses; use extracted problem spans for other word orders.
            audience_match = re.search(re.escape(initial_audience), parts[0], re.I)
            problem = parts[0][audience_match.end():] if audience_match else ""
            problem = re.sub(r"\b(?:something for|idk|i think|i guess|maybe|lol|or something)\b", "", problem, flags=re.IGNORECASE)
            problem = re.sub(r"\s+", " ", problem).strip(" ,.;:!?")
            problem = re.sub(r"^(?:and|who|that)\s+", "", problem, flags=re.I)
            acknowledgement = (f"{initial_audience[0].upper() + initial_audience[1:]}, and {problem}." if problem
                               else f"You mentioned {initial_audience}.")
            return {**progress, "question": acknowledgement + "\n\nWhich group should we focus on first?",
                    "field": field, "choices": suggested_answers(field)}
        if field == "customer" and answers and policy(facts, field) == "skip" and not is_unsure_answer(answers[-1]) and core_answer_status(facts["audience"]["value"], 0, field="customer") == (True, False):
            progress["completed"].append(field)
            accepted_answers[field] = facts["audience"]["value"]
            continue
        if not answers:
            inferred_audience = audience_phrase(intake.get("prefill_answers", {}).get("customer") or "") or audience_phrase(extracted.get("audience") or "")
            if inferred_audience:
                answers = [inferred_audience]
            else:
                question = DISCOVERY_QUESTION if progress["assumptions"] and not has_idea_content(text) else "Who is it for?"
                return {**progress, "question": question, "field": field, "choices": suggested_answers(field)}
        assessed = known.get(field) == answers[-1] and intake.get("prefill_answers", {}).get(field) == answers[-1]
        if field == "customer" and not assessed and not audience_phrase(answers[-1]) and not core_answer_status(answers[-1], 0, field=field)[1]:
            return {**progress, "question": "Who is it for?", "field": field, "choices": suggested_answers(field)}
        followups = len(answers) - 1
        advance, assumed = core_answer_status(answers[-1], followups, field=field,
            assessed=assessed)
        if (field == "customer" and followups == 0 and not has_idea_content(parts[0])
                and is_unsure_answer(answers[-1]) and not re.search(r"\b(?:skip|pass)\b", answers[-1], re.I)):
            advance = False
        if not advance:
            if field == "customer" and initial_audience and len(audience) == 1:
                return {**progress, "question": f"You mentioned {initial_audience}. Which group would you start with?",
                        "field": field, "choices": suggested_answers(field)}
            if field == "customer" and broad_audience(answers[-1]):
                return {**progress, "question": core_followup(field, followups, answer=answers[-1]),
                        "field": field, "choices": suggested_answers(field)}
            return {**progress, "question": core_followup(field, followups), "field": field, "choices": suggested_answers(field)}
        progress["completed"].append(field)
        if not assumed and (followups or not intake.get("prefill_answers", {}).get(field)):
            accepted_answers[field] = answers[-1][:2000]
        if assumed:
            words = " / ".join(answer_metadata(answer)["founder_text"] for answer in answers).strip(" / ")
            progress["assumptions"].append(ASSUMED + f"{field} is unclarified; " + ("using founder words: " + words[:1600] if words else "not answered yet."))
    if intake.get("path") == "clarify" and intake.get("website_answers") and intake.get("choices"):
        progress.update(field="context", question=intake["founder_line"], choices=intake["choices"])
    return progress


def _has_product_context(text: str) -> bool:
    from app.intake.smart import _owned_urls
    return bool(_PRODUCT_CONTEXT.search(text) or _PRODUCT_FUNCTION.search(text) or _PRODUCT_AUDIENCE.search(text) or _PRODUCT_URL.search(text) or _owned_urls(text))


def _venture_switch(text: str, actor_id: str) -> dict | None:
    request = _VENTURE_SWITCH.match(text)
    if not request:
        return None
    named = (request.group("name") or request.group("status_name")).strip().lstrip("\"'")
    with db()() as session:
        ventures = session.scalars(select(Venture).where(
            Venture.workspace_id.in_(accessible_workspace_ids(actor_id)), Venture.archived_at.is_(None),
        ).order_by(Venture.name, Venture.id)).all()
        matches = [venture for venture in ventures if venture.name.strip() and re.match(
            rf"(?:(?:my|our|the)\s+)?(?:(?:venture|company)\s+)?{re.escape(venture.name.strip())}(?!\w)", named, re.I)]
        if matches:
            longest = max(len(venture.name.strip()) for venture in matches)
            matches = [venture for venture in matches if len(venture.name.strip()) == longest]
        if not matches and not re.match(r"\s*(?:please\s+)?switch\s+to\b", text, re.I):
            return None  # "show me my leads", "open the landing page": not a venture switch
        choices = matches or ventures
        message = (f"Open {matches[0].name} to see where you are at." if len(matches) == 1 else
                   "More than one of your ventures matches. Choose the one you want to open." if matches else
                   "I couldn't find that venture in your account. Choose one of your ventures below." if ventures else
                   "I couldn't find that venture in your account. You have no active ventures to open.")
        return {"status": "reply", "body_md": message, "actions": [
            {"label": f"Open {venture.name}" + (f" ({venture.id})" if len(matches) > 1 else ""),
             "href": f"/v/{quote(venture.id, safe='')}?chat=1", "style": "primary"}
            for venture in choices]}


def _named_existing_target(text: str, actor_id: str) -> tuple[str | None, str | None]:
    """Resolve a founder-named venture only within their active workspaces.

    A name collision, multiple mentioned ventures, or an inaccessible ID must
    never silently choose a venture or fall through to creating a new one.
    """
    if not _GTM.search(text) or _NEW_PRODUCT_REQUEST.search(text):
        return None, None
    workspace_ids = accessible_workspace_ids(actor_id)
    if not workspace_ids:
        return None, None
    with db()() as session:
        ventures = session.scalars(select(Venture).where(
            Venture.workspace_id.in_(workspace_ids), Venture.archived_at.is_(None),
        )).all()
        by_id = {venture.id.casefold(): venture for venture in ventures}
        named_ids = {match.group().casefold() for match in _VENTURE_ID.finditer(text)
                     if _name_is_target(text, match.group())}
        if named_ids:
            matches = [by_id[venture_id] for venture_id in named_ids if venture_id in by_id]
            if len(named_ids) != 1 or not matches:
                return None, "I couldn't identify that venture in your account. Which existing venture do you mean?"
            return matches[0].id, None
        matches = [venture for venture in ventures if _name_is_target(text, venture.name)]
        if len(matches) == 1:
            return matches[0].id, None
        if len(matches) > 1:
            options = ", ".join(f"{venture.name} ({venture.id})" for venture in matches[:5])
            return None, f"Which existing venture do you mean: {options}? Reply with its venture ID."
    return None, None


def _name_is_target(text: str, name: str) -> bool:
    """Require a founder-to-venture phrase, not a mere competitor mention."""
    escaped = re.escape(name.strip())
    if not escaped:
        return False
    return any(re.search(pattern, text, re.I) for pattern in (
        rf"\b(?:my|our|existing|use|grow|market|promote)\s+(?:venture\s+|company\s+|product\s+|app\s+)?{escaped}(?!\w)",
        rf"\b(?:gtm|go[ -]?to[ -]?market|marketing|customers|users)\s+(?:for|of|with)\s+(?:my\s+|our\s+|the\s+)?(?:venture\s+|company\s+|product\s+|app\s+)?{escaped}(?!\w)",
        rf"\b(?:already\s+have|built|started|run)\s+(?:a\s+|an\s+|the\s+)?(?:venture\s+|company\s+|product\s+|app\s+)?{escaped}(?!\w)",
        rf"\b(?:venture|company|product|app)\s+(?:called|named)\s+{escaped}(?!\w)",
        rf"\bClarification:\s*{escaped}(?!\w)",
        rf"(?<!\w){escaped}\s+is\b[^\n.]{{0,160}}\b(?:already|existing|in (?:my|this|our) account)\b",
    ))


def _link_prefix(body: str) -> str:
    """"I read host: first line. " for a pasted link; never raises."""
    try:
        from urllib.parse import urlparse

        from app.chat.link_context import read_links

        pages = read_links(body, timeout_s=6.0, max_chars=1000)
        if not pages:
            return ""
        url, page = pages[0]
        first = next((ln.strip(" #*>") for ln in page.splitlines() if ln.strip(" #*>")), "")
        host = (urlparse(url).hostname or "").removeprefix("www.")
        return f"I read {host}: {first[:160]}. " if first else ""
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, None, "entry.link.read")
        return ""


def preview(*, text: str, actor_id: str, target_venture_id: str | None = None,
            request_id: str | None = None, intake: dict | None = None,
            original_source: str | None = None, claim_id: str | None = None) -> dict:
    body = text.strip()
    guard_entry_input(body, original_source or "")
    if intake is not None:
        from app.intake.smart import IntakeResult
        intake = IntakeResult.model_validate(intake).to_dict()
    if not body:
        raise ChatStartError("message_required", "Tell me what you want to work on.")
    if len(body) > IDEA_MAX_CHARS:
        raise ChatStartError("message_too_long", f"That's longer than I can read at once (over {IDEA_MAX_CHARS:,} characters). Trim it to the core idea and send again.")
    if target_venture_id:
        assert_venture_access(target_venture_id, actor_id)
    factory = db()
    with factory() as session:
        if request_id:
            existing = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.request_id == request_id))
            if existing and existing.id != claim_id:
                _assert_active_local_proposal(session, existing)
                guard_saved_input(existing)
                if existing.text != body or (target_venture_id and existing.target_venture_id != target_venture_id):
                    raise ChatStartError("request_id_conflict", "This request ID was already used.", 409)
                if existing.target_venture_id:
                    assert_venture_access(existing.target_venture_id, actor_id)
                _resume_pending(session, existing)
                session.commit()
                record_preview(existing.id, actor_id, existing.route)
                return _preview_result(existing)
    switch = _venture_switch(body, actor_id)
    if switch:
        return switch
    ambiguity = None
    if not target_venture_id:
        target_venture_id, ambiguity = _named_existing_target(body, actor_id)
    classified = _classify(body, target_venture_id)
    if ambiguity:
        route, skipped, next_step, message = "clarify", [], "clarify", ambiguity
    elif classified and classified[0] == "gtm" and not target_venture_id and (
        _IN_THIS_ACCOUNT.search(body) or _EXPLICIT_EXISTING_TARGET.search(body)
        or _NAMED_GTM_TARGET.search(body)
    ):
        route, skipped, next_step, message = (
            "clarify", [], "clarify",
            "I couldn't identify that venture in your account. Which existing venture do you mean?")
    elif classified and classified[0] == "gtm" and not target_venture_id and not _has_product_context(body):
        route, skipped, next_step, message = (
            "clarify", [], "clarify",
            "Which existing product do you want to grow? Tell me what it does.")
    elif not classified:
        from app.intake.smart import classify_fallback
        fallback = classify_fallback(body)
        route, skipped, next_step, message = "clarify", [], "clarify", fallback.founder_line
        if intake is None:
            intake = fallback.to_dict()
    else:
        route, skipped, next_step, message = classified
        if target_venture_id and route == "gtm":
            with factory() as session:
                named_venture = session.get(Venture, target_venture_id)
                if named_venture is not None:
                    message = (f"Continue {named_venture.name} as your existing venture "
                               "and open its go-to-market review?")
    # Account/venture ambiguity always wins over model routing. No model can
    # choose another founder's workspace or turn an unresolved target into a new one.
    protected_ambiguity = bool(ambiguity) or (route == "clarify" and "venture" in message)
    protected_product_question = (route == "clarify" and "Which existing product" in message
                                  and (intake is None or intake["path"] == "clarify"))
    from app.intake.smart import _existing_product, _has_customers
    product_source = (original_source or "") + "\n" + body
    if (intake and not target_venture_id and (_pre_product(body) or not _gtm_request(body) and not (
            _existing_product(product_source) or _has_customers(product_source)
            or intake["extracted"].get("existing_site_url")))
            and intake["path"] in {"gtm", "landing_gtm"}):
        from app.intake.smart import classify_fallback

        intake = classify_fallback(body, page_context={"original_input": original_source or ""}).to_dict()
        if intake["path"] in {"gtm", "landing_gtm"}:
            intake = {**intake, "path": "startup", "button_label": "Review your idea", "choices": []}
    if (intake and classified and classified[0] == "gtm" and
            intake["path"] in {"startup", "clarify", "gtm", "landing_gtm"} and
            (target_venture_id or _has_product_context(body))):
        intake = {**intake, "path": "landing_gtm" if intake["path"] == "landing_gtm" else "gtm", "confidence": max(0.9, intake["confidence"]),
                  "founder_line": intake["founder_line"] if intake["path"] in {"gtm", "landing_gtm"} else "Let's find your next customers.",
                  "button_label": "Get customers", "choices": []}
    if intake and not protected_ambiguity and not protected_product_question:
        path = intake["path"]
        if (path in {"clarify", "multiple_ideas", "refuse"} or intake["confidence"] < 0.75
                or (len(body) < 12 and route != "gtm")):
            route, skipped, next_step = "clarify", [], "clarify"
        elif path in {"gtm", "landing_gtm"}:
            route, skipped, next_step = "gtm", ["idea validation", "build setup"], "gtm"
            # Preserve the established guard against creating a blank GTM venture.
            if not target_venture_id and not _has_product_context(body) and not intake["extracted"].get("idea"):
                route, skipped, next_step = "clarify", [], "clarify"
        elif path == "personal_build":
            route, skipped, next_step = "personal_build", ["startup research", "market validation"], "build"
        else:
            route, skipped, next_step = "startup", (["website building"] if path == "research" else []), "idea"
        message = intake["founder_line"]
        if target_venture_id and route == "gtm":
            with factory() as session:
                existing_target = session.get(Venture, target_venture_id)
                if existing_target:
                    message = f"Let's get customers for {existing_target.name}."
    from app.intake.application import page_read_block
    if route != "clarify" and intake is None and not page_read_block(body):
        message = _link_prefix(body) + message
    from app.chat.intent import find_customers_request
    answers = re.split(r"\n(?:Offer|Audience|Clarification|Context):\s*", body, flags=re.I)
    requested_search = _entry_customer_search(body, target_venture_id) if is_local() else ""
    if requested_search and not protected_ambiguity and (intake is None or intake["path"] != "refuse"):
        route, skipped, next_step = "gtm", ["idea validation", "build setup"], "gtm"
        message = ("I'll look for the customers you asked for." if target_venture_id
                   else "Name your venture and I'll look for the customers you asked for.")
        if intake:
            intake = {**intake, "path": "gtm", "founder_line": message,
                      "button_label": "Find customers" if target_venture_id else "Name and find customers", "choices": []}
    elif (not is_local() and len(answers) > 1 and answers[0].strip() and find_customers_request(answers[-1]) and not protected_ambiguity
            and (target_venture_id or not _pre_product(body))
            and (intake is None or intake["path"] != "refuse")):
        # "Find me dentists in Austin I can email" asks for customers; it never answers the open question.
        route, skipped, next_step = "gtm", ["idea validation", "build setup"], "gtm"
        message = "You want to find customers now. Skip idea validation and build setup and go straight to finding customers?"
        if intake:
            intake = {**intake, "path": "gtm", "confidence": max(0.9, intake["confidence"]),
                      "founder_line": "Let's find your next customers.", "button_label": "Get customers", "choices": []}
    accepted_answers = {}
    onboarding = (_onboarding_progress(intake, body, original_source or "", accepted_answers=accepted_answers) if intake and not target_venture_id
                  and not protected_ambiguity and not protected_product_question else {})
    if accepted_answers:
        prefill = {**intake.get("prefill_answers", {}), **accepted_answers}
        intake = {**intake, "prefill_answers": prefill,
            "prefill_facts": {field: fact for field, fact in intake.get("prefill_facts", {}).items()
                              if prefill.get(field) == fact["value"]}}
    # Only the founder's own onboarding answers may resolve a clarify; an unanswered
    # model ambiguity never becomes a new venture.
    if (route == "clarify" and onboarding.get("completed") == ["offer", "customer"]
            and intake["path"] in {"clarify", "startup", "research"}
            and re.search(r"\n(?:Offer|Audience|Clarification):", body, re.I)):
        route, skipped, next_step = "startup", [], "idea"
        message = "We'll proceed with what you've shared, keeping unclear details as assumptions."
    if route != "clarify" and not target_venture_id:
        _assert_can_start(actor_id)
    website = intake.get("website_answers", {}) if intake else {}
    if website.get("offer"):
        from core.copy import no_dashes
        offer = " ".join(website["offer"].split()).rstrip(".!?")[:200]
        customer = " ".join(website.get("customer", "").split()).rstrip(".!?")[:120]
        summary = no_dashes(f"Your website describes: {offer}" +
            (f" for {customer}" if customer and customer.casefold() not in offer.casefold() else "") + ".")
        message = summary + "\n\n" + message
        if onboarding.get("question"):
            onboarding["question"] = summary + "\n\n" + onboarding["question"]
    from app.ventures.stage import preview_stage
    from app.intake.assets import intake_questions
    asset_snapshot = intake_questions(intake, original_source or body, target_venture_id) if intake else {}
    from app.ventures.stage import get_stage
    stage_snapshot = get_stage(target_venture_id) if intake and target_venture_id else preview_stage(intake) if intake else None
    now = utcnow()
    row = EntryChatProposal(
        id=claim_id or new_id("ecp"), actor_id=actor_id, request_id=request_id,
        text=body, route=route, target_venture_id=target_venture_id,
        skipped_steps_json=json.dumps(skipped), message=message,
        next_step=next_step, state="clarify" if route == "clarify" else "pending",
        receipt_json=json.dumps({"intake": intake, "original_source": original_source,
                                 "founder_stage": stage_snapshot, "onboarding": onboarding, **asset_snapshot,
                                 "created_new": not bool(target_venture_id)}) if intake and not protected_ambiguity else None,
        created_at=now, expires_at=now + timedelta(days=30), updated_at=now,
    )
    with factory() as session:
        if claim_id:
            original_claim = session.get(EntryChatProposal, claim_id)
            if original_claim is not None and original_claim.actor_id == actor_id:
                original_metadata = json.loads(original_claim.receipt_json or "{}")
                if carried := {key: original_metadata[key] for key in ("_intake_model_outputs", "_parent_proposal_id")
                               if key in original_metadata}:
                    next_metadata = json.loads(row.receipt_json or "{}")
                    row.receipt_json = json.dumps({**next_metadata, **carried})
            claimed = session.execute(update(EntryChatProposal).where(
                EntryChatProposal.id == claim_id, EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.state == "classifying",
            ).values(route=row.route, target_venture_id=row.target_venture_id,
                     skipped_steps_json=row.skipped_steps_json, message=row.message, next_step=row.next_step,
                     state=row.state, receipt_json=row.receipt_json, updated_at=now,
                     expires_at=row.expires_at))
            if claimed.rowcount != 1:
                raise ChatStartError("proposal_busy", "This request is already being understood. Try again shortly.", 409)
        else:
            session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if not request_id:
                raise
            existing = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.request_id == request_id))
            if not existing or existing.text != body or (
                target_venture_id and existing.target_venture_id != target_venture_id
            ):
                raise ChatStartError("request_id_conflict", "This request ID was already used.", 409)
            _assert_active_local_proposal(session, existing)
            if existing.target_venture_id:
                assert_venture_access(existing.target_venture_id, actor_id)
            _resume_pending(session, existing)
            session.commit()
            record_preview(existing.id, actor_id, existing.route)
            return _preview_result(existing)
        result = _preview_result(row)
    record_preview(row.id, actor_id, route)
    return result


async def preview_async(*, text: str, actor_id: str, target_venture_id: str | None = None,
                        request_id: str | None = None, use_original: bool = False,
                        original_source: str | None = None, previous_proposal_id: str | None = None) -> dict:
    guard_entry_input(text, original_source or "")
    if not request_id:
        return await _preview_async(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                                    request_id=None, use_original=use_original, original_source=original_source,
                                    previous_proposal_id=previous_proposal_id)
    key = (actor_id, request_id)
    lock = _PREVIEW_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PREVIEW_LOCKS[key] = lock
    async with lock:
        try:
            return await _preview_async(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                                        request_id=request_id, use_original=use_original, original_source=original_source,
                                        previous_proposal_id=previous_proposal_id)
        except Exception as exc:
            from app.chat.recovery import log_founder_exception

            log_founder_exception(exc, target_venture_id, "entry.preview")
            from llm_gateway.errors import ProviderCallError
            if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
                raise ChatStartError("intake_outcome_unknown",
                    "This request is awaiting its saved result. Try this same request again shortly.", 409) from exc
            raise


async def _preview_async(*, text: str, actor_id: str, target_venture_id: str | None,
                         request_id: str | None, use_original: bool, original_source: str | None = None,
                         previous_proposal_id: str | None = None) -> dict:
    """The real entry API classifies once; saved retries never fetch or spend again."""
    body = text.strip()
    if not body or len(body) > IDEA_MAX_CHARS:
        # Use the same existing validation/error contract without provider work.
        return preview(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                       request_id=request_id)
    if _VENTURE_SWITCH.match(body):
        return preview(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                       request_id=request_id)
    workspace_id = None
    if request_id:
        with db()() as session:
            saved = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.actor_id == actor_id, EntryChatProposal.request_id == request_id))
            if saved and saved.state != "classifying":
                return preview(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                               request_id=request_id)
            if saved:
                if saved.text != body or (target_venture_id is not None and saved.target_venture_id != target_venture_id):
                    raise ChatStartError("request_id_conflict", "This request ID was already used.", 409)
                # A retry may omit its optional target. Restore the durable scope
                # before access, active-target, and new-venture quota checks below.
                target_venture_id = saved.target_venture_id
    if not target_venture_id:
        named_target, ambiguity = _named_existing_target(body, actor_id)
        if named_target:
            target_venture_id = named_target
        elif ambiguity or (_GTM.search(body) and not _NEW_PRODUCT_REQUEST.search(body) and (
            _IN_THIS_ACCOUNT.search(body) or _EXPLICIT_EXISTING_TARGET.search(body) or _NAMED_GTM_TARGET.search(body)
        )):
            # Resolving an existing account target never creates a venture or
            # needs a paid classification, including when the account is full.
            return preview(text=text, actor_id=actor_id, request_id=request_id)
    if target_venture_id:
        _, workspace_id = assert_venture_access(target_venture_id, actor_id)
        with db()() as session:
            target = session.get(Venture, target_venture_id)
            if target is None or target.archived_at is not None:
                raise ChatStartError("venture_not_found", "This venture is no longer active. Choose another one.", 404)
    else:
        _assert_can_start(actor_id)
        if is_local():
            from app.workspaces.resolve import ensure_desk
            workspace_id = ensure_desk(actor_id).id
    claim_id = None
    stale_decision = None
    source = original_source
    request_id = request_id or new_id("ecr")
    if request_id:
        with db()() as session:
            saved = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.actor_id == actor_id, EntryChatProposal.request_id == request_id))
            if saved:
                if saved.text != body or (target_venture_id and saved.target_venture_id != target_venture_id):
                    raise ChatStartError("request_id_conflict", "This request ID was already used.", 409)
                if saved.state != "classifying":
                    return preview(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                                   request_id=request_id)
                if _utc(saved.updated_at) > utcnow() - timedelta(seconds=25):  # past interpret()'s budget, so a live classifier is never overtaken
                    raise ChatStartError("proposal_busy", "This request is already being understood. Try again shortly.", 409)
                # A crashed classifier may have an uncertain paid outcome.
                # Recover the saved request using the free fallback, never redial it.
                from app.intake.smart import classify_fallback
                metadata = json.loads(saved.receipt_json or "{}")
                source = metadata.get("original_source")
                guard_saved_input(saved)
                stale_decision = classify_fallback(body, page_context={"original_input": source or ""}).to_dict()
                claim_id = saved.id
    from app.intake.application import interpret
    if use_original and target_venture_id:
        with db()() as session:
            venture = session.get(Venture, target_venture_id)
            source = venture.raw_idea if venture else None
        guard_entry_input(source or "")
    parent_id = None
    previous_intake = None
    if previous_proposal_id:
        with db()() as session:
            parent = session.get(EntryChatProposal, previous_proposal_id)
            if (parent and parent.actor_id == actor_id and parent.target_venture_id == target_venture_id
                    and parent.state in {"pending", "clarify"}):
                guard_saved_input(parent)
                parent_id = parent.id
                if body.startswith(parent.text + "\n"):
                    from app.intake.smart import _owned_urls

                    metadata = json.loads(parent.receipt_json or "{}")
                    prior = metadata.get("intake") or {}
                    if prior.get("website_answers"):
                        sites = _owned_urls(parent.text) or _owned_urls(metadata.get("original_source") or "")
                        website_source = prior.get("extracted", {}).get("existing_site_url") or (sites[0] if len(sites) == 1 else None)
                        if website_source:
                            previous_intake = {**prior, "website_source": website_source}
    if not claim_id:
        now = utcnow()
        claim_id = new_id("ecp")
        claim = EntryChatProposal(id=claim_id, actor_id=actor_id, request_id=request_id,
            text=body, route="clarify", target_venture_id=target_venture_id,
            skipped_steps_json="[]", message="I'm reading your request.", next_step="clarify",
            state="classifying", receipt_json=json.dumps({"original_source": source,
                **({"_parent_proposal_id": parent_id} if is_local() and parent_id else {}),
                "created_new": not bool(target_venture_id)}), created_at=now,
            expires_at=now + timedelta(days=30), updated_at=now)
        with db()() as session:
            session.add(claim)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise ChatStartError("proposal_busy", "This request is already being understood. Try again shortly.", 409)
    if stale_decision is not None:
        from app.intake.smart import recover_original_json
        recovered = recover_original_json(actor_id=actor_id, workspace_id=workspace_id,
            purpose="intake.classify", request_id=f"entry:{claim_id}:classify")
        if recovered is not None and recovered["output"] is not None:
            stale_decision = recovered["output"]
    intake = stale_decision
    if intake is None and target_venture_id:
        from app.chat.idea_bridge import pending_question
        from app.intake.smart import classify_fallback
        if pending_question(target_venture_id, "", force=True):
            intake = classify_fallback(body, page_context={"original_input": source or ""}).to_dict()
    intake = intake or await interpret(body, actor_id=actor_id, workspace_id=workspace_id, original_input=source,
        request_id=f"entry:{claim_id}:classify", **({"previous_intake": previous_intake} if previous_intake else {}))
    return preview(text=text, actor_id=actor_id, target_venture_id=target_venture_id,
                   request_id=request_id, intake=intake, original_source=source, claim_id=claim_id)


def get(proposal_id: str, actor_id: str) -> dict:
    with db()() as session:
        row = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if row is None:
            raise ChatStartError("proposal_not_found", "Open Add Venture and start with your idea again.", 404)
        guard_saved_input(row)
        if row.state == "committed" and not row.target_venture_id:
            raise ChatStartError("venture_not_found", "The saved venture could not be verified.", 404)
        if row.target_venture_id:
            _owned_id, workspace_id = assert_venture_access(row.target_venture_id, actor_id)
            venture = session.get(Venture, row.target_venture_id)
            if venture is None or venture.archived_at is not None:
                raise ChatStartError("venture_not_found", "Open All ventures and restore this venture from Recently deleted to see its conversation.", 404)
            if row.state == "committed":
                with service.venture_scope(row.target_venture_id, actor_id=actor_id, workspace_id=workspace_id):
                    receipt = json.loads(row.receipt_json)
                    if receipt["venture_id"] != row.target_venture_id or receipt["workspace_id"] != workspace_id:
                        raise ChatStartError("venture_not_found", "The saved venture could not be verified.", 404)
        _resume_pending(session, row)
        session.commit()
        return {"status": "proposal", "proposal": _proposal(row)}


def get_text(proposal_id: str, actor_id: str) -> str:
    with db()() as session:
        row = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if row is None:
            raise ChatStartError("proposal_not_found", "Your saved idea could not be found. Start with your idea again.", 404)
        _assert_active_local_proposal(session, row)
        guard_saved_input(row)
        return row.text


def history(actor_id: str, target_venture_id: str | None = None) -> dict:
    if target_venture_id:
        assert_venture_access(target_venture_id, actor_id)
    with db()() as session:
        query = select(EntryChatProposal).where(EntryChatProposal.actor_id == actor_id)
        query = query.where(EntryChatProposal.target_venture_id.is_(None) | EntryChatProposal.target_venture_id.in_(
            select(Venture.id).where(Venture.archived_at.is_(None), Venture.stage != "purged")))
        if target_venture_id:
            query = query.where(EntryChatProposal.target_venture_id == target_venture_id)
        rows = []
        for row in session.scalars(query.order_by(EntryChatProposal.created_at.desc()).limit(100)):
            try:
                guard_saved_input(row)
            except ChatStartError:
                continue
            rows.append(row)
        for row in rows:
            _resume_pending(session, row)
        session.commit()
        return {"items": [
            {"text": row.text, "proposal": _proposal(row)} for row in reversed(rows)
        ]}


def cancel(proposal_id: str, actor_id: str) -> dict:
    with db()() as session:
        row = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if row is None:
            raise ChatStartError("proposal_not_found", "Proposal not found.", 404)
        _assert_active_local_proposal(session, row)
        guard_saved_input(row)
        if row.target_venture_id:
            assert_venture_access(row.target_venture_id, actor_id)
        if row.state in {"pending", "clarify"}:
            row.state = "cancelled"
            row.updated_at = utcnow()
            session.commit()
        return {"status": row.state, "proposal": _proposal(row)}


def _assert_can_start(actor_id: str) -> None:
    from app.ventures.quota import VentureLimitReached, assert_can_create_venture

    try:
        assert_can_create_venture(actor_id)
    except VentureLimitReached as exc:
        raise ChatStartError(exc.code, exc.message, 403) from exc


# shortcut: English naming sentences; "Call It Home" keeps its capitals so a real name is not cut.
_NAME_SENTENCE = re.compile(r"(?:(?i:(?:i|we)(?:['’]d|['’]ll|\s+would|\s+will)?(?:\s+(?:like|want)\s+to)?|let['’]s|please)\s+)?"
                            r"(?:(?:[Cc]all|[Nn]ame)\s+(?:it|this)\s+|(?i:(?:it['’]s|it\s+is)\s+(?:called|named)|the\s+name\s+(?:is|will\s+be))\s+)"
                            r"[\"“'‘]?(.+?)[\"”'’]?[.!]*")


def _typed_name(name: str) -> str:
    """The name inside "I would like to call it Policy Harbor."; anything else is taken as typed."""
    match = _NAME_SENTENCE.fullmatch(name.strip())
    return match.group(1) if match else name


def _clean_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > 48 or any(ord(c) < 32 for c in clean):
        raise ChatStartError("invalid_name", "Give this venture a name of up to 48 characters.")
    return clean


async def suggest_proposal_name(proposal_id: str, actor_id: str, request_id: str) -> dict:
    """An explicit naming request has a durable, non-replaying outcome."""
    service._assert_entry_actor(actor_id)
    with db()() as session:
        original = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if original is not None:
            _assert_active_local_proposal(session, original)
            guard_saved_input(original)
        if original is not None and original.target_venture_id is None:
            _resume_pending(session, original)
            session.commit()
        prior = (json.loads(original.receipt_json or "{}").get("name_requests", {}).get(request_id)
                 if original is not None else None)
        if (prior and prior.get("state") == "unavailable" and original.state == "pending"
                and _utc(original.expires_at) > utcnow()):
            return {"name": None, "reason": prior.get("reason", "name_unavailable"),
                    "message": "You can type a name of your own to continue.", "source": "free_text"}
    if prior:
        from app.intake.smart import recover_original_json
        try:
            recovered = recover_original_json(actor_id=actor_id, workspace_id=None,
                purpose="intake.suggest_name", request_id=f"entry:{proposal_id}:name:{request_id}")
        except Exception as exc:
            from app.chat.recovery import log_founder_exception

            log_founder_exception(exc, original.target_venture_id if original else None, "entry.name.recover")
            from llm_gateway.errors import ProviderCallError
            if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
                raise ChatStartError("name_outcome_unknown",
                    "This naming request is awaiting its saved result. Type a name to continue.", 409) from exc
            raise
        if recovered is not None and recovered["output"] is not None:
            name = _clean_name(recovered["output"]["name"])
            with db()() as session:
                row = session.scalar(select(EntryChatProposal).where(
                    EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id).with_for_update())
                if row is None or row.target_venture_id is not None or row.state != "pending" or _utc(row.expires_at) <= utcnow():
                    raise ChatStartError("proposal_not_pending", "Choose a name before continuing this new venture.", 409)
                raw = row.receipt_json
                metadata = json.loads(raw or "{}")
                slot = metadata.get("name_requests", {}).get(request_id)
                if slot is None:
                    raise ChatStartError("name_unavailable", "The original naming request is unavailable.", 409)
                metadata["name_requests"][request_id] = {"state": "complete", "name": name, "source": "model"}
                changed = session.execute(update(EntryChatProposal).where(
                    EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                    EntryChatProposal.state == "pending", EntryChatProposal.receipt_json == raw,
                ).values(receipt_json=json.dumps(metadata)).execution_options(synchronize_session=False))
                session.commit()
                if changed.rowcount != 1:
                    raise ChatStartError("proposal_busy", "This request is already in progress. Try again shortly.", 409)
            return {"name": name, "source": "model"}
    with db()() as session:
        row = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if row is None:
            raise ChatStartError("proposal_not_found", "Proposal not found.", 404)
        if row.target_venture_id or row.state != "pending" or _utc(row.expires_at) <= utcnow():
            raise ChatStartError("proposal_not_pending", "Choose a name before continuing this new venture.", 409)
        raw = row.receipt_json
        metadata = json.loads(raw or "{}")
        requests = metadata.setdefault("name_requests", {})
        prior = requests.get(request_id)
        if prior:
            if prior.get("name"):
                return {"name": prior["name"], "source": prior.get("source") or "model"}
            raise ChatStartError("name_unavailable", "That naming request has no confirmed result; type a name to continue.", 409)
        if any(record.get("state") == "calling" for record in requests.values()):
            raise ChatStartError("proposal_busy", "A naming suggestion is already being prepared.", 409)
        if len(requests) >= 30:
            raise ChatStartError("name_limit", "Choose one of the suggestions or type a name to continue.", 409)
        requests[request_id] = {"state": "calling"}
        claimed = session.execute(update(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
            EntryChatProposal.state == "pending", EntryChatProposal.receipt_json == raw,
        ).values(receipt_json=json.dumps(metadata)).execution_options(synchronize_session=False))
        session.commit()
        if claimed.rowcount != 1:
            raise ChatStartError("proposal_busy", "This request is already in progress. Try again shortly.", 409)
        intake = metadata.get("intake") or {}
        extracted = intake.get("extracted") or {}
        idea, audience = extracted.get("idea") or row.text, extracted.get("audience") or ""
        brand = extracted.get("venture_name") or ""
        if re.search(r"\n(?:Audience|Clarification):", row.text, re.I) and not brand:
            from app.intake.smart import discovery_idea_text
            idea = discovery_idea_text(row.text) or idea
        avoid = [record["name"] for record in requests.values() if record.get("name")]
    from app.intake.venture_name import suggest_name_or_reason
    try:
        if brand and brand.casefold() not in {name.casefold() for name in avoid}:
            result = {"name": _clean_name(brand), "source": "website"}
        else:
            result = await suggest_name_or_reason(f"{brand}: {idea}" if brand else idea, audience,
                actor_id=actor_id, avoid=avoid, request_id=f"entry:{proposal_id}:name:{request_id}")
        name = _clean_name(result["name"]) if result.get("name") else None
    except ChatStartError:
        result, name = {}, None
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, row.target_venture_id, "entry.name.suggest")
        from llm_gateway.errors import ProviderCallError
        if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
            raise ChatStartError("name_outcome_unknown",
                "This naming request is awaiting its saved result. Type a name to continue.", 409) from exc
        log.warning("entry_name_suggestion_failed reason=%s", type(exc).__name__)
        result, name = {}, None
    reason = result.get("reason") or "name_unavailable"
    with db()() as session:
        row = session.get(EntryChatProposal, proposal_id, with_for_update=True)
        metadata = json.loads(row.receipt_json or "{}")
        metadata.setdefault("name_requests", {})[request_id] = (
            {"state": "complete", "name": name, "source": result.get("source") or "model"} if name else {"state": "unavailable", "name": None, "reason": reason})
        if row.state == "pending":
            row.receipt_json = json.dumps(metadata)
            session.commit()
    if not name:
        return {"name": None, "reason": reason, "message": "You can type a name of your own to continue.", "source": "free_text"}
    return {"name": name, "source": result.get("source") or "model"}


async def confirm(proposal_id: str, actor_id: str, *, name: str | None = None,
                  assets: dict | None = None, founder_stage: dict | None = None,
                  website_answers: dict | None = None, evidence_ids: list[str] | None = None) -> dict:
    guard_entry_input(name or "", *(assets or {}).values(), *(website_answers or {}).values())
    owner = new_id("ecc")
    try:
        return await _confirm(proposal_id, actor_id, name=name, lease_owner=owner,
                              assets=assets, founder_stage=founder_stage, website_answers=website_answers,
                              evidence_ids=evidence_ids)
    except BaseException:
        # A known request failure relinquishes only this invocation's lease.
        # Another request receiving "busy" can never release the active owner.
        with db()() as session:
            row = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.state == "committing"))
            if row and json.loads(row.receipt_json or "{}").get("commit_owner") == owner:
                session.execute(update(EntryChatProposal).where(
                    EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                    EntryChatProposal.state == "committing", EntryChatProposal.receipt_json == row.receipt_json,
                ).values(updated_at=utcnow() - timedelta(minutes=2, seconds=1)).execution_options(synchronize_session=False))
                session.commit()
        raise


async def _confirm(proposal_id: str, actor_id: str, *, name: str | None, lease_owner: str,
                   assets: dict | None = None, founder_stage: dict | None = None,
                   website_answers: dict | None = None, evidence_ids: list[str] | None = None) -> dict:
    service._assert_entry_actor(actor_id)
    factory = db()
    with factory() as session:
        row = session.scalar(select(EntryChatProposal).where(
            EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id))
        if row is None:
            raise ChatStartError("proposal_not_found", "Proposal not found.", 404)
        guard_saved_input(row)
        if row.state == "committed":
            receipt = json.loads(row.receipt_json)
            if row.target_venture_id != receipt["venture_id"]:
                raise ChatStartError("venture_not_found", "The saved venture could not be verified.", 404)
            with service.venture_scope(receipt["venture_id"], actor_id=actor_id,
                                       workspace_id=receipt["workspace_id"]):
                record_confirm(proposal_id, actor_id, receipt["workspace_id"],
                               receipt["venture_id"], row.route)
            return _public_receipt(receipt)
        if row.state not in {"pending", "committing"}:
            raise ChatStartError("proposal_not_pending", "This proposal can no longer be confirmed.", 409)
        if row.target_venture_id:
            assert_venture_access(row.target_venture_id, actor_id)
            target_venture = session.get(Venture, row.target_venture_id)
            if target_venture is None or target_venture.archived_at is not None:
                raise ChatStartError("venture_not_found", "This venture is no longer active. Choose another one.", 404)
        _resume_pending(session, row)
        session.commit()
        if row.state == "pending" and _utc(row.expires_at) <= utcnow():
            raise ChatStartError("proposal_expired", "This proposal expired. Send your message again.", 409)
        route, body, target = row.route, row.text, row.target_venture_id
        pending_metadata = json.loads(row.receipt_json) if row.receipt_json else {}
        if evidence_ids is not None and row.state in {"pending", "clarify"}:
            from app.intake.evidence import validate_entry_files, entry_files
            validate_entry_files(evidence_ids, actor_id)
            old_files = {item["evidence_id"] for item in entry_files(pending_metadata.get("evidence_ids", []), actor_id)}
            answers = [identifier for identifier in pending_metadata.get("evidence_ids", []) if identifier not in old_files]
            pending_metadata["evidence_ids"] = list(dict.fromkeys([*answers, *evidence_ids]))
        if name is not None and row.state == "pending" and not target:
            pending_metadata["chosen_name"] = _clean_name(_typed_name(name))
        if row.state == "pending":
            if website_answers is not None:
                from app.intake.smart import _PREFILL_KEYS
                candidate = pending_metadata.get("intake") or {}
                known = candidate.get("website_answers") or {}
                if (set(website_answers) - set(known) or any(not isinstance(value, str) or len(value) > 2000
                        for value in website_answers.values())):
                    raise ChatStartError("invalid_website_answers", "Correct only the displayed website answers (up to 2,000 characters each).")
                prefill = {**candidate.get("prefill_answers", {}),
                           **{key: value for key, value in website_answers.items() if key in _PREFILL_KEYS}}
                candidate = {**candidate, "prefill_answers": prefill,
                    "prefill_facts": {key: fact for key, fact in candidate.get("prefill_facts", {}).items()
                                      if prefill.get(key) == fact["value"]},
                    "website_answers": {key: value for key, value in known.items()
                    if key not in website_answers or website_answers[key] == value}}
                extracted = {**candidate["extracted"]}
                if "offer" in website_answers:
                    extracted["idea"] = website_answers["offer"]
                if "customer" in website_answers:
                    extracted["audience"] = website_answers["customer"]
                candidate["extracted"] = extracted
                pending_metadata["intake"] = candidate
                onboarding = pending_metadata.get("onboarding") or {}
                completed = set(onboarding.get("completed", [])) | (set(known) & {"offer", "customer"})
                completed.difference_update(key for key, value in website_answers.items() if not value.strip())
                pending_metadata["onboarding"] = {**onboarding,
                    "completed": [key for key in ("offer", "customer") if key in completed]}
            if assets is not None:
                pending_metadata["assets"] = assets
            if founder_stage is not None:
                pending_metadata["founder_stage_confirmation"] = founder_stage
            from app.intake.assets import public_site_url
            candidate_intake = pending_metadata.get("intake") or {}
            from app.intake.smart import DISCOVERY_QUESTION, has_idea_content, no_idea_intent
            candidate_assets = pending_metadata.get("assets") or {}
            implicit_site = candidate_intake.get("extracted", {}).get("existing_site_url")
            effective_site = candidate_assets.get("website") or implicit_site
            if (not candidate_intake.get("next_step") and no_idea_intent(row.text)
                    and not has_idea_content(row.text) and not row.target_venture_id
                    and not effective_site and not candidate_intake.get("extracted", {}).get("repo_url")):
                raise ChatStartError("idea_required", DISCOVERY_QUESTION)
            # A rejected inferred/private link stays a correctable pending
            # proposal. It must never create a venture before validation.
            if implicit_site and not public_site_url(implicit_site) and assets is not None and "website" in assets:
                effective_site = assets["website"]
                extracted = {**candidate_intake["extracted"], "existing_site_url": None}
                if extracted.get("stage") in {"landing", "live"}:
                    extracted["stage"] = "customers" if extracted.get("has_customers") else "code" if extracted.get("repo_url") else "idea"
                pending_metadata["intake"] = {**candidate_intake, "extracted": extracted}
            if effective_site and not public_site_url(effective_site):
                raise ChatStartError("website_url_invalid", "Share a public website URL starting with https:// or http://.")
        if pending_metadata.get("intake") is not None:
            from pydantic import ValidationError
            from app.intake.smart import IntakeResult

            try:
                pending_metadata["intake"] = IntakeResult.model_validate(pending_metadata["intake"]).to_dict()
            except ValidationError as exc:
                raise ChatStartError("idea_needs_detail", "I couldn't save your idea yet. Send your idea again, including who it's for and what it helps you do.") from exc
        if not pending_metadata.get("chosen_name") and any(request.get("state") == "calling" for request in pending_metadata.get("name_requests", {}).values()):
            raise ChatStartError("proposal_busy", "Wait for the name suggestion or type a name after it finishes.", 409)
        chosen_name = pending_metadata.get("chosen_name") or _proposal(row).get("venture_name")
        intake = pending_metadata.get("intake")
        original = pending_metadata.get("original_source") or body
        created_new = pending_metadata.get("created_new", not bool(target))
        saved_assets = pending_metadata.get("assets") or {}
        stage_confirmation = pending_metadata.get("founder_stage_confirmation") or {}
        if not target and row.state == "pending":
            _assert_can_start(actor_id)
        if row.state == "pending":
            pending_metadata["commit_owner"] = lease_owner
            claimed = session.execute(update(EntryChatProposal).where(
                EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.state == "pending", EntryChatProposal.expires_at > utcnow(),
            ).values(state="committing", updated_at=utcnow(), receipt_json=json.dumps(pending_metadata)).execution_options(synchronize_session=False))
            session.commit()
            if claimed.rowcount != 1:
                raise ChatStartError("proposal_busy", "Confirmation is already in progress. Try again.", 409)
        else:
            # A crash before the venture transaction leaves no linked venture.
            # Reclaim only an old attempt; a live first request keeps its lease.
            stale_before = utcnow() - timedelta(minutes=2)
            if _utc(row.updated_at) > stale_before:
                raise ChatStartError("proposal_busy", "Confirmation is already in progress. Try again shortly.", 409)
            pending_metadata["commit_owner"] = lease_owner
            claimed = session.execute(update(EntryChatProposal).where(
                EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                EntryChatProposal.state == "committing",
                EntryChatProposal.updated_at <= stale_before,
            ).values(updated_at=utcnow(), receipt_json=json.dumps(pending_metadata)).execution_options(synchronize_session=False))
            session.commit()
            if claimed.rowcount != 1:
                raise ChatStartError("proposal_busy", "Confirmation is already in progress. Try again shortly.", 409)

    suggested_name = None
    if target:
        with factory() as session:
            venture = session.get(Venture, target)
            if venture is None or not venture.workspace_id or venture.archived_at is not None:
                session.execute(update(EntryChatProposal).where(
                    EntryChatProposal.id == proposal_id, EntryChatProposal.actor_id == actor_id,
                    EntryChatProposal.state == "committing",
                ).values(state="cancelled", updated_at=utcnow()))
                session.commit()
                raise ChatStartError("venture_not_found", "Venture not found.", 404)
            venture_id, workspace_id = venture.id, venture.workspace_id
    else:
        decision = entry_route(body, explicit_mode="product" if route == "gtm" else "idea")
        from app.intake.naming import derive_name
        result = await service.commit(decision, text=body, actor_id=actor_id,
                                      mode_options={
                                          "start": False,
                                          "project_intent": "personal" if route == "personal_build" else "startup",
                                          "defer_initial_clarification": route != "gtm",
                                          "entry_chat_proposal_id": proposal_id,
                                          **({"name": chosen_name or derive_name(None, intake["extracted"].get("idea") or body)} if intake else ({"name": chosen_name} if chosen_name else {})),
                                          **({"founder_chosen_name": chosen_name} if chosen_name else {}),
                                      }, surface="chat")
        venture_id = result["venture_id"]
        workspace_id = result["workspace"]["id"]
        suggested_name = result.get("suggested_name")
    from app.chat.conversations import bind_conversation, current_conversation
    conversation = current_conversation()
    conversation_id = conversation.id if conversation and conversation.venture_id == venture_id else ""
    # Destination writes must not inherit the source venture's conversation.
    with service.venture_scope(venture_id, actor_id=actor_id, workspace_id=workspace_id), bind_conversation(venture_id, conversation_id):
        from app.intake.evidence import adopt_entry_evidence, save_evidence
        adopt_entry_evidence(venture_id, actor_id, pending_metadata.get("evidence_ids", []))
        if not pending_metadata.get("evidence_ids"):
            save_evidence(venture_id, "typed", "typed", original, evidence_id=f"entry_input:{proposal_id}")
        from app.credits.accounts import ensure_account
        ensure_account(venture_id, workspace_id=workspace_id, owner_person_id=actor_id)
        next_step = "gtm" if route == "gtm" else "build" if route == "personal_build" else "idea"
        settings = {"entry_goal": route, "chat_next_step": next_step}
        if route == "personal_build":
            settings["project_intent"] = "personal"
        workspace_service.merge_settings(workspace_id, settings)
        if intake:
            from app.intake.application import apply
            apply(venture_id, workspace_id, original=original, decision=intake,
                  proposal_id=proposal_id, new_venture=created_new, assets=saved_assets)
        from app.ventures.stage import confirm_stage, get_stage, reassess
        site_url = saved_assets.get("website") or (intake or {}).get("extracted", {}).get("existing_site_url")
        site_result = None
        from app.intake.application import page_read_block
        site_block = page_read_block(body) if site_url else None
        if site_url and not site_block:
            from app.intake.assets import import_site
            try:
                site_result = await asyncio.to_thread(import_site, venture_id, site_url)
            except ValueError as exc:
                raise ChatStartError("website_url_invalid", str(exc)) from exc
        reassess(venture_id)
        if stage_confirmation.get("confirmed"):
            confirm_stage(venture_id, stage_confirmation["stage"])
        research_entry = bool(not site_block and intake and route != "personal_build" and (
            intake["path"] in {"research", "repo_build"} or route == "gtm" or
            get_stage(venture_id)["stage"] in {"building", "page_live", "outreach", "traction"}))
        requested_search = _entry_customer_search(body, target) if is_local() and route == "gtm" else ""
        goal_entry = bool(route == "gtm" and (requested_search or _gtm_request(original)))
        from app.ventures.startup_review import enabled
        with factory() as session:
            if route == "startup" and enabled(session, venture_id):
                research_entry = False
        opened = None
        if route in {"startup", "personal_build"} or route == "gtm" and created_new and intake and not goal_entry:
            from app.idea.service import open_session

            with factory() as session:
                venture = session.get(Venture, venture_id)
                session.expunge(venture)
            opened = open_session(venture=venture, actor_subject=actor_id,
                **({"prefill_answers": intake.get("prefill_answers"),
                    "prefill_facts": intake.get("prefill_facts"),
                    "website_answers": intake.get("website_answers"),
                    "intake_idea": intake["extracted"].get("idea"),
                    "intake_text": body,
                    "onboarding": pending_metadata.get("onboarding"),
                    "research_only": research_entry} if intake else {}))
            if opened.get("initialization_error"):
                raise ChatStartError("initial_clarification_unavailable",
                    "Your idea is saved, but the first question could not start. Choose Continue on Add Venture to try again.", 503)
            if research_entry and opened["state"] == "reviewing":
                from app.idea.service import confirm as confirm_idea
                confirm_idea(opened["id"], revision_id=opened["current_revision"]["id"], person_id=actor_id)
        kind = {"gtm": "gtm_review", "personal_build": "build_review", "startup": "idea"}[route]
        reply = {"gtm": "Your venture is ready. I'll use your saved product and audience details to find potential customers.",
                 "personal_build": "Your personal project is saved. I'm reading it now and will ask the one thing I most need to understand.",
                 "startup": "Your startup idea is saved. I'm reading it now and will ask the one thing I most need to understand."}[route]
        if intake:
            reply = ("Your research is queued. I'll show the results here." if intake["path"] == "research" and opened and opened["state"] == "reviewing"
                     else reply if route == "startup" else intake["founder_line"])
            if route == "gtm" and created_new and not goal_entry:
                reply += " I'll research your existing product before we choose the next customer step."
        actions = [{"label": "Review details", "href": f"/v/{venture_id}/idea" if route != "gtm" else f"/gtm?venture={venture_id}", "style": "secondary"}]
        if suggested_name:
            reply += f" I'm calling it {suggested_name} for now; rename it any time."
            actions.append({"label": "Rename", "href": f"/v/{venture_id}/settings#rename", "style": "secondary"})
        if site_block:
            reply += " " + site_block
        reply_kind = "entry_pending" if route in {"startup", "personal_build"} else "text"
        citations = None
        if route == "gtm" and (not created_new or goal_entry):
            from app.chat import customers
            from app.chat.execute import start_requested_work
            from app.chat.intent import find_customers_request, icp_question

            if requested_search:
                customer_reply = await customers.find_reply(venture_id, requested_search)
                customer_reply = await start_requested_work(venture_id, customer_reply, actor_id, actions={"gtm.find"})
            else:
                gtm_view = customers._view(venture_id)
                gtm_view["search_request"] = customers.search_note(body, (gtm_view.get("icp") or {}).get("who") or "")
                customer_reply = (await customers.icp_reply(venture_id, gtm_view) if icp_question(body)
                                  else await customers.find_reply(venture_id, body) if find_customers_request(body)
                                  else customers.render(venture_id, gtm_view))
                if not is_local() and find_customers_request(body) and not icp_question(body):
                    customer_reply = await start_requested_work(venture_id, customer_reply, actor_id, actions={"gtm.find"})
            reply, actions = customer_reply["body_md"], customer_reply["actions"]
            reply_kind = "gtm_route_confirmed"
            citations = customer_reply["citations"]
            if any(action.get("action") == "gtm.find" for action in actions):  # only beside a search that has not started
                actions = [*actions, {"label": "Optional: pressure-test your idea", "action": "idea.pressure_test.prepare", "style": "secondary"}]
        href = {"gtm": f"/gtm?venture={venture_id}",
                "personal_build": f"/v/{venture_id}/idea", "startup": f"/v/{venture_id}/idea"}[route]
        for index, message in enumerate(re.split(r"\n(?:Offer|Audience|Clarification|Context):[ \t]*", body, flags=re.I)):
            append_message(venture_id, role="user", body_md=re.sub(r"^Skip this question$", SKIP_CHOICE_WORDS, message, flags=re.M),
                           idempotency_key=f"entry:{proposal_id}:user" + (f":{index}" if index else ""))
        from app.org.characters import stamp_speaker
        from app.org.manager import manager_speaker

        append_message(venture_id, role="assistant", body_md=stamp_speaker(reply, manager_speaker(venture_id)),
                       kind=reply_kind,
                       actions=actions,
                       citations=citations,
                       idempotency_key=f"entry:{proposal_id}:assistant")
        receipt = {"status": "committed", "route": route, "venture_id": venture_id,
                   "workspace_id": workspace_id, "next_step": next_step, "created_new": created_new,
                   "action": {"kind": kind, "venture_id": venture_id, "href": href},
                   "message": reply}
        receipt["founder_stage"] = get_stage(venture_id)
        if site_result:
            receipt["site"] = site_result["site"]
        if intake:
            receipt.update(intake=intake, intake_path=intake["path"], button_label=intake["button_label"])
        with factory() as session:
            row = session.get(EntryChatProposal, proposal_id, with_for_update=True)
            if row.state == "committed":
                return _public_receipt(json.loads(row.receipt_json))
            if row.state != "committing" or json.loads(row.receipt_json or "{}").get("commit_owner") != lease_owner:
                raise ChatStartError("proposal_busy", "Another continuation owns this request. Try again shortly.", 409)
            if chosen_name and created_new:
                receipt["name"] = session.get(Venture, venture_id).name
            row.state = "committed"
            row.target_venture_id = venture_id
            original_metadata = json.loads(row.receipt_json or "{}")
            saved_receipt = dict(receipt)
            for key in ("_intake_model_outputs", "_parent_proposal_id"):
                if key in original_metadata:
                    saved_receipt[key] = original_metadata[key]
            row.receipt_json = json.dumps(saved_receipt)
            row.updated_at = utcnow()
            session.commit()
        record_confirm(proposal_id, actor_id, workspace_id, venture_id, route)
        if route == "startup":
            enqueue_ready_exploration(venture_id)
        return receipt


def enqueue_ready_exploration(venture_id):
    # shortcut: confirmation and the job use separate commits, use an outbox if cards are missed.
    try:
        from app.core.jobs import enqueue
        from app.experiments import belief_autopilot, exploration
        from app.ventures.current_idea import intake_pending

        if not (belief_autopilot.enabled() or exploration.enabled()):
            return
        with db()() as session:
            if intake_pending(venture_id, session=session):
                return
            proposal = session.scalar(select(EntryChatProposal).where(
                EntryChatProposal.target_venture_id == venture_id, EntryChatProposal.state == "committed",
                EntryChatProposal.route == "startup").order_by(EntryChatProposal.updated_at.desc()).limit(1))
            if not proposal:
                return
            venture = session.get(Venture, venture_id)
            enqueue(session, "experiment.exploration.prepare",
                {"proposal_id": proposal.id, "actor_id": proposal.actor_id},
                workspace_id=venture.workspace_id, venture_id=venture_id,
                dedupe_key=f"entry:{proposal.id}:exploration")
            session.commit()
    except Exception as exc:
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, venture_id, "experiment.exploration.prepare")


async def run_exploration(payload: dict) -> None:
    venture_id = payload["venture_id"]
    try:
        with service.venture_scope(venture_id, actor_id=payload["actor_id"], workspace_id=payload["workspace_id"]):
            from app.chat.store import idempotent_message
            from app.experiments import belief_autopilot, exploration

            key = f"entry:{payload['proposal_id']}:exploration"
            from app.ventures.current_idea import intake_pending

            if await asyncio.to_thread(intake_pending, venture_id):
                return
            autopilot = belief_autopilot.enabled()
            if autopilot:
                # Founder decision: nobody asked for a plan yet, so no unasked budget card on the first screen.
                # The plan is one request away when they want it.
                if os.getenv("SUPYMEM_DAY_ONE_PLAN_CARD") != "1":
                    return
                from app.chat import market_beliefs

                if idempotent_message(venture_id, "assistant", key):
                    return
                card = await market_beliefs.prepare(venture_id, {"day_one": True, "max_emails_per_day": 0})
            else:
                if not exploration.enabled():
                    return
                from app.ventures.current_idea import current_idea

                with db()() as session:
                    venture = exploration._authorize(session, venture_id)
                    idea = current_idea(venture, session=session, for_display=True)[:4000]
                card = await exploration.act(venture_id, "experiment.exploration.next", {"idea": idea})
            if not card or not (card.get("body_md") or "").strip():
                log.info("entry_exploration_empty", extra={"venture_id": venture_id})
                return
            if not autopilot and card.get("missing_field"):
                key += ":scope:" + card["missing_field"]
            append_message(venture_id, role="assistant", body_md=card["body_md"], kind=card.get("kind") or "text",
                           actions=card.get("actions") or [], citations=card.get("citations") or [],
                           idempotency_key=key)
    except Exception as exc:
        from app.chat.recovery import founder_failure, log_founder_exception, post_failure

        failure = founder_failure(exc, venture_id, "your customer ideas",
            {"action": "status", "payload": {}},
            safety="Your idea is saved. Check your saved progress before trying again.")
        try:
            with service.venture_scope(venture_id, actor_id=payload["actor_id"], workspace_id=payload["workspace_id"]):
                await asyncio.to_thread(post_failure, venture_id, step="experiment.evaluate",
                    key=f"entry:{payload['proposal_id']}:exploration:failed", kind="experiment_failed",
                    body_md=failure["body_md"], actions=failure["actions"], requested=True)
        except Exception as save_error:
            log_founder_exception(save_error, venture_id, "experiment.exploration.failure.save")
        raise
