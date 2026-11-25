"""Real, metered GStack/GBrain assessment of the evolving founder revision.

Validation uses founder statements and scoped stored memory. It does not pretend
that methodology or missing memory establishes market demand. External deep
research remains a separate confirmed workflow.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timezone

from sqlalchemy import select

from app.core.jobs import FatalError, enqueue
from app.db.models import Job
from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession, ResearchDossier, ResearchFinding, ValidationRun
from app.research import execution as ex
from app.research.metered import INTERACTIVE_TIMEOUT_SECONDS as VALIDATION_PROVIDER_TIMEOUT_SECONDS
from app.state import db
from core.clock import utcnow

METHODS = ("gstack_office_hours", "gstack_ceo_review", "gbrain_think")
VALIDATION_MAX_TOKENS = 8192
PRESSURE_TEST_MAX_TOKENS = 4096
_REJECTED_OUTPUT_CODES = {"research_invalid_output", "research_output_truncated"}


def _settled_rejection(document: dict) -> bool:
    operations = document.get("_runtime", {}).get("operations", {})
    return bool(operations) and all(operation.get("state") == "completed"
        and operation.get("error_code") in _REJECTED_OUTPUT_CODES
        and operation.get("usage", {}).get("settled") is True for operation in operations.values())


def _saved_summary(document: dict) -> dict:
    from core.copy import founder_copy
    snapshot = document["_runtime"]["snapshot"]
    lines = ["I could not finish your idea review. Your idea and answers are saved.", "", "What I have saved from our conversation:"]
    for field, label in (("summary", "Your idea"), ("customer", "Who you want to help"),
                         ("problem", "Your problem to solve"), ("offer", "Your first version")):
        if isinstance(snapshot.get(field), str) and snapshot[field].strip():
            lines.append(f"{label}: {snapshot[field]}")
    for answer in document["validation_context"].get("gstack_answers") or []:
        lines.append(("An untested assumption: " if answer.get("assumed") else "You said: ") + answer["answer"])
    lines.extend(["", "This summary is not proof of demand, users or revenue. Your idea review is still incomplete.",
        "Review this summary, then tell me to confirm your idea or what you want to change."])
    return {"body_md": founder_copy("\n\n".join(lines)), "provenance": "founder_provided", "verified": False}


def saved_summary_for(session, idea: IdeaSession, revision_id: str) -> dict | None:
    if idea.current_revision_id != revision_id or idea.state in {"confirmed", "abandoned"}:
        return None
    dossier = session.scalars(select(ResearchDossier).where(
        ResearchDossier.workspace_id == idea.workspace_id, ResearchDossier.venture_id == idea.venture_id,
        ResearchDossier.revision_id == revision_id, ResearchDossier.kind == "validation"
    ).order_by(ResearchDossier.created_at.desc(), ResearchDossier.id)).first()
    if dossier is None or dossier.state != "failed":
        return None
    from app.idea.service import _loads
    document = _loads(dossier.summary)
    runtime = document.get("_runtime", {})
    summary = document.get("saved_summary") or {}
    job = session.get(Job, runtime.get("job_id"))
    if (job is None or job.kind != "idea.validate" or job.status not in {"failed", "failed_dead", "poison"}
            or (job.workspace_id, job.venture_id) != (idea.workspace_id, idea.venture_id)
            or runtime.get("session_id") != idea.id or dossier.id != ex.identifier(job.id)
            or job.last_error not in _REJECTED_OUTPUT_CODES or document.get("error_code") != job.last_error
            or not _settled_rejection(document)):
        return None
    payload = _loads(job.payload_json)
    if (payload.get("session_id"), payload.get("revision_id")) != (idea.id, revision_id):
        return None
    if not summary and document.get("validation_context"):
        summary = document["saved_summary"] = _saved_summary(document)
        dossier.summary = json.dumps(document, ensure_ascii=False)
    if (summary.get("verified") is not False or summary.get("provenance") != "founder_provided"
            or not isinstance(summary.get("body_md"), str) or not summary["body_md"].strip()):
        return None
    return summary


def pressure_test_quote(workspace_id: str, source: dict) -> dict:
    from app.credits.estimate import estimate_llm_micro
    from app.idea.validation_agent import messages_for, response_schema
    from app.llm import founder_provider
    from llm_gateway import sole
    from llm_gateway.provider_foundry import load_provider_config_from_env, resolve_live_model

    resolved = founder_provider.resolve_llm_config(workspace_id)
    byok = resolved.source == "founder_override"
    config = founder_provider.to_provider_config(workspace_id, resolved_config=resolved) if byok else load_provider_config_from_env()
    if config is None:
        raise ValueError("review_model_unavailable")
    messages = messages_for(source, [], ["gstack_ceo_review"], pressure_test=True)
    schema = response_schema(["gstack_ceo_review"], [source["id"]], pressure_test=True)
    model = resolve_live_model(config, tier=sole._resolved_tier("idea.validate", "pro"))
    prompt_tokens = sole.transport_prompt_tokens(messages, purpose="idea.validate", config=config,
        response_schema=schema, clock_text=" " * 256)
    prompt_tokens += len(json.dumps({"response_schema": schema}, ensure_ascii=False, allow_nan=False).encode())
    estimate = estimate_llm_micro(model, prompt_tokens=prompt_tokens, max_tokens=PRESSURE_TEST_MAX_TOKENS)
    return {"provider": config.provider, "model": model, "byok": byok,
            "call_ceiling_micro": estimate, "ceiling_micro": 0 if byok else estimate}


async def pressure_test_review(workspace_id: str, venture_id: str, source: dict, quote: dict, request_id: str):
    from app.credits import metered
    from app.credits.errors import CreditsUnavailable
    from app.idea.validation_agent import messages_for, parse_result, response_schema

    def admit(_account_id, _request_id, estimate_micro):
        if type(estimate_micro) is not int or not 0 < estimate_micro <= quote["call_ceiling_micro"]:
            raise CreditsUnavailable("review_credit_ceiling")

    with metered.bounded_calls(workspace_id, venture_id, admit):
        response = await metered.llm(messages_for(source, [], ["gstack_ceo_review"], pressure_test=True),
            purpose="idea.validate", workspace_id=workspace_id, venture_id=venture_id,
            max_tokens=PRESSURE_TEST_MAX_TOKENS, request_id=request_id,
            response_schema=response_schema(["gstack_ceo_review"], [source["id"]], pressure_test=True))
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) != "stop":
        raise ValueError("review_incomplete")
    return parse_result(choice.message.content, source, [], ["gstack_ceo_review"], pressure_test=True)


def start_validation(session_id: str, *, revision_id: str | None = None, methods: list | None = None,
                     venture_id: str | None = None, workspace_id: str | None = None,
                     expected_advance_binding: dict | None = None) -> dict:
    from app.core.fsm import advance
    from app.idea.service import get_session
    methods = list(METHODS) if methods is None else methods
    if not methods or len(set(methods)) != len(methods) or not set(methods) <= set(METHODS):
        raise ValueError("validation_methods_invalid")
    with db()() as session:
        row = session.get(IdeaSession, session_id, with_for_update=True)
        if row is None or (venture_id is not None and row.venture_id != venture_id) or (workspace_id is not None and row.workspace_id != workspace_id):
            raise KeyError("session")
        from app.idea.service import check_advance_binding
        check_advance_binding(session, row, expected_advance_binding)
        if row.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        rid = revision_id or row.current_revision_id
        if rid != row.current_revision_id or not rid:
            raise RuntimeError("validation_revision_superseded")
        from app.core.jobs import ACTIVE_JOB_STATUSES
        active = session.scalars(select(Job).where(Job.venture_id == row.venture_id,
            Job.kind.in_(["idea.validate", "idea.clarify_turn"]), Job.status.in_(ACTIVE_JOB_STATUSES))).all()
        for job in active:
            payload = json.loads(job.payload_json)
            if payload.get("session_id") != session_id or payload.get("revision_id") != rid:
                continue
            if job.kind == "idea.clarify_turn":
                raise RuntimeError("clarification_in_progress")
            if set(payload.get("methods") or METHODS) != set(methods):
                raise RuntimeError("validation_in_progress")
            return {"job_id": job.id, "session": get_session(session_id)}
        from app.gstack import office_hours
        from app.idea.service import _loads, _project_intent
        rev = session.get(IdeaRevision, rid)
        personal = (_project_intent(session, row.workspace_id, row.venture_id) == "personal"
                    and _loads(rev.structured if rev else None).get("project_intent") == "personal")
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id)
                                .order_by(ClarifierTurn.turn.desc()).limit(1))
        # Validating the clarifier's open startup proposal is the same approval as chat's
        # Looks right: one shared, dedupe-keyed GStack question stage, asked once per idea.
        gstack_first = (set(methods) == set(METHODS) and not personal and latest is not None
                        and latest.kind == "proposal" and latest.revision_id == rid
                        and not office_hours.asked_before(session, row.id)
                        and session.scalar(select(Job.id).where(Job.dedupe_key == f"idea.validate:{row.id}:{rid}")) is None)
        if row.state == "open":
            advance(session, row, "recon")
        if row.state in {"recon", "reviewing", "proposing"}:
            advance(session, row, "clarifying")
        if row.state == "clarifying":
            advance(session, row, "validating")
        if gstack_first:
            jid = enqueue(session, "idea.validate", {"session_id": row.id, "revision_id": rid, "methods": methods,
                "stage": "gstack_questions"}, workspace_id=row.workspace_id, venture_id=row.venture_id,
                dedupe_key=f"idea.validate:{row.id}:{rid}")
        else:
            jid = enqueue(session, "idea.validate", {"session_id": row.id, "revision_id": rid,
                "methods": methods}, workspace_id=row.workspace_id, venture_id=row.venture_id)
        session.commit()
    return {"job_id": jid, "session": get_session(session_id)}


def _context(did: str, payload: dict) -> dict:
    from app.idea.validation_agent import MAX_MEMORY_SOURCES, MAX_MEMORY_TEXT
    row = ex.read(did)
    document = json.loads(row.summary)
    if document.get("validation_context"):
        return document["validation_context"]
    memory, gaps = [], []
    snapshot = document["_runtime"]["snapshot"]
    personal = snapshot.get("project_intent") == "personal"
    if "gbrain_think" in payload["methods"]:
        try:
            from app.gbrain.think import gather

            question = str(snapshot.get("offer") or (
                "What prior notes support the founder's own intended workflow?" if personal
                else "What evidence supports the founder's problem and first paid user?"
            ))
            gathered = asyncio.run(gather(venture_id=row.venture_id, question=question))
            memory = [{"id": p["id"], "text": p["text"][:MAX_MEMORY_TEXT]}
                      for p in gathered.pages[:MAX_MEMORY_SOURCES] if p.get("id") and p.get("text")]
        except Exception:
            from app.idea.validation_agent import NO_MEMORY_GAP
            gaps.append(NO_MEMORY_GAP)
    raw_revision = {"id": row.revision_id, "text": json.dumps(snapshot, ensure_ascii=False)}
    from app.idea.validation_agent import MAX_REVISION_TEXT
    if len(raw_revision["text"]) > MAX_REVISION_TEXT:
        raise FatalError("validation_input_too_large")
    context = {"revision_source": None, "memory_sources": [], "methods": payload["methods"], "coverage_gaps": gaps}
    def store(row, doc, session):
        if doc.get("validation_context"):
            return doc["validation_context"]
        for index, source in enumerate([raw_revision] + memory):
            fid = "rsf_" + hashlib.sha256((did + source["id"]).encode()).hexdigest()[:32]
            if session.get(ResearchFinding, fid):
                continue
            text = source["text"] if index == 0 else source["text"][:MAX_MEMORY_TEXT]
            finding = ResearchFinding(id=fid, dossier_id=did, workspace_id=row.workspace_id, venture_id=row.venture_id,
                category="founder_statement" if index == 0 else "stored_memory", title="Founder idea revision" if index == 0 else "Stored venture memory",
                source="founder" if index == 0 else "gbrain", summary=text,
                evidence=json.dumps({"source_record_id": source["id"], "observed_at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
                                     "evidence_kind": "founder_statement" if index == 0 else "stored_memory", "page_verified": False}), created_at=utcnow())
            session.add(finding)
            value = {"id": fid, "text": text}
            if index == 0:
                context["revision_source"] = value
            else:
                context["memory_sources"].append(value)
        from app.gstack.office_hours import answers_for
        answers = answers_for(session, doc["_runtime"]["session_id"])
        # Skipped questions are not sent as answers: the review works only from what the founder said.
        context["gstack_answers"] = [a for a in answers if not a["skipped"]]
        context["not_answered"] = [a["name"] for a in answers if a["skipped"]]
        context["coverage_gaps"] = [*context["coverage_gaps"], *(
            f"{a.get('assumption_note') or 'Untested assumption'} for {a['name']}: {a.get('suggested_text') or a['answer']} (unverified)."
            for a in context["gstack_answers"] if a["assumed"]), *(
            f"{name}: not answered yet." for name in context["not_answered"])]
        doc["validation_context"] = context
        return context
    return ex.mutate(did, payload, store)


def _run_validation(payload: dict) -> None:
    from app.core.local_mode import is_sample_mode
    from app.idea.validation_agent import ASSESSMENT_NOTE, NO_MEMORY_ASSESSMENT, NO_MEMORY_GAP, messages_for, parse_result, response_schema, record_missing_memory
    from app.research.metered import synthesize
    did = ex.claim(payload, kind="validation", budget=5)
    if ex.read(did).state in {"complete", "partial"}:
        return
    context = _context(did, payload)
    from app.research.citations import project, restore
    revision, revision_map = project([context["revision_source"]], prefix="founder_revision")
    memories, memory_map = project(context["memory_sources"], prefix="memory")
    missing_memory = "gbrain_think" in context["methods"] and not memories
    model_methods = [method for method in context["methods"] if not (missing_memory and method == "gbrain_think")]
    inputs = {"revision_source": revision[0], "memory_sources": memories, "methods": model_methods,
              "gstack_answers": context.get("gstack_answers") or []}
    source_map = {**revision_map, **memory_map}
    result = None
    if is_sample_mode():
        from app.gstack.office_hours import founder_topic
        from app.research.deep import sample_research
        from app.research.source_quality import research_topic

        note = sample_research(did, payload, research_topic(json.loads(context["revision_source"]["text"])))
        raw = {"verdict": "needs_evidence", "summary": note,
            "findings": [{"method": method,
                "assessment": NO_MEMORY_ASSESSMENT if method == "gbrain_think" and missing_memory else note,
                "source_ids": ([] if missing_memory else [memories[0]["id"]]) if method == "gbrain_think" else [revision[0]["id"]]}
                for method in context["methods"]],
            "gaps": [note, *([NO_MEMORY_GAP] if missing_memory else [])],
            "proposed_tests": [], "questions": [],
            "forcing_questions": [{"question_id": answer["id"], "question": founder_topic(answer["name"]),
                "assessment": "You have saved this answer. This sample does not assess whether it is correct.",
                "risk": "This sample does not verify demand or willingness to pay.",
                "suggested_test": "Choose one small test based on your saved answers."}
                for answer in context.get("gstack_answers") or []]}
        result = restore(parse_result(json.dumps(raw), revision_source=revision[0], memory_sources=memories,
            methods=context["methods"], gstack_answers=context.get("gstack_answers") or []).model_dump(), source_map)
        result.update(sample=True, verified=False)
    elif model_methods:
        result = synthesize(did, payload, purpose="idea.validate", messages=messages_for(**inputs),
            schema=response_schema(model_methods, [revision[0]["id"], *(m["id"] for m in memories)]), max_tokens=VALIDATION_MAX_TOKENS,
            require_stop=True, cap_at_reservation=True, pin_provider=True, repair=True,
            provider_timeout_seconds=VALIDATION_PROVIDER_TIMEOUT_SECONDS,
            parse=lambda raw: restore(parse_result(raw, **inputs).model_dump(), source_map))
    if missing_memory:
        result = record_missing_memory(result)
    def apply(row, document, session):
        idea = session.get(IdeaSession, payload["session_id"], with_for_update=True)
        if idea.current_revision_id != payload["revision_id"] or idea.state in {"confirmed", "abandoned"}:
            row.state = "partial"
            document["coverage_gaps"] = ["A newer founder revision superseded this review; no decision was applied."]
            row.completed_at = utcnow()
            return
        # One immutable run per method and job; replay cannot create duplicates.
        usage = ex.operation(did, "review").get("usage", {"source": "sample" if result.get("sample") else "no_provider", "credits_micro": 0})
        for method in context["methods"]:
            run_id = "vlr_" + hashlib.sha256((did + method).encode()).hexdigest()[:32]
            if session.get(ValidationRun, run_id):
                continue
            session.add(ValidationRun(id=run_id, session_id=idea.id, revision_id=idea.current_revision_id,
                workspace_id=idea.workspace_id, venture_id=idea.venture_id, method=method, state="complete",
                verdict=result["verdict"], report=json.dumps({**result, "assessment_note": result["summary"] if result.get("sample") else ASSESSMENT_NOTE,
                    "not_answered": context.get("not_answered") or [], "dossier_id": did,
                    "usage": {k: usage.get(k) for k in ("source", "model", "provider", "credits_micro")}}),
                created_at=utcnow(), completed_at=utcnow()))
        document.update(validation=result, coverage_gaps=context["coverage_gaps"] + result["gaps"], review_kind="methodology_assessment")
        row.state, row.completed_at = "partial" if document["coverage_gaps"] else "complete", utcnow()
        # Founder reviews/accepts findings. An LLM verdict cannot confirm the idea.
    ex.mutate(did, payload, apply)


QUESTIONS_MAX_TOKENS = 4096  # 2048 was cut off (finish_reason=length) in production before the JSON finished


def _run_questions(payload: dict) -> None:
    """Pick and ask the GStack forcing questions that matter for this idea (one small call).

    A provider failure asks the standard Q1-Q3 instead; questions already asked for this idea are
    never asked again. The founder can skip at any time.
    """
    from app.gstack import office_hours as oh
    from app.llm.founder_provider import FounderLLMConfigError
    from app.research.metered import synthesize
    did = ex.claim(payload, kind="validation", budget=3)
    if ex.read(did).state in {"complete", "partial"}:
        return
    snapshot = json.loads(ex.read(did).summary)["_runtime"]["snapshot"]
    with db()() as session:
        asked = oh.asked_before(session, payload["session_id"])
        snapshot = {**snapshot, **oh.saved_snapshot(session, payload["venture_id"], payload["revision_id"])}
        from app.idea.agent import answer_metadata
        snapshot["earlier_answers"] = [{"answer": turn.text, **(json.loads(turn.options) if turn.options else answer_metadata(turn.text))} for turn in session.scalars(
            select(ClarifierTurn).where(ClarifierTurn.session_id == payload["session_id"], ClarifierTurn.role == "founder")
            .order_by(ClarifierTurn.turn))]
    try:
        picks = [] if asked else synthesize(did, payload, purpose="idea.clarify_turn", key="questions", messages=oh.messages_for(snapshot),
            schema=oh.response_schema(), max_tokens=QUESTIONS_MAX_TOKENS, require_stop=True, cap_at_reservation=True,
            pin_provider=True, repair=True, provider_timeout_seconds=VALIDATION_PROVIDER_TIMEOUT_SECONDS,
            parse=lambda raw: {"picks": oh.parse_picks(raw, snapshot)})["picks"]
        if not asked and not picks:
            picks = oh.default_picks(snapshot)
    except (FatalError, FounderLLMConfigError) as exc:
        if str(exc) == "research_operation_in_progress":
            raise
        picks = oh.default_picks(snapshot)
    def apply(row, document, session):
        idea = session.get(IdeaSession, payload["session_id"], with_for_update=True)
        row.state, row.completed_at = "complete", utcnow()
        if idea.current_revision_id != payload["revision_id"] or idea.state != "validating":
            row.state = "partial"
            return
        remaining = oh.unanswered_picks(session, idea, picks)
        if remaining:
            turn = oh.add_question_turn(session, idea, remaining[0], remaining[1:], 1, len(remaining), utcnow())
            document["gstack_questions"] = [p["gstack_id"] for p in remaining]
            oh.chat_question(session, idea, turn)
        else:
            document["gstack_questions"] = []
            enqueue(session, "idea.validate", {"session_id": idea.id, "revision_id": idea.current_revision_id,
                "methods": list(METHODS)}, workspace_id=idea.workspace_id, venture_id=idea.venture_id,
                dedupe_key=f"idea.validate:{idea.id}:{idea.current_revision_id}:gstack")
    ex.mutate(did, payload, apply)


def decide(session_id: str, run_id: str, decision: str, *, note=None, patch=None) -> dict:
    from app.core.fsm import advance
    from app.idea.service import get_session, patch_locked
    if decision not in {"accept", "reject", "change"}:
        raise ValueError("decision")
    if note is not None and (not isinstance(note, str) or len(note) > 4000):
        raise ValueError("validation_note_invalid")
    with db()() as session:
        idea = session.get(IdeaSession, session_id, with_for_update=True)
        run = session.get(ValidationRun, run_id, with_for_update=True)
        if not idea or not run or run.session_id != idea.id:
            raise KeyError("validation")
        if run.method not in METHODS:
            raise RuntimeError("validation_not_model_review")
        if idea.state in {"confirmed", "abandoned"}:
            raise RuntimeError("session_closed")
        if run.revision_id != idea.current_revision_id or run.state != "complete":
            raise RuntimeError("validation_revision_superseded")
        if decision == "change" and not patch:
            raise ValueError("validation_change_requires_patch")
        run.founder_decision, run.founder_note = decision, note
        if idea.state == "validating":
            advance(session, idea, "reviewing" if decision == "accept" else "clarifying")
        jid = None
        if decision == "change":
            revision = patch_locked(session, idea, patch)
            if idea.state == "reviewing":
                advance(session, idea, "clarifying")
            advance(session, idea, "validating")
            jid = enqueue(session, "idea.validate", {"session_id": idea.id, "revision_id": revision.id,
                "methods": list(METHODS)}, workspace_id=idea.workspace_id, venture_id=idea.venture_id)
        session.commit()
    if decision == "accept":
        __import__("app.experiments.ledger", fromlist=["x"]).on_validation_accepted(run_id)  # experiment ledger
    if jid:
        return {"job_id": jid, "session": get_session(session_id)}
    return get_session(session_id)


_OUTCOME_VERDICTS = {"needs_evidence", "change", "proceed_with_test"}


def post_outcome_to_chat(session, job_id: str, session_id: str, revision_id: str, *, ok: bool, code: str = "") -> None:
    """Tell the founder in chat how idea.validate ended, with one next step."""
    from app.chat.milestones import link, post_milestone
    from app.core.local_mode import is_local
    idea = session.get(IdeaSession, session_id)
    if idea is None:
        return
    href = f"/v/{idea.venture_id}/idea"
    if ok:
        run = session.scalars(select(ValidationRun).where(
            ValidationRun.session_id == session_id, ValidationRun.revision_id == revision_id,
            ValidationRun.state == "complete").order_by(ValidationRun.created_at.desc())).first()
        if run is None or run.verdict not in _OUTCOME_VERDICTS:
            return  # superseded by a newer revision
        try:
            report = json.loads(run.report or "{}")
        except ValueError:
            report = {}
        from app.idea.validation_agent import review_markdown
        from app.idea.service import _loads
        revision = session.get(IdeaRevision, revision_id)
        body = ("Your sample idea review is ready.\n\n" if report.get("sample") else "Your idea review is back.\n\n") + review_markdown(report, _loads(getattr(revision, "structured", None)))
        from app.ventures.startup_review import enabled
        if enabled(session, idea.venture_id):
            actions = [{"label": "Confirm your idea", "action": "idea.confirm", "style": "primary",
                        "payload": {"session_id": session_id, "revision_id": revision_id}}, link("Full report", href, "secondary")]
        else:
            from app.chat.build_path import research_card
            prepared = research_card(idea.venture_id)
            if prepared["kind"] == "needs_you":
                body += "\n\n" + prepared["body_md"]
            first = prepared["actions"][0] if prepared["kind"] == "needs_you" else {**prepared["actions"][0], "label": "Continue to research"}
            actions = [first, {"label": "Build my landing page now", "action": "idea.confirm",
                "style": "secondary", "payload": {"session_id": session_id, "revision_id": revision_id}},
                link("Full report", href, "secondary")]
        kind = "idea_validation_ready"
    elif is_local() and code == "validation_model_required":
        from app.chat.failed_jobs import retry_button
        from app.core.providers_local import LocalProviderError, load_local_config
        try:
            load_local_config()
        except LocalProviderError as exc:
            body = f"Your answers are saved. {exc} Then return here and choose Retry your idea review."
        else:
            body = "Your model connection is now saved. Choose Retry your idea review to use your saved answers."
        actions = [link("Open Keys and connections", "/settings/local", "primary"),
            {**retry_button("idea.validate"), "label": "Retry your idea review", "style": "secondary"}]
        kind = "idea_validation_failed"
    elif summary := saved_summary_for(session, idea, revision_id):
        body = summary["body_md"]
        actions = [{"label": "Confirm your idea", "action": "idea.confirm", "style": "primary",
                    "payload": {"session_id": session_id, "revision_id": revision_id}},
                   link("Your saved idea", href, "secondary")]
        kind = "idea_validation_ready"
    else:
        from app.chat import failed_jobs

        body = failed_jobs.body("idea.validate", code) + " Retrying uses your saved answers."
        actions, kind = failed_jobs.failure_actions(idea.venture_id, "idea.validate", code), "idea_validation_failed"
    post_milestone(idea.venture_id, key=f"{kind}:{job_id}", body_md=body, actions=actions, kind=kind, session=session,
                   failure={"step": "idea.validate", "code": code, "job_id": job_id}, requested=True)


def run_validation(payload: dict) -> None:
    from app.core.local_mode import is_local
    from app.llm.founder_provider import FounderLLMConfigError
    if payload.get("stage") == "gstack_questions":
        return _run_questions(payload)
    try:
        _run_validation(payload)
        with db()() as session:
            post_outcome_to_chat(session, payload["_job_id"], payload["session_id"], payload["revision_id"], ok=True)
            session.commit()
    except (FatalError, FounderLLMConfigError) as exc:
        if isinstance(exc, FounderLLMConfigError) and not is_local():
            raise
        code = str(exc)
        if code == "research_operation_in_progress":
            raise
        model_required = isinstance(exc, FounderLLMConfigError) or (is_local() and code == "research_byok_unavailable")
        if model_required:
            code = "validation_model_required"
        did = ex.identifier(payload["_job_id"])
        def failed(row, document, session):
            row.state = "failed"
            document["error_code"] = code
            document["coverage_gaps"] = ["Validation did not complete; the founder's revision was retained."]
            if code in _REJECTED_OUTPUT_CODES and _settled_rejection(document) and document.get("validation_context"):
                document["saved_summary"] = _saved_summary(document)
        try:
            if ex.read(did):
                ex.mutate(did, payload, failed)
        except (FatalError, AttributeError):
            pass
        if model_required:
            raise FatalError(code) from None
        raise
