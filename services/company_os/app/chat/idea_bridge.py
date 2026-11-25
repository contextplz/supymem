"""Route answers to a chat-started idea through its saved clarification session."""

from __future__ import annotations

import re

from sqlalchemy import select

from app.db.models import Venture
from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
from app.idea.agent import answer_metadata, is_options_request, is_unsure_answer
from app.idea.service import _chat_entry_deferred, chat_entry_goal, loads
from app.state import db


def load_venture(venture_id: str) -> Venture:
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise KeyError("venture")
        session.expunge(venture)
        return venture


def session_belongs_to(session_id: str, venture_id: str, workspace_id: str) -> bool:
    with db()() as session:
        row = session.get(IdeaSession, session_id)
        return bool(row and (row.venture_id, row.workspace_id) == (venture_id, workspace_id))


def list_sessions(venture_id: str) -> dict:
    from app.idea.service import _project_intent

    with db()() as session:
        rows = session.scalars(select(IdeaSession).where(IdeaSession.venture_id == venture_id)
                               .order_by(IdeaSession.created_at.desc()))
        return {"items": [{"id": row.id, "state": row.state, "case": row.case,
                           "project_intent": _project_intent(session, row.workspace_id, row.venture_id)} for row in rows]}


_OTHER_REQUEST = re.compile(
    r"^\s*(?:what|why|how|where|when|who|show|open|start|run|pause|stop|"
    r"cancel|skip|restore|research|build|publish|send|approve|review|book(?=\s+(?:\d|me\b|us\b|an?\b|the\b|my\b|our\b))|test|go to|switch to|"
    r"change (?:the |my )?(?:task|plan|venture|idea)|i want to (?:build|start|go to|switch))\b",
    re.I,
)

_WORK_PREFIX = (
    r"^\s*(?:please(?:\s*,\s*|\s+))?(?:(?:can|could|would)\s+you\s+|"
    r"(?:i\s+(?:want|would\s+like)\s+to|let['’]?s)\s+)?"
)
_WORK_REQUEST = re.compile(
    _WORK_PREFIX +
    r"(?:write|compose|make|build|plan|research|find|draft|create|generate|design|"
    r"prepare|investigate|search|look\s+up|get|check|skip|restore|rerun|re-run|redo|continue)\b", re.I,
)
_WRITTEN_REQUEST = re.compile(
    _WORK_PREFIX +
    r"(?:(?:write|compose|draft)\b(?!\s+(?:(?:me|some|the|my|our|a|an)\s+)*(?:code|software|app(?:lication)?|program)\b)|"
    r"i\s+(?:need|want|would\s+like)\s+(?:a|an|the|my|our)\s+(?:[\w-]+\s+){0,3}"
    r"(?:checklist|brief|document|worksheet|agenda|outline|handout)\b|"
    r"(?:give\s+me|outline|list|design|create|make|prepare|generate)\b"
    r"[^.!?\n]*\b(?:plan|scripts?|questions?|schedule|checklist|outline|list|proposal|documents?|text|agenda|handout|worksheet|guide|brief|summary)\b|"
    r"(?:shorten|revise|rewrite|edit|condense|tighten|simplify)\b[^.!?\n]*\b"
    r"(?:guide|worksheet|documents?|text|plan|checklist|outline|brief|summary|scripts?|questions?|agenda|handout|proposal)\b|"
    r"(?:design|create|make|prepare|generate)\b[^!?\n]*\b(?:as|in)\s+(?:a\s+)?(?:document|text)\b|"
    r"(?:design|create|prepare)\b[^.!?\n]*\bmarket\s+test\b|"
    r"continue\b[^.!?\n]*\b(?:guide|worksheet|document|text|plan|checklist|outline)\b|"
    r"(?:turn|convert|transform|adapt|format|put)\b[^.!?\n]*\b(?:into|as|in)\b"
    r"[^.!?\n]*\b(?:agenda|handout|worksheet|guide|checklist|outline|document|summary|brief|script)\b)", re.I,
)

_DIRECT_QUESTION = re.compile(
    r"^\s*(?:please(?:\s*,\s*|\s+))?(?:(?:can|could|would|will|may|might|do|have|has|had|am)\s+(?!be\b)"
    r"(?:(?:you|i|we|he|she|it|they|there)\b|(?=[^.!?\n]*\?))|whom)\b", re.I,
)
_CONVERSATIONAL_LEAD = re.compile(
    r"^\s*(?:(?:just|ok(?:ay)?|so|please|hey|hi|hello|yo|um+|uh+|hm+|erm|ah+|well|alright|right|yes|yeah|yep|sure|like|and|also|aur|phir|now|then|anyway)[,\s.!:]+)+", re.I,
)
_IMPERATIVE_SUFFIX = re.compile(
    r"\b(?:kar\s+(?:do|de)|karo|bana\s+do|banao|dikhao|batao|dhoondh\s+do)"
    r"(?:\s+please)?[.!?]*\s*$", re.I,
)

_READ_REQUEST = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:what|why|how|where|when|who|which|whose|is|are|was|were|does|did|"
    r"should|do\s+you|explain|describe|summari[sz]e|compare|review|list(?!\s+of\b)|"
    r"tell\s+me|remind\s+me|ask\s+me|recommend|suggest|advise|"
    r"help\s+me\s+(?:understand|choose|decide|prioriti[sz]e)|walk\s+me\s+through|"
    r"give\s+me\s+(?:(?:an?|one|single)\s+)?(?:explanation|summary|overview|question|recommendation|next\s+step)|"
    r"i\s+(?:want|would\s+like)\s+to\s+(?:know|understand))\b",
    re.I,
)
_READ_QUALIFIER = re.compile(
    r"^(?:(?:without|before|do\s+not|don't)\b[^,;:.!?\n]{1,160}[,;:.]\s*|"
    r"for\s+now\s*[,;:]\s*|"
    r"(?:read[- ]only|advice\s+only|answer\s+only|for\s+information\s+only)"
    r"(?:\s*[:,.-]\s*|\s+))",
    re.I,
)

# ponytail: explicit correction phrases, extend these if a restatement is missed.
_CORRECTION = re.compile(
    r"\b(?:that['’]?s\s+not\s+right|that\s+is\s+not\s+right|not\s+an?\b|"
    r"i\s+want\s+(?!to\b)|it['’]?s\s+just\s+an?\s+idea|"
    r"i\s+(?:don['’]?t|do\s+not)\s+have\s+(?:any\s+)?customers)\b",
    re.I,
)


def informational_request(text: str) -> bool:
    """Read/advice requests after context are not implicit answers to a saved question.

    Match request syntax, not question words inside a descriptive answer. A
    bounded no-work/polite prefix can precede the request. Explicit saved-chip
    replies still use the separate forced, question-ID-fenced answer path.
    """
    from app.chat.intent import _split_clauses, looks_like_recap_request
    from app.chat.llm_router import _command_evidence

    evidence = _command_evidence(text)
    offset = 0
    for clause in _split_clauses(evidence, question_reply=True):
        start = evidence.find(clause, offset)
        offset = start + len(clause)
        request = re.sub(r"^\s*please(?:\s*,\s*|\s+)", "", clause.strip(), flags=re.I)
        for _ in range(2):
            qualifier = _READ_QUALIFIER.match(request)
            if qualifier is None:
                break
            request = request[qualifier.end():].lstrip()
        if _READ_REQUEST.match(request) is not None or _WRITTEN_REQUEST.match(request) is not None or looks_like_recap_request(request):
            if _relative_clause(evidence[:start], request):
                continue
            return True
    return False


def _relative_clause(prefix: str, text: str) -> bool:
    prefix = prefix.rstrip(" \t")
    return bool(prefix.strip() and not prefix.endswith((".", "!", "?", ";", "\n"))
                and not text.rstrip().endswith("?")
                and re.match(r"(?:what|why|how|where|when|who|which|whose)\b", text.strip(), re.I))


def written_request(text: str) -> bool:
    # shortcut: explicit English directives only, extend semantic routing when other phrasing must bypass setup.
    from app.chat.intent import (
        _CONTENT_DRAFT_RE, _CONTENT_OTHER_RE, _PLAN_GENERATE_RE, classify_intent, local_plan_revision_request, native_plan_revision_request,
        outreach_email_request, recruitment_draft_request, single_interview_email_request,
    )
    from app.chat.llm_router import _command_evidence
    from app.chat.steer import _DRAFT_NAMED_NOTE_RE

    evidence = _command_evidence(text)
    clauses = [request_text(evidence), *[request_text(clause) for clause in re.split(r"(?<=[.!?;])\s+|\n+", evidence)]]
    for index, clause in enumerate(clauses):
        for _ in range(2):
            qualifier = _READ_QUALIFIER.match(clause)
            if qualifier is None:
                break
            clause = request_text(clause[qualifier.end():])
        clauses[index] = clause
    request = clauses[0]
    if _DRAFT_NAMED_NOTE_RE.match(request):
        return False
    if (_READ_REQUEST.match(request) or _DIRECT_QUESTION.match(request)) and not _WRITTEN_REQUEST.match(request):
        return False

    if re.search(r"\bmarket\s+beliefs?\b", text, re.I):  # belief cards have their own prepared test plan
        return False
    # A bare "make a plan" is the venture plan flow; a written plan names what it is for.
    if re.search(r"\bplan\b", text, re.I) and not re.search(r"\b(?:interview|launch|outreach|marketing|content|market|test|pilot|\w+-day)\b", text, re.I):
        return False
    if (_CONTENT_DRAFT_RE.match(request) or _CONTENT_OTHER_RE.match(request) or classify_intent(request) in {"start_product_build", "build.tool.start"}
            or native_plan_revision_request(request) or local_plan_revision_request(request) or single_interview_email_request(request)
            or outreach_email_request(request) or recruitment_draft_request(request) or (_PLAN_GENERATE_RE.match(request)
            and not re.match(r"^(?:write|draft)\b", request_text(text), re.I))):
        return False
    effects = r"(?:send|publish|post|contact|build|run|start|find|search|research|look\s+up|buy|pay|delete|approve|launch)"
    if any(re.match(rf"^{effects}\b", clause, re.I) or (
            _WRITTEN_REQUEST.match(clause) and re.search(rf"\band\s+(?:then\s+)?{effects}\b", clause, re.I))
            for clause in clauses):
        return False
    return any(_WRITTEN_REQUEST.match(clause) for clause in clauses)


def request_text(text: str) -> str:
    from app.chat.guard import _LEAD
    from app.org.manager import manager_name
    from app.tenancy import current_or_none

    request = _CONVERSATIONAL_LEAD.sub("", answer_metadata(text)["founder_text"].strip())
    addressed = re.match(r"^([^,:\n]{1,40})[,:]\s*(.+)$", request, re.S)
    if addressed:
        scope = current_or_none()
        if addressed[1].strip().casefold() == manager_name(scope.venture_id if scope else None).casefold():
            request = _CONVERSATIONAL_LEAD.sub("", addressed[2])
    return re.sub(_LEAD, "", request, flags=re.I)


def direct_request(text: str, *, revise: bool = False, question: ClarifierTurn | None = None) -> bool:
    from app.gstack.office_hours import PHASE, skips_all_questions

    if skips_all_questions(text):
        return True
    if is_options_request(text) or is_unsure_answer(text):
        return False
    from app.chat.intent import (
        _DO_START, _QUESTION_LEAD, _split_clauses, classify_intent, declines_gtm, find_customers_request,
        looks_like_recap_request,
        single_interview_email_request, single_outreach_email_request,
    )
    from app.chat.guard import _DELETE_TASK, _HIRE_TASK, _MONEY_TASK, _OUTREACH_TASK
    from app.chat.llm_router import _command_evidence
    from app.chat.send_control import stop_sending_request

    if declines_gtm(text) or stop_sending_request(text):
        return True

    text = answer_metadata(text)["founder_text"]
    command = request_text(text)
    first_clause = next(iter(_split_clauses(_CONVERSATIONAL_LEAD.sub("", _command_evidence(text).strip()), question_reply=True)), "")
    outreach = re.sub(_WORK_PREFIX, "", command, flags=re.I)
    clause = next(iter(_split_clauses(text, question_reply=True)), text) if question is not None else text
    description = request_text(clause)
    negative = bool(re.match(r"^(?:do\s+not|don['’]?t)\b", description, re.I))
    # shortcut: saved descriptive fields and English request prefixes only, use semantic routing for unlabelled or multilingual product answers.
    if (question is not None and getattr(question, "kind", None) == "question"
            and loads(question.options).get("phase") != PHASE
            and loads(question.options).get("field") in {
                "offer", "workflow", "requirements", "constraints", "desired_outcome",
                "first_user_path", "current_solution", "problem", "proposed_options"}
            and description == re.sub(_WORK_PREFIX, "", description, flags=re.I) == _CONVERSATIONAL_LEAD.sub("", clause.strip())
            and not re.match(r"(?:please|kindly)\b", clause.strip(), re.I)  # asking Charlie, not describing the product
            and (_WORK_REQUEST.match(description) or _DO_START.match(description)
                 or _OTHER_REQUEST.match(description) or _READ_REQUEST.match(description) or negative)
            and not _QUESTION_LEAD.match(description)
            and (negative or not _DIRECT_QUESTION.match(_CONVERSATIONAL_LEAD.sub("", clause.strip())))
            and not re.match(r"(?:skip|restore|rerun|re-run|redo|start|run|begin|pause|stop|cancel|approve|book|"
                             r"ship|launch|rename|move|go\s+to|switch\s+to)\b", description, re.I)
            and not re.search(r"\b(?:me|us|my|our|competitors?)\b", description, re.I)
            and not any(pattern.match(description) for pattern in (_DELETE_TASK, _HIRE_TASK, _MONEY_TASK, _OUTREACH_TASK))
            and not written_request(description) and not find_customers_request(description)
            and not looks_like_recap_request(description)
            and not (single_interview_email_request(description) or single_outreach_email_request(description))
            and classify_intent(description) not in {
                "start_product_build", "build.tool.start", "pagebuilder.build", "plan_generate", "plan_revision", "plan_approve", "work_start",
                "waitlist.enable", "waitlist.close", "connections.connect", "connections.disconnect",
                "latest_error", "mac_prepare", "preview"}
            and not _outreach_command(description)):
        return False
    read = command
    for _ in range(2):
        qualifier = _READ_QUALIFIER.match(read)
        if qualifier is None:
            break
        read = read[qualifier.end():].lstrip()
    descriptive_start = bool(re.match(r"^start\s+with\b", command, re.I) and len(command.split()) >= 8)
    return bool(_WORK_REQUEST.match(command) or _outreach_command(outreach)
                or _IMPERATIVE_SUFFIX.search(command)
                or _READ_REQUEST.match(read) or _WRITTEN_REQUEST.match(read) or looks_like_recap_request(read)
                or _DIRECT_QUESTION.match(first_clause)
                or (_DO_START.match(command) and not descriptive_start)
                or (_OTHER_REQUEST.match(command) and not descriptive_start and not revise))


def split_question_reply(text: str, *, options: list[str] | None = None,
                         question: ClarifierTurn | None = None) -> tuple[str, str]:
    """Keep an answer separate from the following request, including its constraints."""
    from app.chat.intent import _split_clauses
    from app.chat.llm_router import _command_evidence

    raw = (text or "").strip()
    if direct_request(raw, question=question) and not _IMPERATIVE_SUFFIX.search(raw) and not any(
            isinstance(option, str) and raw.startswith(option) for option in options or []):
        return "", raw
    # shortcut: ambiguous comma/conjunction followups stay answers, use grounded request spans when these must split.
    descriptive_start = bool(re.match(r"^start\s+with\b", request_text(raw), re.I) and len(raw.split()) >= 8)
    evidence = _command_evidence(raw, preserve_length=True)
    offset = 0
    previous_start = 0
    previous_clause = ""
    for clause in _split_clauses(raw, question_reply=True):
        start = raw.find(clause, offset)
        offset = start + len(clause)
        if _CONVERSATIONAL_LEAD.fullmatch(previous_clause + " "):
            start = previous_start
        answer = raw[:start].strip().rstrip(",;")
        chosen = next((option for option in options or []
                       if isinstance(option, str) and answer.rstrip(".") == option.rstrip(".")), None)
        explicit_lead = request_text(raw[start:offset]) != raw[start:offset].strip()
        lead = re.sub(r"^(?:(?:and|also|then|aur|phir)\b[,\s]*)+", "", evidence[start:offset].strip(), flags=re.I)
        request_boundary = (chosen is not None or bool(re.search(r"[.!?;]\s*$|\n\s*$", evidence[:start]))
                            or request_text(lead) != lead or bool(_IMPERATIVE_SUFFIX.search(lead))
                            or bool(re.search(r"\b(?:me|us)\b", lead, re.I)))  # "and find me 10 leaders" asks; "and flag risks" describes
        request = evidence[start:] if _READ_QUALIFIER.match(evidence[start:]) else evidence[start:offset]
        if answer and request_boundary and (chosen is not None or not direct_request(answer, question=question)) and direct_request(request, question=question) and not _relative_clause(evidence[:start], request) and (
                not descriptive_start or explicit_lead):
            if question is not None and chosen is None:
                fields = {}
                if question.session_id:
                    with db()() as session:
                        idea = session.get(IdeaSession, question.session_id)
                        if idea and idea.venture_id == question.venture_id and idea.workspace_id == question.workspace_id:
                            revision = session.get(IdeaRevision, idea.current_revision_id) if idea.current_revision_id else None
                            fields = loads(revision.structured) if revision else {}
                if not _plausible_answer(answer, question, fields):
                    return "", raw
            return chosen if chosen is not None else answer, raw[start:]
        previous_start, previous_clause = start, clause
    return ("", raw) if direct_request(raw, question=question) or written_request(raw) else (raw, "")


def limit_answer(answer: str, question: ClarifierTurn | None) -> bool:
    """A limit in the founder's own words answers a constraints question."""
    return bool(question is not None and loads(question.options).get("field") == "constraints"
                and re.match(r"(?:do\s+not|don['’]?t|never|must\s+not)\b", answer.strip(), re.I))


def _plausible_answer(answer: str, question: ClarifierTurn, fields: dict) -> bool:
    from app.intake.naming import _GENERIC, _NOISE, _STOP, _WEAK_VERB
    from app.idea.agent import ASSUMED, is_evidence_question

    if direct_request(answer) and not direct_request(answer, question=question):
        return True
    if limit_answer(answer, question):
        return True
    options = loads(question.options)
    ignored = _GENERIC | _NOISE | _STOP | _WEAK_VERB

    def words(text):
        return set(re.findall(r"[\w'-]+", text.casefold())) - ignored

    def stems(text):  # "reimburse" is on topic when the idea says "reimbursement"
        return {word[:6] for word in words(text)}

    if answer.removeprefix(ASSUMED).split("\n", 1)[0] in options.get("options", []):  # a tapped choice, with any detail typed after it
        return True
    if is_unsure_answer(answer) or re.fullmatch(r"(?:skip|pass)[.!]?", answer, re.I):
        return True
    if is_evidence_question(question.text, options.get("options", [])) and is_evidence_question("", [answer]):
        return True
    field = options.get("field") or ("customer" if re.match(r"^who\b", question.text, re.I) else None)
    from app.intake.smart import _prefill
    if field and _prefill(answer).get(field):
        return True
    context = " ".join([question.text, *options.get("options", []),
                        *(value for value in fields.values() if isinstance(value, str))])
    if field == "customer":
        from app.intake.smart import audience_phrase

        # shortcut: English audience phrases and saved vocabulary, use typed relevance if legitimate replies are missed.
        phrase = re.sub(r"^(?:" + "|".join(_WEAK_VERB) + r")\s+with\s+", "", answer, flags=re.I)
        if audience_phrase(phrase):
            return True
        customer = fields.get("customer")
        known = {word.removesuffix("s") for word in words(customer if isinstance(customer, str) else "")}
        mentioned = {word.removesuffix("s") for word in words(answer)}
        vocabulary = {word.removesuffix("s") for word in words(context)} - known
        return bool(known & mentioned) and any(word.startswith(term) for term in vocabulary if len(term) >= 4 for word in mentioned)
    if stems(answer) & stems(context):
        return True
    # A full sentence is a real answer even with none of the idea's exact words; "I didn't catch that" loses it.
    if field != "customer" and len(re.findall(r"[^\W\d_]{2,}", answer)) >= 10:
        return True
    if field in {"offer", "workflow", "current_solution", "problem", "first_user_path"} and re.search(
            r"\b(?:owners?|staff|teams?|people|who|workers?|managers?|clinics?|shops?)\b", answer, re.I):
        return True
    if field in {"offer", "workflow", "current_solution", "problem"} and re.search(
            r"\b(?:helps?|tracks?|tracking|manage|manages|use|using|orders?|booking|invoic\w*|schedul\w*)\b", answer, re.I):
        return True
    if field == "pricing_hypothesis" and re.search(r"[$€£₹]|\b\d+(?:\.\d+)?\b", answer):
        return True
    return False


def _outreach_command(text: str) -> bool:
    """A send instruction, not an answer: "send ..." or an outreach verb with a number of recipients.
    Answers such as "email mostly" or "WhatsApp groups" describe channels and stay answers."""
    from app.chat.guard import _OUTREACH_TASK
    return bool(re.match(r"(?:send|blast|mass[- ]?\w+|reach out to|cold[- ]?(?:e-?mail|dm|call)|(?:post|publish|tweet|share|e-?mail|message|text|dm|contact)\s+(?:an?|the|this|that|my|our|one|every|all|each)\b)\b", text, re.I)
                or (_OUTREACH_TASK.match(text) and re.search(r"\b\d[\d,]*\s*k?\b", text)))


def pending_question(venture_id: str, text: str, *, force: bool = False,
                     revise: bool = False) -> tuple[str, str] | None:
    """Return a pending chat question or an awaiting proposal being corrected.

    The caller has already authenticated and checked venture membership. A
    Page-created sessions accept explicit corrections, not unrelated commands.
    An empty forced read only looks up the open question.
    """
    answer = text.strip()
    if not answer and not force:
        return None
    from app.chat.intent import VENTURE_RENAME_RE

    if VENTURE_RENAME_RE.fullmatch(answer):
        return None
    correction = revise or bool(_CORRECTION.search(answer))
    # force: the founder clicked an option of the saved question, so skip the
    # command heuristics. All ownership and pending-state checks below still apply.
    question_help = is_options_request(answer) or is_unsure_answer(answer)
    if not force and not question_help:
        from app.chat.new_venture import idea_of

        command = request_text(answer)
        if idea_of(command) is not None:
            return None  # a new-venture command is never an answer to this venture's question
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None or not venture.workspace_id:
            return None
        idea = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id,
            IdeaSession.state.notin_(("confirmed", "abandoned")),
        ).order_by(IdeaSession.created_at.desc()).limit(1))
        if idea is None:
            return None
        if revise and idea.state not in {"reviewing", "clarifying"}:
            return None
        from app.idea.question_history import guard_pending_question
        if guard_pending_question(session, idea):
            session.commit()
            from app.ventures.state_brief import invalidate_venture_state_brief
            invalidate_venture_state_brief(venture_id)
        latest = session.scalar(select(ClarifierTurn).where(
            ClarifierTurn.session_id == idea.id,
        ).order_by(ClarifierTurn.turn.desc()).limit(1))
        if latest is None or latest.role != "agent":
            return None
        from app.chat.idea_proposal import EDIT_QUESTION

        awaiting = idea.state in {"reviewing", "clarifying"}
        from app.gstack.office_hours import PHASE
        gstack = latest.kind == "question" and loads(latest.options).get("phase") == PHASE
        chat_entry = (_chat_entry_deferred(session, venture.workspace_id, venture.id)
                      or chat_entry_goal(session, venture.workspace_id, venture.id))
        if _chat_entry_deferred(session, venture.workspace_id) and not chat_entry:
            return None
        from app.chat.next_moves import question_asked_last

        # A statement typed right after the question answers it, whichever screen created the venture.
        asked_last = latest.kind == "question" and not answer.endswith("?") and question_asked_last(session, venture_id, latest.id)
        if not (force or chat_entry or asked_last) and not (
                gstack or latest.kind == "question" and loads(latest.options).get("detail_pending")
                or awaiting and (correction or latest.text == EDIT_QUESTION)):
            return None
        if latest.kind == "proposal":
            if not (correction and idea.state == "reviewing"
                    and latest.revision_id == idea.current_revision_id):
                return None
        elif latest.kind != "question":
            return None
        product_answer = False
        if not force and not question_help:
            if direct_request(answer, revise=revise, question=latest if asked_last else None):
                return None
            product_answer = direct_request(answer, revise=revise)
            from app.chat.intent import classify_intent

            # Descriptive answers mention waitlists, LinkedIn, invoices... and the
            # keyword classifier would hijack them. Only short text can be a command.
            # ponytail: word-count heuristic; use a model classifier if it misroutes.
            if not product_answer and not correction and len(command.split()) < 8 and classify_intent(command.rstrip("?")) not in (
                    {"general", "assistant"} if command.endswith("?") else {"general"}):
                return None
        if not (force or correction or question_help):
            revision = session.get(IdeaRevision, idea.current_revision_id) if idea.current_revision_id else None
            if not _plausible_answer(answer, latest, loads(revision.structured) if revision else {}):
                return None
        return idea.id, latest.id
