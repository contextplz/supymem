"""Memory-backed factual answers with explicit abstention."""
from __future__ import annotations

import re
import sqlite3

from app.memory import get_memory


_CASE_REQUEST = re.compile(r"\b(case stud(?:y|ies)|workbook|historical example)\b", re.I)
_CASE_META = frozenset({"case", "study", "studies", "source", "cite", "relevant",
                        "workbook", "founder", "explain", "differs", "where", "exact", "show"})


def yc_quote_for_reply(question: str, recent_citations: list[list], *, context: str = "", reply: str = "") -> dict | None:
    from app.knowledge import yc_video_advice
    from app.chat.intent import _ANSWER_ONLY, find_customers_request, requested_customer_search, discovery_progress_question
    from app.chat.idea_bridge import written_request
    from app.chat.search_recovery import manual_discovery_request
    from app.chat.intent import classify_intent, linkedin_people_request
    from app.chat.assets_in_chat import pasted_contacts

    if (linkedin_people_request(question) or classify_intent(question).startswith("connections.") or pasted_contacts(question)
            or re.search(r"\bimport\b", question, re.I)
            or written_request(question) or _ANSWER_ONLY.search(question) or manual_discovery_request(question)
            or find_customers_request(question) or requested_customer_search(question)
            or not (discovery_progress_question(question)
                    or re.search(r"\b(?:customers?|users?|pricing|willingness to pay|paid pilot|enterprise sales|gtm|go[ -]?to[ -]?market)\b", question, re.I))):
        return None
    if re.search(r"how is the search going", reply, re.I):  # a progress receipt is not advice: no quote under it
        return None
    if any(citation.get("source") == "yc_video_quote"
           for citations in recent_citations[:3] for citation in citations if isinstance(citation, dict)):
        return None
    return yc_video_advice.quote(question + " " + reply, context=context, topic=reply)


def _workbook_reference(question: str) -> str | None:
    """Offer a cited analogy only when the founder explicitly asks for one."""
    if not _CASE_REQUEST.search(question):
        return None
    from app.knowledge import pmf
    from app.core.local_mode import is_local
    from app.knowledge.local_data import ReferenceDataError

    if is_local():
        try:
            message = pmf.corpus().get("message")
        except (ReferenceDataError, OSError) as error:
            return str(error)
        if message:
            return message

    query = " ".join(word for word in pmf.tokens(question) if word not in _CASE_META)
    if re.search(r"\b(user.provided|manual)\b", question, re.I):
        # Manual-input questions benefit from workflow/feedback analogies;
        # these added terms are retrieval context, not a claim about history.
        query += " manual beta workflow"
    cases = pmf.search(query, kind="historical_claim", limit=6)
    case = next((row for row in cases if len(set(row["matched_terms"]) - _CASE_META) >= 3
                 and row.get("documented_action") and row.get("company")), None)
    if case is None:
        return None
    company = re.sub(r"[\r\n<>]", " ", str(case["company"]))[:120]
    action = re.sub(r"[\r\n<>]", " ", str(case["documented_action"]))[:600]
    sheet = re.sub(r"[\r\n<>]", " ", str(case["sheet"]))[:120]
    action_field = next((field for field in case["fields"]
                         if field.get("value") == case["documented_action"]), None)
    citation = (action_field or {}).get("citation")
    location = f"cell `{citation}`" if citation else f"row {case['row']}"
    return (
        "I don't have a saved record establishing the reason for this specific venture decision. "
        f"A related workbook example is **{company}**: {action} "
        f"Source: *Startup PMF Playbook (1).xlsx*, **{sheet}**, {location} "
        "This is an analogy from the founder-supplied workbook, "
        "not independent verification or evidence that the same tactic will work here. "
        "The same customer constraints and outcome have not been established here. "
        "It does not validate this venture's data sources, constraints or outcomes. "
        "Check the differences and test the smallest version with real users before adopting it."
    )


async def answer_from_memory(venture_id: str, question: str) -> dict:
    """Return a cited answer, or abstain instead of constructing a plausible fact."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.db.models import Venture
    from app.state import db
    from app.tenancy import current_or_none
    from app.ventures.current_idea import current_idea
    with db()() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        if not venture or scope and (scope.workspace_id != venture.workspace_id or scope.venture_id not in {"", venture_id}):
            raise PermissionError("memory_scope_mismatch")
        summary = current_idea(venture, session=session)
    unavailable = False
    try:
        answer = await get_memory().answer(venture_id=venture_id, question=question)
    except PermissionError:
        raise
    except (LookupError, OSError, RuntimeError, NotImplementedError, sqlite3.Error, SQLAlchemyError) as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "memory.answer")
        answer, unavailable = None, True
    if answer is None or answer.abstained or not answer.text.strip():
        reference = _workbook_reference(question)
        note = ("Saved memory is unavailable" if unavailable else "No source-backed memory answers this question") + "; current saved context is not proof of the requested fact."
        return {
            "kind": "text",
            "body_md": (reference or "I don't have a record of that.") + "\n\n" + note +
                ("\nSaved idea context: " + summary[:1200] if summary else " You can continue with a draft; missing facts stay unknown."),
            "actions": [{"label": "Choose your next step", "action": "next_moves"}],
            "citations": [],
            "abstained": reference is None,
        }
    return {
        "kind": "text",
        "body_md": answer.text,
        "citations": [
            {"label": citation.statement, "href": f"#memory:{citation.mem_id}"}
            for citation in answer.citations
        ],
        "abstained": False,
    }
