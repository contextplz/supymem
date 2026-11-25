"""One paid extraction attempt, with immutable founder source and durable receipts.

The job/catalog owner admits memory.extract. This handler never enqueues work,
repairs model output, or treats a quoted founder report as action authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from types import SimpleNamespace

from app.core.jobs import FatalError
from app.core.operation_receipts import OperationJournal
from app.db.models import ChatConversation, Job, VentureDocument
from app.memory.extraction import FactExtraction
from app.state import db
from app.tenancy import current
from core.clock import as_utc, utcnow

PURPOSE = "memory.extract"
KIND = "memory_extract_runtime_v1"
MAX_TOKENS = 1200


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def identifier(job_id):
    return "memory_extract:" + hashlib.sha256(job_id.encode()).hexdigest()


def fence(session, payload):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if (type(payload.get("_job_attempt")) is not int or payload["_job_attempt"] < 1
            or not job or job.status != "leased" or not job.locked_by or job.locked_by != payload.get("_worker_id")
            or job.attempts != payload.get("_job_attempt")):
        raise FatalError("memory_extract_lease_lost")
    saved = json.loads(job.payload_json)
    scope = current()
    if (job.kind != PURPOSE or (job.workspace_id, job.venture_id, saved.get("_actor_subject"))
            != (scope.workspace_id, scope.venture_id, scope.actor)
            or any(saved.get(key) != payload.get(key) for key in
                   ("workspace_id", "venture_id", "source_message_id", "_actor_subject", "_chat_conversation"))
            or (job.workspace_id, job.venture_id) != (payload.get("workspace_id"), payload.get("venture_id"))):
        raise FatalError("memory_extract_scope_invalid")
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(kind=job.kind, workspace_id=job.workspace_id,
                               venture_id=job.venture_id, payload=saved))
    return job


def source_snapshot(session, payload):
    from app.memory.facts import source_options
    from app.memory.extraction import private_chat_source
    row = private_chat_source(session, payload.get("source_message_id"))
    if not row or row.venture_id != payload.get("venture_id"):
        raise FatalError("memory_extract_source_invalid")
    try:
        source_options(session, venture_id=row.venture_id, source_type="chat", reference="chat:" + row.id)
    except (ValueError, PermissionError) as exc:
        raise FatalError("memory_extract_source_invalid") from exc
    if not isinstance(row.body_md, str) or not 1 <= len(row.body_md.encode()) <= 16000:
        raise FatalError("memory_extract_source_ineligible")
    chat = session.get(ChatConversation, row.conversation_id)
    receipt = {"source_message_id": row.id, "workspace_id": row.workspace_id,
               "venture_id": row.venture_id, "person_id": row.author_person_id,
               "conversation_id": row.conversation_id, "created_at": as_utc(row.created_at).isoformat(),
               "visibility": chat.visibility, "creator_person_id": chat.creator_person_id}
    captured = payload.get("_chat_conversation")
    if captured is None:
        raise FatalError("memory_extract_conversation_invalid")
    from app.chat.conversations import bind_job_conversation
    try:
        with bind_job_conversation(payload, venture_id=row.venture_id, workspace_id=row.workspace_id) as bound:
            if bound.id != chat.id or bound.actor_id != row.author_person_id:
                raise ValueError("conversation source mismatch")
    except (ValueError, PermissionError) as exc:
        raise FatalError("memory_extract_conversation_invalid") from exc
    if as_utc(row.created_at) > utcnow():
        raise FatalError("memory_extract_source_invalid")
    receipt["digest"] = _digest({**receipt, "body": row.body_md, "role": row.role, "kind": row.kind})
    return receipt, row.body_md


def _custody(session, row, doc):
    from app.chat.conversations import actor_id, current_conversation
    from app.authz.workspace import subject_person_id
    runtime = doc["_runtime"]
    job = session.get(Job, runtime["job_id"])
    saved = json.loads(job.payload_json) if job else {}
    source = runtime["source"]
    scope = current()
    chat = current_conversation()
    proof = saved.get("_chat_conversation")
    if (not isinstance(proof, dict) or set(proof) != {"schema_version", "conversation_id", "workspace_id", "venture_id", "actor_id"}
            or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or (proof.get("conversation_id"), proof.get("workspace_id"), proof.get("venture_id"), proof.get("actor_id")) !=
               (source["conversation_id"], source["workspace_id"], source["venture_id"], source["person_id"])
            or type(runtime.get("calls_used")) is not int or not 0 <= runtime["calls_used"] <= 1
            or not chat or chat.visibility != "private" or (chat.id, chat.workspace_id, chat.venture_id, chat.actor_id) !=
            (source["conversation_id"], source["workspace_id"], source["venture_id"], source["person_id"])
            or not actor_id() or (scope.workspace_id, scope.venture_id, actor_id()) !=
            (source["workspace_id"], source["venture_id"], source["person_id"])
            or actor_id() != subject_person_id(runtime["actor"])
            or not job or job.kind != PURPOSE or row.kind != KIND or row.id != identifier(job.id)
            or (job.workspace_id, job.venture_id, row.venture_id) !=
               (source["workspace_id"], source["venture_id"], source["venture_id"])
            or (saved.get("source_message_id"), saved.get("_actor_subject")) !=
               (source["source_message_id"], runtime["actor"])):
        raise FatalError("memory_extract_receipt_invalid")


def claim(payload):
    did = identifier(payload["_job_id"])
    with db()() as session:
        fence(session, payload)
        row = session.get(VentureDocument, did, with_for_update=True)
        if row is None:
            source, _ = source_snapshot(session, payload)
            doc = {"_runtime": {"job_id": payload["_job_id"], "actor": current().actor,
                                "source": source, "operations": {}, "calls_used": 0}}
            row = VentureDocument(id=did, venture_id=payload["venture_id"], kind=KIND,
                title="Private memory extraction receipt", provenance_json=_json(doc),
                created_at=utcnow(), updated_at=utcnow())
            session.add(row)
        else:
            _custody(session, row, json.loads(row.provenance_json))
        session.commit()
    return did


def read(did):
    with db()() as session:
        row = session.get(VentureDocument, did)
        if row is None:
            raise FatalError("memory_extract_receipt_missing")
        doc = json.loads(row.provenance_json)
        _custody(session, row, doc)
        return SimpleNamespace(id=row.id, venture_id=row.venture_id,
                               workspace_id=doc["_runtime"]["source"]["workspace_id"], document=doc)


def mutate(did, payload, fn):
    with db()() as session:
        if payload is not None:
            fence(session, payload)
        row = session.get(VentureDocument, did, with_for_update=True)
        doc = json.loads(row.provenance_json)
        _custody(session, row, doc)
        row.workspace_id = doc["_runtime"]["source"]["workspace_id"]
        result = fn(row, doc, session)
        row.provenance_json, row.updated_at = _json(doc), utcnow()
        session.commit()
        return result


def before_call(row, doc, session):
    runtime = doc["_runtime"]
    source, _ = source_snapshot(session, runtime["payload"])
    if source != runtime["source"]:
        raise FatalError("memory_extract_source_changed")
    if runtime["calls_used"] >= 1:
        raise FatalError("memory_extract_call_budget_exhausted")
    runtime["calls_used"] += 1


def validate_usage(row, doc, session, usage):
    runtime, route = doc["_runtime"], doc["_runtime"]["route"]
    if (usage.get("source") != "platform" or not usage.get("reservation_id")
            or (usage.get("workspace_id"), usage.get("venture_id"), usage.get("purpose")) !=
               (row.workspace_id, row.venture_id, PURPOSE)
            or (usage.get("provider"), usage.get("model"), usage.get("estimate_micro"), usage.get("provider_identity")) !=
               (route["provider"], route["catalog_model"], route["estimate_micro"], _digest(route))
            or usage.get("request_id") != f"{row.id}:extract:{runtime['operations']['extract']['attempt']}"
            or "task_invocation" not in usage or runtime["calls_used"] > 1):
        raise FatalError("memory_extract_usage_invalid")


journal = OperationJournal(read=read, mutate=mutate, document=lambda did: read(did).document,
                           before_call=before_call, validate_usage=validate_usage, prefix="memory_extract")


def validate_quotes(content, text):
    result = FactExtraction.model_validate_json(content)
    if len({item.subject for item in result.facts}) != len(result.facts):
        raise ValueError("memory_extract_duplicate_subject")
    if any(not item.quote.strip() or item.quote not in text for item in result.facts):
        raise ValueError("memory_extract_quote_unproven")
    for item in result.facts:
        item.predicate = "statement"
    return result.model_dump(mode="json")


def capture_route(workspace_id, text):
    """Resolve once before reservation; no credentials leave this process."""
    from app.llm.founder_provider import resolve_llm_config
    from app.credits.estimate import estimate_llm_micro
    from llm_gateway import sole
    from llm_gateway.provider_foundry import load_provider_config_from_env, deployment_for
    from llm_gateway.routing import resolve_purpose_config, is_sol_model, sol_output_cap
    if resolve_llm_config(workspace_id).source != "platform_default":
        raise FatalError("memory_extract_byok_unavailable")
    config = resolve_purpose_config(PURPOSE, load_provider_config_from_env())
    model = deployment_for(config, config.captured_tier)
    if config.source != "platform_default" or config.api_mode != "responses" or not is_sol_model(model, config.catalog_model):
        raise FatalError("memory_extract_sol_unavailable")
    schema = FactExtraction.model_json_schema()
    messages = [{"role": "system", "content": "Extract at most five durable venture facts, decisions or preferences from the founder's words. Use canonical subjects when applicable: idea.audience, idea.pricing, idea.problem, idea.solution, preference.answer_length, preference.tone, decision.strategy, metric.customers, metric.revenue. Each quote must be an exact complete supported statement from the source, preserving punctuation. Use each subject once and choose the final statement after a self-correction. Do not extract questions, proposals, approvals, secrets or claims of verified effects. Return an empty facts list when uncertain. Return only the schema JSON."},
                {"role": "user", "content": text}]
    prompt_tokens = sole.transport_prompt_tokens(messages, purpose=PURPOSE, config=config, response_schema=schema)
    max_tokens = sol_output_cap(config, MAX_TOKENS)
    catalog = config.catalog_model or model
    route = {"provider": config.provider, "model": model, "catalog_model": catalog,
             "api_mode": config.api_mode, "reasoning_effort": config.reasoning_effort,
             "tier": config.captured_tier, "endpoint_digest": _digest(config.base_url.rstrip("/")),
             "request_digest": _digest({"messages": messages, "schema": schema, "max_tokens": max_tokens}),
             "max_tokens": max_tokens,
             "prompt_tokens": prompt_tokens,
             "estimate_micro": estimate_llm_micro(catalog, prompt_tokens=prompt_tokens, max_tokens=max_tokens)}
    return config, messages, schema, route


def _reconcile_existing(payload):
    """Reconcile only persisted owned custody; this step grants no work authority."""
    did = identifier(payload["_job_id"])
    with db()() as session:
        row = session.get(VentureDocument, did)
        if row is None:
            return
        doc = json.loads(row.provenance_json)
        _custody(session, row, doc)
        job = session.get(Job, doc["_runtime"]["job_id"])
        saved = json.loads(job.payload_json)
        if any(saved.get(key) != payload.get(key) for key in
               ("workspace_id", "venture_id", "source_message_id", "_actor_subject", "_chat_conversation")):
            raise FatalError("memory_extract_scope_invalid")
        operation = doc["_runtime"]["operations"].get("extract", {})
        eligible = operation.get("state") in {"responded", "completed", "release_pending", "retryable", "failed"}
    if eligible:
        # Ignore the returned body. Claim and normal recovery below still require
        # a fresh lease and live work authority before any application or dial.
        journal.recover(did, "extract", None)


def _run(payload):
    from app.config import settings
    _reconcile_existing(payload)
    if not (settings.company_os_memory_facts_enabled and settings.company_os_memory_model_extract_enabled):
        raise FatalError("memory_extract_disabled")
    did = claim(payload)
    # Funded response recovery precedes current config/source checks. Settlement
    # survives source retirement; applying its body still requires the lease.
    result = journal.recover(did, "extract", payload)
    if result is None:
        if os.environ.get("LLM_OFFLINE", "0").lower() in {"1", "true", "yes"}:
            raise FatalError("memory_extract_provider_offline")
        with db()() as session:
            source, text = source_snapshot(session, payload)
        try:
            config, messages, schema, route = capture_route(payload["workspace_id"], text)
        except (ImportError, ValueError, AttributeError) as exc:
            raise FatalError("memory_extract_routing_unavailable") from exc
        def pin(row, doc, session):
            runtime = doc["_runtime"]
            if source != runtime["source"] or runtime.get("route", route) != route:
                raise FatalError("memory_extract_inputs_changed")
            runtime.update(route=route, payload=payload)
        mutate(did, payload, pin)
        from app.research.metered import perform_llm
        result = perform_llm(did, "extract", payload, purpose=PURPOSE, provider=route["provider"],
            model=route["catalog_model"], estimate=route["estimate_micro"], byok=False,
            journal=journal, prefix="memory_extract", require_stop=True, cap_at_reservation=True,
            provider_identity=_digest(route), schema=schema, token_ceiling=route["prompt_tokens"] + route["max_tokens"],
            parse=lambda content: validate_quotes(content, text),
            dial=dict(messages=messages, purpose=PURPOSE,
                workspace_id=payload["workspace_id"], tier=config.captured_tier, max_tokens=route["max_tokens"],
                config=config, use_workspace_override=False, single_attempt=True,
                response_schema=schema, trace_id=did + ":extract"))
    def apply(row, doc, session):
        from sqlalchemy import select
        from app.db.fact_models import VentureFact
        from app.memory.facts import remember_fact, source_current
        runtime = doc["_runtime"]
        source, text = source_snapshot(session, payload)
        if source != runtime["source"]:
            raise FatalError("memory_extract_source_changed")
        if "fact_ids" in runtime:
            return runtime["fact_ids"]
        operation = runtime["operations"]["extract"]
        if operation["state"] != "completed" or not operation["usage"].get("settled"):
            raise FatalError("memory_extract_settlement_required")
        parsed = validate_quotes(_json(result), text)
        ids = []
        for candidate in parsed["facts"]:
            key = _digest({"subject": candidate["subject"], "predicate": "statement", "source_type": "chat",
                           "ref": "chat:" + source["source_message_id"]})
            existing = session.scalar(select(VentureFact).where(
                VentureFact.workspace_id == source["workspace_id"], VentureFact.venture_id == row.venture_id,
                VentureFact.person_id == source["person_id"], VentureFact.dedupe_key == key))
            if existing is not None:
                if not source_current(session, existing):
                    raise FatalError("memory_extract_existing_fact_changed")
                ids.append(existing.id)
                continue  # Preserve the exact deterministic quote, including retired/forgotten records.
            fact = remember_fact(session, venture_id=row.venture_id, subject=candidate["subject"],
                predicate=candidate["predicate"], value=candidate["quote"],
                source={"type": "chat", "ref": "chat:" + source["source_message_id"], "quote": candidate["quote"]},
                valid_from=as_utc(datetime.fromisoformat(source["created_at"])))
            ids.append(fact.id)
        runtime["fact_ids"] = ids
        return ids
    ids = mutate(did, payload, apply)
    if ids:
        from app.ventures.state_brief import invalidate_venture_state_brief
        invalidate_venture_state_brief(payload["venture_id"])
    return {"availability": "available", "fact_ids": ids, "source_message_id": payload["source_message_id"]}


async def run(payload):
    return await asyncio.to_thread(_run, payload)
