"""One explicitly approved, durable GTM diagnosis. No external actions or strategy execution."""
from __future__ import annotations
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import or_, select
from app.core.jobs import FatalError, RetryableError, enqueue
from app.core.local_mode import is_local
from app.core.operation_receipts import OperationJournal
from app.db.models import GtmGodPlan, GtmGodRun as Run, CreditReservation, Job, Venture, Workspace
from app.state import db
from core.clock import utcnow
from core.ids import new_id
from app.gtm.god import generation_schema as schema

ACTIVE = {"queued", "running"}

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def authority(wid, vid, *, write=False, actor=None):
    from app.tenancy import current_or_none
    from app.authz.workspace import assert_venture_access, subject_person_id
    from app.workspaces.service import role_of
    scope = current_or_none()
    actor = actor or (scope.actor if scope and scope.workspace_id == wid else None)
    if not actor or actor in {"system", "anonymous", "local", "founder"}:
        raise ValueError("gtm_actor_required")
    if assert_venture_access(vid, actor)[1] != wid:
        raise ValueError("gtm_scope_mismatch")
    with db()() as session:
        venture = session.get(Venture, vid)
        workspace = session.get(Workspace, wid)
        if not venture or venture.archived_at is not None or not workspace or workspace.archived_at is not None:
            raise ValueError("gtm_venture_unavailable")
    if write and role_of(wid, subject_person_id(actor)) in {None, "viewer"}:
        raise ValueError("gtm_write_forbidden")
    return actor


def resources(wid, vid):
    from app.llm.founder_provider import FounderLLMConfigError, resolve_llm_config, resolved_provider_identity
    from app.credits.accounts import get_account
    from app.credits.estimate import _lookup_model_row, estimate_llm_micro
    try:
        cfg = resolve_llm_config(wid)
    except FounderLLMConfigError:
        if not is_local():
            raise
        raise ValueError("gtm_provider_unavailable") from None
    founder = cfg.source == "founder_override"
    pricing = None if is_local() else _lookup_model_row(cfg.model)
    if not pricing and not founder:
        raise ValueError("gtm_pricing_unavailable")
    if not cfg.key_is_set:
        raise ValueError("gtm_provider_unavailable")
    account = None if is_local() else get_account(vid)
    if account and ((account.scope != "person" and (account.scope != "venture" or account.workspace_id != wid or account.venture_id != vid))):
        raise ValueError("gtm_credit_scope_mismatch")
    estimate = 0 if founder else estimate_llm_micro(cfg.model, prompt_tokens=schema.MAX_PROMPT_TOKENS, max_tokens=schema.MAX_OUTPUT_TOKENS)
    provider_identity = resolved_provider_identity(cfg)
    charge_identity = digest([provider_identity, account.id if account else None])
    fingerprint = digest({"provider": cfg.provider, "model": cfg.model, "source": cfg.source,
        "endpoint": cfg.base_url, "key_ref": cfg.api_key_ref, "pricing": pricing,
        "account": account.id if account else None, "estimate": estimate})
    return {"provider": cfg.provider, "model": cfg.model, "payer": "founder" if founder else "platform_credits",
        "resource_digest": fingerprint, "provider_identity": provider_identity, "charge_identity": charge_identity, "call_ceiling_micro": estimate,
        "available_micro": account.granted_micro - account.spent_micro - account.reserved_micro if account else None,
        "account_active": bool(account and account.status == "active")}


def effective_state(row, job, doc):
    """A read may describe a terminated worker, but never writes domain state."""
    if row.state in ACTIVE and job and job.status in {"failed_dead", "cancelled"}:
        uncertain = any(v.get("state") in {"calling", "uncertain", "responded"}
            for v in doc["_runtime"]["operations"].values())
        return ("needs_input", "gtm_outcome_uncertain") if uncertain else ("failed", "gtm_job_failed")
    return row.state, row.error_code


def snapshot(session, wid, vid, request):
    parsed = schema.Request.model_validate(request).model_dump()
    data = {"kind": "diagnose", "brief": parsed, "input_provenance": "founder_reported", "policy_version": schema.POLICY_VERSION}
    if len(dump(data).encode()) > 64000:
        raise ValueError("gtm_input_too_large")
    return data


def _quote(wid, vid, data, actor):
    r = resources(wid, vid)
    ceiling = r["call_ceiling_micro"]
    return {**r, "input_digest": digest({"actor": actor, "workspace": wid, "venture": vid, "input": data}),
        "call_budget": 1, "operations": ["diagnosis:1"], "max_output_tokens": schema.MAX_OUTPUT_TOKENS,
        "input_provenance": "founder_reported", "policy_version": schema.POLICY_VERSION,
        "ceiling_micro": ceiling, "sufficient_now": r["payer"] == "founder" or
        (r["account_active"] and r["available_micro"] is not None and r["available_micro"] >= ceiling),
        "basis": "One provisional diagnosis only. No research or specialist execution. This quote does not reserve credits; actual provider usage is charged within the accepted ceiling."}


def quote(wid, vid, request):
    actor = authority(wid, vid, write=True)
    with db()() as session:
        q = _quote(wid, vid, snapshot(session, wid, vid, request), actor)
    return {k: v for k, v in q.items() if k not in {"provider_identity", "charge_identity"}}


def start(wid, vid, request, *, idempotency_key, input_digest, resource_digest, ceiling_micro):
    actor = authority(wid, vid, write=True)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
        raise ValueError("gtm_idempotency_required")
    if type(ceiling_micro) is not int or ceiling_micro < 0:
        raise ValueError("gtm_ceiling_invalid")
    intent = digest(idempotency_key)
    reqdigest = digest({"request": request, "input": input_digest, "resource": resource_digest, "ceiling": ceiling_micro})
    with db()() as session:
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.workspace_id != wid:
            raise ValueError("gtm_scope_mismatch")
        old = session.scalar(select(Run).where(Run.workspace_id == wid, Run.venture_id == vid,
            Run.actor_id == actor, Run.idempotency_hash == intent))
        if old:
            if old.request_digest != reqdigest:
                raise ValueError("gtm_idempotency_conflict")
            return _view(old, session)
        data = snapshot(session, wid, vid, request)
        q = _quote(wid, vid, data, actor)
        if (input_digest, resource_digest, ceiling_micro) != (q["input_digest"], q["resource_digest"], q["ceiling_micro"]):
            raise ValueError("gtm_quote_changed")
        if not q["sufficient_now"]:
            raise ValueError("gtm_insufficient_credits")
        rid, pid, now = new_id("ggr"), new_id("gtmgp"), utcnow()
        plan = GtmGodPlan(id=pid, workspace_id=wid, venture_id=vid, status="paused",
            idea=data["brief"]["idea"], problem=data["brief"]["problem"], brief_json=dump(data["brief"]),
            artifacts_json=dump({"execution_version": 1, "diagnosis_run_id": rid, "input_provenance": "founder_reported"}),
            created_at=now, updated_at=now)
        session.add(plan)
        jid = enqueue(session, "gtm.god.run", {"gtm_run_id": rid, "_actor_subject": actor},
            workspace_id=wid, venture_id=vid, dedupe_key="gtm:" + rid, max_attempts=3)
        session.flush()
        row = Run(id=rid, plan_id=pid, workspace_id=wid, venture_id=vid, actor_id=actor, kind="diagnose",
            job_id=jid, idempotency_hash=intent, request_digest=reqdigest, state="queued",
            document_json=dump({"_runtime": {"input": data, "quote": q, "operations": {}, "calls_used": 0}, "result": {}}),
            ceiling_micro=ceiling_micro, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        result = _view(row, session)
        session.commit()
        return result


def reserved_micro(row, doc, session):
    # A rejection can occur after reserve but before journal.begin stores the
    # hold ID. Include that attempt's reservation even if release failed.
    ids = [v.get("usage", {}).get("reservation_id") for v in doc["_runtime"]["operations"].values()]
    return sum(session.scalars(select(CreditReservation.amount_micro).where(
        CreditReservation.workspace_id == row.workspace_id,
        CreditReservation.status.in_(["open", "active", "reconciling"]),
        or_(CreditReservation.id.in_([value for value in ids if value]),
            (CreditReservation.purpose == "gtm.god_diagnose") &
            CreditReservation.request_id.startswith(row.id + ":diagnosis:1:", autoescape=True)))))


def proven_pre_call_failure(doc):
    runtime = doc["_runtime"]
    return runtime["calls_used"] == 0 and all(
        operation.get("state") == "reserving" for operation in runtime["operations"].values())


def _view(row, session):
    doc = json.loads(row.document_json)
    runtime, operations = doc["_runtime"], doc["_runtime"]["operations"]
    job = session.get(Job, row.job_id)
    reserved = reserved_micro(row, doc, session)
    state, error = effective_state(row, job, doc)
    return {"run_id": row.id, "plan_id": row.plan_id, "job_id": row.job_id, "venture_id": row.venture_id, "kind": row.kind,
        "state": state, "phase": runtime.get("phase", "queued"), "error_code": error,
        "ceiling_micro": row.ceiling_micro, "reserved_micro": reserved,
        "consumed_micro": sum(v.get("usage", {}).get("credits_micro", 0) for v in operations.values()),
        "calls_used": runtime["calls_used"], "call_budget": runtime["quote"]["call_budget"],
        "payer": runtime["quote"]["payer"], "result": doc["result"], "created_at": row.created_at.isoformat()}


def get(wid, vid, rid):
    authority(wid, vid)
    with db()() as session:
        row = session.get(Run, rid)
        if not row or (row.workspace_id, row.venture_id) != (wid, vid):
            raise ValueError("gtm_not_found")
        return _view(row, session)


def recent(wid, vid):
    authority(wid, vid)
    with db()() as session:
        rows = session.scalars(select(Run).where(Run.workspace_id == wid, Run.venture_id == vid).order_by(Run.created_at.desc()).limit(20))
        return {"runs": [_view(row, session) for row in rows]}


def fence(session, payload, row=None):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if not job or job.status != "leased" or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt"):
        raise FatalError("gtm_lease_lost")
    raw = json.loads(job.payload_json)
    if job.kind not in {"gtm.god.run"} or (job.workspace_id, job.venture_id, raw.get("gtm_run_id")) != (
        payload.get("workspace_id"), payload.get("venture_id"), payload.get("gtm_run_id")):
        raise FatalError("gtm_scope_mismatch")
    if row and (row.job_id, row.workspace_id, row.venture_id, "gtm.god.run", row.actor_id) != (
        job.id, job.workspace_id, job.venture_id, job.kind, raw.get("_actor_subject")):
        raise FatalError("gtm_scope_mismatch")
    authority(job.workspace_id, job.venture_id, write=True, actor=raw.get("_actor_subject"))
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(kind=job.kind, workspace_id=job.workspace_id, venture_id=job.venture_id, payload=raw))
    return job


def read(rid):
    with db()() as session:
        row = session.get(Run, rid)
        if row is None:
            raise FatalError("gtm_not_found")
        session.expunge(row)
        return row


def mutate(rid, payload, fn):
    with db()() as session:
        if payload is not None:
            fence(session, payload)
        row = session.get(Run, rid, with_for_update=True)
        if row is None:
            raise FatalError("gtm_not_found")
        if payload is not None:
            fence(session, payload, row)
        doc = json.loads(row.document_json)
        result = fn(row, doc, session)
        row.document_json = dump(doc)
        row.updated_at = utcnow()
        session.commit()
        return result


def validate_usage(row, doc, session, usage):
    """Fence the actual captured route/hold, not another mutable provider lookup."""
    q = doc["_runtime"]["quote"]
    if (usage.get("provider_identity") != q["provider_identity"] or
            digest([usage.get("provider_identity"), usage.get("account_id")]) != q["charge_identity"] or
            (usage.get("provider"), usage.get("model"), usage.get("source")) !=
            (q["provider"], q["model"], "byok" if q["payer"] == "founder" else "platform")):
        raise FatalError("gtm_resources_changed")
    if q["payer"] != "founder" and usage.get("estimate_micro", 0) > q["call_ceiling_micro"]:
        raise FatalError("gtm_ceiling_exhausted")


def before_call(row, doc, session):
    runtime = doc["_runtime"]
    try:
        current = resources(row.workspace_id, row.venture_id)
    except ValueError as exc:
        raise FatalError(str(exc)) from exc
    q = runtime["quote"]
    if current["resource_digest"] != q["resource_digest"]:
        raise FatalError("gtm_resources_changed")
    if runtime["calls_used"] >= q["call_budget"]:
        raise FatalError("gtm_call_budget_exhausted")
    spent = sum(v.get("usage", {}).get("credits_micro", 0) for v in runtime["operations"].values())
    if spent + q["call_ceiling_micro"] > row.ceiling_micro:
        raise FatalError("gtm_ceiling_exhausted")
    runtime["calls_used"] += 1

journal = OperationJournal(read=read, mutate=mutate, document=lambda rid: json.loads(read(rid).document_json),
                           before_call=before_call, prefix="gtm", validate_usage=validate_usage)


def run_diagnosis(payload):
    from app.credits.estimate import count_prompt_tokens
    from app.research.metered import synthesize
    rid = payload["gtm_run_id"]
    mutate(rid, payload, lambda row, doc, session: None)
    row = read(rid)
    if row.state == "completed":
        return {"run_id": rid, "state": row.state}
    if row.state in {"failed", "cancelled"}:
        raise FatalError("gtm_run_closed")
    data = json.loads(row.document_json)["_runtime"]["input"]
    try:
        messages = schema.messages(data)
        if count_prompt_tokens(messages) > schema.MAX_PROMPT_TOKENS:
            raise FatalError("gtm_prompt_too_large")
        def phase(row, doc, session):
            row.state, row.error_code = "running", None
            doc["_runtime"]["phase"] = "diagnosis:1"
        mutate(rid, payload, phase)
        result = synthesize(rid, payload, messages=messages, schema={"type": "json_object"}, parse=schema.parse,
            purpose="gtm.god_diagnose", key="diagnosis:1", journal=journal, prefix="gtm",
            max_tokens=schema.MAX_OUTPUT_TOKENS, require_stop=True, cap_at_reservation=True,
            pin_provider=True, provider_timeout_seconds=240)
        def finish(row, doc, session):
            if row.state == "completed":
                return
            plan = session.get(GtmGodPlan, row.plan_id, with_for_update=True)
            if not plan or (plan.workspace_id, plan.venture_id) != (row.workspace_id, row.venture_id):
                raise FatalError("gtm_scope_mismatch")
            plan.diagnosis_json = dump(result)
            plan.status, plan.updated_at = "paused", utcnow()  # Diagnosis is not execution or GTM success.
            doc["result"] = {"diagnosis": result, "input_provenance": "founder_reported", "scope": "diagnosis_only", "strategy_execution_supported": False}
            row.state, row.error_code, row.finished_at = "completed", None, utcnow()
        mutate(rid, payload, finish)
        return {"run_id": rid, "state": "completed"}
    except RetryableError:
        raise
    except FatalError as exc:
        code = str(exc)
        if code in {"gtm_lease_lost", "gtm_operation_in_progress", "gtm_scope_mismatch"}:
            raise
        safe = code if re.fullmatch(r"gtm_[a-z_]{1,80}", code) else "gtm_generation_failed"
        def fail(row, doc, session):
            state = "needs_input" if any(s in safe for s in ("uncertain", "credit", "resources_changed", "ceiling", "settlement")) else "failed"
            # Only durable proof of no provider entry and no remaining hold
            # permits a fresh approval. Never infer safety from zero spend.
            if proven_pre_call_failure(doc) and reserved_micro(row, doc, session) == 0:
                state = "failed"
            row.state, row.error_code, row.finished_at = state, safe, utcnow()
        mutate(rid, payload, fail)
        raise
