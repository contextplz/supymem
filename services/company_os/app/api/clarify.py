"""Clarify questions GET + answers POST (resume onboarding)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.local_mode import is_local
from app.db.models import Venture
from app.onboarding.state import active_run
from app.state import MEM, db

router = APIRouter(tags=["clarify"])


class AnswerItem(BaseModel):
    question_id: str
    option_id: str | None = None
    option_ids: list[str] | None = None
    other_text: str | None = None
    slot_key: str | None = None


class ClarificationAnswers(BaseModel):
    answers: list[AnswerItem] = Field(default_factory=list)


def _pause_reason(venture_id: str) -> str:
    run = active_run(venture_id)
    if run is None:
        from app.onboarding.state import resumable_run

        run = resumable_run(venture_id)
    return getattr(run, "paused_reason", "") or ""


def _live_questions(venture_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    factory = db()
    name = ""
    one_liner = ""
    raw_idea = ""
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        name = getattr(ven, "name", "") or ""
        one_liner = getattr(ven, "one_liner", "") or ""
        raw_idea = getattr(ven, "raw_idea", "") or ""
    reason = _pause_reason(venture_id)
    from app.clarify.dilemmas import is_product_questions

    if reason == "awaiting_product_understanding":
        questions = MEM.get(f"product_questions:{venture_id}") or MEM.get(f"questions:{venture_id}")
        from app.ventures import known
        if known.enabled():
            questions = MEM.get(f"questions:{venture_id}") or []
    else:
        questions = MEM.get(f"questions:{venture_id}")
        if is_product_questions(questions):
            questions = MEM.get(f"gstack_questions:{venture_id}") or questions
    brief = MEM.get(f"brief:{venture_id}") or {}
    if not questions:
        return [], brief
    if is_product_questions(questions):
        from app.ventures import known
        if not known.enabled():
            MEM[f"product_questions:{venture_id}"] = questions
        return questions, brief
    from app.clarify.generator import refresh_question_copy

    idea = str(brief.get("normalized") or raw_idea or "")
    questions = refresh_question_copy(
        questions,
        idea=idea,
        name=name,
        one_liner=one_liner,
        research=brief.get("research") if isinstance(brief.get("research"), dict) else None,
    )
    MEM[f"questions:{venture_id}"] = questions
    MEM[f"gstack_questions:{venture_id}"] = questions
    return questions, brief


@router.get("/ventures/{venture_id}/clarify/questions")
def get_questions(venture_id: str):
    questions, brief = _live_questions(venture_id)
    if not questions and not is_local():
        raise HTTPException(404, "run_intake_first")
    reason = _pause_reason(venture_id)
    process = "product_understanding" if reason == "awaiting_product_understanding" else "gstack_office_hours"
    stages = []
    if process == "gstack_office_hours":
        from app.gstack.methodology import FORCING_QUESTIONS

        stages = [{"id": spec["id"], "name": spec["name"]} for spec in FORCING_QUESTIONS.values()]
    return {
        "questions": questions,
        "process": process,
        "stages": stages,
        "question_budget": brief.get("budget"),
        "complexity": brief.get("complexity"),
        "product": brief.get("product"),
    }


@router.get("/ventures/{venture_id}/clarification")
def get_clarification(venture_id: str):
    """Phase 9 shape used by A8."""
    questions, _brief = _live_questions(venture_id)
    if not questions and not is_local():
        raise HTTPException(404, detail={"error": {"code": "no_questions", "message": "Questions are not ready yet."}})
    public = []
    for q in questions:
        public.append(
            {
                "id": q["id"],
                "slot_key": q.get("slot_key"),
                "prompt": q.get("prompt"),
                "why_asked": q.get("why_asked"),
                "options": q.get("options"),
                "allow_other": q.get("allow_other", True),
                "selection_mode": q.get("selection_mode", "single"),
                "exclusive_option_ids": q.get("exclusive_option_ids", []),
                "implication": None,  # per-option
                "gstack_id": q.get("gstack_id"),
                "gstack_name": q.get("gstack_name"),
                "process": q.get("process") or "gstack_office_hours",
            }
        )
    reason = _pause_reason(venture_id)
    process = public[0].get("process") if public else "gstack_office_hours"
    if reason == "awaiting_product_understanding":
        process = "product_understanding"
    stages = []
    if process == "gstack_office_hours":
        from app.gstack.methodology import FORCING_QUESTIONS

        stages = [{"id": spec["id"], "name": spec["name"]} for spec in FORCING_QUESTIONS.values()]
    return {
        "questions": public,
        "items": public,
        "process": process,
        "stages": stages,
        "product": (MEM.get(f"brief:{venture_id}") or {}).get("product"),
    }


def _normalized_answer(item: AnswerItem, q: dict[str, Any]) -> tuple[str, str]:
    """Return the durable ID/text view consumed by downstream planning."""
    options = {str(o["id"]): str(o.get("label") or o["id"]) for o in q.get("options") or []}
    mode = q.get("selection_mode", "single")
    ids = item.option_ids if item.option_ids is not None else ([item.option_id] if item.option_id else [])
    if len(ids) != len(set(ids)) or any(oid not in options for oid in ids):
        raise HTTPException(422, "invalid_clarification_option")
    other = (item.other_text or "").strip()
    if other and (not q.get("allow_other", True) or len(other) < 3):
        raise HTTPException(422, "invalid_clarification_other")
    needs_detail = any(option.get("requires_detail") and option["id"] in ids for option in q.get("options") or [])
    if mode != "multiple" and len(ids) + bool(other and not needs_detail) != 1:
        raise HTTPException(422, "select_one_clarification_option")
    if mode == "multiple":
        if not ids and not other:
            raise HTTPException(422, "select_clarification_option")
        exclusive = set(q.get("exclusive_option_ids") or [])
        if exclusive.intersection(ids) and (len(ids) != 1 or other and not needs_detail):
            raise HTTPException(422, "exclusive_clarification_option")
    if needs_detail:
        from app.idea.agent import answer_metadata
        other = answer_metadata(other, list(options.values()))["founder_text"].strip()
        if other:
            return other, other
    labels = [options[oid] for oid in ids]
    if other:
        labels.append(other)
    if mode == "multiple":
        joined = " · ".join(labels)
        return joined, joined
    return (ids[0] if ids else other), labels[0]


@router.post("/ventures/{venture_id}/clarification/answers", status_code=202)
async def post_clarification_answers(venture_id: str, payload: ClarificationAnswers):
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        raw_idea = getattr(ven, "raw_idea", "") or ""
        name = getattr(ven, "name", "") or ""
        one_liner = getattr(ven, "one_liner", "") or ""

    reason = _pause_reason(venture_id)
    from app.clarify.dilemmas import is_product_questions

    if reason == "awaiting_product_understanding":
        questions = MEM.get(f"product_questions:{venture_id}") or MEM.get(f"questions:{venture_id}") or []
        from app.ventures import known
        if known.enabled():
            questions = MEM.get(f"questions:{venture_id}") or []
    else:
        questions = MEM.get(f"questions:{venture_id}") or []
    by_id = {q["id"]: q for q in questions if q.get("id")}
    by_slot = {q.get("slot_key"): q for q in questions if q.get("slot_key")}
    answers: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    known_updates = {}
    from app.secrets.detect import looks_like_secret
    from app.ventures import known
    if known.enabled() and reason == "awaiting_product_understanding":
        answers = dict(MEM.get(f"product_answers:{venture_id}") or {})
        provenance = dict(answers.get("_answer_provenance") or {})
    seen: set[str] = set()
    for item in payload.answers:
        q = by_id.get(item.question_id) or by_slot.get(item.question_id) or by_slot.get(item.slot_key or "")
        if not q:
            raise HTTPException(422, "unknown_clarification_question")
        qid = q.get("id") or item.question_id
        if qid in seen:
            raise HTTPException(422, "duplicate_clarification_question")
        seen.add(qid)
        slot = q.get("slot_key") or qid
        answers[qid], answers[slot] = _normalized_answer(item, q)
        from app.idea.agent import SUGGESTED_CHOICE_NOTE
        suggested = bool(item.option_ids or item.option_id and item.option_id != "other")
        selected_detail = any(option.get("requires_detail") and option["id"] in (item.option_ids or [item.option_id]) for option in q.get("options") or [])
        founder_text = (item.other_text or "").strip()
        if selected_detail:
            from app.idea.agent import answer_metadata
            founder_text = answer_metadata(founder_text, [option.get("label") or option["id"] for option in q.get("options") or []])["founder_text"].strip()
            if founder_text:
                suggested = False
        provenance[qid] = provenance[slot] = {"provenance": "suggested_choice" if suggested else "founder_stated",
            "founder_text": answers[slot] if selected_detail and founder_text else founder_text,
            **({"assumption_note": SUGGESTED_CHOICE_NOTE} if suggested else {})}
        if known.enabled() and q.get("known_check") and answers[slot] != "I'll edit it":
            value = answers[slot]
            field = known.ALIASES.get(slot, slot)
            if field in known.founder_facts.FIELDS and isinstance(value, str) and len(value) <= 2000 and not looks_like_secret(value):
                known_updates[field] = known.founder_facts._fact(value, "review", 1)
                provenance[qid] = provenance[slot] = {"provenance": "founder_stated", "founder_text": value}
    if len(seen) != len(questions):
        raise HTTPException(422, "incomplete_clarification_answers")
    if known_updates:
        with factory() as session:
            known.founder_facts.save(session, session.get(Venture, venture_id), known_updates)
            session.commit()
    answers["_answer_provenance"] = provenance
    run = active_run(venture_id)
    product_round = reason == "awaiting_product_understanding" or is_product_questions(questions)
    if run and run.status == "paused" and product_round:
        MEM[f"product_answers:{venture_id}"] = answers
        if known.enabled():
            MEM[f"product_answers_source:{venture_id}"] = "founder"
        MEM[f"answers:{venture_id}"] = answers
        MEM[f"answers_source:{venture_id}"] = "founder"
        from app.clarify.dilemmas import brief_from_answers

        brief = brief_from_answers(
            idea=raw_idea or str((MEM.get(f"brief:{venture_id}") or {}).get("normalized") or ""),
            name=name,
            answers=answers,
            questions=questions,
        )
        store = MEM.get(f"brief:{venture_id}") or {}
        store["product"] = brief
        MEM[f"brief:{venture_id}"] = store
        MEM[f"product_brief:{venture_id}"] = brief
        from app.onboarding.gates import set_run_gate

        set_run_gate("product_understanding", venture_id, run.id, "accepted")
        from app.onboarding.spawn import start_onboarding

        resumed = await start_onboarding(
            venture_id,
            mode=run.mode,
            resume=True,
            run_id=run.id,
            entry_mode=getattr(run, "entry_mode", None) or "idea",
            workspace_id=getattr(run, "workspace_id", None),
        )
        return {"status": "resumed", "run_id": resumed.id, "process": "product_understanding", "brief": brief}

    MEM[f"answers:{venture_id}"] = answers
    MEM[f"answers_source:{venture_id}"] = "founder"
    from app.gstack.plan import apply_inferred_answers, infer_from_office_hours

    plan = infer_from_office_hours(
        idea=raw_idea or str((MEM.get(f"brief:{venture_id}") or {}).get("normalized") or ""),
        name=name,
        one_liner=one_liner,
        answers=answers,
        questions=questions,
    )
    product = (MEM.get(f"brief:{venture_id}") or {}).get("product")
    if isinstance(product, dict):
        plan["product_brief"] = product
        plan["product"] = product.get("name") or plan.get("product")
    MEM[f"gstack_plan:{venture_id}"] = plan
    MEM[f"answers:{venture_id}"] = apply_inferred_answers(answers, plan)

    if run and run.status == "paused" and run.paused_reason == "awaiting_clarification":
        from app.onboarding.spawn import start_onboarding

        resumed = await start_onboarding(
            venture_id,
            mode=run.mode,
            resume=True,
            run_id=run.id,
            entry_mode=getattr(run, "entry_mode", None) or "idea",
            workspace_id=getattr(run, "workspace_id", None),
        )
        return {"status": "resumed", "run_id": resumed.id}
    return {"status": "accepted"}
