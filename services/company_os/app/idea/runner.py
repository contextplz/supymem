"""Durable, metered clarification with one external call per recorded attempt.

A persisted provider response is reused after a crash. An ambiguous outbound call
is never automatically repeated. Job ownership and revision checks fence writes.
"""
from __future__ import annotations

from app.chat.navigation_map import in_chat

from app.intake.smart import condense_input

import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import copy_context
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any

from sqlalchemy import or_, select

from app.core.jobs import STALE_LOCK_S, FatalError, RetryableError
from app.core.local_mode import is_local
from app.db.models import ChatMessage, Job, Venture
from app.db.supymem_models import ClarifierTurn, IdeaClarificationRun, IdeaRevision, IdeaSession
from app.idea.agent import READY_CONFIDENCE, ASSUMED, Clarification, Question, Understanding, core_answer_status, core_followup, messages_for, parse_result, rejected_output_diagnostics, response_schema, safe_recommendation
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

PURPOSE = "idea.clarify_turn"
MAX_TOKENS = 4096
CLARIFY_TIMEOUT_SECONDS = int(os.getenv("LLM_CLARIFY_TIMEOUT_SECONDS", "180"))
TURN_PROGRESS_SECONDS = 10
log = logging.getLogger(__name__)


def _fence(session, payload, *, kind=PURPOSE):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if not job or job.status != "leased" or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt"):
        raise FatalError("clarification_lease_lost")
    if job.kind != kind:
        raise FatalError("clarification_scope_mismatch")
    if job.workspace_id != payload.get("workspace_id") or job.venture_id != payload.get("venture_id"):
        raise FatalError("clarification_scope_mismatch")
    if json.loads(job.payload_json).get("_chat_conversation") != payload.get("_chat_conversation"):
        raise FatalError("clarification_scope_mismatch")
    from types import SimpleNamespace
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(workspace_id=job.workspace_id, venture_id=job.venture_id,
                               kind=job.kind, payload=json.loads(job.payload_json)))
    _reply_conversation_id(session, payload)
    return job


def _post_progress(payload, tick, kind):
    if is_local() and kind == PURPOSE:
        return
    speaker = _speaker(payload["venture_id"])
    with db()() as session:
        _fence(session, payload, kind=kind)
        idea = session.get(IdeaSession, payload["session_id"])
        if (not idea or (idea.workspace_id, idea.venture_id) != (payload["workspace_id"], payload["venture_id"])
                or idea.state in {"confirmed", "abandoned"}):
            return
        from app.chat.store import request_citations
        session.add(ChatMessage(id=new_id("msg"), workspace_id=idea.workspace_id, venture_id=idea.venture_id,
            conversation_id=_reply_conversation_id(session, payload), role="assistant", kind="idea_clarification_pending",
            body_md=_manager_body(speaker, "Your idea and answers are saved. I'm still reviewing your idea in the background. "
                "You can keep working or come back here for your next question or review."),
            actions_json="[]", citations_json=json.dumps(request_citations(request_id=payload.get("_chat_request_nonce"))),
            idempotency_key="chat:" + hashlib.sha256(json.dumps(
                [idea.venture_id, "assistant", f"idea-progress:{payload['_job_id']}:{payload['_job_attempt']}:{tick}"], separators=(",", ":")).encode()).hexdigest(),
            created_at=utcnow()))
        session.commit()


@contextmanager
def turn_progress(payload, *, kind=PURPOSE):
    stop = Event()
    def notices():
        tick = 0
        while not stop.wait(TURN_PROGRESS_SECONDS):
            tick += 1
            try:
                _post_progress(payload, tick, kind)
                return
            except FatalError:
                return
            except Exception:
                log.warning("idea_progress_unavailable", extra={"job_id": payload.get("_job_id")})
    context = copy_context()
    thread = Thread(target=lambda: context.run(notices), name="idea-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _reply_conversation_id(session, payload):
    value = payload.get("_chat_conversation")
    if value is None:
        from app.chat.conversations import _legacy
        venture = session.get(Venture, payload.get("venture_id"))
        if venture is None or venture.workspace_id != payload.get("workspace_id"):
            raise FatalError("clarification_scope_mismatch")
        return _legacy(session, venture).id
    from app.chat.conversations import capture_job_conversation
    from app.db.models import ChatConversation
    if capture_job_conversation(payload.get("venture_id"), payload.get("workspace_id")) != value:
        raise FatalError("clarification_scope_mismatch")
    row = session.get(ChatConversation, value["conversation_id"])
    if (row is None or row.visibility != "private" or row.venture_id != value["venture_id"]
            or row.workspace_id != value["workspace_id"] or row.creator_person_id != value["actor_id"]):
        raise FatalError("clarification_scope_mismatch")
    return row.id


def _questionnaire_interrupted(session, payload) -> bool:
    from app.chat.idea_bridge import split_question_reply

    answer = session.scalar(select(ClarifierTurn).where(
        ClarifierTurn.session_id == payload["session_id"], ClarifierTurn.role == "founder",
        ClarifierTurn.revision_id == payload.get("revision_id")))
    question_at = session.scalar(select(ClarifierTurn.created_at).where(
        ClarifierTurn.session_id == payload["session_id"], ClarifierTurn.role == "agent",
        ClarifierTurn.kind == "question", ClarifierTurn.turn == answer.turn - 1)) if answer else None
    message = session.scalar(select(ChatMessage).where(
        ChatMessage.venture_id == payload["venture_id"], ChatMessage.workspace_id == payload["workspace_id"],
        ChatMessage.conversation_id == _reply_conversation_id(session, payload), ChatMessage.role == "user",
    ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
    return bool(answer and message and (
        (message.kind == "idea_answer_with_request" and question_at
         and as_utc(message.created_at) >= as_utc(question_at) and message.body_md.startswith(answer.text))
        or (message.kind != "idea_answer" and as_utc(message.created_at) >= as_utc(answer.created_at)
            and (message.kind == "questionnaire_request" or question_at and split_question_reply(message.body_md)[1]))))


def unresolved_clarification(session, idea, *, exclude_job_id=None) -> bool:
    query = select(IdeaClarificationRun).where(
        IdeaClarificationRun.session_id == idea.id,
        IdeaClarificationRun.workspace_id == idea.workspace_id,
        IdeaClarificationRun.venture_id == idea.venture_id,
        IdeaClarificationRun.state.in_(("prepared", "calling", "uncertain")),
    )
    if exclude_job_id is not None:
        query = query.where(IdeaClarificationRun.job_id != exclude_job_id)
    for run in session.scalars(query):
        retry_id = json.loads(run.snapshot).get("_founder_retry_job_id") if run.state == "uncertain" else None
        retry = session.get(Job, retry_id) if retry_id else None
        if retry and (retry.kind, retry.workspace_id, retry.venture_id) == (PURPOSE, idea.workspace_id, idea.venture_id) and json.loads(retry.payload_json).get("session_id") == idea.id:
            continue
        return True
    return False


def _claim(payload: dict) -> str | None:
    from app.idea.service import _loads
    with db()() as session:
        _fence(session, payload)
        idea = session.get(IdeaSession, payload["session_id"], with_for_update=True)
        if not idea or (idea.workspace_id, idea.venture_id) != (payload["workspace_id"], payload["venture_id"]):
            raise FatalError("clarification_scope_mismatch")
        run = session.scalar(select(IdeaClarificationRun).where(IdeaClarificationRun.job_id == payload["_job_id"]).with_for_update())
        if run:
            if run.state in {"completed", "superseded"}:
                return None
            if run.state == "calling" and run.attempt == payload["_job_attempt"] and run.worker_id == payload["_worker_id"]:
                raise FatalError("clarification_operation_in_progress")
            if run.state in {"calling", "uncertain"}:
                # The previous worker may have sent a paid request. A new lease
                # does not prove the request failed; do not redial.
                raise FatalError("clarification_outcome_uncertain")
            if run.state == "failed":
                raise FatalError(run.error_code or "clarification_failed")
            if run.state == "responded":
                return run.id
            if run.state in {"prepared", "retryable"} and run.attempt >= payload["_job_attempt"]:
                # Claim before entering charge_sync. Otherwise a duplicate
                # delivery can release the first invocation's idempotent hold.
                raise FatalError("clarification_operation_in_progress")
        if idea.state in {"confirmed", "abandoned"} or idea.current_revision_id != payload.get("revision_id"):
            if run:
                run.state = "superseded"
                session.commit()
            return None
        if unresolved_clarification(session, idea, exclude_job_id=payload["_job_id"]):
            raise FatalError("clarification_outcome_uncertain")
        if run is None:
            from app.ventures.current_idea import current_idea
            venture = session.get(Venture, idea.venture_id)
            if not venture or venture.workspace_id != idea.workspace_id:
                raise FatalError("clarification_scope_mismatch")
            rev = session.get(IdeaRevision, idea.current_revision_id)
            understanding = _loads(rev.structured)
            from app.ventures.founder_facts import read as read_facts, saved_fact_times, answer_field_times
            from app.ventures.startup_review import enabled
            facts = read_facts(session, venture) if enabled(session, venture.id) else {}
            turns = list(session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id).order_by(ClarifierTurn.turn.desc())))
            # Keep agent turns bounded, but derive the repeat
            # guard from the full durable question history. A long research /
            # validation journey can push an earlier pricing or acquisition
            # question outside the last 20 turns; forgetting it here makes the
            # founder pay to answer the same structured field again.
            historical_question_options = session.scalars(
                select(ClarifierTurn.options).where(
                    ClarifierTurn.session_id == idea.id,
                    ClarifierTurn.role == "agent",
                    ClarifierTurn.kind == "question",
                    ClarifierTurn.options.is_not(None),
                )
            ).all()
            question_fields_asked: list[str] = []
            core_counts = {"offer": 0, "customer": 0}
            for options in historical_question_options:
                try:
                    field = json.loads(options).get("field")
                except (TypeError, ValueError, AttributeError):
                    continue
                if isinstance(field, str) and field not in question_fields_asked:
                    question_fields_asked.append(field)
                if field in core_counts:
                    core_counts[field] += 1
            latest_agent = next((turn for turn in turns if turn.role == "agent"), None)
            latest_question = latest_agent if latest_agent and latest_agent.kind == "question" else None
            from app.idea.question_history import answered_questions
            from app.ventures.stage import has_existing_customers
            snapshot = {
                # The mode chip is not the product brief. Include the idea
                # already saved during company creation, including constraints,
                # in this immutable attempt so founders need not enter it twice.
                "original_idea": condense_input(current_idea(venture, session=session)),
                "founder_idea": condense_input(venture.raw_idea or ""),
                "project_intent": "personal" if understanding.get("project_intent") == "personal" else "startup",
                "has_customers": has_existing_customers(session, venture.id),
                "understanding": {k: v for k, v in understanding.items() if not k.startswith("_") and k != "project_intent"},
                "conversation": [{**_loads(t.options), "role": t.role, "kind": t.kind,
                                  "text": condense_input(t.text)} for t in reversed(turns)
                                 if t.role == "founder" or t in turns[:20]],
                "website_answers": understanding.get("_website_answers", {}),
                "answered_fields": understanding.get("_answered_fields", {}),
                "answered_questions": answered_questions(session, idea.id),
                "answered_field_updated_at": {field: saved_at.isoformat() for field, saved_at in answer_field_times(session, rev).items()},
                "prefill_facts": understanding.get("_prefill_facts", {}),
                "question_fields_asked": list(dict.fromkeys(question_fields_asked + understanding.get("_onboarding_completed", [])
                    + list(understanding.get("_website_answers", {})))),
                "core_question_counts": core_counts,
                "active_core_field": _loads(latest_question.options).get("field") if latest_question else None,
                "previous_question_options": _previous_question_options(turns),
                "case_hint": idea.case, "explicit_case": understanding.get("_explicit_case"),
                "questions_asked": idea.questions_asked, "question_budget": idea.question_budget,
                "force_propose": bool(payload.get("force_propose") or idea.questions_asked >= idea.question_budget),
                "research_status": "not_performed_by_clarifier",
                "founder_facts": facts,
                "founder_fact_updated_at": saved_fact_times(session, venture, facts),
                "revision_created_at": rev.created_at.isoformat() if rev else "",
            }
            from app.clarify.dilemmas import venture_context
            audience, job = venture_context(snapshot)
            snapshot["question_context"] = {"audience": audience, "job": job}
            from app.connections.context_docs import research_context
            connected = research_context(session, venture.id, venture.workspace_id)
            # Connected context is optional: drop it rather than fail clarification.
            if connected and len(json.dumps({**snapshot, **connected})) <= 60000:
                snapshot.update(connected)
            if len(json.dumps(snapshot)) > 60000:
                raise FatalError("clarification_context_limit")
            run = IdeaClarificationRun(id=new_id("icr"), job_id=payload["_job_id"], session_id=idea.id,
                workspace_id=idea.workspace_id, venture_id=idea.venture_id, revision_id=rev.id,
                state="prepared", attempt=payload["_job_attempt"], worker_id=payload["_worker_id"],
                snapshot=json.dumps(snapshot), usage="{}", created_at=utcnow(), updated_at=utcnow())
            session.add(run)
        else:
            run.state = "prepared"
            run.attempt = payload["_job_attempt"]
            run.worker_id = payload["_worker_id"]
            run.error_code = None
        session.commit()
        return run.id


def _previous_question_options(turns) -> list[str]:
    """Chip labels of the most recent saved question (turns are newest first)."""
    for t in turns:
        if t.role == "agent" and t.kind == "question" and t.options:
            try:
                opts = json.loads(t.options).get("options")
            except (TypeError, ValueError, AttributeError):
                return []
            return [o for o in opts or [] if isinstance(o, str)]
    return []


def _receipt(run_id: str):
    with db()() as session:
        run = session.get(IdeaClarificationRun, run_id)
        session.expunge(run)
        return run


def _save(run_id: str, **changes):
    with db()() as session:
        run = session.get(IdeaClarificationRun, run_id, with_for_update=True)
        for key, value in changes.items():
            setattr(run, key, value)
        run.updated_at = utcnow()
        session.commit()


def _settle_saved(run, *, persist=True):
    """Recover the already-held charge, never create a second reservation."""
    from app.credits.estimate import actual_llm_micro
    from app.credits.reserve import Hold, settle
    usage = json.loads(run.usage)
    if usage.get("source") == "platform" and usage.get("reservation_id"):
        try:
            result = settle(Hold(usage["reservation_id"], usage["account_id"], usage["estimate_micro"]),
                            actual_micro=usage.get("actual_micro", actual_llm_micro(usage["model"], prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])),
                            prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
        except Exception as exc:
            raise RetryableError("clarification_settlement_pending") from exc
        usage.update({"credits_micro": result.actual_micro, "settled": True})
        if persist:
            _save(run.id, usage=json.dumps(usage))
    elif usage.get("source") == "byok":
        usage.update({"credits_micro": 0, "settled": True})
        if persist:
            _save(run.id, usage=json.dumps(usage))
    return usage


def reconcile_interrupted_clarifications(*, now=None, limit=100) -> int:
    """Resolve abandoned calls and credit holds without provider I/O or replay."""
    from app.credits.gate import _check_hold
    from app.credits.ledger import append, find_by_idempotency_key
    from app.credits.reserve import _close_open_hold
    from app.db.models import CreditAccount, CreditReservation
    from app.db.session import serialize_writes

    clock = as_utc(now or utcnow())
    cutoff = clock - timedelta(seconds=CLARIFY_TIMEOUT_SECONDS + 60)
    stale = clock - timedelta(seconds=STALE_LOCK_S)
    terminal = ("failed_dead", "failed", "poison", "cancelled", "succeeded")
    with db()() as session:
        ids = list(session.scalars(select(IdeaClarificationRun.id).join(Job, Job.id == IdeaClarificationRun.job_id).where(
            IdeaClarificationRun.state == "calling", IdeaClarificationRun.updated_at < cutoff,
            or_(Job.status.in_(terminal),
                Job.status.in_(("leased", "running")) & (Job.locked_at < stale)),
        ).order_by(IdeaClarificationRun.updated_at, IdeaClarificationRun.id).limit(limit)))
    resolved = 0
    for run_id in ids:
        saved = _receipt(run_id)
        usage = json.loads(saved.usage)
        if saved.state != "calling":
            continue
        settled_usage = None
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            settled_usage = _settle_saved(saved, persist=False)
        with db()() as session:
            serialize_writes(session, key=usage.get("account_id") or run_id)
            job = session.get(Job, saved.job_id, with_for_update=True)
            if not job or not (job.status in terminal or (
                    job.status in {"leased", "running"} and job.locked_at is not None and as_utc(job.locked_at) < stale)):
                continue
            run = session.get(IdeaClarificationRun, run_id, with_for_update=True)
            if run.state != "calling" or as_utc(run.updated_at) >= cutoff:
                continue
            if (job.kind, job.workspace_id, job.venture_id) != (PURPOSE, run.workspace_id, run.venture_id):
                log.warning("clarification_reconciliation_scope_mismatch", extra={"run_id": run_id})
                continue
            usage = json.loads(run.usage)
            if "prompt_tokens" in usage and "completion_tokens" in usage:
                if settled_usage is None or not settled_usage.get("settled"):
                    continue
                usage = settled_usage
                usage["billing_resolution"] = "confirmed_charge"
            else:
                if usage.get("source") not in {"platform", "byok"}:
                    log.warning("clarification_reconciliation_scope_mismatch", extra={"run_id": run_id})
                    continue
                if usage.get("source") == "platform":
                    reservation = session.get(CreditReservation, usage.get("reservation_id"), with_for_update=True)
                    _check_hold(reservation, {**usage, "workspace_id": run.workspace_id, "venture_id": run.venture_id,
                        "purpose": PURPOSE, "request_id": f"{run.id}:{run.attempt}", "task_invocation": None})
                    if reservation.status not in {"open", "active", "reconciling", "expired", "released"}:
                        log.warning("clarification_reconciliation_scope_mismatch", extra={"run_id": run_id})
                        continue
                    account = session.get(CreditAccount, reservation.account_id, with_for_update=True)
                    if account is None:
                        log.warning("clarification_reconciliation_scope_mismatch", extra={"run_id": run_id})
                        continue
                    _close_open_hold(session, reservation, "released", definitive=True)
                    session.refresh(account)
                    key = "clarification-absorb:" + reservation.id
                    if find_by_idempotency_key(key, session) is None:
                        append(session, account=account, kind="release", amount_micro=0,
                            reason="Supymem absorbed an unconfirmed clarification cost; no call was repeated.",
                            actor_kind="system", actor_id="idea.maintenance", idempotency_key=key,
                            purpose=reservation.purpose, provider=reservation.provider, model=reservation.model,
                            reservation_id=reservation.id, request_id=reservation.request_id,
                            meta={"maximum_micro": reservation.amount_micro, "provider_outcome": "unknown"})
                usage.update(settled=True, credits_micro=0, billing_resolution="platform_absorbed", provider_outcome="unknown")
            run.state, run.error_code, run.updated_at = "uncertain", "clarification_outcome_uncertain", clock
            run.usage = json.dumps(usage)
            session.commit()
            resolved += 1
    return resolved


def _perform(run_id: str, payload: dict):
    from app.credits.accounts import ensure_account, get_account
    from app.credits.errors import CreditError
    from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
    from app.credits.gate import charge_sync
    from app.llm import founder_provider
    from app.llm.provider_errors import definitive_provider_rejection, safe_rate_limit_retry
    from llm_gateway import sole
    from llm_gateway.boundary import BoundaryViolation
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.usage import extract_confirmed_token_usage

    run = _receipt(run_id)
    snapshot = json.loads(run.snapshot)
    from app.core.local_mode import is_test_mode, is_offline, is_local
    offline = is_test_mode() or (is_offline() if is_local() else os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"})
    model_missing = False
    if not offline:
        founder_provider.register_workspace_llm_resolver()
        try:
            resolved = founder_provider.resolve_llm_config(run.workspace_id)
        except founder_provider.FounderLLMConfigError:
            if not is_local():
                raise
            offline = model_missing = True
    if offline:
        result = Clarification(case=snapshot.get("case_hint") or "know", action="proposal",
            message=("Your answers are saved. Review this summary and correct anything you want to change." if model_missing
                else "You are viewing sample data based on what you shared. Review this summary and correct anything you want to change."),
            understanding=Understanding.model_validate(snapshot.get("understanding") or {}))
        result = _core_clarification(snapshot, result)
        if (result.action == "proposal" and snapshot.get("project_intent") == "personal"
                and not snapshot.get("force_propose") and "workflow" not in _known_fields(snapshot)):
            from app.idea.service import fallback_first_question
            result = result.model_copy(update={"action": "question", "question": Question(
                text=fallback_first_question("personal"), field="workflow",
                why="Understand what the first version should do", options=[])})
        result.understanding.summary = _summary_text(result.understanding.offer)
        _save(run_id, state="responded", response=result.model_dump_json())
        return
    messages = messages_for(snapshot)
    byok = resolved.source == "founder_override"
    override = founder_provider.to_provider_config(run.workspace_id) if byok else None
    if byok and override is None:
        _save(run_id, state="failed", error_code="clarification_byok_unavailable")
        raise FatalError("clarification_byok_unavailable")
    override = (override or sole.load_provider_config_from_env()).model_copy(deep=True)
    override.timeout_seconds = max(override.timeout_seconds, CLARIFY_TIMEOUT_SECONDS)
    override = sole.resolve_purpose_config(PURPOSE, override, tier="pro")
    account = get_account(run.venture_id)
    if not byok and account is None:
        # Old or partially-created ventures may predate credit bootstrap, and
        # a transient failure during /entry must not permanently strand the
        # guided journey. This operation is idempotent and uses the standard
        # starter allowance.
        try:
            account = ensure_account(run.venture_id, workspace_id=run.workspace_id)
        except Exception:
            account = None
    from app.credits.accounts import account_can_spend
    with db()() as session:
        account_ready = account is not None and account_can_spend(session, account, run.workspace_id, run.venture_id)
    if not byok and not account_ready:
        _save(run_id, state="failed", error_code="clarification_credits_unavailable")
        raise FatalError("clarification_credits_unavailable")
    free_local = founder_provider.keyless_local_config(override)
    estimate = 0 if free_local else estimate_llm_micro(resolved.model, prompt_tokens=count_prompt_tokens(messages), max_tokens=MAX_TOKENS)
    meter = None
    try:
        with charge_sync(workspace_id=run.workspace_id, account_id=account.id if account else None,
                         purpose=PURPOSE, estimate_micro=estimate, model=resolved.model, provider=resolved.provider,
                         request_id=run.id + ":" + str(run.attempt), payer="founder" if byok else None) as meter:
            if not byok and (meter.passthrough or not meter.hold):
                raise FatalError("clarification_credit_reservation_unavailable")
            usage = {"source": "byok" if byok else "platform", "model": resolved.model, "provider": resolved.provider,
                     "estimate_micro": estimate, "account_id": account.id if account else None,
                     "reservation_id": meter.hold.reservation_id if meter.hold else None}
            with db()() as session:
                _fence(session, payload)
                row = session.get(IdeaClarificationRun, run_id, with_for_update=True)
                if row.state != "prepared" or row.attempt != payload["_job_attempt"]:
                    raise FatalError("clarification_outcome_uncertain")
                idea = session.get(IdeaSession, run.session_id, with_for_update=True)
                if idea.current_revision_id != run.revision_id or idea.state in {"confirmed", "abandoned"}:
                    row.state = "superseded"
                    session.commit()
                    return
                row.state, row.usage, row.updated_at = "calling", json.dumps(usage), utcnow()
                session.commit()
            meter.protect_outcome()
            try:
                from app.research.metered import _transmit_with_deadline
                dial = dict(messages=messages, purpose=PURPOSE, workspace_id=run.workspace_id,
                    tier="pro", max_tokens=MAX_TOKENS, config=override,
                    response_schema=response_schema(), single_attempt=True, trace_id=run.id,
                    use_workspace_override=False)
                response = asyncio.run(_transmit_with_deadline(
                    sole.transmit, dial, CLARIFY_TIMEOUT_SECONDS))
            except BoundaryViolation as exc:
                _save(run_id, state="failed", error_code="clarification_boundary_blocked")
                meter.confirm_unspent()
                raise FatalError("clarification_boundary_blocked") from exc
            except ProviderCallError as exc:
                retry = safe_rate_limit_retry(exc)
                uncertain = not definitive_provider_rejection(exc)
                code = "clarification_outcome_uncertain" if uncertain else "clarification_provider_" + exc.code
                _save(run_id, state="retryable" if retry else "uncertain" if uncertain else "failed", error_code=code)
                if not uncertain:
                    meter.confirm_unspent()
                if retry:
                    raise RetryableError("clarification_provider_rate_limited") from exc
                raise FatalError(code) from exc
            except Exception as exc:
                _save(run_id, state="uncertain", error_code="clarification_outcome_uncertain")
                raise FatalError("clarification_outcome_uncertain") from exc
            counts = extract_confirmed_token_usage(getattr(response, "usage", None))
            if counts is None:
                _save(run_id, state="uncertain", error_code="clarification_outcome_uncertain")
                raise FatalError("clarification_outcome_uncertain")
            usage.update(counts)
            usage["provenance"] = "provider_usage"
            meter.record(prompt_tokens=counts["prompt_tokens"], completion_tokens=counts["completion_tokens"])
            if free_local:
                meter.record_micro(0, provider_actual_micro=0)
            choices = getattr(response, "choices", None) or []
            content = str(getattr(getattr(choices[0], "message", None), "content", "") or "") if choices else ""
            invalid = None
            try:
                result = parse_result(content, force_propose=snapshot["force_propose"],
                                      previous_options=snapshot.get("previous_question_options") or [])
                if snapshot.get("explicit_case") and result.case != snapshot["explicit_case"]:
                    # The founder's explicit workflow choice outranks the
                    # model's classification. Preserve the validated content
                    # rather than rejecting this paid response over a label.
                    result = result.model_copy(update={"case": snapshot["explicit_case"]})
                result = _core_clarification(snapshot, result)
                if (
                    not is_local()
                    and result.action == "question"
                    and result.question is not None
                    and result.confidence >= READY_CONFIDENCE
                    and _enough_answers(snapshot, result.question)
                    and result.question.field not in {"offer", "customer"}
                ):
                    result = result.model_copy(
                        update={
                            "action": "proposal",
                            "question": None,
                            "message": "Here is what I took from your answers. Tell me if I got anything wrong.",
                        }
                    )
                body, invalid = result.model_dump_json(), None
            except ValueError as exc:
                body, invalid = None, "clarification_invalid_output"
                usage["output"] = rejected_output_diagnostics(content, choices[0] if choices else None, exc)
                log.warning("clarification_model_output_rejected", extra={
                    "job_id": payload["_job_id"], "run_id": run_id,
                    "output_diagnostics": usage["output"],
                })
                try:
                    from app.analytics.error_events import record_model_rejected

                    record_model_rejected(operation_kind="idea.clarify_turn", rule="clarification_invalid_output", feature="idea",
                        operation_id=f"{run_id}:0", venture_id=run.venture_id,
                        workspace_id=run.workspace_id, job_id=payload["_job_id"])
                except Exception:  # noqa: BLE001 - telemetry never changes a charged, rejected answer
                    pass
            # Receipt must be committed before a charge can settle or a retry can
            # apply it. No provider body is logged, including invalid output.
            _save(run_id, state="responded", response=body, usage=json.dumps(usage), error_code=invalid)
        # charge_sync contains settlement errors to avoid masking provider errors.
        # Reconcile explicitly so the durable job cannot falsely report settlement.
        _settle_saved(_receipt(run_id))
    except (FatalError, RetryableError) as exc:
        if meter is not None and meter.definitive_failure:
            from app.chat.assistant import _rejection_release_confirmed
            if not _rejection_release_confirmed(meter, workspace_id=run.workspace_id, byok=byok):
                _save(run_id, state="uncertain", error_code="clarification_outcome_uncertain")
                raise FatalError("clarification_outcome_uncertain") from exc
        raise
    except CreditError as exc:
        code = "clarification_" + exc.code
        _save(run_id, state="failed", error_code=code,
              **({"usage": json.dumps({"required_micro": exc.required_micro})} if exc.code == "insufficient_credits" and meter is None else {}))
        raise FatalError(code) from exc


def _apply(run_id: str, payload: dict):
    from app.core.fsm import advance
    from app.idea.service import REQUIRED, _append_revision, _chat_entry_deferred, _loads, _next_turn_number, _project_intent, chat_entry_goal
    speaker = _speaker(payload["venture_id"])
    run = _receipt(run_id)
    usage = _settle_saved(run)
    if run.error_code:
        if (run.error_code == "clarification_invalid_output" and payload.get("reply_in_chat")
                and post_fallback_question(payload, run_id=run_id)):
            return
        _save(run_id, state="failed")
        raise FatalError(run.error_code)
    parsed = Clarification.model_validate_json(run.response)
    with db()() as session:
        _fence(session, payload)
        row = session.get(IdeaSession, run.session_id, with_for_update=True)
        receipt = session.get(IdeaClarificationRun, run_id, with_for_update=True)
        if receipt.state in {"completed", "superseded"}:
            return
        if row.state in {"confirmed", "abandoned"} or row.current_revision_id != run.revision_id:
            receipt.state = "superseded"
        else:
            from app.idea.question_history import CONFIRM, answered_questions, repeated_question, unanswered_question
            history = answered_questions(session, row.id)
            repeated = bool(parsed.question and repeated_question(parsed.question.model_dump(), history))
            if repeated:
                following = unanswered_question(parsed.question.model_dump(), history, snapshot=_loads(run.snapshot)) if row.questions_asked < row.question_budget else None
                parsed = parsed.model_copy(update={"question": Question.model_validate(following)}) if following else parsed.model_copy(
                    update={"action": "proposal", "question": None, "message": CONFIRM})
            previous = session.get(IdeaRevision, row.current_revision_id)
            structured = parsed.understanding.model_dump()
            prior_structured = _loads(previous.structured)
            if prior_structured.get("_website_answers"):
                structured["_website_answers"] = dict(prior_structured["_website_answers"])
                founder = next((turn["text"] for turn in reversed(_loads(run.snapshot).get("conversation", []))
                    if turn["role"] == "founder"), "")
                for key, answer in list(structured["_website_answers"].items()):
                    updated = structured.get(key)
                    if isinstance(updated, str) and updated.strip() and updated != answer["answer"] and updated in founder:
                        del structured["_website_answers"][key]
                for key, answer in structured["_website_answers"].items():
                    if key in structured:
                        structured[key] = answer["answer"]
            explicit = prior_structured.get("_explicit_case")
            if explicit:
                structured["_explicit_case"] = explicit
            if prior_structured.get("_corrects_revision_id"):
                structured["_corrects_revision_id"] = prior_structured["_corrects_revision_id"]
            if isinstance(prior_structured.get("_answer_corrections"), dict):
                structured["_answer_corrections"] = prior_structured["_answer_corrections"]
            if isinstance(prior_structured.get("_answer_provenance"), dict):
                structured["_answer_provenance"] = prior_structured["_answer_provenance"]
            if isinstance(prior_structured.get("_answered_fields"), dict):
                structured["_answered_fields"] = prior_structured["_answered_fields"]
            if isinstance(prior_structured.get("_prefill_facts"), dict):
                structured["_prefill_facts"] = prior_structured["_prefill_facts"]
            if prior_structured.get("_onboarding_completed"):
                structured["_onboarding_completed"] = prior_structured["_onboarding_completed"]
            snapshot = _loads(run.snapshot)
            known = _known_fields(snapshot, extracted=parsed.understanding.model_dump())
            sources = _founder_messages(snapshot)
            stated = {field: value for field, value in known.items()
                if field in (*REQUIRED, "requirements", "constraints")
                and all(any(part in source for source in sources) for part in (value if isinstance(value, list) else [value]))}
            if stated:
                structured.setdefault("_answered_fields", {}).update(stated)
                structured["_onboarding_completed"] = list(dict.fromkeys([
                    *structured.get("_onboarding_completed", []), *(field for field in ("offer", "customer") if field in stated)]))
            prior_assumptions = prior_structured.get("assumptions", [])
            structured["assumptions"] = list(dict.fromkeys(prior_assumptions + structured["assumptions"]))[:15]
            for field in ("offer", "customer"):
                marker = f"Assumed: {field} is unclarified;"
                if not any(assumption.startswith(marker) for assumption in structured["assumptions"]):
                    continue
                if prior_structured.get(field) and any(assumption.startswith(marker) for assumption in prior_assumptions):
                    # The founder filled this field after it was assumed: keep it and retire the stale assumption.
                    structured["assumptions"] = [a for a in structured["assumptions"] if not a.startswith(marker)]
                    structured[field] = structured[field] or prior_structured[field]
                else:
                    structured[field] = ""
            parsed.understanding = Understanding.model_validate({key: value for key, value in structured.items() if key in Understanding.model_fields})
            structured["project_intent"] = "personal" if prior_structured.get("project_intent") == "personal" else "startup"
            rev = _append_revision(session, row, structured, source="agent", summary=structured["summary"])
            row.case = parsed.case
            if row.state == "recon":
                advance(session, row, "clarifying")
            # Set before the branch: the repeat guard below can turn a question into a proposal.
            personal = (_project_intent(session, row.workspace_id, row.venture_id) == "personal"
                        and structured["project_intent"] == "personal")
            if parsed.action == "proposal":
                if row.state in {"reviewing", "proposing", "validating"}:
                    advance(session, row, "clarifying")
                advance(session, row, "proposing")
                advance(session, row, "reviewing")
                # The proposal waits for the founder. GStack office hours start only
                # from an explicit, revision-bound Looks right (chat.idea_proposal).
            else:
                if row.state in {"reviewing", "proposing", "validating"}:
                    advance(session, row, "clarifying")
                row.questions_asked += 1
            safe_usage = {k: usage.get(k) for k in ("source", "model", "provider", "prompt_tokens", "completion_tokens", "provenance", "credits_micro")}
            question = parsed.question.model_dump() if parsed.question else None
            if question:
                question["allow_free_text"] = True
                from app.clarify.dilemmas import contextual_choices
                from app.idea.agent import is_evidence_question
                if is_evidence_question(parsed.question.text, question["options"]):
                    options = contextual_choices("Q1", {**_loads(run.snapshot), **structured})
                    question.update(options=options, requires_detail=options[1:], contextual_evidence=True,
                        detail_prompt="Describe the evidence: who, what happened, and what did they do?",
                        recommended_option="", recommendation_reason="")
                from app.ventures.founder_facts import ALIASES, policy
                facts = _loads(run.snapshot).get("founder_facts", {})
                if policy(facts, question.get("field")) == "confirm":
                    question["prefill"] = facts[ALIASES.get(question["field"], question["field"])]["value"]
            if question:
                from app.clarify.dilemmas import ground_question
                from app.gstack.office_hours import saved_snapshot
                current = saved_snapshot(session, row.venture_id, row.current_revision_id)
                question = ground_question(question, current)
                parsed.question = Question.model_validate({key: value for key, value in question.items() if key in Question.model_fields})
                if (not is_local() and ground_question({"field": question["field"], "text": parsed.message}, current)["text"] != parsed.message):
                    parsed.message = "Your answers are saved."
                _question_reply(parsed.question.text, question, facts=current.get("founder_facts", {}), context=current)
            saved_turn = ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
                venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent",
                kind=parsed.action, text=parsed.question.text if parsed.question else parsed.message,
                options=json.dumps(question) if question else None, revision_id=rev.id,
                llm_usage=json.dumps(safe_usage), created_at=utcnow())
            from app.idea.question_history import guard_question_turn
            if guard_question_turn(session, row, saved_turn):
                question = _loads(saved_turn.options) if saved_turn.options else None
                if saved_turn.kind == "proposal":
                    repeated = True
                    parsed = parsed.model_copy(update={"action": "proposal", "question": None, "message": saved_turn.text})
            session.add(saved_turn)
            from app.ventures.startup_review import enabled
            from app.ventures.journey_overrides import chose_customers, read as journey_choices
            from app.gstack import office_hours
            new_questions = (parsed.action == "proposal" and not personal and enabled(session, row.venture_id)
                             and (not is_local() or payload.get("first_question_fallback")
                                  or _chat_entry_deferred(session, row.workspace_id, row.venture_id)
                                  or chat_entry_goal(session, row.workspace_id, row.venture_id))
                             and not (is_local() and payload.get("force_propose"))
                             and not repeated
                             and not chose_customers(row.venture_id)
                             and "validation" not in journey_choices(row.venture_id, session=session)["skipped"]
                             and not office_hours.asked_before(session, row.id)
                             and not _questionnaire_interrupted(session, payload))
            if new_questions:
                advance(session, row, "clarifying")
                advance(session, row, "validating")
                # idea_revisions is append-only in Postgres: never mark the revision. run() starts the
                # question stage from this job's own clarification run (2026-10-24 ProgrammingError).
            elif (payload.get("reply_in_chat") or ((_chat_entry_deferred(session, row.workspace_id, row.venture_id) or chat_entry_goal(session, row.workspace_id, row.venture_id))
                    and not _questionnaire_interrupted(session, payload))):
                from core.copy import strip_emdash

                references = []
                if saved_turn.kind == "question":
                    body, actions = _question_reply(saved_turn.text, question, saved_turn.id,
                                                   facts=current.get("founder_facts", {}), context=current)
                    if row.questions_asked == 1 and not is_local():
                        body = "**Help me understand your idea better**\n\n" + body
                    if parsed.message.strip() and not is_local():
                        head, _, rest = body.partition("\n\n") if row.questions_asked == 1 else ("", "", body)
                        body = (head + "\n\n" if head else "") + parsed.message.strip() + "\n\n" + rest
                else:
                    body = (saved_turn.text + _understanding_summary(_loads(rev.structured))
                            + "\n\nYour idea is ready. Review it, then validate it when you're happy.")
                    from app.chat.idea_proposal import proposal_actions

                    actions = proposal_actions(row.id, rev.id)
                    if personal:  # personal projects skip GStack: the Idea page holds the final confirm
                        actions[0] = {"label": "Looks right", "href": f"/v/{row.venture_id}/idea", "style": "primary"}
                    from app.knowledge.pmf import idea_references

                    references = idea_references(structured)
                key = "chat:" + hashlib.sha256(json.dumps(
                    [row.venture_id, "assistant", f"idea-turn:{saved_turn.id}"],
                    separators=(",", ":")
                ).encode()).hexdigest()
                from app.chat.store import request_citations
                message = ChatMessage(id=new_id("msg"), venture_id=row.venture_id,
                    workspace_id=row.workspace_id, conversation_id=_reply_conversation_id(session, payload),
                    role="assistant", kind="idea_clarification_ready", body_md=_manager_body(speaker, strip_emdash(body)),
                    actions_json=json.dumps(in_chat(actions)) if actions else None,
                    citations_json=json.dumps(request_citations(references, request_id=payload.get("_chat_request_nonce"))),
                    idempotency_key=key, created_at=utcnow())
                pending = session.scalar(select(ChatMessage).where(
                    ChatMessage.workspace_id == row.workspace_id, ChatMessage.venture_id == row.venture_id,
                    ChatMessage.conversation_id == message.conversation_id, ChatMessage.role == "assistant",
                    ChatMessage.created_at >= row.created_at,
                    ChatMessage.kind == "entry_pending").order_by(ChatMessage.created_at.desc()).limit(1)) if is_local() and row.questions_asked == 1 else None
                if pending:
                    pending.kind, pending.body_md = message.kind, message.body_md
                    pending.actions_json, pending.citations_json = message.actions_json, message.citations_json
                else:
                    session.add(message)
            receipt.state = "completed"
        receipt.updated_at = utcnow()
        session.commit()
        from app.ventures.state_brief import invalidate_venture_state_brief
        invalidate_venture_state_brief(row.venture_id)


_SUMMARY_FIELDS = (("problem", "Problem"), ("customer", "Customer"), ("offer", "What it does"),
                   ("workflow", "How it works"), ("current_workaround", "How it is handled today"),
                   ("why_now", "Why now"), ("evidence", "What you have learned"),
                   ("first_user_path", "First users"), ("pricing_hypothesis", "Pricing"),
                   ("desired_outcome", "Goal"))


def _summary_text(value: str) -> str:
    from app.idea.agent import answer_metadata, founder_description
    from app.intake.smart import _PREFILL_KEYS
    labels = {alias for aliases in _PREFILL_KEYS.values() for alias in aliases}
    source = founder_description(answer_metadata(value)["founder_text"]).strip()
    source = re.sub(r"^([\w ]+):\s*(.*)", lambda match: match[2] if match[1].casefold() in labels else match[0], source, count=1)
    lines = [line.strip() for line in source.splitlines()
             if not (match := re.match(r"^([\w ]+):", line.strip())) or match[1].casefold() not in labels]
    text = " ".join(lines).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    # "Could you help me shape this?" is the request, not the product.
    return " ".join(sentence for sentence in sentences if not sentence.endswith("?")) or sentences[0]


def _understanding_summary(u) -> str:
    """The idea as understood, as bullets the founder can check (empty fields are skipped)."""
    fields = u if isinstance(u, dict) else vars(u)
    # A field that only repeats the summary or an earlier field adds nothing for the founder to check.
    seen = set()
    lines = []
    for key, label in _SUMMARY_FIELDS:
        value = _summary_text(fields.get(key) or "")
        normalized = value.casefold().rstrip(" .!?")
        if value and normalized not in seen:
            lines.append(f"- **{label}:** {value}")
            seen.add(normalized)
    for key, label in (("requirements", "Needs"), ("constraints", "Limits")):
        values = []
        for item in fields.get(key) or []:
            value = _summary_text(item)
            normalized = value.casefold().rstrip(" .!?")
            if value and normalized not in seen:
                values.append(value)
                seen.add(normalized)
        if values:
            lines.append(f"- **{label}:** {'; '.join(values)}")
    summary = _summary_text(fields.get("summary") or "")
    if summary and summary.casefold().rstrip(" .!?") not in seen:
        lines.insert(0, summary)
    return ("\n\n" + "\n".join(lines)) if lines else ""


def _source_facts(snapshot: dict) -> dict:
    from app.ventures.founder_facts import ALIASES
    return {**{ALIASES.get(field, field): fact for field, fact in snapshot.get("prefill_facts", {}).items()},
        **{field: fact for field, fact in snapshot.get("founder_facts", {}).items() if fact.get("value")}}


def _founder_messages(snapshot: dict) -> list[str]:
    from app.idea.agent import answer_metadata
    from app.intake.smart import _founder_data
    from app.intake.website import changing_idea
    from app.ventures.current_idea import _is_idea_control

    original = _founder_data(snapshot.get("founder_idea", snapshot.get("original_idea")) or "")
    answered = [pair for pair in snapshot.get("answered_questions", []) if not pair.get("reused")]
    sources = [] if _is_idea_control(original) else [original]
    for turn in snapshot.get("conversation", []):
        if turn["role"] != "founder" or turn.get("kind") == "chip" or turn.get("reused"):
            continue
        text = (turn.get("founder_text", "") if turn.get("provenance") == "suggested_choice"
                else answer_metadata(turn["text"])["founder_text"])
        if _is_idea_control(text):
            continue
        if changing_idea(text):
            sources = []
        source = _founder_data(text)
        if source not in sources:
            sources.append(source)
    for pair in answered:
        text = pair.get("founder_text")
        if text and not _is_idea_control(text) and _founder_data(text) not in sources:
            sources.append(_founder_data(text))
    offer = snapshot.get("answered_fields", {}).get("offer")
    offer = answer_metadata(offer)["founder_text"] if isinstance(offer, str) else ""
    if offer and not _is_idea_control(offer) and _founder_data(offer) not in sources:
        sources.append(_founder_data(offer))
    return sources


def _known_fields(snapshot: dict, *, extracted: dict | None = None) -> dict:
    from app.idea.agent import Understanding, answer_metadata, answer_prefill, founder_audience, founder_description
    from app.gstack.office_hours import confirms_summary, is_skip
    from app.ventures.founder_facts import ALIASES, policy, usable_problem
    from app.secrets.detect import looks_like_secret
    from app.intake.smart import _founder_data, _prefill
    from app.ventures.startup_review import _known
    understanding = snapshot.get("understanding", {})
    facts = _source_facts(snapshot)
    sources = _founder_messages(snapshot)
    source_fields = {_founder_data(text): turn.get("field")
        for turn in [*snapshot.get("conversation", []), *snapshot.get("answered_questions", [])]
        if turn.get("role", "founder") == "founder" and not turn.get("reused")
        and (text := turn.get("founder_text") or answer_metadata(turn.get("text", ""))["founder_text"])}
    prefills = {}
    customer_sources = []
    for source in sources:
        parsed = _prefill(source)
        customer = answer_prefill(source, source_fields.get(source)).get("customer")
        if customer:  # An answer to who it is for, or an explicit correction inside any answer.
            parsed["customer"] = customer
        elif prefills.get("customer"):  # Later answers describe reach or scope.
            parsed.pop("customer", None)
        prefills.update(parsed)
        if parsed.get("customer"):
            customer_sources.append(source)
    supported = {field: value for field, value in (extracted or {}).items()
        if isinstance(value, str) and _known(value) and any(value in source for source in sources)
        and (field != "customer" or any(value in source for source in customer_sources))
        and not any(value in assumption for assumption in (extracted or {}).get("assumptions", []))
        and (field not in {"offer", "customer"} or core_answer_status(value, 0, field=field, assessed=True) == (True, False))}
    known = {field: value for field, value in {**prefills, **supported}.items()
        if not any(value in assumption for assumption in understanding.get("assumptions", []))
        and (not facts.get(ALIASES.get(field, field), {}).get("value") or policy(facts, field) == "skip")}
    for field, value in understanding.items():
        fact = facts.get(ALIASES.get(field, field), {})
        if (isinstance(value, str) and _known(value)
                and not any(value in assumption for assumption in understanding.get("assumptions", []))
                and not (fact.get("value") and policy({ALIASES.get(field, field): fact}, field) != "skip")):
            known[field] = value
    for field in Understanding.model_fields:
        if policy(facts, field) == "skip":
            known[field] = facts[ALIASES.get(field, field)]["value"]
    known.update({field: value for field, value in snapshot.get("answered_fields", {}).items()
        if facts.get(ALIASES.get(field, field), {}).get("source") != "review"
        and (not isinstance(value, str) or not value.strip()
            or not is_skip(value) and answer_metadata(value)["founder_text"])})
    if policy(facts, "customer") == "skip":
        try:
            fact_at = as_utc(datetime.fromisoformat(snapshot.get("founder_fact_updated_at", {})["audience"]))
            answer_times = snapshot.get("answered_field_updated_at", {})
            answer_at = as_utc(datetime.fromisoformat(answer_times.get("customer") or answer_times["audience"]))
        except (KeyError, TypeError, ValueError):
            pass
        else:
            if fact_at > answer_at:
                known["customer"] = facts["audience"]["value"]
    if "offer" not in known and "offer" in snapshot.get("question_fields_asked", []):
        offer = next((source for source in reversed(sources) if core_answer_status(source, 0, field="offer") == (True, False)), None)
        if offer:
            known["offer"] = offer[:2000]
    latest = next((turn for turn in reversed(snapshot.get("conversation", []))
                   if turn["role"] == "founder" and turn.get("kind") != "chip"), {})
    field = latest.get("field")
    answer = (latest.get("founder_text", "") if latest.get("provenance") == "suggested_choice"
              else answer_metadata(latest.get("text", ""))["founder_text"])
    if _known(answer) and not confirms_summary(answer):
        explicit = _prefill(answer, explicit_only=True)
        customer = answer_prefill(answer, field).get("customer") or explicit.get("customer")
        if not latest.get("reused") and customer:
            known["customer"] = customer
        if field != "customer" and isinstance(field, str) and isinstance(getattr(Understanding(), field, None), str):
            known[field] = explicit.get(field) or answer[:2000]
    if snapshot.get("project_intent") == "personal" and "workflow" not in known:
        # shortcut: English personal-purpose clauses, use grounded model extraction for other phrasing.
        for source in sources:
            task = re.search(r"\bfor\s+(?:myself|me|my\s+[\w'-]+(?:\s+[\w'-]+){0,3})\s+to\s+([^.!?\n;]+)", source, re.I)
            if task and core_answer_status("It can " + task[1], 0, field="offer") == (True, False):
                known["workflow"] = task[1].strip()[:2000]
                break
    offer_answer = next((pair for pair in reversed(snapshot.get("answered_questions", []))
        if pair["field"] == "offer" and pair.get("founder_text") and not pair.get("reused")), None)
    if offer_answer:
        known["offer"] = _prefill(offer_answer["founder_text"]).get("offer") or offer_answer["founder_text"]
    if isinstance(known.get("customer"), str) and not looks_like_secret(known["customer"]):
        known["customer"] = founder_audience(known["customer"])
    if is_local():
        from app.idea.audience import local_audience
        customer = local_audience(snapshot, known.get("customer", ""), extracted=extracted)
        if customer:
            known["customer"] = customer
        else:
            known.pop("customer", None)
    if isinstance(known.get("problem"), str):
        known["problem"] = answer_prefill(known["problem"], "problem").get("problem", known["problem"])
    known = {field: value for field, value in known.items() if isinstance(value, str) and _known(value)
        and not looks_like_secret(value) and (field != "problem" or usable_problem(value))
        and (field not in {"offer", "customer"} or core_answer_status(value, 0, field=field,
            assessed=supported.get(field) == value or field in snapshot.get("answered_fields", {})
                or policy(facts, field) == "skip") == (True, False))}
    # Checks above read the joined answers; what leaves here can be shown to the founder.
    known = {field: founder_description(value) for field, value in known.items()}
    for field in ("requirements", "constraints"):
        values = snapshot.get("answered_fields", {}).get(field,
            (extracted or {}).get(field) or understanding.get(field))
        if isinstance(values, list) and values and all(
                isinstance(value, str) and _known(value) and not looks_like_secret(value)
                and any(value in source for source in sources)
                and not any(value in assumption for assumption in understanding.get("assumptions", [])) for value in values):
            known[field] = values
    return known


def _core_clarification(snapshot: dict, result: Clarification) -> Clarification:
    from app.idea.agent import answered_field, answer_metadata
    from app.ventures.startup_review import _known
    from app.ventures.current_idea import _is_idea_control
    from app.ventures.founder_facts import ALIASES, policy, confirmation, filter_options, contradicts
    facts = _source_facts(snapshot)
    question_fields_asked = snapshot.get("question_fields_asked", [])
    known = _known_fields(snapshot, extracted=result.understanding.model_dump())
    for field, definition in Understanding.model_fields.items():
        limit = next((rule.max_length for rule in definition.metadata if hasattr(rule, "max_length")), None)
        if limit and isinstance(known.get(field), str):
            known[field] = known[field][:limit]
    sources = _founder_messages(snapshot)
    from app.idea.audience import Audience, validated_audience
    if result.understanding.customer_extraction is not None and not validated_audience(result.understanding.customer_extraction, sources):
        result.understanding.customer_extraction = Audience(text="", kind="unknown", quote="")
    offer = snapshot.get("answered_fields", {}).get("offer")
    if "offer" not in known and (isinstance(offer, str) and answer_metadata(offer)["founder_text"]
            or any(pair.get("field") == "offer" and pair.get("founder_text") for pair in snapshot.get("answered_questions", []))):
        result.understanding.offer = ""
    if not snapshot.get("force_propose"):
        for field in ("offer", "customer", "workflow", "pricing_hypothesis"):
            value = getattr(result.understanding, field)
            if (field not in known and field not in snapshot.get("website_answers", {})
                    and not snapshot.get("understanding", {}).get(field) and value
                    and (field == "customer" or not any(value in source for source in sources))):
                setattr(result.understanding, field, "")
    for field in ("requirements", "constraints"):
        if field not in known and not snapshot.get("understanding", {}).get(field):
            setattr(result.understanding, field, [])
    unresolved = set(snapshot.get("answered_fields", {})) - set(known)
    question_fields_asked = [field for field in question_fields_asked if field not in unresolved]
    facts = {field: fact for field, fact in facts.items() if field not in {ALIASES.get(key, key) for key in unresolved}}
    snapshot = {**snapshot, "question_fields_asked": list(dict.fromkeys([*question_fields_asked, *(field for field in known if field != "summary")])),
        "website_answers": {field: value for field, value in snapshot.get("website_answers", {}).items()
            if field not in snapshot.get("answered_fields", {})}}
    for field, value in known.items():
        if (field in type(result.understanding).model_fields and field not in snapshot.get("website_answers", {})
                and (isinstance(getattr(result.understanding, field), str)
                    or isinstance(value, list) and isinstance(getattr(result.understanding, field), list))):
            inferred = getattr(result.understanding, field)
            replace_known = (not inferred or isinstance(inferred, str) and not _known(inferred)
                    or field in snapshot.get("answered_fields", {})
                    and (field != "offer" or value != snapshot.get("original_idea")
                        and not (value == snapshot.get("founder_idea") == snapshot.get("understanding", {}).get("offer")))
                    or policy(facts, field) == "skip" or isinstance(value, list))
            if replace_known or field == "customer":
                if field in {"offer", "customer"} and inferred and inferred != value:
                    summary = result.understanding.summary.casefold()
                    if (replace_known and value != snapshot.get("understanding", {}).get(field)
                            and (not known.get("offer") or known["offer"].casefold() not in summary)
                            or inferred.casefold() in summary or contradicts(result.understanding.summary, facts)):
                        result.understanding.summary = known.get("offer") or value
                setattr(result.understanding, field, value)
    result.understanding.proposed_options = [option for option in result.understanding.proposed_options
        if not contradicts(" ".join(str(value) for value in option.model_dump().values()), facts)]
    completed_facts = [field for field in type(result.understanding).model_fields
                       if field in known and isinstance(getattr(result.understanding, field), str) and policy(facts, field) == "skip"]
    snapshot = {**snapshot, "question_fields_asked": list(dict.fromkeys([*snapshot.get("question_fields_asked", []), *completed_facts]))}
    for field in completed_facts:
        if field in type(result.understanding).model_fields:
            result.understanding = result.understanding.model_copy(update={field: known[field]})
    asked = None
    if result.question:
        asked = answered_field(result.question.text, result.question.field, result.question.options)
        from app.idea.question_history import question_field
        if question_field(result.question.model_dump()) == "offer" and policy(facts, asked) != "confirm":
            asked = "offer"
        options = filter_options(result.question.options, facts)
        recommended = result.question.recommended_option in options and not contradicts(result.question.recommendation_reason, facts)
        result.question = result.question.model_copy(update={"options": options,
            "recommended_option": result.question.recommended_option if recommended else "",
            "recommendation_reason": result.question.recommendation_reason if recommended else ""})
        if (asked in known or asked in snapshot.get("website_answers", {})
                or asked in snapshot.get("question_fields_asked", []) and asked != snapshot.get("active_core_field")
                or policy(facts, asked) == "skip"):
            from app.idea.question_history import unanswered_question
            following = unanswered_question(result.question.model_dump(), snapshot.get("answered_questions", []), snapshot=snapshot, known=known) if is_local() and not snapshot.get("force_propose") else None
            result = result.model_copy(update={"action": "question", "question": Question.model_validate(following)}) if following else result.model_copy(update={"action": "proposal", "question": None})
        elif policy(facts, asked) == "confirm":
            result.question = result.question.model_copy(update={"text": confirmation(facts, asked), "options": [], "recommended_option": "", "recommendation_reason": ""})
            return result
    field = snapshot.get("active_core_field")
    if field in known or policy(facts, field) == "skip":
        field = None
    website = snapshot.get("website_answers", {})
    completed = set(snapshot.get("question_fields_asked") or []) | set(website)
    website_customer = website.get("customer")
    if isinstance(website_customer, dict):
        website_customer = website_customer.get("answer")
    customer = website_customer or known.get("customer")
    if field != "customer" and customer and core_answer_status(customer, 0, field="customer") == (True, False):
        completed.add("customer")
        if not result.understanding.customer:
            result.understanding.customer = customer
    if field in website:
        field = None
    if not field and "offer" not in completed and (snapshot.get("founder_idea", snapshot.get("original_idea")) or "").strip():
        advance, assumed = core_answer_status(snapshot.get("founder_idea", snapshot.get("original_idea")) or "", 0, field="offer")
        if not advance or assumed:
            field = "offer"
    if field in {"offer", "customer"}:
        completed.discard(field)
        answers = [turn for turn in snapshot.get("conversation", []) if turn["role"] == "founder"
            and turn.get("kind") != "chip" and not _is_idea_control(turn["text"])]
        answer = answers[-1]["text"] if answers else snapshot.get("founder_idea", snapshot.get("original_idea")) or ""
        if answers and answers[-1].get("provenance") == "suggested_choice":
            answer = ASSUMED + answers[-1]["suggested_text"] + "\n" + answers[-1]["founder_text"]
        followups = max(0, snapshot.get("core_question_counts", {}).get(field, 0) - (1 if field == "customer" else 0))
        advance, assumed = core_answer_status(answer, followups, field=field)
        if snapshot.get("force_propose") and not advance:
            advance, assumed = True, True
        if not advance and not snapshot.get("force_propose"):
            from app.idea.agent import broad_audience
            broad = field == "customer" and broad_audience(answer)
            options = result.question.options if result.question and result.question.field == field else []
            if broad:
                options = list(dict.fromkeys(option for option in options if core_answer_status(option, 0, field="customer") == (True, False)))[:3]
            question = core_followup(field, followups, answer=answer)
            return result.model_copy(update={"action": "question", "question": Question(
                text=question, field=field, why="Clarify your answer before moving on", options=options if len(options) >= 2 else []),
                "message": "Your answer is saved. Let's clarify this part before moving on."})
        completed.add(field)
        if assumed:
            from app.idea.agent import answer_metadata
            words = answer_metadata(answer)["founder_text"]
            assumption = ASSUMED + f"{field} is unclarified; " + ("using founder words: " + words[:1600] if words else "not answered yet.")
            result.understanding.assumptions = list(dict.fromkeys(result.understanding.assumptions + [assumption]))[:15]
            result.understanding.open_questions = list(dict.fromkeys(result.understanding.open_questions + [f"Clarify {field}"]))[:15]
            setattr(result.understanding, field, "")
        elif answer and not result.understanding.model_dump().get(field):
            from app.idea.agent import answer_metadata
            setattr(result.understanding, field, answer_metadata(answer)["founder_text"][:2000])
        known_customer = known.get("customer")
        if known_customer and core_answer_status(known_customer, 0, field="customer") == (True, False):
            completed.add("customer")
    if (field == "offer" or website or "offer" in known or asked == "offer" and result.case == "know" and "offer" in completed) and snapshot.get("project_intent") != "personal" and "customer" not in completed and not snapshot.get("force_propose"):
        return result.model_copy(update={"action": "question", "question": Question(
            text=confirmation(facts, "customer") if policy(facts, "customer") == "confirm" else "Who is it for?",
            field="customer", why="Identify the first audience", options=result.question.options if result.question and result.question.field == "customer" else []),
            "message": "Your idea is saved. Let's identify who it would help."})
    if result.question and answered_field(result.question.text, result.question.field, result.question.options) in completed:
        return result.model_copy(update={"action": "proposal", "question": None,
            "message": "Here is what I took from your answers. Tell me if I got anything wrong."})
    return result


def _enough_answers(snapshot: dict, question: Question | None = None) -> bool:
    from app.ventures.founder_facts import confirmation, policy
    facts = _source_facts(snapshot)
    if question and any(policy(facts, field) == "confirm" and question.text == confirmation(facts, field) for field in facts):
        return False
    known = _known_fields(snapshot)
    website = {field: value for field, value in snapshot.get("website_answers", {}).items()
        if field not in snapshot.get("answered_fields", {})}
    return snapshot["project_intent"] == "personal" or all(
        field in known or isinstance(website.get(field), dict)
        and website[field].get("source") == "From your website" and website[field].get("status") == "answered"
        and core_answer_status(website[field].get("answer", ""), 0, field=field) == (True, False)
        for field in ("offer", "customer"))


def _manager_body(speaker: dict, body: str) -> str:
    from app.org.characters import stamp_speaker

    return stamp_speaker(body, speaker)


def _speaker(venture_id: str) -> dict:
    # Resolved before any write session opens: manager_name reads the DB itself.
    from app.org.manager import manager_speaker

    return manager_speaker(venture_id)


def _question_reply(text: str, question: dict | None, question_id: str | None = None, *, facts: dict | None = None, idea: str = "", context: dict | None = None) -> tuple[str, list[dict] | None]:
    """Chat body and one reply button per option of a saved clarifier question."""
    if not (text or "").strip():
        from app.idea.service import fallback_first_question
        text = fallback_first_question("startup")
    facts = {**_source_facts(context or {}), **(facts or {})}
    from app.clarify.dilemmas import ground_question
    if question is not None:
        grounded = ground_question({**question, "text": text}, {**(context or {}), "founder_facts": facts})
        text = grounded.pop("text")
        question.update(grounded)
    from app.gstack.office_hours import SKIP
    skip_actions = [{"label": SKIP, "action": "chat.reply", "text": SKIP, "style": "secondary",
                     "payload": {"text": SKIP, "question_id": question_id}}] if question_id else []
    opts = [o.strip() for o in (question or {}).get("options") or [] if isinstance(o, str) and o.strip()][:4]
    from app.idea.agent import fitting_options, has_evidence_options, is_evidence_question, is_timing_question
    if not opts and not any((question_id, idea, facts, context, (question or {}).get("field"), (question or {}).get("gstack_id"), is_timing_question(text))):
        return text + "\n\nReply here with your answer.", None
    if opts and not (question or {}).get("contextual_evidence") and (is_evidence_question(text, opts) or has_evidence_options(opts) or is_evidence_question("", opts)):
        opts = fitting_options(text, opts)
    from app.gstack.office_hours import _unsupported_claim
    # Same rule as the idea-check questions: no statistic the founder never gave.
    from app.ventures.founder_facts import filter_options
    from app.clarify.dilemmas import _founder_request
    stated = {**(context or {}), "idea": "\n".join(filter(None, (idea, (context or {}).get("idea"), (context or {}).get("original_idea")))),
              "founder_facts": facts or {}}
    opts = [o for o in filter_options(opts, facts or {}) if not _unsupported_claim(o, stated) and not _founder_request(o)]
    if (question or {}).get("free_text_only") or (question or {}).get("detail_pending"):
        opts = []
    if not opts:
        from app.clarify.dilemmas import contextual_choices
        if not ((question or {}).get("free_text_only") or (question or {}).get("detail_pending")):
            try:
                question_type = (question or {}).get("gstack_id")
                if not question_type:
                    from app.idea.agent import answered_field
                    field = answered_field(text, (question or {}).get("field"))
                    question_type = ("Q3" if is_timing_question(text) else "Q1" if is_evidence_question(text, []) else
                                     "Q2" if field == "current_workaround" else "")
                generated = contextual_choices(question_type, stated)
                if generated:
                    opts = generated
                    if question is not None and question_type == "Q1":
                        question["contextual_evidence"] = True
            except Exception:
                opts = []
        opts = [option for option in filter_options(opts, facts or {})
                if not _unsupported_claim(option, stated) and not _founder_request(option)]
    if not (question or {}).get("contextual_evidence") and not is_evidence_question(text):
        # shortcut: English evidence-status phrases only, use typed option intent for other languages.
        opts = [option for option in opts if not re.fullmatch(
            r"(?:(?:i|we)\s+have\s+)?(?:no|some)\s+(?:(?:direct|customer)\s+)?evidence(?:\s+(?:yet|to\s+describe))?[.!]?",
            option, re.IGNORECASE)]
    if question is not None:
        question["options"] = opts
    if not opts:
        if question is not None:
            question.update(free_text_only=True, allow_free_text=True, requires_detail=[],
                            recommended_option="", recommendation_reason="")
        return text + "\n\nType your own answer below.", skip_actions or None
    recommended, reason = safe_recommendation(text, opts, (question or {}).get("recommended_option") or "",
                                            (question or {}).get("recommendation_reason") or "", facts=facts)
    reason = "" if _unsupported_claim(reason, stated) else re.sub(r"\b[Tt]he founder\b", "you", reason)
    if (question or {}).get("contextual_evidence"):
        recommended, reason = "", ""
    body = text + "\n\nSelect any that fit, then submit, or type or speak your own answer below."
    from app.clarify.dilemmas import detail_choices
    requires_detail = detail_choices(opts, (question or {}).get("requires_detail") or [])
    return body, [
        {"label": o, "action": "chat.reply", "text": o, "style": "secondary",
         "payload": {"text": o, "question_id": question_id, "recommended": o == recommended,
                     "requires_detail": o in requires_detail,
                     "detail_prompt": (question or {}).get("detail_prompt") or "Describe your example: who, when, what happened?",
                     "recommendation_reason": reason if o == recommended else ""}}
        for o in opts] + skip_actions


def post_fallback_question(payload: dict[str, Any], *, run_id: str | None = None) -> bool:
    """Recover a rejected chat answer from saved facts, without another provider call."""
    from app.idea.service import _loads, _next_turn_number, _project_intent, fallback_first_question
    from app.idea.agent import existing_business
    from app.ventures.stage import has_existing_customers
    speaker = _speaker(payload["venture_id"])
    with db()() as session:
        if run_id is not None or payload.get("_chat_conversation") is not None:
            _fence(session, payload)
        row = session.get(IdeaSession, payload["session_id"], with_for_update=True)
        if (not row or (row.workspace_id, row.venture_id) != (payload["workspace_id"], payload["venture_id"])
                or row.state in {"confirmed", "abandoned"} or row.current_revision_id != payload.get("revision_id")
                or row.questions_asked and run_id is None):
            return False
        receipt = session.get(IdeaClarificationRun, run_id, with_for_update=True) if run_id else None
        if run_id and (not receipt or receipt.state != "responded" or receipt.error_code != "clarification_invalid_output"
                or (receipt.job_id, receipt.session_id, receipt.workspace_id, receipt.venture_id, receipt.revision_id)
                != (payload["_job_id"], row.id, row.workspace_id, row.venture_id, row.current_revision_id)):
            raise FatalError("clarification_scope_mismatch")
        if unresolved_clarification(session, row):
            return False
        intent = _project_intent(session, row.workspace_id, row.venture_id)
        text = fallback_first_question(intent) if intent == "personal" else core_followup("customer", 0)
        revision = session.get(IdeaRevision, row.current_revision_id)
        understanding = _loads(revision.structured if revision else None)
        completed = set(understanding.get("_onboarding_completed", [])) | set(understanding.get("_website_answers", {}))
        field = "workflow" if intent == "personal" else "customer"
        venture = session.get(Venture, row.venture_id)
        turns = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == row.id,
            ClarifierTurn.role == "founder").order_by(ClarifierTurn.turn)).all()
        snapshot = {"original_idea": venture.raw_idea if venture else "", "understanding": understanding,
            "project_intent": intent, "has_customers": has_existing_customers(session, row.venture_id),
            "answered_fields": understanding.get("_answered_fields", {}),
            "prefill_facts": understanding.get("_prefill_facts", {}),
            "conversation": [{"role": turn.role, "kind": turn.kind, "text": turn.text,
                **_loads(turn.options)} for turn in turns]}
        known = _known_fields(snapshot)
        live = existing_business(snapshot)
        completed.update(known)
        if known:
            from app.idea.service import _append_revision, REQUIRED
            stated = {key: value for key, value in known.items() if key in (*REQUIRED, "requirements", "constraints")}
            understanding.update(stated)
            understanding.setdefault("_answered_fields", {}).update({key: value for key, value in stated.items()
                if all(any(part in source for source in _founder_messages(snapshot)) for part in (value if isinstance(value, list) else [value]))})
            revision = _append_revision(session, row, understanding, source="founder", summary="Read from your earlier answers")
        offer = understanding.get("offer") or (venture.raw_idea if venture else "")
        if intent != "personal" and "offer" not in completed and (understanding.get("_website_answers")
                or core_answer_status(offer, 0, field="offer") != (True, False)):
            field, text = "offer", core_followup("offer", 0)
        elif intent != "personal" and "customer" in completed:
            field, text = "workflow", ("What do you want to improve in the product your customers already use? Describe one task you want help with."
                if live else "What should the first version do? Describe one task it should help with.")
        if field in completed:
            following = next(((key, question) for key, question in (
                ("workflow", "How do your customers use your product today?" if live else "How would someone use the first version, from start to finish?"),
                ("pricing_hypothesis", "How do you charge your customers today?" if live else "How would you charge for it?"),
                ("first_user_path", "How do your customers find you today?" if live else "How would you reach the first people who would use it?"),
                ("desired_outcome", "What result do you want from this improvement?" if live else "What would a good first result look like?"),
                ("constraints", "What must stay reliable as you make this improvement?" if live else "Is there anything the first version must avoid?"),
                ("problem", "What problem do you want to fix for your customers?" if live else "What problem would it solve?"),
                ("requirements", "What else do you need for this improvement?" if live else "Is there anything else the first version needs to do?"))
                if key not in completed and not (intent == "personal" and key in {"pricing_hypothesis", "first_user_path"})), None)
            field, text = following if following else (None, "Your answers are saved. Review this short summary of what you shared and correct anything I missed.")
        if row.questions_asked and row.questions_asked >= row.question_budget:
            field, text = None, "Your answers are saved. Review this short summary of what you shared and correct anything I missed."
        question = {"field": field, "options": []} if field else None
        from app.idea.question_history import CONFIRM, answered_questions, repeated_question, unanswered_question
        if question and repeated_question({**question, "text": text}, answered_questions(session, row.id)):
            question = unanswered_question({**question, "text": text}, answered_questions(session, row.id))
            field, text = (question["field"], question.pop("text")) if question else (None, CONFIRM)
        turn_id = new_id("ict")
        if field:
            body, actions = _question_reply(text, question, turn_id, idea=venture.raw_idea if venture else "")
            row.questions_asked += 1
        else:
            from app.core.fsm import advance
            from app.chat.idea_proposal import proposal_actions
            if row.state == "open":
                advance(session, row, "recon")
            if row.state == "recon" or row.state in {"reviewing", "proposing", "validating"}:
                advance(session, row, "clarifying")
            advance(session, row, "proposing")
            advance(session, row, "reviewing")
            body = text + _understanding_summary(understanding)
            actions = in_chat(proposal_actions(row.id, revision.id))
        turn = ClarifierTurn(id=turn_id, session_id=row.id, workspace_id=row.workspace_id,
            venture_id=row.venture_id, turn=_next_turn_number(session, row.id), role="agent",
            kind="question" if field else "proposal", text=text, options=json.dumps(question) if question else None,
            revision_id=revision.id if revision else None, created_at=utcnow())
        session.add(turn)
        from core.copy import strip_emdash
        from app.chat.store import request_citations
        session.add(ChatMessage(id=new_id("msg"), venture_id=row.venture_id, workspace_id=row.workspace_id,
            conversation_id=_reply_conversation_id(session, payload),
            role="assistant", kind="idea_clarification_ready",
            body_md=_manager_body(speaker, strip_emdash((("**What do you want to improve next?**\n\n" if live
                else "**Help me understand your idea better**\n\n") if field else "") + body)),
            actions_json=json.dumps(actions),
            citations_json=json.dumps(request_citations(request_id=payload.get("_chat_request_nonce"))),
            idempotency_key="chat:" + hashlib.sha256(json.dumps(
                [row.venture_id, "assistant", f"idea-turn:{turn.id}"], separators=(",", ":")).encode()).hexdigest(),
            created_at=utcnow()))
        if receipt is not None:
            receipt.state, receipt.updated_at, receipt.error_code = "completed", utcnow(), None
        session.commit()
    return True


def run(payload: dict[str, Any]) -> None:
    with turn_progress(payload):
        _run(payload)


def _run(payload: dict[str, Any]) -> None:
    run_id = _claim(payload)
    if run_id is not None:
        receipt = _receipt(run_id)
        if receipt.state != "responded":
            _perform(run_id, payload)
        if _receipt(run_id).state == "responded":
            _apply(run_id, payload)
    with db()() as session:
        _fence(session, payload)
        idea = session.get(IdeaSession, payload["session_id"])
        revision = session.get(IdeaRevision, idea.current_revision_id) if idea else None
        from app.gstack import office_hours
        own_run = session.scalar(select(IdeaClarificationRun.id).where(
            IdeaClarificationRun.job_id == payload["_job_id"], IdeaClarificationRun.session_id == payload["session_id"]))
        if not revision or idea.state != "validating" or not own_run or office_hours.asked_before(session, idea.id):
            return
        questions_payload = {**payload, "revision_id": revision.id, "stage": "gstack_questions"}
    from app.idea.validation import _run_questions
    _run_questions(questions_payload)
