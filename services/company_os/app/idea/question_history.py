"""Durable question identity shared by clarification and office hours."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select

from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
from app.idea.agent import answer_metadata, answered_field, core_answer_status, core_followup, is_unsure_answer

CONFIRM = "Your answers are saved. Review this summary and confirm your idea, or correct anything I missed."


def normalized_question(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", unicodedata.normalize("NFKC", text).casefold()).split())


def _target_field(field: str | None) -> str | None:
    from app.core.local_mode import is_local
    from app.ventures.founder_facts import ALIASES
    field = ALIASES.get(field, field)
    # shortcut: local job templates share one target, use typed intents before adding detailed workflow questions.
    return "offer" if is_local() and field in {"workflow", "requirements"} else field


def question_field(question: dict) -> str | None:
    from app.ventures.founder_facts import GSTACK_FIELDS
    gstack_fields = {**GSTACK_FIELDS, "Q4": "workflow", "Q5": "evidence"}
    field = question.get("known_field") or question.get("field")
    if question.get("gstack_id"):
        field = gstack_fields.get(question["gstack_id"], "gstack_" + question["gstack_id"])
    elif isinstance(field, str) and field.startswith("gstack_"):
        field = gstack_fields.get(field.removeprefix("gstack_"), field)
    field = answered_field(question.get("text", ""), field, question.get("options") or [])
    return _target_field(field)


def answered_questions(session, session_id: str) -> list[dict]:
    from app.chat.idea_proposal import EDIT_QUESTION
    from app.idea.service import _loads
    from app.intake.website import changing_idea
    idea = session.get(IdeaSession, session_id)
    if idea is None:
        return []
    turns = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id,
        ClarifierTurn.workspace_id == idea.workspace_id, ClarifierTurn.venture_id == idea.venture_id)
        .order_by(ClarifierTurn.turn)).all()
    questions = {turn.id: turn for turn in turns if turn.role == "agent" and turn.kind == "question"}
    if not questions:
        return []
    revision = session.scalar(select(IdeaRevision).where(IdeaRevision.session_id == session_id)
        .order_by(IdeaRevision.rev.desc()).limit(1))
    reopened = bool(revision and _loads(revision.structured).get("_gstack_reopened_from"))
    pending = None
    pairs = {}
    counts = {}
    for turn in turns:
        if turn.id in questions:
            pending = turn
            continue
        if turn.role != "founder" or turn.kind not in {"answer", "answer_correction"}:
            continue
        metadata = _loads(turn.options)
        if changing_idea(metadata.get("founder_text", "") if metadata.get("provenance") == "suggested_choice" else turn.text):
            pairs.clear()
            counts.clear()
            pending = None
            continue
        question_id = metadata.get("question_id")
        question = (questions.get(question_id) if isinstance(question_id, str) else None) or pending
        pending = None
        if question is None or question.turn >= turn.turn:
            continue
        options = _loads(question.options)
        if question.text == EDIT_QUESTION and not options.get("field"):
            continue
        metadata = {**answer_metadata(turn.text, options.get("options") or []), **metadata}
        if (reopened and options.get("phase") == "gstack" and turn.revision_id != revision.id
                and (metadata.get("provenance") == "suggested_choice" or turn.text.startswith("Assumed: ") or is_unsure_answer(turn.text))):
            continue
        field = answered_field(question.text, options.get("known_field") or options.get("field"), options.get("options") or [])
        if field not in {"workflow", "requirements"}:
            field = question_field({**options, "text": question.text})
        if question.id not in pairs:
            counts[field] = counts.get(field, 0) + 1
        answer = metadata.get("founder_text") or metadata.get("suggested_text") or turn.text
        resolved = True
        if (field in {"offer", "audience"} and options.get("field") not in {"workflow", "requirements"}
                and not is_unsure_answer(turn.text) and not turn.text.startswith("Assumed: ")):
            resolved = core_answer_status(answer, max(0, counts[field] - 1),
                field="customer" if field == "audience" else field)[0]
        pairs[question.id] = {**metadata, "question_id": question.id, "question": question.text, "field": field,
            "answer": turn.text, "resolved": resolved, "answered_at": turn.created_at.isoformat(),
            "answer_turn": turn.turn, "phase": options.get("phase")}
    return sorted(pairs.values(), key=lambda pair: pair["answer_turn"])


def repeated_question(question: dict, history: list[dict]) -> bool:
    text, field = normalized_question(question.get("text") or ""), question_field(question)
    return any(text and text == normalized_question(pair["question"])
        or field and field == _target_field(pair["field"]) and pair["resolved"] for pair in history)


def unanswered_question(question: dict, history: list[dict], *, snapshot: dict | None = None, known: dict | None = None) -> dict | None:
    from app.core.local_mode import is_local
    from app.idea.runner import _known_fields
    if known is None:
        known = _known_fields({**snapshot,
            "understanding": snapshot.get("understanding", snapshot),
            "answered_fields": snapshot.get("answered_fields", snapshot.get("_answered_fields", {})),
            "original_idea": snapshot.get("original_idea", snapshot.get("idea", "")),
        }) if snapshot and is_local() else {}
    completed = {_target_field(field) for field in known}
    if not repeated_question(question, history) and question_field(question) not in completed:
        return question
    field = question_field(question)
    unresolved = next((pair for pair in reversed(history) if _target_field(pair["field"]) == field and not pair["resolved"]), None)
    if field in {"offer", "audience"} and unresolved and field not in completed:
        attempts = sum(_target_field(pair["field"]) == field for pair in history)
        for followups in range(max(0, attempts - 1), 2):
            candidate = {**question, "text": core_followup("customer" if field == "audience" else field,
                followups, answer=unresolved["answer"]), "options": [], "requires_detail": [], "option_labels": [],
                "recommended_option": "", "recommendation_reason": ""}
            if not repeated_question(candidate, history):
                return candidate
    if snapshot and is_local() and snapshot.get("project_intent") != "personal":
        from app.clarify.dilemmas import ground_question
        completed.update(_target_field(pair["field"]) for pair in history if pair["resolved"])
        for target in ("customer", "current_workaround", "evidence", "pricing_hypothesis", "first_user_path", "desired_outcome"):
            if _target_field(target) in completed:
                continue
            candidate = ground_question({"field": target, "text": "What would you like to clarify next?", "options": [], "why": ""}, snapshot)
            if not repeated_question(candidate, history):
                return candidate
    return None


def confirm_turn(session, row, turn: ClarifierTurn) -> None:
    from app.core.fsm import advance
    from app.idea.service import _loads
    phase = _loads(turn.options).get("phase")
    turn.kind, turn.text, turn.options = "proposal", CONFIRM, None
    turn.revision_id = row.current_revision_id
    if row.state == "open":
        advance(session, row, "recon")
    if row.state in {"recon", "reviewing", "proposing", "validating"}:
        advance(session, row, "clarifying")
    advance(session, row, "proposing")
    advance(session, row, "reviewing")
    if phase == "gstack" and row.current_revision_id:
        from app.core.jobs import enqueue
        from app.db.models import Job
        from app.idea.validation import METHODS
        revision = session.get(IdeaRevision, row.current_revision_id)
        if revision is None:
            return
        structured = _loads(revision.structured)
        reviews = session.scalars(select(Job).where(Job.venture_id == row.venture_id,
            Job.workspace_id == row.workspace_id, Job.kind == "idea.validate")).all()
        reviewed = any(_loads(job.payload_json).get("session_id") == row.id
            and _loads(job.payload_json).get("revision_id") == row.current_revision_id
            and _loads(job.payload_json).get("stage") != "gstack_questions" for job in reviews)
        if not reviewed and not structured.get("_questions_skipped") and structured.get("project_intent") != "personal":
            enqueue(session, "idea.validate", {"session_id": row.id, "revision_id": row.current_revision_id,
                "methods": list(METHODS)}, workspace_id=row.workspace_id, venture_id=row.venture_id,
                dedupe_key=f"idea.validate:{row.id}:{row.current_revision_id}:gstack")


def guard_question_turn(session, row, turn: ClarifierTurn) -> bool:
    from app.idea.service import _loads, _dump
    if turn.kind != "question":
        return False
    options = _loads(turn.options)
    history = answered_questions(session, row.id)
    from app.clarify.dilemmas import ground_question
    from app.gstack.office_hours import saved_snapshot
    snapshot = saved_snapshot(session, row.venture_id, row.current_revision_id)
    original = {**options, "text": turn.text}
    grounded = ground_question(original, snapshot)
    repeated = original if repeated_question(original, history) else grounded
    following = unanswered_question(repeated, history, snapshot=snapshot if options.get("phase") != "gstack" else None)
    if following == repeated:
        if grounded != original:
            turn.text = grounded.pop("text")
            turn.options = _dump(grounded)
            return True
        return False
    if row.questions_asked >= row.question_budget:
        following = None
    if following:
        turn.text = following.pop("text")
        turn.options = _dump(following)
        return True
    queue = list(options.get("queue") or [])
    while queue:
        question = queue.pop(0)
        if repeated_question(question, history):
            continue
        from app.gstack.office_hours import question_turn_options, saved_snapshot, render_question
        snapshot = saved_snapshot(session, row.venture_id, row.current_revision_id)
        question = render_question(question, snapshot)
        if repeated_question(question, history):
            continue
        turn.text = question["text"]
        turn.options = _dump(question_turn_options(question, queue, options.get("index", 1),
            options.get("total", 1), snapshot))
        return True
    confirm_turn(session, row, turn)
    return True


def guard_pending_question(session, row) -> bool:
    if row.state in {"confirmed", "abandoned"}:
        return False
    latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
        .order_by(ClarifierTurn.turn.desc()).limit(1))
    from app.idea.service import _loads
    if not latest or latest.role != "agent" or latest.kind != "question":
        return False
    # Only a question already answered is replaced: a saved question otherwise keeps its words.
    if not repeated_question({**_loads(latest.options), "text": latest.text}, answered_questions(session, row.id)):
        return False
    from app.tenancy import current_or_none
    scope = current_or_none()
    if scope:
        from app.authz.workspace import subject_person_id
        from app.workspaces.service import role_of
        if ((scope.workspace_id, scope.venture_id) != (row.workspace_id, row.venture_id)
                or role_of(row.workspace_id, subject_person_id(scope.actor)) == "viewer"):
            return False
    row = session.get(type(row), row.id, with_for_update=True, populate_existing=True)
    if row.state in {"confirmed", "abandoned"}:
        return False
    latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
        .order_by(ClarifierTurn.turn.desc()).limit(1).execution_options(populate_existing=True))
    return bool(latest and latest.role == "agent" and guard_question_turn(session, row, latest))
