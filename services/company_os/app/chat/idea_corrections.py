"""A founder saying what should change in their idea: saved as a new revision, shown in the very next reply."""
from __future__ import annotations

import re

_LABELS = {"customer": "Customer", "offer": "What it does", "problem": "Problem", "pricing_hypothesis": "Pricing", "first_user_path": "First users", "summary": "Summary"}
_FIELD = (r"(?P<field>first\s+user\s+path|target\s+(?:customers?|audience|market|users?)|first\s+(?:customers?|users?)|customers?|audience|buyers?|"
          r"icp|users?|product|offer|problem|pain\s+point|price|pricing)")
_OWNER = r"(?:(?:the|my|our)\s+)?(?:(?:first|real|actual|main|primary)\s+)?"
_VALUE = r"(?P<value>[^\n]{2,2000})"
_MARKED = r"(?:no|nope|actually|wait|sorry|correction|wrong|not\s+quite|i\s+meant)\b"
# shortcut: English phrasings seen in founder tests; extend from missed real messages, or use model extraction.
_CHANGE = re.compile(r"\b(?:change|changing|update|updating|set|setting|replace|replacing|switch|switching|make|making|narrow|narrowing)\s+"
                     + _OWNER + _FIELD + r"(?:\s+field)?\s+(?:to|with|as|=)\s+" + _VALUE, re.IGNORECASE)
_FOCUS = re.compile(r"\b(?:focus\s+on|(?:start|begin)\s+with)\s+" + _VALUE, re.IGNORECASE)
_STATED = re.compile(r"(?:^|[.;,:!]\s+|\b(?:and|but)\s+)" + _OWNER + _FIELD + r"(?:\s+field)?\s+(?:is\s+now|is|are\s+now|are|(?:should|will|would)\s+be)\s+" + _VALUE, re.IGNORECASE)
_FOR = re.compile(r"(?:^|[.;,:!]\s+|\b(?:actually|no|wait|sorry)[,:]?\s+)(?:it['’]?s|it\s+is|this\s+is|it\s+should\s+be)\s+(?:(?:really|actually|meant|now)\s+)?for\s+" + _VALUE, re.IGNORECASE)
_RESTATED = re.compile(r"^(?:as\s+i\s+(?:just\s+)?said|like\s+i\s+(?:just\s+)?said|as\s+i\s+mentioned|again)[,:]?\s+" + _VALUE, re.IGNORECASE)
_REPLACE = re.compile(r"\breplac(?:e|ing)\s+(?P<old>[^\n]{2,300}?)\s+with\s+" + _VALUE, re.IGNORECASE)
_SUMMARY = re.compile(r"^(?:(?:here\s+is|(?:please\s+)?(?:use|apply|replace))\s+)?my\s+(?:own\s+)?summary(?:\s+with)?\s*:\s*(?P<value>.{2,2000})$", re.IGNORECASE | re.DOTALL)
_SUMMARY_INSTRUCTION = re.compile(r"(?:^|\s+)(?:please\s+)?(?:use|save|apply)\s+this\s+as\s+my\s+(?:own\s+)?summary[.!]?\s*$", re.IGNORECASE)
_USE_SUMMARY = re.compile(r"^(?:please\s+)?use\s+my\s+(?:own\s+)?summary[.!]?$", re.IGNORECASE)
_SCOPE = re.compile(r"\b(?:correct|change|update)\s+(?:the\s+)?(?:scope|first\s+audience)\s*:\s*"
    r"start\s+with\s+" + _VALUE, re.IGNORECASE)
_BARE_SCOPE = re.compile(r"^" + _MARKED + r"\s*[:,.!]\s*" + _VALUE, re.IGNORECASE)
_ALSO_REQUESTS = re.compile(r"\b(?:and|then)\s+(?:please\s+)?(?:find|send|start|run|rerun|re-run|redo|search|build|publish|email|post)\b", re.IGNORECASE)


def _field(name: str) -> str:
    name = " ".join(name.casefold().split())
    if name in {"product", "offer"}:
        return "offer"
    if name in {"problem", "pain point"}:
        return "problem"
    if name == "first user path":
        return "first_user_path"
    return "pricing_hypothesis" if name in {"price", "pricing"} else "customer"


def _clean(value: str) -> str:
    value = re.split(r"(?<=[.!?])\s|[,;]\s*(?:not|rather|instead|but|starting|they|who|which|because|since)\b|\s+(?:not|rather\s+than|instead\s+of)\s", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(r"^(?:for\s+|still\s+|really\s+|actually\s+)+", "", value.strip(" ,.;:!\"'"), flags=re.IGNORECASE)


def _also_answers(text: str, patch: dict, field: str | None) -> str:
    """The rest of a message that also corrects a field, when it is a full sentence or more: it answers what's being asked."""
    rest = " ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", text.strip())
                    if not any(isinstance(value, str) and value.casefold() in sentence.casefold() for value in patch.values()))
    # shortcut: ten words or an explicit field statement; use question-aware extraction for other short answers.
    full_answer = len(re.findall(r"[^\W\d_]{2,}", rest)) >= 10 or _STATED.search(rest)
    if field in {"problem", "pricing_hypothesis", "requirements", "constraints"} and patch.get("customer"):
        from app.chat.guard import _THANKS
        from app.idea.agent import answer_prefill, core_answer_status
        if field == "problem":
            rest = answer_prefill(text, field).get("problem", rest)
        full_answer = not _THANKS.match(rest) and (full_answer or core_answer_status(rest, 0, field=field, assessed=True) == (True, False))
    return rest if full_answer and not rest.endswith("?") else ""


def _usable(field: str, value: str) -> bool:
    from app.intake.smart import audience_phrase
    from app.secrets.detect import looks_like_secret

    return bool(value) and not looks_like_secret(value) and (field != "customer" or bool(audience_phrase(value)))


def parse(text: str, fields: dict | None = None) -> dict[str, str] | None:
    """The fields this message says to change, or None. Anything unclear stays ordinary chat."""
    from app.chat.guard import hostile_input
    from app.chat.research_revision import parse as research_correction
    from app.intake.smart import _prefill

    text = (text or "").strip()
    if not text or text.endswith("?") or hostile_input(text) or research_correction(text):
        return None
    if summary := _SUMMARY.fullmatch(text):
        from app.chat.guard import outreach_task_reply

        if outreach_task_reply("", text) is not None:
            return None
        customer = str((fields or {}).get("customer") or "").strip()
        value = _SUMMARY_INSTRUCTION.sub("", summary["value"]).strip()
        patch = {field: value for field, value in _prefill(value, infer_offer=False, grounded_customer=customer).items() if field in _LABELS}
        patch["summary"] = value
        return patch if all(_usable(field, value) for field, value in patch.items()) else None
    patch = {}
    correcting = bool(re.match(_MARKED + r"\s*[:,.!](?:\s|$)", text, re.IGNORECASE))
    for statement in re.split(r"(?<=[.!?])\s+|\n+", text):
        changes = {field: _clean(value) for field, value in _prefill(statement, explicit_only=True).items() if field in _LABELS}
        if not changes:
            if match := _CHANGE.search(statement) or _STATED.search(statement) or _SCOPE.search(statement):
                field = "customer" if match.re is _SCOPE else _field(match["field"])
                # An unmarked problem statement answers a question rather than correcting the idea.
                if field == "customer" or match.re is _CHANGE or correcting or re.match(_MARKED, statement, re.IGNORECASE):
                    changes[field] = _clean(match["value"])
            if not changes and (match := _FOR.search(statement) or _RESTATED.match(statement)):
                changes["customer"] = _clean(match["value"])
            if not changes and correcting and (match := _FOCUS.search(statement)):
                changes["customer"] = _clean(match["value"])
            if not changes and (match := _REPLACE.search(statement)):
                old = match["old"].strip(" \"'").casefold()
                field = next((key for key in _LABELS if old in str((fields or {}).get(key) or "").casefold()), None)
                if field:
                    changes[field] = _clean(match["value"])
            if (not changes and (match := _BARE_SCOPE.match(statement))
                    and not re.match(r"(?:i|we|you)\b", _clean(match["value"]), re.IGNORECASE)
                    and _usable("customer", _clean(match["value"]))):
                changes["customer"] = _clean(match["value"])
        patch.update(changes)
        correcting = correcting or bool(changes)
    if not patch or _ALSO_REQUESTS.search(text):
        return None
    return patch if all(_usable(field, value) for field, value in patch.items()) else None


def correction_receipt(venture_id: str, patch: dict) -> dict:
    from core.copy import founder_copy
    updated = "\n".join(f"- **{_LABELS[field]}:** {value}" for field, value in patch.items() if field in _LABELS)
    return {"kind": "text", "body_md": founder_copy("Updated. You've saved these changes:\n" + updated
        + "\n\nOpen Idea to review your saved direction, or ask me what to do next."),
        "actions": [{"label": "Review your idea", "href": f"/v/{venture_id}/idea"}], "citations": []}


def apply(venture_id: str, text: str, actor_subject: str | None) -> dict | None:
    """Save the change and return the reply, or None when this message isn't a correction."""
    from core.clock import utcnow
    from core.copy import founder_copy
    from core.ids import new_id
    from sqlalchemy import select

    from app.authz.roles import role_allows
    from app.authz.workspace import subject_person_id
    from app.chat.actions import get_action
    from app.chat.execute import _review_idea
    from app.chat.guard import hostile_input
    from app.chat.idea_proposal import EDIT_QUESTION
    from app.chat.research_revision import parse as research_correction
    from app.core.fsm import advance as fsm_transition
    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.db.models import Job, Venture
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea import service
    from app.state import db
    from app.tenancy import current_or_none
    from app.ventures.current_idea import revision_fields
    from app.workspaces.service import role_of

    with db()() as session:
        venture = session.get(Venture, venture_id)
        row = venture and session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state != "abandoned")
            .order_by(IdeaSession.created_at.desc()).limit(1))
        if not row or not row.current_revision_id:
            return None
        fields = revision_fields(session.get(IdeaRevision, row.current_revision_id))
        patch = parse(text, fields)
        scope = current_or_none()
        if actor_subject is None and scope and scope.actor != "system":
            actor_subject = scope.actor
        if not role_allows(role_of(row.workspace_id, subject_person_id(actor_subject)), get_action("idea.answer").required_permission):
            return None  # Ordinary routing answers these, including anyone without permission to change the idea.
        if not patch:
            if _USE_SUMMARY.fullmatch(text.strip()) and _usable("summary", fields.get("summary") or ""):
                return {"kind": "text", "body_md": founder_copy("Your summary is saved:\n\n" + fields["summary"]
                    + "\n\nOpen Idea to review it, or tell me what should change."),
                    "actions": [{"label": "Review your idea", "href": f"/v/{venture_id}/idea"}], "citations": []}
            if (re.match(r"^correction\s*:", text, re.IGNORECASE) and not text.strip().endswith("?")
                    and not hostile_input(text) and not research_correction(text) and not _ALSO_REQUESTS.search(text)):
                return {"kind": "needs_you", "body_md": "Which part of your idea should change? Tell me your customer, problem or what you want to offer.", "actions": [], "citations": []}
            return None
        marked_scope = _BARE_SCOPE.match(text)
        exclusion_scope = (_FOCUS.match(marked_scope["value"]) or marked_scope) if marked_scope else None
        marked_scope = marked_scope if marked_scope and patch.get("customer") == _clean(marked_scope["value"]) else None
        if (exclusion_scope and patch.get("customer") == _clean(exclusion_scope["value"])
                and (exclusion := re.search(r"[,;]\s*(?:not|rather\s+than|instead\s+of)\s+([^.!?\n]+)", exclusion_scope["value"], re.IGNORECASE))):
            limit = "Do not include " + _clean(exclusion[1])
            if not _usable("constraints", limit):
                return None
            patch["constraints"] = list(dict.fromkeys([*(fields.get("constraints") or []), limit]))
        session_id, state, revision_id = row.id, row.state, row.confirmed_revision_id
    if state == "confirmed":  # Confirmed revisions are history: a correction forks a review, which keeps the audit trail.
        session_id = service.start_direction_correction(session_id, "know", venture_id=venture_id, revision_id=revision_id)["id"]
    current = service.patch_revision(session_id, patch, venture_id=venture_id)
    with db()() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                                .order_by(ClarifierTurn.turn.desc()).limit(1))
        question = latest if latest and latest.role == "agent" and latest.kind == "question" else None
        answers = None
        if question:
            field = service._loads(question.options).get("field")
            answers = (patch.get(field) if "summary" in patch or marked_scope else text if question.text == EDIT_QUESTION
                       else patch.get(field) or _also_answers(text, patch, field))
            if not isinstance(answers, str):
                answers = None
            question_id = question.id
            if not answers:  # The question still showing was worded from the old idea.
                from app.idea.question_history import guard_question_turn
                guard_question_turn(session, row, question)
        elif latest and latest.kind == "proposal" or "summary" in patch and row.state == "reviewing" and not any(
                (payload := service.loads(job.payload_json)).get("session_id") == session_id
                and payload.get("revision_id") == current["id"] for job in session.scalars(select(Job).where(
                    Job.venture_id == venture_id, Job.workspace_id == row.workspace_id,
                    Job.kind.in_(("idea.clarify_turn", "idea.validate")), Job.status.in_(ACTIVE_JOB_STATUSES)))):
            # The proposal now shows this revision, so its Looks right and Confirm stay valid.
            session.add(ClarifierTurn(id=new_id("ict"), session_id=session_id, workspace_id=row.workspace_id, venture_id=venture_id,
                turn=service.next_turn_number(session, session_id), role="agent", kind="proposal",
                text=latest.text if latest and latest.kind == "proposal" else "Your summary is saved. Review your idea, or tell me what should change.",
                options=latest.options if latest and latest.kind == "proposal" else None,
                revision_id=current["id"], created_at=utcnow()))
            if row.state == "clarifying":
                fsm_transition(session, row, "proposing")
                fsm_transition(session, row, "reviewing")
        session.commit()
    if answers:  # It was also the answer to what's being asked, so the conversation moves on.
        service.post_message(session_id, answers, actor_subject=actor_subject, expected_question_id=question_id, reply_in_chat=True)
    lead = correction_receipt(venture_id, {field: current["structured"][field] for field in patch})["body_md"]
    if state == "confirmed":
        try:
            service.confirm(session_id, revision_id=current["id"], person_id=actor_subject, venture_id=venture_id,
                            start_research=False, accept_validation=True)
            return {"kind": "text", "body_md": founder_copy(lead), "actions": [], "citations": []}
        except RuntimeError:
            pass  # Still shown for review below, so nothing is lost.
    reply = _review_idea(venture_id)
    return {**reply, "body_md": founder_copy(lead) + "\n\n" + reply["body_md"]}
