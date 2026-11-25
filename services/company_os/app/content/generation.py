"""Durable, metered company content authoring. No account connection or publication."""
from __future__ import annotations
import hashlib
import json
import re
from types import SimpleNamespace

from sqlalchemy import select
from app.core.jobs import FatalError, RetryableError, enqueue
from app.core.operation_receipts import OperationJournal
from app.db.models import ContentBrief, ContentGenerationRun as Run, ContentPost, ContentTopic, CreditReservation, Job, Venture, Workspace
from app.state import db
from core.clock import utcnow
from core.ids import new_id
from app.content import generation_schema as schema

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
        raise ValueError("content_actor_required")
    if assert_venture_access(vid, actor)[1] != wid:
        raise ValueError("content_scope_mismatch")
    with db()() as session:
        venture = session.get(Venture, vid)
        workspace = session.get(Workspace, wid)
        if not venture or venture.archived_at is not None or not workspace or workspace.archived_at is not None:
            raise ValueError("content_venture_unavailable")
    if write and role_of(wid, subject_person_id(actor)) in {None, "viewer"}:
        raise ValueError("content_write_forbidden")
    if write:
        from app.core.local_mode import is_local
        from app.content.agent.policy import agent_enabled
        if is_local() and not agent_enabled():
            raise ValueError("content_disabled")
    return actor


def resources(wid, vid):
    from app.llm.founder_provider import resolve_llm_config, resolved_provider_identity
    from app.credits.accounts import get_account
    from app.credits.estimate import _lookup_model_row, estimate_llm_micro
    cfg = resolve_llm_config(wid)
    founder = cfg.source == "founder_override"
    pricing = _lookup_model_row(cfg.model)
    if not pricing and not founder:
        raise ValueError("content_pricing_unavailable")
    if not cfg.key_is_set:
        raise ValueError("content_provider_unavailable")
    account = get_account(vid)
    if account and ((account.scope != "person" and (account.scope != "venture" or account.workspace_id != wid or account.venture_id != vid))):
        raise ValueError("content_credit_scope_mismatch")
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
        return ("needs_input", "content_outcome_uncertain") if uncertain else ("failed", "content_job_failed")
    return row.state, row.error_code


def snapshot(session, wid, vid, request):
    kind = request.get("kind")
    if kind not in {"topics", "draft"}:
        raise ValueError("content_kind_invalid")
    brief = session.get(ContentBrief, request.get("brief_id")) if request.get("brief_id") else session.scalar(
        select(ContentBrief).where(ContentBrief.workspace_id == wid, ContentBrief.venture_id == vid,
            ContentBrief.status == "active").order_by(ContentBrief.version.desc()).limit(1))
    if not brief or brief.workspace_id != wid or brief.venture_id != vid or brief.status != "active":
        raise ValueError("content_brief_required")
    from app.content.brief import validate, view
    data = {"kind": kind, "brief_id": brief.id, "brief_version": brief.version, "brief": validate(view(brief)),
            "authoring_contract": schema.AUTHORING_CONTRACT}
    if len(dump(data).encode()) > 32000:
        raise ValueError("content_brief_too_large")
    if kind == "topics":
        if request.get("platform") is not None:
            if not isinstance(request["platform"], str) or request["platform"] not in {"x", "linkedin", "reddit", "bluesky", "mastodon", "devto"}:
                raise ValueError("content_platform_invalid")
            data["platform"] = request["platform"]
        count = request.get("count", 12)
        if type(count) is not int or not 1 <= count <= 12:
            raise ValueError("content_count_invalid")
        data["count"] = count
        data["avoid"] = list(session.scalars(select(ContentTopic.angle).join(Run, ContentTopic.generation_run_id == Run.id)
            .where(Run.workspace_id == wid, Run.venture_id == vid).order_by(ContentTopic.created_at.desc()).limit(30)))
    else:
        platform = request.get("platform")
        if not isinstance(platform, str) or platform not in {"x", "linkedin", "reddit", "bluesky", "mastodon", "devto"}:
            raise ValueError("content_platform_invalid")
        query = select(ContentTopic).join(Run, ContentTopic.generation_run_id == Run.id).where(
            Run.workspace_id == wid, Run.venture_id == vid, ContentTopic.workspace_id == wid,
            ContentTopic.brief_id == brief.id, ContentTopic.status == "queued")
        if platform == "reddit":
            # Promotion is prohibited by this channel's existing linter. Do not
            # spend three writer calls on a topic that can never pass it.
            query = query.where(ContentTopic.intent == "value")
        # A running or uncertain intent owns its topic until resolved. A new
        # founder action may reuse a definitely failed/refused topic.
        pending = session.execute(select(Run, Job).join(Job, Run.job_id == Job.id).where(
            Run.workspace_id == wid, Run.venture_id == vid, Run.topic_id.is_not(None),
            Run.state.in_(["queued", "running", "needs_input"])).limit(1001)).all()
        if len(pending) > 1000:
            raise ValueError("content_active_runs_limit")
        busy = [run.topic_id for run, job in pending if
            effective_state(run, job, json.loads(run.document_json))[0] in {"queued", "running", "needs_input"}]
        query = query.where(ContentTopic.id.not_in(busy))
        if request.get("topic_id"):
            query = query.where(ContentTopic.id == request["topic_id"])
        topic = session.scalar(query.order_by(ContentTopic.score.desc(), ContentTopic.id).limit(1))
        if not topic:
            raise ValueError("content_company_topic_required")
        data.update(platform=platform, topic_id=topic.id, topic={k: getattr(topic, k) for k in
            ("angle", "hook", "rationale", "icp_segment", "intent")})
        data["topic"]["evidence"] = json.loads(topic.evidence_json)
    return data


def _quote(wid, vid, data, actor):
    r = resources(wid, vid)
    calls = 1 if data["kind"] == "topics" else 6
    ceiling = calls * r["call_ceiling_micro"]
    return {**r, "input_digest": digest({"actor": actor, "workspace": wid, "venture": vid, "input": data}), "brief_id": data["brief_id"], "brief_version": data["brief_version"],
        "topic_id": data.get("topic_id"), "call_budget": calls, "max_output_tokens": schema.MAX_OUTPUT_TOKENS,
        "ceiling_micro": ceiling, "sufficient_now": r["payer"] == "founder" or
        (r["account_active"] and r["available_micro"] is not None and r["available_micro"] >= ceiling),
        "basis": "Conservative bounded prompt and output allowance. Each call reserves separately; only actual settled usage is consumed."}


def quote(wid, vid, request):
    actor = authority(wid, vid, write=True)
    with db()() as session:
        return _quote(wid, vid, snapshot(session, wid, vid, request), actor)


def start(wid, vid, request, *, idempotency_key, input_digest, resource_digest, ceiling_micro):
    actor = authority(wid, vid, write=True)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
        raise ValueError("content_idempotency_required")
    intent = digest(idempotency_key)
    reqdigest = digest({"request": request, "input": input_digest, "resource": resource_digest, "ceiling": ceiling_micro})
    with db()() as session:
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.workspace_id != wid:
            raise ValueError("content_scope_mismatch")
        old = session.scalar(select(Run).where(Run.workspace_id == wid, Run.venture_id == vid,
            Run.actor_id == actor, Run.idempotency_hash == intent))
        if old:
            if old.request_digest != reqdigest:
                raise ValueError("content_idempotency_conflict")
            return _view(old, session)
        data = snapshot(session, wid, vid, request)
        q = _quote(wid, vid, data, actor)
        if (input_digest, resource_digest, ceiling_micro) != (q["input_digest"], q["resource_digest"], q["ceiling_micro"]):
            raise ValueError("content_quote_changed")
        if not q["sufficient_now"]:
            raise ValueError("content_insufficient_credits")
        rid = new_id("cgr")
        jid = enqueue(session, "content." + data["kind"], {"content_run_id": rid, "_actor_subject": actor},
            workspace_id=wid, venture_id=vid, dedupe_key="content:" + rid, max_attempts=3)
        session.flush()
        now = utcnow()
        row = Run(id=rid, workspace_id=wid, venture_id=vid, actor_id=actor, kind=data["kind"], brief_id=data["brief_id"],
            topic_id=data.get("topic_id"), job_id=jid, idempotency_hash=intent, request_digest=reqdigest, state="queued",
            document_json=dump({"_runtime": {"input": data, "quote": q, "operations": {}, "calls_used": 0}, "result": {}}),
            ceiling_micro=ceiling_micro, created_at=now, updated_at=now)
        session.add(row)
        session.flush()
        result = _view(row, session)
        session.commit()
        return result


def _view(row, session):
    doc = json.loads(row.document_json)
    runtime = doc["_runtime"]
    operations = runtime["operations"]
    job = session.get(Job, row.job_id)
    reservation_ids = [v.get("usage", {}).get("reservation_id") for v in operations.values() if v.get("usage", {}).get("reservation_id")]
    reserved = sum(session.scalars(select(CreditReservation.amount_micro).where(CreditReservation.id.in_(reservation_ids),
        CreditReservation.status.in_(["open", "active", "reconciling"])))) if reservation_ids else 0
    state, error = effective_state(row, job, doc)
    current = session.scalar(select(ContentBrief.version).where(ContentBrief.workspace_id == row.workspace_id,
        ContentBrief.venture_id == row.venture_id, ContentBrief.status == "active").order_by(ContentBrief.version.desc()).limit(1))
    from app.core.local_mode import is_local
    next_step = ""
    if is_local() and state in {"failed", "needs_input", "refused"}:
        next_step = ("Open Content and check your saved progress. In Settings > Keys and connections > Your AI provider, "
            "check your AI provider URL, model and API key, then test your connection.")
        if error and any(part in error for part in ("uncertain", "settlement")):
            next_step += " Before retrying, check your provider's request history so you do not repeat work that may have finished."
        elif error in {"content_quality_refused", "content_near_duplicate"}:
            next_step = "Open Content, review your draft's claim checks and edit your saved draft before requesting another draft."
        else:
            next_step += " Then retry your request in Content."
    return {"run_id": row.id, "job_id": row.job_id, "venture_id": row.venture_id, "kind": row.kind, "state": state,
        "phase": runtime.get("phase", "queued"), "error_code": error, "next_step": next_step, "brief_id": row.brief_id,
        "brief_version": runtime["input"]["brief_version"], "brief_changed": current != runtime["input"]["brief_version"],
        "ceiling_micro": row.ceiling_micro, "reserved_micro": reserved, "consumed_micro": sum(v.get("usage", {}).get("credits_micro", 0) for v in operations.values()),
        "calls_used": runtime["calls_used"], "call_budget": runtime["quote"]["call_budget"],
        "payer": runtime["quote"]["payer"], "result": doc["result"], "created_at": row.created_at.isoformat()}


def get(wid, vid, rid):
    authority(wid, vid)
    with db()() as session:
        row = session.get(Run, rid)
        if not row or (row.workspace_id, row.venture_id) != (wid, vid):
            raise ValueError("content_not_found")
        return _view(row, session)


def recent(wid, vid):
    authority(wid, vid)
    with db()() as session:
        rows = session.scalars(select(Run).where(Run.workspace_id == wid, Run.venture_id == vid).order_by(Run.created_at.desc()).limit(20))
        return {"runs": [_view(row, session) for row in rows]}


def fence(session, payload, row=None):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if not job or job.status != "leased" or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt"):
        raise FatalError("content_lease_lost")
    raw = json.loads(job.payload_json)
    if job.kind not in {"content.topics", "content.draft"} or (job.workspace_id, job.venture_id, raw.get("content_run_id")) != (
        payload.get("workspace_id"), payload.get("venture_id"), payload.get("content_run_id")):
        raise FatalError("content_scope_mismatch")
    if row and (row.job_id, row.workspace_id, row.venture_id, "content." + row.kind, row.actor_id) != (
        job.id, job.workspace_id, job.venture_id, job.kind, raw.get("_actor_subject")):
        raise FatalError("content_scope_mismatch")
    authority(job.workspace_id, job.venture_id, write=True, actor=raw.get("_actor_subject"))
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(kind=job.kind, workspace_id=job.workspace_id, venture_id=job.venture_id, payload=raw))
    return job


def read(rid):
    with db()() as session:
        row = session.get(Run, rid)
        if row is None:
            raise FatalError("content_not_found")
        session.expunge(row)
        return row


def mutate(rid, payload, fn):
    with db()() as session:
        if payload is not None:
            fence(session, payload)
        row = session.get(Run, rid, with_for_update=True)
        if row is None:
            raise FatalError("content_not_found")
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
        raise FatalError("content_resources_changed")
    if q["payer"] != "founder" and usage.get("estimate_micro", 0) > q["call_ceiling_micro"]:
        raise FatalError("content_ceiling_exhausted")


def before_call(row, doc, session):
    runtime = doc["_runtime"]
    try:
        current = resources(row.workspace_id, row.venture_id)
    except ValueError as exc:
        raise FatalError(str(exc)) from exc
    q = runtime["quote"]
    if current["resource_digest"] != q["resource_digest"]:
        raise FatalError("content_resources_changed")
    if runtime["calls_used"] >= q["call_budget"]:
        raise FatalError("content_call_budget_exhausted")
    spent = sum(v.get("usage", {}).get("credits_micro", 0) for v in runtime["operations"].values())
    if spent + q["call_ceiling_micro"] > row.ceiling_micro:
        raise FatalError("content_ceiling_exhausted")
    runtime["calls_used"] += 1

journal = OperationJournal(read=read, mutate=mutate, document=lambda rid: json.loads(read(rid).document_json),
                           before_call=before_call, prefix="content", validate_usage=validate_usage)


def _call(rid, payload, kind, key, data, **kwargs):
    from app.credits.estimate import count_prompt_tokens
    from app.research.metered import synthesize
    messages = schema.messages(kind, data, **kwargs)
    if count_prompt_tokens(messages) > schema.MAX_PROMPT_TOKENS:
        raise FatalError("content_prompt_too_large")
    def phase(row, doc, session):
        row.state = "running"
        doc["_runtime"]["phase"] = key
    mutate(rid, payload, phase)
    def parse_output(raw):
        if kind == "critic":
            return schema.parse_critic_for_repair(raw, data, draft=kwargs["draft"])
        return schema.parse(kind, raw, data, draft=kwargs.get("draft"))
    return synthesize(rid, payload, messages=messages, schema={"type": "json_object"},
        parse=parse_output, purpose="content." + kind, key=key,
        journal=journal, prefix="content", max_tokens=schema.MAX_OUTPUT_TOKENS, require_stop=True, cap_at_reservation=True, pin_provider=True,
        provider_timeout_seconds=240)


def _finish_topics(rid, payload, result):
    def apply(row, doc, session):
        if row.state == "completed":
            return
        ids = []
        for index, topic in enumerate(result["topics"]):
            tid = "ctp_" + digest([rid, index])[:32]
            session.add(ContentTopic(id=tid, workspace_id=row.workspace_id, brief_id=row.brief_id,
                generation_run_id=row.id, evidence_json=dump(topic["evidence"]), status="queued", created_at=utcnow(),
                **{k: v for k, v in topic.items() if k != "evidence"}))
            ids.append(tid)
        row.state, row.finished_at = "completed", utcnow()
        doc["result"] = {"topic_ids": ids, "score_kind": "model_editorial_assessment"}
    mutate(rid, payload, apply)


def _finish_draft(rid, payload, draft, lint, critic, attempt, ok, refusal):
    from app.content.safety import originality
    def apply(row, doc, session):
        if row.state in {"completed", "refused"}:
            return
        hashes = list(session.scalars(select(ContentPost.simhash).join(Run, ContentPost.generation_run_id == Run.id)
            .where(Run.workspace_id == row.workspace_id, Run.venture_id == row.venture_id,
                ContentPost.state == "published", ContentPost.simhash.is_not(None))))
        duplicate = originality.is_duplicate(draft["body"], hashes)[0]
        accepted = ok and not duplicate
        reason = "content_near_duplicate" if duplicate else refusal
        pid = "cps_" + digest(rid)[:32]
        data = doc["_runtime"]["input"]
        session.add(ContentPost(id=pid, workspace_id=row.workspace_id, topic_id=row.topic_id, generation_run_id=rid,
            account_id=None, platform=data["platform"], intent=data["topic"]["intent"], body=draft["body"],
            title=draft["title"] or None, state="draft" if accepted else "refused", scheduled_for=None,
            simhash=originality.simhash(draft["body"]), lint_json=dump(lint), critic_json=dump({**critic, "attempts": attempt}),
            visual_json=dump(draft["visual"] or {}), refusal_reason=None if accepted else reason,
            dry_run=False, created_at=utcnow(), updated_at=utcnow()))
        topic = session.get(ContentTopic, row.topic_id, with_for_update=True)
        topic.status = "drafted" if accepted else "queued"
        row.state, row.finished_at = ("completed" if accepted else "refused"), utcnow()
        doc["result"] = {"post_id": pid, "editable": True, "scheduled": False, "attempts": attempt}
    mutate(rid, payload, apply)


def _run(payload, kind):
    rid = payload["content_run_id"]
    mutate(rid, payload, lambda row, doc, session: None)
    row = read(rid)
    if row.kind != kind:
        raise FatalError("content_scope_mismatch")
    if row.state in {"completed", "refused"}:
        return {"run_id": rid, "state": row.state}
    data = json.loads(row.document_json)["_runtime"]["input"]
    try:
        if kind == "topics":
            _finish_topics(rid, payload, _call(rid, payload, "topics", "topics", data))
        else:
            from app.content.safety.linter import lint
            feedback = []
            for attempt in range(1, 4):
                draft = _call(rid, payload, "writer", f"draft_{attempt}", data, feedback=feedback)
                linted = lint(draft["body"], platform=data["platform"], brief=data["brief"], intent=data["topic"]["intent"]).to_dict()
                critic = {}
                if linted["ok"]:
                    critic = _call(rid, payload, "critic", f"critic_{attempt}", data, draft=draft)
                ok = bool(linted["ok"] and schema.critic_passes(critic, data))
                if ok or attempt == 3:
                    _finish_draft(rid, payload, draft, linted, critic, attempt, ok, "content_quality_refused")
                    break
                feedback = [str(s)[:300] for s in [*linted["reasons"], *critic.get("problems", []),
                    *critic.get("unsupported_claims", []), *schema.grounding_feedback(critic, data)]][:16]
        return {"run_id": rid, "state": read(rid).state}
    except RetryableError:
        raise
    except FatalError as exc:
        code = str(exc)
        if code in {"content_lease_lost", "content_operation_in_progress", "content_scope_mismatch"}:
            raise
        safe = code if re.fullmatch(r"content_[a-z_]{1,80}", code) else "content_generation_failed"
        state = "needs_input" if any(s in safe for s in ("uncertain", "credit", "resources_changed", "ceiling", "settlement")) else "failed"
        def fail(row, doc, session):
            row.state, row.error_code, row.finished_at = state, safe, utcnow()
        mutate(rid, payload, fail)
        raise


def run_topics(payload):
    return _run(payload, "topics")

def run_draft(payload):
    return _run(payload, "draft")


def post_query(wid, vid):
    from sqlalchemy import and_, or_

    return select(ContentPost, ContentBrief.venture_id).outerjoin(Run, ContentPost.generation_run_id == Run.id).join(
        ContentTopic, ContentPost.topic_id == ContentTopic.id).join(ContentBrief, ContentTopic.brief_id == ContentBrief.id).where(
        ContentPost.workspace_id == wid, ContentTopic.workspace_id == wid, ContentBrief.workspace_id == wid,
        or_(and_(Run.workspace_id == wid, Run.venture_id == vid),
            and_(ContentPost.generation_run_id.is_(None), or_(ContentBrief.venture_id == vid, ContentBrief.venture_id.is_(None)))))


def posts(wid, vid, limit=40):
    """Company output plus explicitly unassigned legacy/manual workspace posts."""
    from app.content.engine import _post_view
    authority(wid, vid)
    with db()() as session:
        rows = session.execute(post_query(wid, vid)
            .order_by(ContentPost.created_at.desc()).limit(max(1, min(200, limit)))).all()
        return [{**_post_view(row), "generation_run_id": row.generation_run_id,
            "provenance": "company_generation" if row.generation_run_id else "chat_draft" if venture_id else "legacy_unassigned",
            "edit_digest": digest([row.body, row.title, row.state, row.updated_at.isoformat()])} for row, venture_id in rows]


def edit_post(wid, vid, pid, *, body, title, expected_digest):
    from app.content.safety.linter import lint
    from app.content.safety.originality import simhash
    authority(wid, vid, write=True)
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 8000 or not isinstance(title, str) or len(title) > 200:
        raise ValueError("content_edit_invalid")
    with db()() as session:
        row = session.get(ContentPost, pid, with_for_update=True)
        run = session.get(Run, row.generation_run_id) if row and row.generation_run_id else None
        if not row or not run or (row.workspace_id, run.workspace_id, run.venture_id) != (wid, wid, vid):
            raise ValueError("content_not_found")
        if row.state not in {"draft", "refused"}:
            raise ValueError("content_edit_state_changed")
        current = digest([row.body, row.title, row.state, row.updated_at.isoformat()])
        if current != expected_digest:
            raise ValueError("content_edit_conflict")
        data = json.loads(run.document_json)["_runtime"]["input"]
        checked = lint(body, platform=row.platform, intent=row.intent, brief=data["brief"])
        row.body, row.title, row.updated_at = body.strip(), title or None, utcnow()
        row.lint_json = dump(checked.to_dict())
        row.critic_json = dump({"source": "founder_edit", "previous_model_review_applies": False})
        row.visual_json, row.visual_path, row.simhash = "{}", None, simhash(row.body)
        row.state = "draft" if checked.ok else "refused"
        row.refusal_reason = None if checked.ok else "content_edit_lint_refused"
        row.scheduled_for = None
        session.commit()
        return {"post_id": row.id, "state": row.state, "model_review_required": True, "scheduled": False}
