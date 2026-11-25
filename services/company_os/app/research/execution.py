"""Persisted operation receipts on the existing dossier, fenced by a durable job.

Dossier IDs derive from the job ID. The row lock serializes each small receipt
update; external I/O is outside transactions. Private runtime data never enters
API serializers. A calling receipt is ambiguous after a crash, never a retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.core.jobs import FatalError, RetryableError
from app.core.operation_receipts import OperationJournal
from app.db.models import Job, Venture
from app.db.supymem_models import IdeaClarificationRun, IdeaRevision, IdeaSession, ResearchDossier
from app.state import db
from core.clock import as_utc, utcnow

COST_CEILING_MICRO = 2_000_000


def identifier(job_id: str) -> str:
    return "rsd_" + hashlib.sha256(job_id.encode()).hexdigest()[:32]


def cancel_requested(payload: dict) -> bool:
    job_id = payload.get("_job_id")
    if not job_id:
        return False
    with db()() as session:
        return bool(session.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def fence(session, payload):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if not job or job.status != "leased" or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt"):
        raise FatalError("research_lease_lost")
    if job.kind == "idea.clarify_turn" and payload.get("stage") == "gstack_questions":
        from app.idea.runner import _fence
        _fence(session, payload)
        revision = session.get(IdeaRevision, payload.get("revision_id"))
        if (not revision or revision.session_id != payload.get("session_id")
                or revision.session_id != json.loads(job.payload_json).get("session_id")
                or not session.scalar(select(IdeaClarificationRun.id).where(
                    IdeaClarificationRun.job_id == job.id, IdeaClarificationRun.session_id == revision.session_id))):
            raise FatalError("research_scope_mismatch")
        return job
    if job.kind not in {"research.deep", "idea.validate"} or (job.workspace_id, job.venture_id) != (payload.get("workspace_id"), payload.get("venture_id")):
        raise FatalError("research_scope_mismatch")
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(kind=job.kind, workspace_id=job.workspace_id,
                              venture_id=job.venture_id, payload=json.loads(job.payload_json)))
    return job


def claim(payload: dict, *, kind: str, budget: int, prior_art_enabled: bool = False) -> str:
    did = identifier(payload["_job_id"])
    with db()() as session:
        job = fence(session, payload)
        idea = session.get(IdeaSession, payload.get("session_id"), with_for_update=True)
        rev = session.get(IdeaRevision, payload.get("revision_id"))
        if not idea or not rev or rev.session_id != idea.id or (idea.workspace_id, idea.venture_id) != (payload["workspace_id"], payload["venture_id"]):
            raise FatalError("research_scope_mismatch")
        if kind == "deep":
            from app.research.context import current_input
            from app.research.errors import ResearchPreflightBlocked
            try:
                current_idea, current_revision_id = current_input(session, idea.venture_id)
            except ResearchPreflightBlocked as exc:
                raise FatalError(str(exc)) from exc
            if (current_idea.id, current_revision_id) != (idea.id, rev.id):
                raise FatalError("research_inputs_changed")
        row = session.get(ResearchDossier, did, with_for_update=True)
        if row is None and kind == "validation" and (idea.state in {"confirmed", "abandoned"} or idea.current_revision_id != rev.id):
            raise FatalError("validation_revision_superseded")
        if row is None:
            snapshot = json.loads(rev.structured or "{}")
            answer_provenance = snapshot.get("_answer_provenance") or {}
            snapshot = {k: v for k, v in snapshot.items() if not k.startswith("_")}
            snapshot["answer_provenance"] = answer_provenance
            if not snapshot.get("summary") and not json.loads(rev.structured or "{}").get("_missing_idea_summary"):
                snapshot["summary"] = rev.summary or ""
            venture = session.get(Venture, idea.venture_id)
            if not venture or venture.workspace_id != idea.workspace_id:
                raise FatalError("research_scope_mismatch")
            if not snapshot.get("summary"):
                from app.ventures.current_idea import current_idea
                snapshot["summary"] = current_idea(venture, session=session)
            # The idea revision describes the offer, but does not contain the
            # venture's identity. Freeze it for this dossier so the reviewer
            # cannot mistake Supymem (the platform) for the founder's product.
            snapshot["venture_name"] = venture.name[:160]
            from app.ventures.startup_review import enabled
            if enabled(session, venture.id):
                from app.ventures.founder_facts import read
                snapshot["founder_facts"] = read(session, venture)
            from app.db.supymem_models import ClarifierTurn
            from app.idea.agent import SUGGESTED_CHOICE_NOTE, answer_metadata
            turns = session.scalars(select(ClarifierTurn).join(IdeaRevision, ClarifierTurn.revision_id == IdeaRevision.id)
                                    .where(ClarifierTurn.session_id == idea.id, IdeaRevision.rev <= rev.rev)
                                    .order_by(ClarifierTurn.turn)).all()
            suggested = list(answer_provenance.values())
            options = []
            for turn in turns:
                if turn.role == "agent":
                    saved = json.loads(turn.options or "{}")
                    options = saved.get("options", []) if isinstance(saved, dict) and turn.kind == "question" else []
                elif turn.role == "founder":
                    suggested.append(json.loads(turn.options) if turn.options else answer_metadata(turn.text, options))
            snapshot["suggested_assumptions"] = [answer for answer in suggested if answer.get("provenance") == "suggested_choice"]
            snapshot["assumption_note"] = SUGGESTED_CHOICE_NOTE
            from app.idea.validation_recovery import notes
            founder_notes = notes(session, idea, rev.id)
            if founder_notes:
                snapshot["founder_discovery_notes"] = founder_notes
            if kind == "deep":
                from app.ventures.journey_overrides import NOTE, read
                if read(idea.venture_id, session=session)["skipped"]:
                    snapshot["limitations"] = [*(snapshot.get("limitations") or []), NOTE]
                from app.research.context import capture
                snapshot.update(capture(session, idea.venture_id, idea.workspace_id, snapshot))
                # Research should investigate the riskiest assumptions of the founder's GStack review.
                from app.gstack.office_hours import brief
                gstack = brief(session, idea.id, rev.id)
                if gstack:
                    snapshot["gstack_brief"] = gstack
            row = ResearchDossier(id=did, workspace_id=idea.workspace_id, venture_id=idea.venture_id,
                revision_id=rev.id, kind=kind, state="running", budget_calls=budget, calls_used=0,
                summary=json.dumps({"_runtime": {"job_id": payload["_job_id"], "session_id": idea.id,
                    "started_at": as_utc(job.started_at or utcnow()).isoformat(),
                    "snapshot": snapshot, "operations": {}, "prior_art_enabled": prior_art_enabled,
                    **({"progress_plan": ["questions" if payload.get("stage") == "gstack_questions" else "review"]} if kind == "validation" else {}),
                    **({"cost_ceiling_micro": COST_CEILING_MICRO} if prior_art_enabled else {})}, "coverage_gaps": []}), created_at=utcnow())
            session.add(row)
        session.commit()
    return did


def read(did: str):
    with db()() as session:
        row = session.get(ResearchDossier, did)
        if row is not None:
            session.expunge(row)
        return row


def mutate(did: str, payload: dict | None, fn):
    with db()() as session:
        from app.db.session import serialize_writes
        serialize_writes(session, key=did)
        if payload is not None:
            fence(session, payload)
        row = session.get(ResearchDossier, did, with_for_update=True)
        document = json.loads(row.summary)
        result = fn(row, document, session)
        row.summary = json.dumps(document, ensure_ascii=False)
        session.commit()
        return result


def _before_call(row, doc, session):
    if row.kind == "validation":
        idea = session.get(IdeaSession, doc["_runtime"]["session_id"], with_for_update=True)
        if idea.state in {"confirmed", "abandoned"} or idea.current_revision_id != row.revision_id:
            raise FatalError("validation_revision_superseded")
    if row.calls_used >= row.budget_calls:
        raise FatalError("research_call_budget_exhausted")
    row.calls_used += 1



def _validate_usage(row, doc, session, usage):
    """Block new paid I/O after uncertainty and enforce the prior-art cost ceiling."""
    operations = doc["_runtime"]["operations"]
    # Recovery validates the saved usage again. Cleanup of an already admitted
    # receipt must never reserve the same cost/calls twice or block settlement.
    if usage.get("request_id") and any(op.get("usage", {}).get("request_id") == usage["request_id"]
                                      for op in operations.values() if op.get("state") != "reserving"):
        return
    if usage.get("source") != "free" and any(
            op.get("state") == "uncertain" and op.get("usage", {}).get("source") != "free"
            for op in operations.values()) and row.kind == "deep":
        raise FatalError("research_outcome_uncertain")
    ceiling = doc["_runtime"].get("cost_ceiling_micro")
    if ceiling is None:
        return
    total = sum(max(op.get("usage", {}).get("estimate_micro", 0),
                    op.get("usage", {}).get("provider_actual_micro", 0),
                    op.get("usage", {}).get("actual_micro", 0))
                for op in doc["_runtime"]["operations"].values()
                if op.get("state") not in {"failed", "retryable", "reserving"})
    if total + usage.get("estimate_micro", 0) > ceiling:
        raise FatalError("research_cost_budget_exhausted")
    purpose = usage.get("purpose", "")
    # Reserve worst-case raw requests before every callback, including failed
    # attempts on later leases. Unused slots are not returned. Child API caps
    # are included; Firecrawl makes one attempt, so real I/O stays below 60.
    calls = (4 if purpose == "research.prior_art.github-repos" else
             3 if purpose.startswith("research.prior_art.hn-comments-") else 1)
    reserved = doc["_runtime"].get("raw_calls_reserved", 0)
    if reserved + calls > 60:
        raise FatalError("research_call_budget_exhausted")
    doc["_runtime"]["raw_calls_reserved"] = reserved + calls


journal = OperationJournal(read=read, mutate=mutate, document=lambda did: json.loads(read(did).summary),
                           before_call=_before_call, prefix="research", validate_usage=_validate_usage)
operation = journal.operation
save_operation = journal.save_operation
prepare = journal.prepare
begin = journal.begin
checkpoint_model_receipt = journal.checkpoint_model_receipt
recover = journal.recover


def _resolve_interrupted(saved, expired, row, doc, session, *, operation_key, job_kind, identifier, interrupted_code):
    from app.credits.errors import CreditsUnavailable
    from app.credits.gate import _check_hold, _check_passthrough, _counts, project_model_receipt
    from app.credits.ledger import append, find_by_idempotency_key
    from app.credits.reserve import _close_open_hold
    from app.db.models import CreditAccount, CreditReservation
    from app.db.session import serialize_writes

    usage = deepcopy(saved.get("usage", {}))
    job_id = doc["_runtime"]["job_id"]
    job = session.get(Job, job_id, with_for_update=True)
    # The founder-facing question stage runs inside the clarify job, so its validation records belong to that job.
    owner_kinds = {job_kind, "idea.clarify_turn"} if job_kind == "idea.validate" else {job_kind}
    if (not job or job.kind not in owner_kinds or identifier(job.id) != row.id
            or (job.workspace_id, job.venture_id) != (row.workspace_id, row.venture_id)
            or (usage.get("workspace_id"), usage.get("venture_id"), usage.get("request_id")) != (
                row.workspace_id, row.venture_id, f"{row.id}:{operation_key}:{saved.get('attempt')}")):
        raise FatalError("research_scope_mismatch")
    resuming = job_kind == "research.deep" and job.status == "retry_wait" and job.last_error == interrupted_code
    if job.status in {"leased", "scheduled", "retry_wait"} and not resuming:
        return None
    if "provider_receipt" in usage:
        try:
            priced = project_model_receipt(usage)
        except CreditsUnavailable:
            pass
        else:
            if usage["source"] == "byok" and usage.get("account_id") and not usage.get("reservation_id"):
                serialize_writes(session, key=usage["account_id"])
                account = session.get(CreditAccount, usage["account_id"], with_for_update=True)
                if account is None:
                    raise FatalError("research_scope_mismatch")
                passthrough_key = "passthrough:" + usage["request_id"]
                entry = find_by_idempotency_key(passthrough_key, session)
                if entry is None:
                    entry = append(session, account=account, kind="spend", amount_micro=0,
                        reason="passthrough usage", actor_kind="system", actor_id=usage.get("payer") or "system",
                        idempotency_key=passthrough_key, purpose=usage["purpose"], model=usage["model"],
                        provider=usage["provider"], prompt_tokens=priced["prompt_tokens"],
                        completion_tokens=priced["completion_tokens"], request_id=usage["request_id"],
                        meta={"passthrough": True, **{name: priced[name] for name in ("source", "estimate_micro",
                            "actual_micro", "provider_actual_micro", "workspace_id", "venture_id", "task_invocation", "cached_tokens")}})
                _check_passthrough(entry, priced, _counts(priced, required=True))
            return {"state": "provider_responded", "usage": usage,
                "response": saved.get("response") or {},
                "error_code": saved.get("error_code") if saved.get("response") is not None else interrupted_code}
    elif saved.get("response") is not None and "actual_micro" in usage:
        return {"state": "responded"}
    reservation = None
    entry = None
    if usage.get("reservation_id"):
        serialize_writes(session, key=usage["account_id"])
        reservation = session.get(CreditReservation, usage["reservation_id"], with_for_update=True)
        _check_hold(reservation, usage)
        entry = find_by_idempotency_key("settle:" + reservation.id, session)
    if entry is not None:
        meta = json.loads(entry.meta_json)
        if (reservation.status != "settled" or entry.kind != "spend"
                or entry.account_id != reservation.account_id or entry.reservation_id != reservation.id
                or entry.amount_micro != -reservation.settled_micro
                or any(getattr(entry, name) != usage.get(name) for name in ("purpose", "provider", "model", "request_id"))
                or any(meta.get(name) != usage.get(name) for name in ("workspace_id", "venture_id", "task_invocation"))
                or meta.get("hold_micro") != reservation.amount_micro):
            raise FatalError("research_scope_mismatch")
        usage.update(credits_micro=reservation.settled_micro, settled=True,
            actual_micro=meta["provider_actual_micro"],
            provider_actual_micro=meta.get("raw_provider_actual_micro", meta["provider_actual_micro"]),
            prompt_tokens=entry.prompt_tokens, completion_tokens=entry.completion_tokens,
            cached_tokens=meta.get("cached_tokens", 0))
        return {"state": "failed", "usage": usage, "billing_resolution": "confirmed_charge",
                "error_code": interrupted_code}
    if not expired:
        return None
    if usage.get("source") not in {"platform", "byok", "free"} or (usage.get("source") == "platform" and reservation is None):
        raise FatalError("research_scope_mismatch")
    if reservation is not None:
        if reservation.status not in {"open", "active", "reconciling", "expired", "released"}:
            raise FatalError("research_scope_mismatch")
        account = session.get(CreditAccount, reservation.account_id, with_for_update=True)
        if account is None:
            raise FatalError("research_scope_mismatch")
        _close_open_hold(session, reservation, "released", definitive=True)
        audit_key = "research-absorb:" + reservation.id
        if find_by_idempotency_key(audit_key, session) is None:
            append(session, account=account, kind="release", amount_micro=0,
                reason="Supymem absorbed an unconfirmed research provider cost; no call was repeated.",
                actor_kind="system", actor_id="research.maintenance", idempotency_key=audit_key,
                purpose=reservation.purpose, provider=reservation.provider, model=reservation.model,
                reservation_id=reservation.id, request_id=reservation.request_id,
                meta={"maximum_micro": reservation.amount_micro, "provider_outcome": "unknown"})
    usage.update(settled=True, credits_micro=0)
    return {"state": "failed", "usage": usage, "billing_resolution": "platform_absorbed",
            "error_code": interrupted_code}


def reconcile_interrupted(*, now=None, limit=100):
    """Maintenance-only receipt/ledger cleanup; no provider I/O or job replay."""
    from sqlalchemy import or_, select

    from app.db.supymem_models import ExecutionPlan
    from app.planning import execution as planning

    resolved = 0
    now = as_utc(now or utcnow())
    for model, column, dossier_kind, receipt, row_identifier, job_kind, code, milestone_kind in (
            (ResearchDossier, ResearchDossier.summary, "deep", journal, identifier,
             "research.deep", "research_interrupted", "research_failed"),
            (ResearchDossier, ResearchDossier.summary, "validation", journal, identifier,
             "idea.validate", "validation_interrupted", "idea_failed"),
            (ExecutionPlan, ExecutionPlan.structured, None, planning.journal, planning.identifier,
             "plan.compile", "plan_interrupted", "plan_failed")):
        # ponytail: bounded receipt batch, keyset paging if interrupted runs exceed 100 per tick.
        query = select(model.id).where(
            or_(*(column.contains('"state": "' + state + '"') for state in
                ("calling", "uncertain", "provider_responded", "responded", "release_pending")),
                column.contains('"billing_resolution":')),
            ~column.contains('"recovery": "new_run_required"'))
        if dossier_kind:
            query = query.where(model.kind == dossier_kind, model.state.in_(("running", "partial", "failed")))
        with db()() as session:
            ids = list(session.scalars(query.order_by(model.created_at).limit(limit)))
        resolved += _reconcile_interrupted_runs(ids, receipt, row_identifier, job_kind, code, milestone_kind, now)
        limit -= len(ids)
        if limit <= 0:
            break
    return resolved


def _reconcile_interrupted_runs(ids, journal, identifier, job_kind, interrupted_code, milestone_kind, now):
    from app.credits.errors import CreditsUnavailable
    from app.tasks.usage import abort_call, after_call

    mutate = journal.mutate
    resolved = 0
    for did in ids:
        document = journal.document(did)
        runtime = document.get("_runtime", {})
        if not runtime.get("job_id"):
            continue
        if runtime.get("recovery_retry_at") and as_utc(datetime.fromisoformat(runtime["recovery_retry_at"])) > now:
            continue
        terminal_code = None
        try:
            for key, operation in runtime.get("operations", {}).items():
                if operation.get("state") in {"calling", "uncertain", "provider_responded"}:
                    try:
                        journal.reconcile_interrupted(did, key,
                            lambda *args: _resolve_interrupted(*args, operation_key=key, job_kind=job_kind,
                                identifier=identifier, interrupted_code=interrupted_code), now=now,
                            started_at=runtime.get("started_at") or as_utc(journal.read(did).created_at).isoformat())
                    except FatalError:
                        if journal.operation(did, key).get("state") != "completed":
                            raise
                elif operation.get("state") in {"responded", "release_pending"}:
                    try:
                        journal.recover(did, key)
                    except FatalError:
                        if journal.operation(did, key).get("state") != "completed":
                            raise
                saved = journal.operation(did, key)
                invocation = saved.get("usage", {}).get("task_invocation")
                if saved.get("billing_resolution") and invocation:
                    usage = saved["usage"]
                    if saved["billing_resolution"] == "confirmed_charge":
                        after_call(invocation, usage["provider_actual_micro"],
                            (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0))
                    else:
                        abort_call(invocation, definitive=True)
        except FatalError as exc:
            terminal_code = str(exc)
        except (RetryableError, CreditsUnavailable, ValueError, KeyError) as exc:
            retry_after_s = getattr(exc, "retry_after_s", None) or 0
            def delay(row, doc, session):
                state = doc["_runtime"]
                attempts = min(state.get("recovery_attempts", 0) + 1, 7)
                state.update(recovery_attempts=attempts,
                    recovery_retry_at=(now + timedelta(seconds=max(retry_after_s,
                        min(3600, 60 * 2 ** (attempts - 1))))).isoformat())
            mutate(did, None, delay)
            logging.getLogger(__name__).warning("research_recovery_pending", extra={"dossier_id": did,
                "exception_type": type(exc).__name__})
            continue
        def finish(row, doc, session):
            from app.tasks.locks import lock_for_job
            lock_for_job(session, doc["_runtime"]["job_id"])
            job = session.get(Job, doc["_runtime"]["job_id"], with_for_update=True)
            if job and (job.kind != job_kind or identifier(job.id) != row.id
                    or (job.workspace_id, job.venture_id) != (row.workspace_id, row.venture_id)):
                job = None
            operations = doc["_runtime"]["operations"]
            if not terminal_code and (not job or job.status == "leased" or any(op.get("state") in {
                    "calling", "uncertain", "provider_responded", "responded", "release_pending"} for op in operations.values())):
                return False
            if not terminal_code and job.last_error not in {journal.prefix + "_outcome_uncertain", "job_outcome_uncertain", interrupted_code}:
                return False
            if doc.get("recovery") == "new_run_required":
                return False
            if job_kind == "plan.compile":
                doc["_runtime"]["generation_failed"] = True
            else:
                from app.research.deep import _RUN_FATAL, sources_for
                usable = job_kind == "research.deep" and terminal_code not in _RUN_FATAL and bool(sources_for(did))
                row.state, row.completed_at = "partial" if usable else "failed", utcnow()
                if usable:
                    doc.update(review=None, review_kind="retrieved_sources_only")
            code = terminal_code or interrupted_code
            doc["error_code"] = code
            doc["recovery"] = "new_run_required"
            if job:
                if job.status != "cancelled":
                    job.status, job.last_error, job.finished_at = "failed_dead", code, utcnow()
                from app.tasks.service import after_job
                after_job(session, job.id, error=code)
            from app.chat.failed_jobs import body, failure_actions
            from app.chat.milestones import post_milestone
            key = (f"research_recovery_failed:{did}" if job_kind == "research.deep"
                   else f"{milestone_kind}:{doc['_runtime']['job_id']}")
            post_milestone(row.venture_id, key=key,
                body_md=body(job_kind, code), actions=failure_actions(row.venture_id, job_kind, code),
                kind=milestone_kind, session=session,
                failure={"step": job_kind, "code": code, "job_id": job.id if job else None})
            return True
        if mutate(did, None, finish):
            resolved += 1
    return resolved
