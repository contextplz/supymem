"""Idea sessions, revisions, Case A/B, confirm (docs/supymem/10)."""

from __future__ import annotations

from app.ventures.current_idea import current_idea
from app.ventures.state_brief import invalidate_venture_state_brief

import json
import logging
import os
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.fsm import advance as fsm_transition
from app.core.jobs import ACTIVE_JOB_STATUSES, enqueue
from app.core.local_mode import is_local
from app.db.models import Job, Venture, VentureDocument, Workspace
from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaRevision, IdeaSession, ValidationRun
from app.idea.agent import founder_description, is_options_request, is_unsure_answer, safe_recommendation
from app.intake.normalizer import question_budget, score_complexity, classify_deterministic
from app.state import db
from core.clock import utcnow
from core.copy import founder_copy
from core.ids import new_id

OPENING = (
    "Tell me what you want to build, or tell me you're not sure yet and we'll figure it out together."
)
SAVED_IDEA_OPENING = (
    "Your idea is saved. I am reading what you shared to prepare your next question or a summary for you to review. "
    "You can add anything important while you wait."
)
CHIPS = ["I know what I want", "Help me explore"]
log = logging.getLogger("app.idea.service")
REQUIRED = ("problem", "customer", "offer", "first_user_path", "pricing_hypothesis", "workflow", "desired_outcome")


def _sync() -> bool:
    from app.worker.handlers import hosted_mode
    if hosted_mode():
        return False  # Hosted execution must survive the desktop/API request.
    return (os.getenv("IDEA_SYNC") or os.getenv("ONBOARDING_SYNC") or "0").lower() in {
        "1",
        "true",
        "yes",
    }


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _dump(data: Any) -> str:
    return json.dumps(data, sort_keys=True)


def _project_intent(session: Session, workspace_id: str, venture_id: str | None = None) -> str:
    if venture_id:
        document = session.get(VentureDocument, f"intake_decision:{venture_id}")
        if document and document.venture_id == venture_id:
            return "personal" if _loads(document.body_md).get("path") == "personal_build" else "startup"
    workspace = session.get(Workspace, workspace_id)
    settings = _loads(workspace.settings_json) if workspace else {}
    if venture_id and settings.get("intake_venture_id") and settings["intake_venture_id"] != venture_id:
        prior = session.scalar(select(IdeaRevision.structured).where(IdeaRevision.venture_id == venture_id)
            .order_by(IdeaRevision.created_at.desc(), IdeaRevision.id.desc()).limit(1))
        return "personal" if _loads(prior).get("project_intent") == "personal" else "startup"
    return "personal" if settings.get("project_intent") == "personal" else "startup"


def _chat_entry_deferred(session: Session, workspace_id: str, venture_id: str | None = None) -> bool:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return False
    settings = _loads(workspace.settings_json)
    if venture_id is None:
        return bool(settings.get("chat_entry_defer_clarification"))
    document = session.get(VentureDocument, f"intake_decision:{venture_id}")
    if document and document.venture_id == venture_id:
        return _loads(document.body_md).get("path") in {"startup", "personal_build", "repo_build"}
    return _chat_entry_goal(settings, venture_id)


def _chat_entry_goal(settings: dict, venture_id: str) -> bool:
    return (settings.get("chat_entry_defer_clarification") is True
            and settings.get("intake_venture_id") in {None, venture_id}
            and settings.get("entry_goal") in {"startup", "personal_build"})


def chat_entry_goal(session: Session, workspace_id: str, venture_id: str) -> bool:
    """The intake path can stay "clarify" (an unreadable website) after the founder's answers route this venture to a startup in chat."""
    workspace = session.get(Workspace, workspace_id)
    return bool(workspace and _chat_entry_goal(_loads(workspace.settings_json), venture_id))


def _session(factory, sid: str) -> IdeaSession:
    with factory() as session:
        row = session.get(IdeaSession, sid)
        if row is None:
            raise KeyError(sid)
        return row


def get_session(session_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as db_session:
        row = db_session.get(IdeaSession, session_id)
        if row is None:
            raise KeyError(session_id)
        from app.idea.question_history import guard_pending_question
        if guard_pending_question(db_session, row):
            db_session.commit()
            invalidate_venture_state_brief(row.venture_id)
        turns = list(
            db_session.scalars(
                select(ClarifierTurn)
                .where(ClarifierTurn.session_id == session_id)
                .order_by(ClarifierTurn.turn)
            )
        )
        validations = list(
            db_session.scalars(
                select(ValidationRun).where(ValidationRun.session_id == session_id)
                .order_by(ValidationRun.created_at, ValidationRun.id.desc())
            )
        )
        rev = None
        if row.current_revision_id:
            rev = db_session.get(IdeaRevision, row.current_revision_id)
        from app.gstack.office_hours import answers_for, saved_snapshot
        snapshot = saved_snapshot(db_session, row.venture_id, row.current_revision_id)
        data = serialize_session(row, turns, validations, rev, snapshot)
        if data["current_revision"]:
            data["current_revision"]["structured"].update({field: value for field, value in snapshot.get("_answered_fields", {}).items()
                                                        if field in {"offer", "customer"}})
        data["gstack_answers"] = answers_for(db_session, row.id)
        data["project_intent"] = _project_intent(db_session, row.workspace_id, row.venture_id)
        venture = db_session.get(Venture, row.venture_id)
        if venture and venture.workspace_id == row.workspace_id:
            from app.ventures.startup_review import enabled
            if enabled(db_session, venture.id) and data["project_intent"] != "personal":
                data["next_href"] = f"/v/{venture.id}/context"
            from app.ventures.current_idea import idea_summary
            data["current_idea"] = data["idea_summary"] = idea_summary(venture, session=db_session)
        last_answer = next((turn for turn in reversed(turns) if turn.role == "founder"), None)
        invalid_failures = _clarification_invalid_failures(db_session, row, last_answer)
        data["clarification_invalid_output_seen"] = invalid_failures > 0
        data["clarification_retries_remaining"] = max(0, 2 - invalid_failures) if invalid_failures else 1
        manual_available, retry_available = _clarification_recovery(db_session, row, rev, invalid_failures)
        data["manual_review_available"] = manual_available
        data["clarification_retry_available"] = retry_available
        from app.credits.recovery import eligible_job
        failed = db_session.scalar(select(Job).where(Job.venture_id == row.venture_id,
            Job.workspace_id == row.workspace_id, Job.kind.in_(("idea.clarify_turn", "research.deep")))
            .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        data["credits_continue_job_id"] = failed.id if failed and _loads(failed.payload_json).get("session_id") == row.id and eligible_job(db_session, failed) else None
        data["credits_continue_kind"] = failed.kind if data["credits_continue_job_id"] else None
        return data


def _clarification_invalid_failures(session: Session, row: IdeaSession, last_answer: ClarifierTurn | None) -> int:
    if last_answer is None:
        return 0
    failures = session.scalars(select(IdeaClarificationRun.id).where(
        IdeaClarificationRun.session_id == row.id,
        IdeaClarificationRun.error_code == "clarification_invalid_output",
        IdeaClarificationRun.created_at >= last_answer.created_at,
    )).all()
    return len(failures)


def _clarification_recovery(session: Session, row: IdeaSession, rev: IdeaRevision | None,
                            invalid_failures: int) -> tuple[bool, bool]:
    if (row.state in {"confirmed", "abandoned"} or rev is None
            or _loads(rev.structured).get("_manual_recovery")):
        return False, False
    latest = session.execute(select(Job, IdeaClarificationRun).join(IdeaClarificationRun,
        IdeaClarificationRun.job_id == Job.id).where(
        IdeaClarificationRun.session_id == row.id,
    ).order_by(IdeaClarificationRun.created_at.desc()).limit(1)).first()
    if latest is None or latest[0].status not in {"failed", "failed_dead", "poison"}:
        return False, False
    job, run = latest
    # The normal runner records this definitive failure before provider setup
    # or credit admission. Never infer zero usage from an unknown/late outcome.
    offline = (run.state == "failed" and run.error_code == job.last_error == "clarification_provider_offline"
        and (run.revision_id, run.workspace_id, run.venture_id) == (rev.id, row.workspace_id, row.venture_id)
        and run.response is None and run.usage == "{}")
    if invalid_failures == 0 and not offline:
        return False, False
    if offline:
        from app.idea.runner import unresolved_clarification
        if unresolved_clarification(session, row):
            return False, False
    active_jobs = session.scalars(select(Job).where(Job.venture_id == row.venture_id,
        Job.kind.in_(("idea.clarify_turn", "idea.validate")),
        Job.status.in_(("scheduled", "retry_wait", "leased", "pending", "running")))).all()
    if any(_loads(job.payload_json).get("session_id") == row.id for job in active_jobs):
        return False, False
    return True, job.last_error == "clarification_invalid_output" and invalid_failures < 2


def serialize_session(
    row: IdeaSession,
    turns: list[ClarifierTurn],
    validations: list[ValidationRun],
    rev: IdeaRevision | None,
    snapshot: dict | None = None,
) -> dict[str, Any]:
    opening = SAVED_IDEA_OPENING if ((snapshot or {}).get("idea") or rev and rev.summary or any(
        turn.role == "founder" and (turn.text or "").strip() for turn in turns)) else OPENING
    return {
        "id": row.id,
        "venture_id": row.venture_id,
        "workspace_id": row.workspace_id,
        "state": row.state,
        "case": row.case,
        "question_budget": row.question_budget,
        "questions_asked": row.questions_asked,
        "recon_summary": _loads(row.recon_summary),
        "current_revision": serialize_revision(rev) if rev else None,
        "turns": [{**serialize_turn(t, snapshot if snapshot is not None else _loads(rev.structured if rev else None)),
                   **({"text": opening} if t.role == "agent" and t.kind == "opening" else {})} for t in turns],
        "validation_runs": [serialize_validation(v, _loads(rev.structured) if rev and v.revision_id == rev.id else None) for v in validations],
        "opening": opening,
        "chips": CHIPS,
        "clarifier_mode": "ai_clarifier",
        "limitations": ["Exploration options are hypotheses; external research and demand validation are separate steps."],
    }


def _founder_fields(structured: dict[str, Any]) -> dict[str, Any]:
    """Idea fields without the labels that join answers for the model; older revisions are cleaned on read."""
    return {**structured, **{key: founder_description(structured[key]) for key in (*REQUIRED, "summary")
                             if isinstance(structured.get(key), str)}}


def serialize_revision(rev: IdeaRevision) -> dict[str, Any]:
    summary = founder_description(rev.summary or "")
    return {
        "id": rev.id,
        "rev": rev.rev,
        "source": rev.source,
        "summary": founder_copy(summary) if rev.source == "agent" else summary,
        "structured": _founder_fields(_loads(rev.structured)),
        "diff_from_prev": _loads(rev.diff_from_prev),
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


def serialize_turn(t: ClarifierTurn, snapshot: dict | None = None) -> dict[str, Any]:
    options = json.loads(t.options) if t.options else None
    text = t.text
    if t.role == "agent" and t.kind == "question":
        if isinstance(options, dict) and options.get("phase") == "gstack":
            from app.gstack.office_hours import render_question
            rendered = render_question({**options, "text": text}, snapshot)
            text = rendered.pop("text")
            options = rendered
        elif not (text or "").strip():
            text = fallback_first_question("startup")
        if options is None:
            options = {}
    if t.role == "agent" and t.kind == "question" and isinstance(options, dict):
        from app.ventures.founder_facts import filter_options, contradicts
        facts = (snapshot or {}).get("founder_facts", {})
        if not options.get("known_check"):
            options["options"] = filter_options(options.get("options") or [], facts)
        if contradicts(options.get("recommendation_reason") or "", facts):
            options.update(recommended_option="", recommendation_reason="")
        from app.idea.agent import fitting_options, has_evidence_options, is_evidence_question
        if options.get("phase") != "gstack" and not options.get("contextual_evidence") and (is_evidence_question(text, options.get("options") or []) or has_evidence_options(options.get("options") or []) or is_evidence_question("", options.get("options") or [])):
            options["options"] = fitting_options(text, options.get("options") or [])
        if len(options.get("options") or []) < 2:
            from app.idea.runner import _question_reply
            _question_reply(text, options, t.id, facts=facts, context=snapshot)
        if options.get("phase") == "gstack" or options.get("contextual_evidence"):
            from app.gstack.office_hours import _recommendation
            options["recommended_option"], options["recommendation_reason"] = _recommendation({**options, "text": text}, snapshot)
        else:
            options["recommended_option"], options["recommendation_reason"] = safe_recommendation(
                text, options.get("options") or [], options.get("recommended_option") or "",
                options.get("recommendation_reason") or "", facts=facts)
    return {
        "id": t.id,
        "turn": t.turn,
        "role": t.role,
        "kind": t.kind,
        "text": text,
        "options": options,
        "chosen": t.chosen,
        "revision_id": t.revision_id,
        "llm_usage": _loads(t.llm_usage) if t.llm_usage else None,
    }


def serialize_validation(v: ValidationRun, context: dict | None = None) -> dict[str, Any]:
    from app.idea.validation_agent import _plain_strings, review_presentation
    report = _loads(v.report)
    return {
        "id": v.id,
        "method": v.method,
        "revision_id": v.revision_id,
        "state": v.state,
        "verdict": v.verdict,
        "report": {**_plain_strings(report), "presentation": review_presentation(report, context)},
        "founder_decision": v.founder_decision,
        "founder_note": v.founder_note,
    }


def open_session(
    *,
    venture: Venture,
    case: str = "know",
    actor_subject: str | None = None,
    prefill_answers: dict[str, str] | None = None,
    prefill_facts: dict | None = None,
    website_answers: dict[str, str] | None = None,
    intake_idea: str | None = None,
    research_only: bool = False,
    intake_text: str | None = None,
    onboarding: dict | None = None,
) -> dict[str, Any]:
    from app.intake.smart import condense_input
    factory = db()
    now = utcnow()
    with factory() as session:
        existing = session.scalar(
            select(IdeaSession).where(
                IdeaSession.venture_id == venture.id,
                IdeaSession.state != "abandoned",
            ).order_by(IdeaSession.created_at.desc())
        )
        if existing is not None:
            # Ventures created before the guided handoff may already have an
            # empty opening session. Backfill the durable raw idea exactly once
            # so they receive the same first clarification question as new
            # founders rather than being sent back to the generic greeting.
            submitted = condense_input((intake_text if intake_text is not None else venture.raw_idea or "").strip())
            has_submitted_idea = session.scalar(
                select(ClarifierTurn.id)
                .where(
                    ClarifierTurn.session_id == existing.id,
                    ClarifierTurn.role == "founder",
                    ClarifierTurn.text == submitted,
                )
                .limit(1)
            )
            sid = existing.id
            # A prior chip (for example, “I know what I want”) is not the
            # founder's actual idea. Backfill the raw idea once, while a later
            # page refresh sees the matching durable turn and is a no-op.
            current_revision = session.get(IdeaRevision, existing.current_revision_id) if existing.current_revision_id else None
            is_direction_correction = bool(_loads(current_revision.structured).get("_corrects_revision_id")) if current_revision else False
            should_submit = bool(submitted) and has_submitted_idea is None and not is_direction_correction and existing.state not in {"confirmed", "abandoned"}
            # One narrowly-scoped migration recovery: a session that has its
            # saved idea but no generated question may have been handled by a
            # worker image that predated the credit-account bootstrap.  Queue
            # exactly one new attempt after a known infrastructure failure.
            # Do not retry model/provider outcomes; those remain explicit,
            # non-duplicating founder-visible failures.
            if not should_submit and submitted and not is_direction_correction:
                last_turn = session.scalar(
                    select(ClarifierTurn)
                    .where(ClarifierTurn.session_id == existing.id)
                    .order_by(ClarifierTurn.turn.desc())
                    .limit(1)
                )
                prior = session.scalar(
                    select(Job)
                    .where(
                        Job.venture_id == venture.id,
                        Job.kind == "idea.clarify_turn",
                        Job.status.in_(("failed", "failed_dead", "poison")),
                    )
                    .order_by(Job.created_at.desc())
                    .limit(1)
                )
                recoverable = {"clarification_credits_unavailable", "clarification_credit_reservation_unavailable"}
                has_question = session.scalar(
                    select(ClarifierTurn.id)
                    .where(ClarifierTurn.session_id == existing.id, ClarifierTurn.role == "agent", ClarifierTurn.kind == "question")
                    .limit(1)
                )
                recovery_text = (
                    "Recovered initial clarification after platform credit-account "
                    f"initialization for failed job {prior.id}."
                    if prior
                    else ""
                )
                has_recovery = session.scalar(
                    select(ClarifierTurn.id)
                    .where(
                        ClarifierTurn.session_id == existing.id,
                        ClarifierTurn.role == "system",
                        ClarifierTurn.kind == "infrastructure_recovery",
                        ClarifierTurn.text == recovery_text,
                    )
                    .limit(1)
                )
                if prior and prior.last_error in recoverable and last_turn and last_turn.text == submitted and not has_question and not has_recovery:
                    session.add(ClarifierTurn(
                        id=new_id("ict"), session_id=existing.id, workspace_id=existing.workspace_id,
                        venture_id=existing.venture_id, turn=_next_turn_number(session, existing.id),
                        role="system", kind="infrastructure_recovery",
                        text=recovery_text, created_at=now,
                    ))
                    should_submit = True
        else:
            classification = classify_deterministic(current_idea(venture, session=session) or "")
            complexity = score_complexity(current_idea(venture, session=session) or "", classification)
            budget, _reason = question_budget(
                complexity.score, complexity.uncertainty, str(classification.get("regulatory_surface") or "none")
            )
            row = IdeaSession(
                id=new_id("ids"),
                workspace_id=venture.workspace_id or "",
                venture_id=venture.id,
                case=case if case in {"know", "explore"} else "know",
                state="open",
                question_budget=max(5, int(budget)),
                questions_asked=0,
                recon_summary="{}",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            should_submit = bool((current_idea(venture, session=session) or "").strip())
            session.add(
                ClarifierTurn(
                    id=new_id("ict"),
                    session_id=row.id,
                    workspace_id=row.workspace_id,
                    venture_id=row.venture_id,
                    turn=1,
                    role="agent",
                    kind="opening",
                    text=SAVED_IDEA_OPENING if should_submit else OPENING,
                    options=_dump(CHIPS),
                    created_at=now,
                )
            )
            sid = row.id
        session.commit()
    # A founder who already supplied an idea should never be dropped back into
    # the generic greeting. Store that exact text as the first answer and queue
    # the ordinary clarification workflow, preserving the same audit trail as
    # an answer typed in the canvas.
    submitted = condense_input((intake_text if intake_text is not None else venture.raw_idea or "").strip())
    intake_answers = {}
    if intake_text is not None:
        import re

        from app.idea.agent import answer_metadata, core_answer_status
        from app.idea.runner import _known_fields
        from app.ventures.founder_facts import literal_facts

        parts = re.split(r"\n(Audience|Clarification|Context):\s*", submitted, flags=re.IGNORECASE)
        for label, answer in zip(parts[1::2], parts[2::2]):
            answer = answer_metadata(answer)["founder_text"]
            if label.casefold() == "clarification" and core_answer_status(answer, 0, field="offer", assessed=True) == (True, False):
                intake_answers.update(offer=answer[:2000], workflow=answer[:2000])
        intake_answers = {key: value for key, value in _known_fields({
            "original_idea": submitted, "answered_fields": intake_answers,
            "conversation": [{"role": "founder", "text": submitted}],
            "founder_facts": literal_facts(submitted),
        }).items() if key in REQUIRED}
        prefill_answers = {**intake_answers, **(prefill_answers or {})}
    if prefill_answers or website_answers or intake_idea or research_only or onboarding:
        from app.idea.agent import Understanding

        correction = None
        with factory() as session:
            row = session.get(IdeaSession, sid)
            prior = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
            signature = hashlib.sha256(_dump({"answers": prefill_answers or {}, **({"facts": prefill_facts} if prefill_facts else {}), "website": website_answers or {}, "idea": intake_idea or "",
                "research_only": research_only, "direction": submitted, **({"onboarding": onboarding} if onboarding else {}),
                "project_intent": _project_intent(session, row.workspace_id, row.venture_id)}).encode()).hexdigest()
            if row.state == "confirmed" and _loads(prior.structured if prior else None).get("_intake_prefill") != signature:
                correction = (row.id, row.confirmed_revision_id)
        if correction:
            corrected = start_direction_correction(correction[0], "know", venture_id=venture.id, revision_id=correction[1])
            sid = corrected["id"]
            should_submit = bool(submitted)
        with factory() as session:
            row = session.get(IdeaSession, sid, with_for_update=True)
            prior = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
            structured = _loads(prior.structured if prior else None)
            signature = hashlib.sha256(_dump({"answers": prefill_answers or {}, **({"facts": prefill_facts} if prefill_facts else {}), "website": website_answers or {}, "idea": intake_idea or "",
                "research_only": research_only, "direction": submitted, **({"onboarding": onboarding} if onboarding else {}),
                "project_intent": _project_intent(session, row.workspace_id, row.venture_id)}).encode()).hexdigest()
            if structured.get("_intake_prefill") != signature:
                candidate = {k: v for k, v in structured.items() if k in Understanding.model_fields}
                for key, value in (prefill_answers or {}).items():
                    if key in REQUIRED and isinstance(value, str) and value.strip():
                        candidate[key] = value
                if intake_idea:
                    candidate["summary"] = intake_idea[:2000]
                if onboarding:
                    candidate["assumptions"] = list(dict.fromkeys(candidate.get("assumptions", []) + onboarding.get("assumptions", [])))[:15]
                    for field in ("offer", "customer"):
                        if any(assumption.startswith(f"Assumed: {field} is unclarified;") for assumption in onboarding.get("assumptions", [])):
                            candidate[field] = ""
                    structured["_onboarding_completed"] = onboarding.get("completed", [])
                if prefill_facts or structured.get("_prefill_facts"):
                    structured["_prefill_facts"] = {key: fact for key, fact in
                        {**structured.get("_prefill_facts", {}), **(prefill_facts or {})}.items()
                        if key in REQUIRED and candidate.get(key) == fact.get("value")}
                structured.update(Understanding.model_validate(candidate).model_dump())
                structured["_answered_fields"] = {key: candidate[key] for key, value in intake_answers.items()
                    if candidate.get(key, "").casefold() == value.casefold()}
                structured.update(_intake_prefill=signature, project_intent=_project_intent(session, row.workspace_id, row.venture_id))
                structured["_website_answers"] = {key: {"answer": value, "source": "From your website", "status": "answered"}
                    for key, value in (website_answers or {}).items() if value.strip()}
                if website_answers:
                    completed = set(structured.get("_onboarding_completed", [])) | set(structured["_website_answers"])
                    structured["_onboarding_completed"] = [key for key in ("offer", "customer") if key in completed]
                _append_revision(session, row, structured, source="website" if website_answers else "founder",
                                 summary="From your website" if website_answers else "Read from your original input")
            if research_only and row.state in {"open", "recon", "clarifying", "proposing"}:
                # The entry button explicitly approves research on this saved
                # brief. It does not manufacture a validation verdict.
                research_states = {"open": "recon", "recon": "clarifying", "clarifying": "proposing", "proposing": "reviewing"}
                while row.state in research_states:
                    fsm_transition(session, row, research_states[row.state])
            if research_only:
                should_submit = False
            session.commit()
    queued_job_id: str | None = None
    initialization_error: str | None = None
    if should_submit and submitted:
        try:
            with factory() as session:
                defer_initial_job = _chat_entry_deferred(session, venture.workspace_id or "", venture.id)
            queued = post_message(sid, submitted, actor_subject=actor_subject,
                                  initial_idea=True, defer_job=defer_initial_job)
            queued_job_id = str(queued.get("job_id") or "") or None
        except Exception:
            # The session and founder input are already durable. A temporary
            # worker/queue problem must not erase the submitted idea or return
            # the user to an empty greeting. It must still be diagnosable.
            log.exception("could not queue initial clarification for session %s", sid)
            initialization_error = "initial_clarification_unavailable"
    result = get_session(sid)
    # Return the job created by this request so the client cannot miss it by
    # querying the job list before the transaction above commits. These fields
    # are response metadata rather than persisted idea state.
    result["initial_job_id"] = queued_job_id
    result["initialization_error"] = initialization_error
    return result


def set_case(session_id: str, case: str, *, venture_id: str | None = None) -> dict[str, Any]:
    if case not in {"know", "explore"}:
        raise ValueError("case must be know or explore")
    factory = db()
    with factory() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or (venture_id is not None and row.venture_id != venture_id):
            raise KeyError(session_id)
        if row.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        prev = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
        structured = _loads(prev.structured if prev else None)
        row.case = case
        structured["_explicit_case"] = case
        _append_revision(session, row, structured, source="founder")
        row.updated_at = utcnow()
        session.commit()
    return get_session(session_id)


def start_direction_correction(session_id: str, case: str, *, venture_id: str, revision_id: str) -> dict[str, Any]:
    """Fork a confirmed idea into a new review, retaining its original audit trail."""
    if case not in {"know", "explore"}:
        raise ValueError("case must be know or explore")
    with db()() as session:
        original = session.get(IdeaSession, session_id, with_for_update=True)
        if original is None or original.venture_id != venture_id:
            raise KeyError(session_id)
        if original.state != "confirmed" or original.confirmed_revision_id != revision_id:
            raise RuntimeError("idea_review_conflict")
        # Serialize competing corrections for this venture. A pending correction
        # must be continued, rather than silently replaced by another fork.
        session.get(Venture, venture_id, with_for_update=True)
        active = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id,
            IdeaSession.state.notin_(("confirmed", "abandoned")),
        ).order_by(IdeaSession.created_at.desc()).limit(1))
        if active is not None:
            raise RuntimeError("direction_correction_in_progress")
        current = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.state == "confirmed",
        ).order_by(IdeaSession.confirmed_at.desc()).limit(1))
        if current is None or current.id != original.id:
            raise RuntimeError("idea_review_conflict")
        prior = session.get(IdeaRevision, revision_id)
        if prior is None or prior.session_id != original.id:
            raise RuntimeError("idea_review_conflict")
        now = utcnow()
        row = IdeaSession(id=new_id("ids"), workspace_id=original.workspace_id,
            venture_id=venture_id, case=case, state="clarifying",
            question_budget=original.question_budget, questions_asked=0,
            recon_summary="{}", created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        structured = _loads(prior.structured)
        structured.pop("_manual_recovery", None)
        structured.pop("_questions_skipped", None)
        website = structured.pop("_website_answers", {})
        for key in website:
            if key in REQUIRED:
                structured[key] = ""
        if website:
            structured["summary"] = ""
        structured.pop("_onboarding_completed", None)
        structured.pop("_answered_fields", None)
        structured.pop("_prefill_facts", None)
        structured.pop("_intake_prefill", None)
        structured["_explicit_case"] = case
        structured["_corrects_revision_id"] = prior.id
        _append_revision(session, row, structured, source="founder",
            summary="Direction correction from confirmed revision")
        personal = (_project_intent(session, original.workspace_id, original.venture_id) == "personal"
                    and structured.get("project_intent") == "personal")
        question = ("What would you change about this personal project? Describe the task, workflow, or smallest useful result you want."
                    if personal else
                    "What would you change about this product direction? Describe the new problem, customer, or offer in your own words.")
        session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id,
            workspace_id=row.workspace_id, venture_id=venture_id, turn=1,
            role="agent", kind="question",
            text=question,
            created_at=now))
        session.commit()
        new_id_value = row.id
    return get_session(new_id_value)


def reopen_assumed_questions(session_id: str, *, venture_id: str, revision_id: str) -> dict[str, Any]:
    """Ask only assumed questions, retaining the previously confirmed idea in history."""
    from app.gstack import office_hours
    with db()() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or row.venture_id != venture_id:
            raise KeyError(session_id)
        venture = session.get(Venture, venture_id, with_for_update=True)
        if venture is None or venture.archived_at is not None or venture.stage in {"archived", "purged"}:
            raise KeyError(venture_id)
        if row.current_revision_id != revision_id or row.state == "abandoned":
            raise RuntimeError("idea_review_conflict")
        latest = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned",
        ).order_by(IdeaSession.created_at.desc()).limit(1))
        if latest.id != row.id:
            current = session.get(IdeaRevision, latest.current_revision_id)
            if _loads(current.structured if current else None).get("_gstack_reopened_from") == revision_id:
                session.commit()
                return get_session(latest.id)  # an overlapping reopen reuses the saved review
            raise RuntimeError("idea_review_conflict")
        if office_hours.pending(session, row):
            session.commit()
            return get_session(row.id)
        active = session.scalars(select(Job).where(Job.venture_id == venture_id,
            Job.kind.in_(["idea.validate", "idea.clarify_turn"]), Job.status.in_(ACTIVE_JOB_STATUSES))).all()
        if any(_loads(job.payload_json).get("session_id") == row.id for job in active):
            raise RuntimeError("validation_pending")
        assumed = {answer["id"] for answer in office_hours.answers_for(session, row.id) if answer["assumed"]}
        turns = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
            .order_by(ClarifierTurn.turn)).all()
        questions = {}
        for turn in turns:
            opts = _loads(turn.options)
            if turn.role == "agent" and turn.kind == "question" and opts.get("phase") == office_hours.PHASE:
                for question in [{**opts, "text": turn.text}, *(opts.get("queue") or [])]:
                    questions[question["gstack_id"]] = question
        todo = [questions[gid] for gid in office_hours.QUESTION_IDS if gid in assumed and gid in questions]
        if not todo:
            raise ValueError("no assumed questions")
        prior = session.get(IdeaRevision, revision_id)
        structured = _loads(prior.structured)
        structured.update(_gstack_reopened_from=revision_id, _corrects_revision_id=revision_id)
        structured.pop("_questions_skipped", None)
        now = utcnow()
        if row.state == "confirmed":
            row = IdeaSession(id=new_id("ids"), workspace_id=row.workspace_id, venture_id=venture_id,
                case=row.case, state="clarifying", question_budget=row.question_budget,
                questions_asked=row.questions_asked, recon_summary=row.recon_summary, created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            id_map = {turn.id: new_id("ict") for turn in turns}
            corrections = structured.get("_answer_corrections")
            if isinstance(corrections, dict):
                structured["_answer_corrections"] = {id_map.get(key, key): value for key, value in corrections.items()}
            for turn in turns:
                session.add(ClarifierTurn(id=id_map[turn.id], session_id=row.id,
                    workspace_id=row.workspace_id, venture_id=venture_id, turn=turn.turn,
                    role=turn.role, kind=turn.kind, text=turn.text,
                    options=_dump({**_loads(turn.options), "question_id": id_map.get(_loads(turn.options)["question_id"], _loads(turn.options)["question_id"])})
                        if _loads(turn.options).get("question_id") else turn.options,
                    chosen=turn.chosen, created_at=now))
        elif row.state in {"reviewing", "proposing", "validating"}:
            fsm_transition(session, row, "clarifying")
        _append_revision(session, row, structured, source="founder", summary="Answering previously assumed questions")
        fsm_transition(session, row, "validating")
        office_hours.add_question_turn(session, row, todo[0], todo[1:], 1, len(todo), now)
        session.commit()
        reopened_id = row.id
    return get_session(reopened_id)


def start_confirmed_answer_correction(session_id: str, *, venture_id: str, revision_id: str,
                                      question_id: str, text: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Fork a confirmed review and revise one saved answer without a provider call."""
    with db()() as session:
        original = session.get(IdeaSession, session_id, with_for_update=True)
        if original is None or original.venture_id != venture_id:
            raise KeyError(session_id)
        if original.state != "confirmed" or original.confirmed_revision_id != revision_id:
            raise RuntimeError("idea_review_conflict")
        session.get(Venture, venture_id, with_for_update=True)
        active = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id,
            IdeaSession.state.notin_(("confirmed", "abandoned")),
        ).order_by(IdeaSession.created_at.desc()).limit(1))
        if active is not None:
            raise RuntimeError("direction_correction_in_progress")
        current = session.scalar(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.state == "confirmed",
        ).order_by(IdeaSession.confirmed_at.desc()).limit(1))
        if current is None or current.id != original.id:
            raise RuntimeError("idea_review_conflict")
        prior = session.get(IdeaRevision, revision_id)
        if prior is None or prior.session_id != original.id:
            raise RuntimeError("idea_review_conflict")
        question = session.get(ClarifierTurn, question_id)
        if question is None or question.session_id != original.id:
            raise ValueError("unknown saved question")
        field = _loads(question.options).get("field")
        from app.idea.agent import Understanding
        if field not in {"problem", "customer", "offer", "first_user_path", "pricing_hypothesis",
                         "workflow", "current_workaround", "why_now", "evidence", "desired_outcome", "requirements", "constraints"}:
            raise ValueError("question cannot be revised directly")
        if set(patch) != {field}:
            raise ValueError("question field mismatch")
        _verified_answer_correction(session, original, question_id, field)
        if not text.strip() or len(text) > 4000:
            raise ValueError("invalid answer correction")
        structured = _loads(prior.structured)
        structured.pop("_manual_recovery", None)
        structured.pop("_questions_skipped", None)
        candidate = {key: value for key, value in structured.items() if key in Understanding.model_fields}
        candidate.update(patch)
        validated = Understanding.model_validate(candidate).model_dump()
        structured.update(validated)
        # A corrected answer replaces the saved one, which would otherwise win again in _append_revision.
        structured.setdefault("_answered_fields", {}).update(patch)
        structured["_corrects_revision_id"] = prior.id
        now = utcnow()
        personal = (_project_intent(session, original.workspace_id, original.venture_id) == "personal"
                    and structured.get("project_intent") == "personal")
        row = IdeaSession(id=new_id("ids"), workspace_id=original.workspace_id,
            venture_id=venture_id, case=original.case, state="reviewing" if original.case == "know" or personal else "clarifying",
            question_budget=original.question_budget, questions_asked=original.questions_asked,
            recon_summary=original.recon_summary, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        old_turns = session.scalars(select(ClarifierTurn).where(
            ClarifierTurn.session_id == original.id).order_by(ClarifierTurn.turn)).all()
        id_map = {turn.id: new_id("ict") for turn in old_turns}
        for turn in old_turns:
            session.add(ClarifierTurn(id=id_map[turn.id], session_id=row.id,
                workspace_id=row.workspace_id, venture_id=venture_id, turn=turn.turn,
                role=turn.role, kind=turn.kind, text=turn.text,
                options=_dump({**_loads(turn.options), "question_id": id_map.get(_loads(turn.options)["question_id"], _loads(turn.options)["question_id"])})
                    if _loads(turn.options).get("question_id") else turn.options,
                chosen=turn.chosen, created_at=now))
        corrections = structured.get("_answer_corrections")
        if isinstance(corrections, dict):
            structured["_answer_corrections"] = {id_map.get(key, key): value for key, value in corrections.items()}
        _record_answer_correction(structured, id_map[question_id], text)
        _append_revision(session, row, structured, source="founder", summary="Saved answer correction")
        session.commit()
        new_id_value = row.id
    return get_session(new_id_value)


def _append_revision(
    session: Session,
    row: IdeaSession,
    structured: dict[str, Any],
    *,
    source: str,
    summary: str = "",
    refresh_page: bool = True,
) -> IdeaRevision:
    from app.tenancy import current_or_none
    actor = current_or_none()
    prev = None
    if row.current_revision_id:
        prev = session.get(IdeaRevision, row.current_revision_id)
    n = (prev.rev + 1) if prev else 1
    old = _founder_fields(_loads(prev.structured)) if prev else {}
    for field in (*REQUIRED, "current_workaround", "why_now", "evidence", "requirements", "constraints"):
        answer = (structured.get("_answered_fields") or {}).get(field)
        if isinstance(answer, str) and answer.strip():
            structured[field] = [answer] if field in {"requirements", "constraints"} else answer
        elif field in {"requirements", "constraints"} and isinstance(answer, list) and answer:
            structured[field] = answer
    structured, summary = _founder_fields(structured), founder_description(summary)
    # Answer bookkeeping is internal; it never makes a no-op edit look like a change.
    diff = {k: {"from": old.get(k), "to": structured.get(k)} for k in set(old) | set(structured)
            if old.get(k) != structured.get(k) and k not in {"_answered_fields", "_prefill_facts"}}
    rev = IdeaRevision(
        id=new_id("idv"),
        session_id=row.id,
        workspace_id=row.workspace_id,
        venture_id=row.venture_id,
        rev=n,
        source=source,
        summary=summary or str(structured.get("summary") or structured.get("offer") or "")[:200],
        structured=_dump(structured),
        diff_from_prev=_dump(diff),
        created_by=actor.actor if actor else None,
        created_at=utcnow(),
    )
    session.add(rev)
    session.flush()
    row.current_revision_id = rev.id
    row.updated_at = utcnow()
    if refresh_page:
        from app.ventures import idea_page
        idea_page.schedule(session, row.workspace_id, row.venture_id)
    return rev


def repair_saved_audiences() -> None:
    if not is_local():
        return
    import re

    from app.db.session import serialize_writes
    from app.idea.audience import _BUYER_CLAUSE, safe_audience, stated_buyer
    from app.idea.question_history import answered_questions
    from app.intake.smart import _founder_data, _prefill
    from app.ventures.current_idea import current_revision, revision_fields

    with db()() as session:
        serialize_writes(session, key="idea:repair-audience")
        for venture in session.scalars(select(Venture).where(Venture.archived_at.is_(None))).all():
            source = _founder_data(venture.raw_idea or "")
            buyer = stated_buyer(source)
            if not buyer or _prefill(source, explicit_only=True).get("customer"):
                continue
            revision = current_revision(venture, session)
            fields = revision_fields(revision)
            customer, offer = fields.get("customer"), fields.get("offer")
            if not isinstance(customer, str) or not customer.strip() or safe_audience(customer) or not isinstance(offer, str):
                continue
            # shortcut: require a literal product span and a stated buyer; leave paraphrased or ambiguous history untouched.
            product = re.escape(" ".join(customer.split()))
            description = _BUYER_CLAUSE.split(offer, maxsplit=1)[0]
            if not re.search(rf"(?<!\w){product}(?!\w)", " ".join(description.split()), re.I):
                continue
            idea = session.get(IdeaSession, revision.session_id)
            if idea.state != "confirmed" or idea.confirmed_revision_id != revision.id:
                continue
            answers = fields.get("_answered_fields") or {}
            if not isinstance(answers, dict) or answers.get("customer", customer) != customer:
                continue
            if any(answer["field"] in {"customer", "audience"} and not answer.get("reused")
                   for answer in answered_questions(session, idea.id)):
                continue
            if session.scalar(select(Job.id).where(Job.venture_id == venture.id,
                    Job.workspace_id == venture.workspace_id, Job.status.in_(ACTIVE_JOB_STATUSES)).limit(1)):
                continue
            fields["customer"] = buyer
            if "customer" in answers:
                answers["customer"] = buyer
            if isinstance(facts := fields.get("_prefill_facts"), dict) and isinstance(fact := facts.get("customer"), dict) and fact.get("value") == customer:
                facts.pop("customer")
            repaired = _append_revision(session, idea, fields, source="audience_repair", summary=revision.summary, refresh_page=False)
            idea.confirmed_revision_id = repaired.id
        session.commit()


def _next_turn_number(session: Session, session_id: str) -> int:
    session.flush()
    last = session.scalar(
        select(ClarifierTurn.turn)
        .where(ClarifierTurn.session_id == session_id)
        .order_by(ClarifierTurn.turn.desc())
        .limit(1)
    )
    return int(last or 0) + 1


def _verified_answer_correction(session: Session, row: IdeaSession, question_id: str, field: str) -> None:
    question = session.get(ClarifierTurn, question_id)
    if (question is None or question.session_id != row.id or question.venture_id != row.venture_id
            or question.kind != "question" or question.role != "agent"):
        raise ValueError("unknown saved question")
    options = _loads(question.options)
    if options.get("field") != field:
        raise ValueError("question field mismatch")
    reply = session.scalar(select(ClarifierTurn.id).where(
        ClarifierTurn.session_id == row.id,
        ClarifierTurn.turn == question.turn + 1,
        ClarifierTurn.role == "founder",
    ))
    if reply is None:
        raise ValueError("question has no saved answer")


def _record_answer_correction(structured: dict[str, Any], question_id: str, text: str) -> None:
    if not text.strip() or len(text) > 4000:
        raise ValueError("invalid answer correction")
    corrections = structured.get("_answer_corrections")
    if not isinstance(corrections, dict):
        corrections = {}
    corrections[question_id] = text.strip()
    structured["_answer_corrections"] = corrections
    from app.idea.agent import answer_metadata
    metadata = answer_metadata(text.strip())
    structured.setdefault("_answer_provenance", {})[question_id] = metadata
    if metadata["provenance"] == "suggested_choice":
        structured["assumptions"] = list(dict.fromkeys([
            *(structured.get("assumptions") or []), metadata["assumption_note"] + ": " + metadata["suggested_text"]]))


# Pasted briefs: same cap as the /new composer (entry.chat_start.IDEA_MAX_CHARS).
INITIAL_IDEA_MAX_CHARS = 200000


def post_message(session_id: str, text: str, **kwargs) -> dict[str, Any]:
    result = _post_message(session_id, text, **kwargs)
    if result.get("suggestion_turn_id") or is_unsure_answer(kwargs.get("chip") or text) or is_options_request(kwargs.get("chip") or text):
        return result
    from app.ventures.founder_facts import schedule
    with db()() as session:
        row = session.get(IdeaSession, session_id)
        venture_id = row.venture_id
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id,
            ClarifierTurn.role == "founder").order_by(ClarifierTurn.turn.desc()).limit(1))
        if not latest or latest.text != (kwargs.get("chip") or text).strip():
            return result
        metadata = _loads(latest.options) if latest else {}
        question = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id,
            ClarifierTurn.role == "agent", ClarifierTurn.kind == "question",
            ClarifierTurn.turn < latest.turn).order_by(ClarifierTurn.turn.desc()).limit(1)) if latest else None
        options = _loads(question.options) if question else {}
    testimony = metadata.get("founder_text", kwargs.get("chip") or text)
    if not testimony.strip():
        return result
    from app.idea.agent import answered_field
    field = answered_field(question.text if question else "", options.get("field"), options.get("options") or [])
    schedule(venture_id, testimony, kwargs.get("actor_subject"),
        source="gstack" if options.get("phase") == "gstack" else "clarification",
        field=field)
    return result


def _skip_questions(session: Session, row: IdeaSession, now) -> dict[str, Any]:
    from app.gstack.office_hours import SKIP, saved_snapshot
    from app.idea.agent import answer_metadata
    revision = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
    structured = _loads(revision.structured if revision else None)
    if structured.get("_questions_skipped") and row.state == "reviewing":
        return {"job_id": None, "skipped_questions": True}
    answers = saved_snapshot(session, row.venture_id, row.current_revision_id).get("_answered_fields", {})
    structured.setdefault("_answered_fields", {}).update({field: value for field, value in answers.items()
                                                        if field in {"offer", "customer"}})
    from app.idea.runner import _known_fields
    from app.idea.agent import Understanding
    for field in Understanding.model_fields:
        if isinstance(structured.get(field), str):
            structured[field] = answer_metadata(structured[field])["founder_text"]
    structured["_answered_fields"] = {field: value for field, value in structured.get("_answered_fields", {}).items()
        if not isinstance(value, str) or not value.strip() or answer_metadata(value)["founder_text"]}
    venture = session.get(Venture, row.venture_id)
    known = _known_fields({"original_idea": venture.raw_idea, "understanding": structured,
        "answered_fields": structured.get("_answered_fields", {}),
        "question_fields_asked": structured.get("_onboarding_completed", []),
        "conversation": [{"role": turn.role, "kind": turn.kind, "text": turn.text,
                          **(_loads(turn.options) if turn.role == "founder" else {})}
                         for turn in session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
                             .order_by(ClarifierTurn.turn))]})
    for field, value in known.items():
        if field in Understanding.model_fields and not structured.get(field):
            structured[field] = [value] if field in {"requirements", "constraints"} and isinstance(value, str) else value
    session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
        venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="founder", kind="answer",
        text=SKIP, options=_dump({**answer_metadata(SKIP), "provenance": "not_answered"}),
        revision_id=row.current_revision_id, created_at=now))
    session.flush()
    structured["_questions_skipped"] = True
    Understanding.model_validate({key: value for key, value in structured.items() if key in Understanding.model_fields})
    revision = _append_revision(session, row, structured, source="founder", summary=structured.get("summary") or "Your saved idea")
    if row.state == "open":
        fsm_transition(session, row, "recon")
    if row.state == "recon":
        fsm_transition(session, row, "clarifying")
    if row.state == "clarifying":
        fsm_transition(session, row, "proposing")
    if row.state in {"proposing", "validating"}:
        fsm_transition(session, row, "reviewing")
    session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
        venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent", kind="proposal",
        text="You've saved your answers, and the questions you skipped will stay blank. Review your idea when you're ready.",
        revision_id=revision.id, created_at=now))
    session.flush()
    return {"job_id": None, "skipped_questions": True}


def _post_message(
    session_id: str,
    text: str,
    *,
    chip: str | None = None,
    actor_subject: str | None = None,
    correction_question_id: str | None = None,
    initial_idea: bool = False,
    defer_job: bool = False,
    reply_in_chat: bool = False,
    expected_question_id: str | None = None,
) -> dict[str, Any]:
    body = (chip or text or "").strip()
    if not body:
        raise ValueError("empty message")
    factory = db()
    now = utcnow()
    with factory() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=f"idea-message:{session_id}")
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None:
            raise KeyError(session_id)
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(text or "") or looks_like_secret(chip or ""):
            raise ValueError("Your answer includes a credential. Open Settings > Keys and connections, save it in your provider's key field, then send your answer without the credential.")
        latest = session.scalar(select(ClarifierTurn).where(
            ClarifierTurn.session_id == row.id,
        ).order_by(ClarifierTurn.turn.desc()).limit(1))
        from app.gstack.office_hours import skips_all_questions
        skip_all = not initial_idea and skips_all_questions(body)
        force = body.lower() in {"just propose", "propose now"}
        if expected_question_id:
            if (latest is None or latest.id != expected_question_id or
                    latest.role != "agent" or latest.kind not in ({"question", "proposal"} if skip_all else {"question"})):
                raise RuntimeError("idea_question_no_longer_pending")
        if initial_idea:
            # Two overlapping page loads can both pass open_session's earlier
            # raw-idea check. Recheck under the session lock before queuing a
            # second clarification or charging the provider twice.
            prior = session.scalar(select(ClarifierTurn).where(
                ClarifierTurn.session_id == row.id,
                ClarifierTurn.role == "founder",
                ClarifierTurn.text == body,
            ).order_by(ClarifierTurn.turn).limit(1))
            if prior is not None:
                session.commit()
                return {"turn": prior.turn, "job_id": None, "session": get_session(row.id)}
        if row.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        cap = INITIAL_IDEA_MAX_CHARS if initial_idea else 12000
        if len(body) > cap:
            raise ValueError(f"message exceeds {cap} characters")
        if correction_question_id is not None:
            _verified_answer_correction(session, row, correction_question_id, "proposed_options")
        if skip_all:
            result = _skip_questions(session, row, now)
            session.commit()
            return {"turn": None, **result, "session": get_session(row.id)}
        if not initial_idea and latest and latest.role == "agent" and latest.kind == "question" and is_options_request(body):
            from app.gstack.office_hours import saved_snapshot
            options = serialize_turn(latest, saved_snapshot(session, row.venture_id, row.current_revision_id))["options"]
            if options.get("recommended_option") not in options["options"]:
                options.update(recommended_option="", recommendation_reason="")
            options["note"] = ("Here are a few possibilities. Pick any that fit or type your own answer." if options["options"] else
                               "I do not have enough context to offer useful choices yet. Type your own answer.")
            options.pop("retry", None)
            options.pop("text", None)
            latest.options = _dump(options)
            session.commit()
            return {"turn": None, "job_id": None,
                    "gstack_turn_id" if options.get("phase") == "gstack" else "suggestion_turn_id": latest.id,
                    "session": get_session(row.id)}
        from app.idea.agent import answer_metadata, is_evidence_question
        question_options = {}
        if latest and latest.role == "agent" and latest.kind == "question":
            from app.gstack.office_hours import saved_snapshot
            question_options = serialize_turn(latest, saved_snapshot(session, row.venture_id, row.current_revision_id))["options"]
        from app.idea.agent import name_like_reply
        if not initial_idea and name_like_reply(body, question_options.get("field"), question_options.get("options") or []):
            candidate = body.strip(" \t\r\n.!?\"'“”‘’")
            return {"turn": None, "job_id": None, "suggestion_turn_id": latest.id,
                    "note": f'Did you mean "{candidate}" as your venture name? Say "Rename this venture to {candidate}", or describe your answer to the question below.',
                    "session": get_session(row.id)}
        from app.gstack.office_hours import confirms_summary, record_answer
        if row.state == "validating" and question_options.get("phase") == "gstack" and confirms_summary(body) and not (question_options.get("known_check") or question_options.get("prefill")):
            result = record_answer(session, row, latest, question_options, body, now)
            session.commit()
            return {"turn": None, **result, "session": get_session(row.id)}
        uncertain = bool(question_options) and is_unsure_answer(body)
        if uncertain and expected_question_id and question_options.get("phase") != "gstack":
            body = f"Assumed: {question_options.get('field') or 'This point'} is still open; you did not have an answer yet."
        metadata = answer_metadata(body, question_options.get("detail_source_options") or question_options.get("options") or [])
        original = {}
        if latest and latest.role == "agent" and latest.kind == "question":
            original = _loads(latest.options)
            selected = answer_metadata(body, original.get("detail_source_options") or original.get("options") or [])
            if selected["provenance"] == "suggested_choice":
                metadata = selected
        if metadata["provenance"] == "suggested_choice":
            for choices in (original.get("detail_source_options") or original.get("options") or [], question_options.get("detail_source_options") or question_options.get("options") or []):
                metadata["founder_text"] = answer_metadata(metadata["founder_text"], choices)["founder_text"].strip()
            from app.clarify.dilemmas import detail_choices
            required = detail_choices(original.get("options") or [], original.get("requires_detail") or []) + detail_choices(question_options.get("options") or [], question_options.get("requires_detail") or [])
            selected_detail = any(option in metadata["suggested_text"] for option in required)
            if selected_detail and metadata["founder_text"]:
                body, chip = metadata["founder_text"], None
                metadata = answer_metadata(body)
            elif not (chip or expected_question_id or selected_detail or body.startswith("Assumed: ")):
                metadata = answer_metadata(body)
        from app.intake.evidence import save_statement
        from app.ventures import known
        from app.secrets.detect import looks_like_secret
        check = original.get("known_check") or {}
        known_field = original.get("known_field")
        if known.enabled() and check.get("status") in {"confirm", "conflict"} and not uncertain:
            if confirms_summary(body) and check["status"] == "confirm":
                body = check["value"]
            values = check.get("values") or [{"value": check.get("value")}]
            if body in [item["value"] for item in values] or metadata["founder_text"]:
                metadata = answer_metadata(body)
                if known_field in known.founder_facts.FIELDS and len(body) <= 2000 and not looks_like_secret(body):
                    known.founder_facts.save(session, session.get(Venture, row.venture_id),
                        {known_field: known.founder_facts._fact(body, "review", 1)})
        if metadata["founder_text"] and not uncertain:
            save_statement(row.venture_id, metadata["founder_text"], session=session)
        from app.ventures.founder_facts import capture
        from app.idea.agent import answered_field
        field = answered_field(latest.text if latest else "", question_options.get("field"), question_options.get("options") or [])
        if field:
            metadata["field"] = field
        if latest and latest.role == "agent" and latest.kind == "question":
            metadata["question_id"] = correction_question_id or latest.id
        if question_options.get("phase") != "gstack" and metadata["founder_text"] and not uncertain:
            capture(session, session.get(Venture, row.venture_id), metadata["founder_text"], source="clarification", field=field)
        if row.state == "validating" and correction_question_id is None and not (is_local() and force and not initial_idea):
            # A pending GStack forcing question is answered here, not by the clarifier.
            from app.gstack import office_hours
            pending = office_hours.pending(session, row)
            if pending is not None:
                if initial_idea:
                    raise RuntimeError("idea_question_no_longer_pending")
                sent = office_hours.record_answer(session, row, *pending, body, now, metadata=metadata)
                session.commit()
                return {"turn": None, **sent, "session": get_session(row.id)}
        from app.idea.runner import unresolved_clarification
        if unresolved_clarification(session, row):
            raise RuntimeError("clarification_outcome_uncertain")
        if row.state == "open":
            fsm_transition(session, row, "recon")
        if row.state in {"proposing", "reviewing"}:
            fsm_transition(session, row, "clarifying")
        n = _next_turn_number(session, session_id)
        founder_turn = ClarifierTurn(
                id=new_id("ict"),
                session_id=row.id,
                workspace_id=row.workspace_id,
                venture_id=row.venture_id,
                turn=n,
                role="founder",
                kind="chip" if chip else "answer_correction" if correction_question_id else "answer",
                text=body,
                chosen=chip,
                options=_dump(metadata),
                created_at=now,
        )
        session.add(founder_turn)
        structured = {}
        if row.current_revision_id:
            prev = session.get(IdeaRevision, row.current_revision_id)
            structured = _loads(prev.structured if prev else "{}")
        structured.pop("_manual_recovery", None)
        structured.pop("_questions_skipped", None)
        structured.setdefault("project_intent", _project_intent(session, row.workspace_id, row.venture_id))
        if field and question_options.get("phase") != "gstack" and not uncertain and metadata["founder_text"]:
            from app.idea.agent import core_answer_status
            if field not in {"offer", "customer"} or core_answer_status(metadata["founder_text"], 0, field=field) == (True, False):
                structured.setdefault("_answered_fields", {})[field] = metadata["founder_text"]
        if not uncertain and metadata["founder_text"]:
            from app.idea.agent import answer_prefill
            from app.intake.smart import _prefill
            explicit = _prefill(metadata["founder_text"], explicit_only=True)
            if field not in {"customer", "audience"}:
                explicit["customer"] = answer_prefill(metadata["founder_text"], field).get("customer", "")
            structured.setdefault("_answered_fields", {}).update({key: value for key, value in explicit.items()
                if key in {"offer", "customer"} and value and not looks_like_secret(value)})
        if (field in {"offer", "customer"} and not uncertain and not metadata["founder_text"] and metadata["suggested_text"]
                and not looks_like_secret(metadata["suggested_text"])
                and not is_evidence_question(latest.text if latest else "", question_options.get("options") or [])):
            structured.setdefault("_answered_fields", {})[field] = metadata["suggested_text"]
            structured[field] = metadata["suggested_text"]
        from app.intake.website import changing_idea
        if changing_idea(body):
            structured.pop("_answered_fields", None)
            structured.pop("_prefill_facts", None)
            for key in structured.pop("_website_answers", {}):
                if key in REQUIRED:
                    structured[key] = ""
            structured["summary"] = ""
            structured.pop("_intake_prefill", None)
            structured.pop("_onboarding_completed", None)
        if body in CHIPS:
            row.case = "explore" if body == CHIPS[1] else "know"
            structured["_explicit_case"] = row.case
        if correction_question_id is not None:
            _record_answer_correction(structured, correction_question_id, body)
        if metadata["provenance"] == "suggested_choice":
            structured["assumptions"] = list(dict.fromkeys([
                *(structured.get("assumptions") or []), metadata["assumption_note"] + ": " + metadata["suggested_text"]]))
        # Save input exactly. The model interprets it; a keyword matcher must not
        # silently invent customer, pricing, workflow or acquisition requirements.
        from app.chat.idea_proposal import EDIT_QUESTION

        force = force or (
            latest is not None and latest.role == "agent" and latest.kind == "question"
            and latest.text == EDIT_QUESTION)
        rev = _append_revision(session, row, structured, source="founder", summary=body[:200])
        founder_turn.revision_id = rev.id
        job_kind = "idea.clarify_turn"
        payload = {
            "session_id": row.id,
            "venture_id": row.venture_id,
            "workspace_id": row.workspace_id,
            "text": body,
            "force_propose": force,
            "revision_id": rev.id,
            "reply_in_chat": reply_in_chat,
        }
        if actor_subject:
            payload["_actor_subject"] = actor_subject
        if initial_idea:
            # The first question is a real model turn; if it fails for good the
            # job handler posts the canned fallback_first_question instead.
            payload["first_question_fallback"] = True if defer_job else "invalid_output"
        job_id = enqueue(
            session,
            job_kind,
            payload,
            workspace_id=row.workspace_id,
            venture_id=row.venture_id,
            dedupe_key=f"{job_kind}:{row.id}:{n}",
        )
        session.commit()
        sid = row.id
        vid = row.venture_id
    invalidate_venture_state_brief(vid)
    return {"turn": n, "job_id": job_id, "session": get_session(sid)}


def fallback_first_question(intent: str) -> str:
    return ("What should the first version do for you?" if intent == "personal"
            else "Who has this problem, and what do they do today?")


def run_clarify_turn(payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError
    from app.idea.runner import run
    try:
        run(payload)
    except FatalError as exc:
        if str(exc) in {"clarification_outcome_uncertain", "clarification_operation_in_progress"}:
            raise
        # Only the very first question degrades to the old canned one; the
        # founder must never be left with no question after saving an idea.
        fallback = payload.get("first_question_fallback")
        if not fallback or fallback == "invalid_output" and str(exc) != "clarification_invalid_output":  # credit and offline failures keep their own recovery
            raise
        from app.idea.runner import post_fallback_question
        if not post_fallback_question(payload):
            raise


def retry_failed_clarification(
    session_id: str,
    *,
    venture_id: str,
    actor_subject: str | None = None,
    failed_job_id: str | None = None,
    allow_uncertain: bool = False,
) -> dict[str, Any]:
    """Continue credit-blocked work or create a new attempt from saved inputs.

    Credit admission failures reuse their saved job and unspent receipt.
    Prior usage and reservations remain unchanged. Only explicit founder entry
    points may pass allow_uncertain to authorize a new attempt; they never revive
    the uncertain provider call or run alongside live clarification work.
    """
    factory = db()
    with factory() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key="idea-retry:" + session_id)
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or row.venture_id != venture_id:
            raise KeyError(session_id)
        from app.credits.recovery import continue_job
        credit_job = session.get(Job, failed_job_id) if failed_job_id else session.scalar(select(Job).where(
            Job.venture_id == venture_id, Job.workspace_id == row.workspace_id, Job.kind == "idea.clarify_turn")
            .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        if credit_job and credit_job.kind == "idea.clarify_turn" and (
                _loads(credit_job.payload_json).get("session_id") == row.id) and (
                str(credit_job.last_error or "").partition(":")[0] == "clarification_insufficient_credits"
                or (_loads(credit_job.payload_json).get("_credits_continued")
                    and credit_job.status in (*ACTIVE_JOB_STATUSES, "succeeded", "done"))):
            job_id = continue_job(session, credit_job, venture_id=venture_id, actor_subject=actor_subject)
            session.commit()
            return {"job_id": job_id, "session": get_session(session_id)}
        if row.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        from app.idea.runner import unresolved_clarification
        uncertain = session.scalars(select(IdeaClarificationRun).where(
            IdeaClarificationRun.session_id == row.id,
            IdeaClarificationRun.workspace_id == row.workspace_id,
            IdeaClarificationRun.venture_id == row.venture_id,
            IdeaClarificationRun.state.in_(("prepared", "calling", "uncertain")),
        )).all()
        if unresolved_clarification(session, row) and (not allow_uncertain or any(run.state != "uncertain" for run in uncertain)):
            raise RuntimeError("clarification_outcome_uncertain")
        active = session.scalar(
            select(Job.id)
            .where(
                Job.venture_id == row.venture_id,
                Job.kind == "idea.clarify_turn",
                Job.status.in_(("scheduled", "retry_wait", "leased", "pending", "running")),
            )
            .limit(1)
        )
        if active:
            raise RuntimeError("clarification_already_running")
        candidates = session.scalars(
            select(Job)
            .where(
                Job.venture_id == row.venture_id,
                Job.kind == "idea.clarify_turn",
                Job.status.in_(("failed", "failed_dead", "poison")),
            )
            .order_by(Job.created_at.desc())
            .limit(20)
        ).all()
        prior = None
        prior_payload: dict[str, Any] = {}
        for candidate in candidates:
            try:
                payload = json.loads(candidate.payload_json)
            except (TypeError, ValueError):
                continue
            if payload.get("session_id") == row.id:
                prior, prior_payload = candidate, payload
                break
        if prior is None or (failed_job_id and prior.id != failed_job_id) or prior.last_error not in (
                {"clarification_invalid_output", "clarification_outcome_uncertain", "job_outcome_uncertain"} if allow_uncertain
                else {"clarification_invalid_output"}):
            raise RuntimeError("clarification_retry_unavailable")
        last_answer = session.scalar(select(ClarifierTurn).where(
            ClarifierTurn.session_id == row.id, ClarifierTurn.role == "founder",
        ).order_by(ClarifierTurn.turn.desc()).limit(1))
        if prior.last_error == "clarification_invalid_output" and _clarification_invalid_failures(session, row, last_answer) >= 2:
            raise RuntimeError("clarification_retry_exhausted")
        if prior_payload.get("revision_id") != row.current_revision_id:
            # A founder may have changed only the known/explore toggle after
            # the rejected response. That explicit choice is safe to carry into
            # a new attempt; edits to actual product facts still require a new
            # answer rather than silently replaying stale inputs.
            old_revision = session.get(IdeaRevision, prior_payload.get("revision_id"))
            new_revision = session.get(IdeaRevision, row.current_revision_id)
            old_values = _loads(old_revision.structured if old_revision else None)
            new_values = _loads(new_revision.structured if new_revision else None)
            old_values.pop("_explicit_case", None)
            new_case = new_values.pop("_explicit_case", None)
            if old_revision is None or new_revision is None or old_values != new_values or new_case != row.case:
                raise RuntimeError("clarification_inputs_changed")
        payload = {
            "session_id": row.id,
            "venture_id": row.venture_id,
            "workspace_id": row.workspace_id,
            "text": str(prior_payload.get("text") or "")[:12000],
            "force_propose": bool(prior_payload.get("force_propose")),
            "revision_id": row.current_revision_id,
        }
        if actor_subject:
            payload["_actor_subject"] = actor_subject
        job_id = enqueue(
            session,
            "idea.clarify_turn",
            payload,
            workspace_id=row.workspace_id,
            venture_id=row.venture_id,
            dedupe_key=f"idea.clarify.retry:{row.id}:{row.current_revision_id}:{prior.id}",
        )
        if allow_uncertain:
            for run in uncertain:
                snapshot = _loads(run.snapshot)
                snapshot["_founder_retry_job_id"] = job_id
                run.snapshot = json.dumps(snapshot)
        session.commit()
    return {"job_id": job_id, "session": get_session(session_id)}


def review_clarification_manually(session_id: str, patch: dict[str, Any], *, venture_id: str,
                                  workspace_id: str, revision_id: str) -> dict[str, Any]:
    """Let the founder supply a reviewable idea after rejected model output."""
    from app.idea.agent import Understanding
    if not isinstance(patch, dict) or not set(patch).issubset({"problem", "customer", "offer"}):
        raise ValueError("invalid manual idea fields")
    with db()() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or (row.venture_id, row.workspace_id) != (venture_id, workspace_id):
            raise KeyError(session_id)
        if row.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        if not revision_id or revision_id != row.current_revision_id:
            raise RuntimeError("idea_review_conflict")
        last_answer = session.scalar(select(ClarifierTurn).where(
            ClarifierTurn.session_id == row.id, ClarifierTurn.role == "founder",
        ).order_by(ClarifierTurn.turn.desc()).limit(1))
        previous = session.get(IdeaRevision, row.current_revision_id)
        if not _clarification_recovery(session, row, previous,
                                       _clarification_invalid_failures(session, row, last_answer))[0]:
            raise RuntimeError("manual_review_unavailable")
        structured = _loads(previous.structured if previous else None)
        candidate = {key: value for key, value in structured.items() if key in Understanding.model_fields}
        candidate.update(patch)
        candidate = Understanding.model_validate(candidate).model_dump()
        if not all(isinstance(candidate[field], str) and candidate[field].strip() for field in ("problem", "customer", "offer")):
            raise ValueError("problem, customer, and offer are required for manual review")
        candidate["summary"] = candidate["problem"].strip()[:200]
        structured.update(candidate)
        structured.setdefault("_answered_fields", {}).update(patch)
        structured["_manual_recovery"] = True
        if row.state == "open":
            fsm_transition(session, row, "recon")
        if row.state in {"recon", "validating", "reviewing"}:
            fsm_transition(session, row, "clarifying")
        if row.state == "clarifying":
            fsm_transition(session, row, "proposing")
        if row.state == "proposing":
            fsm_transition(session, row, "reviewing")
        _append_revision(session, row, structured, source="founder", summary=candidate["summary"])
        session.commit()
    return get_session(session_id)


def patch_locked(session: Session, row: IdeaSession, patch: dict[str, Any], *, answer_correction: dict[str, Any] | None = None) -> IdeaRevision:
    if row.state in {"confirmed", "abandoned"}:
        raise RuntimeError("session_closed")
    from app.db.session import serialize_writes
    serialize_writes(session, key=f"startup_review:{row.venture_id}")
    prev = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
    structured = _loads(prev.structured if prev else "{}")
    from app.idea.agent import Understanding
    allowed = set(Understanding.model_fields)
    if set(patch) - allowed:
        raise ValueError("unknown idea field")
    if answer_correction is not None:
        if set(answer_correction) != {"question_id", "text"} or not all(isinstance(value, str) for value in answer_correction.values()):
            raise ValueError("invalid answer correction")
        if len(patch) != 1:
            raise ValueError("answer correction needs one idea field")
        _verified_answer_correction(session, row, answer_correction["question_id"], next(iter(patch)))
        _record_answer_correction(structured, answer_correction["question_id"], answer_correction["text"])
    candidate = {k: v for k, v in structured.items() if k in allowed}
    candidate.update(patch)
    validated = Understanding.model_validate(candidate).model_dump()
    from app.idea.agent import answer_metadata, core_answer_status
    from app.intake.smart import _prefill
    typed_fields = {}
    for field in patch:
        if field in (*REQUIRED, "current_workaround", "why_now", "evidence") and isinstance(validated[field], str):
            metadata = answer_metadata(answer_correction["text"] if answer_correction else validated[field])
            testimony = metadata["founder_text"]
            if field == "customer" and metadata["provenance"] == "suggested_choice":
                testimony = _prefill("Audience: " + testimony).get("customer", "")
            typed_fields[field] = testimony
        elif field in {"requirements", "constraints"}:
            typed_fields[field] = validated[field]
    from app.ventures.founder_facts import FIELDS, ALIASES, literal_facts, save
    from app.ventures.startup_review import enabled
    if enabled(session, row.venture_id):
        updates = {}
        for field in patch:
            if ALIASES.get(field, field) in FIELDS and isinstance(validated[field], str):
                testimony = typed_fields.get(field, validated[field])
                if not testimony.strip() or field == "customer" and core_answer_status(testimony, 0, field="customer") != (True, False):
                    updates[ALIASES.get(field, field)] = None
                else:
                    updates.update(literal_facts(testimony, source="clarification", field=field))
        if updates:
            save(session, session.get(Venture, row.venture_id), updates)
    structured.update(validated)
    if typed_fields:
        structured.setdefault("_answered_fields", {}).update(typed_fields)
        for field in typed_fields:
            structured.get("_prefill_facts", {}).pop(field, None)
    if structured.get("_website_answers"):
        structured["_website_answers"] = {key: value for key, value in structured["_website_answers"].items() if key not in patch}
    # An explicitly edited proposal remains the founder's reviewable proposal.
    # Do not require another paid model rewrite to approve a manual correction.
    # The new revision ID invalidates prior validation and confirmation tokens.
    if row.state in {"proposing", "reviewing", "validating"}:
        fsm_transition(session, row, "clarifying")
        personal = (_project_intent(session, row.workspace_id, row.venture_id) == "personal"
                    and structured.get("project_intent") == "personal")
        if row.case == "know" or personal:
            # A founder's explicit edit is reviewable without another model
            # rewrite. Personal projects also avoid a stale market review.
            fsm_transition(session, row, "proposing")
            fsm_transition(session, row, "reviewing")
    rev = _append_revision(session, row, structured, source="founder")
    return rev


def patch_revision(session_id: str, patch: dict[str, Any], *, venture_id: str | None = None, workspace_id: str | None = None,
                   answer_correction: dict[str, Any] | None = None) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or (venture_id is not None and row.venture_id != venture_id) or (workspace_id is not None and row.workspace_id != workspace_id):
            raise KeyError(session_id)
        rev = patch_locked(session, row, patch, answer_correction=answer_correction)
        session.commit()
        rid = rev.id
        vid = row.venture_id
    invalidate_venture_state_brief(vid)
    factory = db()
    with factory() as session:
        rev = session.get(IdeaRevision, rid)
        assert rev is not None
        return serialize_revision(rev)


def list_revisions(session_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(IdeaRevision).where(IdeaRevision.session_id == session_id).order_by(IdeaRevision.rev)
            )
        )
        return [serialize_revision(r) for r in rows]


def advance_binding(session: Session, row: IdeaSession) -> dict[str, Any]:
    """Source identity/phase used by review cards and locked mutation guards."""
    from app.ventures.next_step import validation_settled

    settled = row.state == "validating" and validation_settled(session, row)
    return {"idea_session_id": row.id, "idea_revision_id": row.current_revision_id,
            "idea_phase": f"{row.state}:{bool(settled)}"}


def check_advance_binding(session: Session, row: IdeaSession, expected: dict[str, Any] | None) -> None:
    if expected is not None and expected != advance_binding(session, row):
        raise RuntimeError("idea_advance_scope_changed")


def confirm(session_id: str, *, revision_id: str | None = None, name: str | None = None, slug: str | None = None,
            person_id: str | None = None, expected_advance_binding: dict[str, Any] | None = None,
            start_research: bool = True, accept_validation: bool = False, venture_id: str | None = None) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    accepted_runs = []
    with factory() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or (venture_id is not None and row.venture_id != venture_id):
            raise KeyError(session_id)
        check_advance_binding(session, row, expected_advance_binding)
        if row.state == "confirmed":
            if not revision_id or revision_id != row.current_revision_id or revision_id != row.confirmed_revision_id:
                raise RuntimeError("idea_review_conflict")
            saved = get_session(session_id)
            return {"venture_id": row.venture_id, "dossier_job_id": None, "session": saved,
                    **({"next_href": saved["next_href"]} if saved.get("next_href") else {})}
        rev = session.get(IdeaRevision, row.current_revision_id) if row.current_revision_id else None
        structured = _loads(rev.structured if rev else None)
        from app.ventures.journey_overrides import read as journey_choices
        skipped = journey_choices(row.venture_id, session=session)["skipped"]
        personal = (_project_intent(session, row.workspace_id, row.venture_id) == "personal"
                    and structured.get("project_intent") == "personal")
        if row.state not in {"reviewing", "proposing", "validating"} and not (
            row.case == "explore" and row.state == "clarifying" and not personal
        ):
            raise RuntimeError("not_ready_to_confirm")
        if not row.current_revision_id:
            raise RuntimeError("no_revision")
        if revision_id != row.current_revision_id:
            raise RuntimeError("idea_review_conflict")
        pending = session.scalars(select(Job).where(Job.venture_id == row.venture_id,
            Job.kind.in_(["idea.clarify_turn", "idea.validate"]),
            Job.status.in_(ACTIVE_JOB_STATUSES))).all()
        for job in pending:
            payload = _loads(job.payload_json)
            if payload.get("session_id") == row.id and payload.get("revision_id") == revision_id:
                # A legacy personal project may still have an old market
                # validation in flight. Keep its job and usage receipt, but
                # do not make an irrelevant paid review block confirmation.
                # The worker will mark a late result partial for this now
                # confirmed revision instead of applying a verdict.
                if (personal or "validation" in skipped) and job.kind == "idea.validate":
                    continue
                raise RuntimeError("validation_pending" if job.kind == "idea.validate" else "clarification_in_progress")
        from app.idea.validation import saved_summary_for
        summary_ready = not start_research and saved_summary_for(session, row, revision_id) is not None
        if row.state == "validating" and summary_ready:
            fsm_transition(session, row, "reviewing")
        elif row.state == "validating" and personal:
            # Legacy personal sessions may have entered the old paid review.
            # Once that job is no longer active, return to the founder's saved
            # proposal without requiring another market-validation receipt.
            fsm_transition(session, row, "reviewing")
        elif row.state == "validating" and ("validation" in skipped or structured.get("_questions_skipped")):
            fsm_transition(session, row, "reviewing")
        elif row.state == "validating" and row.case == "know":
            from app.idea.validation import METHODS

            completed_methods = set(
                session.scalars(
                    select(ValidationRun.method).where(
                        ValidationRun.session_id == row.id,
                        ValidationRun.revision_id == revision_id,
                        ValidationRun.state == "complete",
                        ValidationRun.method.in_(METHODS),
                    )
                ).all()
            )
            if completed_methods != set(METHODS):
                raise RuntimeError("validation_required")
            fsm_transition(session, row, "reviewing")
        if (row.case == "explore" or accept_validation) and not personal and "validation" not in skipped and not structured.get("_questions_skipped") and not summary_ready:
            from app.idea.validation import METHODS
            findings = session.scalars(select(ValidationRun).where(ValidationRun.session_id == row.id,
                ValidationRun.revision_id == revision_id, ValidationRun.workspace_id == row.workspace_id,
                ValidationRun.venture_id == row.venture_id).order_by(ValidationRun.created_at.desc(), ValidationRun.id)).all()
            latest = {}
            for finding in findings:
                latest.setdefault(finding.method, finding)
            # This internal driver previously called decide(), then confirm()
            # in separate transactions. Bind acceptance to this reviewed phase
            # and revision under the same row lock as confirmation.
            if expected_advance_binding is not None or accept_validation:
                if any(method not in latest or latest[method].state != "complete" for method in METHODS):
                    raise RuntimeError("validation_required")
                for finding in (latest[method] for method in METHODS):
                    if finding.founder_decision != "accept":
                        finding.founder_decision = "accept"
                        accepted_runs.append(finding.id)
                if row.state == "validating":
                    fsm_transition(session, row, "reviewing")
            if any(method not in latest or latest[method].state != "complete" or
                   latest[method].founder_decision != "accept" for method in METHODS):
                raise RuntimeError("validation_required")
        if row.state == "proposing":
            fsm_transition(session, row, "reviewing")
        fsm_transition(session, row, "confirmed")
        row.confirmed_revision_id = row.current_revision_id
        row.confirmed_at = now
        row.confirmed_by_person_id = person_id
        row.updated_at = now
        ven = session.get(Venture, row.venture_id)
        if ven is not None:
            label = (name or structured.get("name") or ven.name or "Untitled").strip()
            ven.name = label
            ven.updated_at = now
            if hasattr(ven, "stage"):
                # Keep legacy onboarding stage; research job is the real next step.
                pass
        # Personal projects move directly to a reviewed local build. Their
        # confirmed idea does not authorize paid market research.
        job_id = None
        from app.ventures.startup_review import enabled, initialize
        new_path = not personal and ven is not None and enabled(session, ven.id)
        if new_path:
            initialize(session, ven)
        elif not personal and start_research and "research" not in skipped:
            job_id = enqueue(
                session,
                "research.deep",
                {
                    "session_id": row.id,
                    "venture_id": row.venture_id,
                    "workspace_id": row.workspace_id,
                    "revision_id": row.confirmed_revision_id,
                },
                workspace_id=row.workspace_id,
                venture_id=row.venture_id,
                dedupe_key=f"research.deep:{row.id}",
                effect_key=f"research.deep:{row.id}",
            )
        session.commit()
        vid = row.venture_id
        ws = row.workspace_id
        conf_rev = row.confirmed_revision_id
    for run_id in accepted_runs:
        __import__("app.experiments.ledger", fromlist=["x"]).on_validation_accepted(run_id)
    try:
        from app.analytics.events import emit

        emit("idea.confirmed", person_id=person_id, workspace_id=ws, venture_id=vid,
             props={"revision_id": conf_rev})
    except Exception:
        pass
    from app.entry.chat_start import enqueue_ready_exploration

    if start_research:
        enqueue_ready_exploration(vid)
    return {"venture_id": vid, "dossier_job_id": job_id, "session": get_session(session_id),
            **({"next_href": f"/v/{vid}/context"} if new_path else {})}


def append_revision(
    session: Session,
    row: IdeaSession,
    structured: dict[str, Any],
    *,
    source: str,
    summary: str = "",
) -> IdeaRevision:
    return _append_revision(session, row, structured, source=source, summary=summary)


def loads(raw: str | None) -> dict[str, Any]:
    return _loads(raw)


def next_turn_number(session: Session, session_id: str) -> int:
    return _next_turn_number(session, session_id)


__all__ = [
    "OPENING",
    "CHIPS",
    "REQUIRED",
    "append_revision",
    "confirm",
    "get_session",
    "list_revisions",
    "loads",
    "next_turn_number",
    "open_session",
    "patch_revision",
    "post_message",
    "run_clarify_turn",
    "set_case",
]
