"""Scoped core profiles and leased background embedding jobs.

Run ``python -m app.memory.profile`` alongside the bulk workers. The durable
outbox is populated transactionally by s058 triggers, not by scanning founders.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text

from app.memory.fuse import hybrid_sql, tokenize
from core.clock import as_utc, utcnow

log = logging.getLogger(__name__)
CORE_LIMIT = 8
SEARCH_LIMIT = 100


def enabled(session=None) -> bool:
    if session is not None and session.get_bind().dialect.name != "postgresql":
        return False
    from app.credits.policy import get_policy
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("memory_search_v2") is True


def _scope(session, venture_id):
    from app.memory.facts import _scope as fact_scope
    workspace_id, person_id = fact_scope(session, venture_id)
    return {"workspace_id": workspace_id, "venture_id": venture_id, "person_id": person_id}


def _hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def vector_available(session):
    return bool(session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid='venture_facts'::regclass AND attname='embedding' AND NOT attisdropped)")))


def deployment_config(workspace_id):
    from app.llm import founder_provider
    from llm_gateway.provider_foundry import load_provider_config_from_env
    founder_provider.register_workspace_llm_resolver()
    resolved = founder_provider.resolve_llm_config(workspace_id)
    config = (founder_provider.to_provider_config(workspace_id, resolved_config=resolved)
        if resolved.source == "founder_override" else load_provider_config_from_env())
    if config is None or not config.deployment_embed:
        raise ValueError("memory_embedding_config_unavailable")
    return config, resolved.source == "founder_override"


def queue_refresh(session, scope, *, query=""):
    request_hash = _hash(query) if query else ""
    try:
        with session.get_bind().begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout='5ms'"))
            connection.execute(text("""INSERT INTO memory_search_jobs
                (workspace_id,venture_id,person_id,request_hash,query_text)
                VALUES (:workspace_id,:venture_id,:person_id,:request_hash,:query)
                ON CONFLICT (workspace_id,venture_id,person_id,request_hash) DO NOTHING"""),
                {**scope, "request_hash": request_hash, "query": query})
    except Exception as exc:
        log.warning("memory_search_enqueue_failed", extra={"error_class": type(exc).__name__})


def cached_embedding(session, scope, query):
    if not query or not vector_available(session):
        return None, None
    try:
        config, _ = deployment_config(scope["workspace_id"])
    except ValueError:
        log.warning("memory_search_embedding_unconfigured_fulltext_only")
        return None, None
    deployment = config.deployment_embed
    saved = session.scalar(text("""SELECT embedding_json FROM memory_embedding_cache
        WHERE workspace_id=:workspace_id AND venture_id=:venture_id
        AND deployment=:deployment AND content_hash=:content_hash"""),
        {**scope, "deployment": deployment, "content_hash": _hash(query)})
    if saved is None:
        # Cold queries remain full-text only until this background job completes.
        queue_refresh(session, scope, query=query)
    return saved, deployment


def search_rows(session, *, venture_id, query, table="all", subject=None,
                history=False, limit=SEARCH_LIMIT, kinds=None, subjects=None, at=None):
    scope = _scope(session, venture_id)
    if not isinstance(query, str) or len(query.encode()) > 16000:
        raise ValueError("memory_search_query_invalid")
    limit = max(1, min(limit, SEARCH_LIMIT))
    embedding, deployment = cached_embedding(session, scope, query)
    parameters = {**scope, "query": query, "subject": subject, "history": history,
        "at": at or utcnow(), "candidates": min(limit * 2, 200), "limit": limit,
        "embedding": embedding, "deployment": deployment,
        "kinds": [kind.value for kind in kinds] if kinds else None, "subjects": subjects or None}
    rows = session.execute(text(hybrid_sql(table=table, vector=embedding is not None,
        history=history, as_of=at is not None)), parameters).mappings().all()
    return [{**row["row_json"], "record_type": row["record_type"]} for row in rows] if table == "all" else rows


def _latest(session, scope, *, subjects=None, core=False):
    from app.db.fact_models import VentureFact
    condition = "AND subject=ANY(CAST(:subjects AS text[]))" if subjects else ""
    if core:
        condition += " AND (subject LIKE 'idea.%' OR subject LIKE 'decision.%' OR subject LIKE 'preference.%')"
    priority = "CASE WHEN subject LIKE 'idea.%' THEN 0 WHEN subject LIKE 'preference.%' THEN 1 ELSE 2 END," if core else ""
    identities = session.execute(text(f"""SELECT id FROM (
        SELECT DISTINCT ON (subject,predicate) id,subject,predicate,valid_from FROM venture_facts
        WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id {condition}
        ORDER BY subject,predicate,valid_from DESC,recorded_at DESC,id DESC) latest
        ORDER BY {priority} valid_from DESC,id LIMIT :limit"""),
        {**scope, "subjects": subjects, "limit": CORE_LIMIT if core else SEARCH_LIMIT}).scalars().all()
    return list(session.scalars(select(VentureFact).where(VentureFact.workspace_id == scope["workspace_id"],
        VentureFact.venture_id == scope["venture_id"], VentureFact.person_id == scope["person_id"],
        VentureFact.id.in_(identities)))) if identities else []


def scoped_record(scope, identity, dedupe_key, payload):
    from app.memory.model import MemoryKind, MemoryRecord
    try:
        record = MemoryRecord.model_validate_json(payload)
    except ValueError:
        return None
    if (record.mem_id != identity or record.dedupe_key != dedupe_key
            or record.venture_id != scope["venture_id"]
            or record.metadata.get("workspace_id") != scope["workspace_id"]
            or record.author != scope["person_id"] and record.kind != MemoryKind.OUTCOME):
        return None
    return record


def _legacy_records(session, scope, *, subjects=None):
    saved = session.execute(text("""SELECT id,dedupe_key,record_json FROM (
        SELECT id,dedupe_key,record_json,report_at,dense_rank() OVER
        (PARTITION BY reported_subject ORDER BY report_at DESC NULLS FIRST) AS position FROM institutional_memories,
        LATERAL unnest(report_subjects) AS reports(reported_subject)
        WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND record_json::jsonb->>'author'=:person_id
        AND report_subjects_ready
        AND (CAST(:wanted AS text[]) IS NULL OR report_subjects && CAST(:wanted AS text[]))
        ) latest WHERE position=1 ORDER BY report_at DESC NULLS FIRST LIMIT :limit"""),
        {**scope, "wanted": subjects, "limit": SEARCH_LIMIT if subjects else CORE_LIMIT}).mappings()
    records = [record for row in saved if (record := scoped_record(scope, row["id"], row["dedupe_key"], row["record_json"]))]
    return list({record.mem_id: record for record in records}.values())


def _reports_pending(session, scope):
    if session.get_bind().dialect.name != "postgresql":
        return False
    return session.scalar(text("""SELECT EXISTS (SELECT 1 FROM institutional_memories
        WHERE workspace_id=:workspace_id AND venture_id=:venture_id
        AND record_json::jsonb->>'author'=:person_id AND NOT report_subjects_ready)"""), scope)


def _prime_sources(session, scope, rows, records):
    from app.db.models import ChatConversation, ChatMessage, Venture
    references = []
    for row in rows:
        try:
            references.append(json.loads(row.source_json).get("ref", ""))
        except (ValueError, TypeError, AttributeError):
            continue
    references.extend(reference for record in records for reference in record.evidence)
    identities = {reference.split(":", 1)[1] for reference in references if isinstance(reference, str) and reference.startswith("chat:")}
    sources = list(session.scalars(select(ChatMessage).where(ChatMessage.workspace_id == scope["workspace_id"],
        ChatMessage.venture_id == scope["venture_id"], ChatMessage.id.in_(identities)))) if identities else []
    conversations = {source.conversation_id for source in sources}
    chats = list(session.scalars(select(ChatConversation).where(ChatConversation.workspace_id == scope["workspace_id"],
        ChatConversation.venture_id == scope["venture_id"], ChatConversation.id.in_(conversations)))) if conversations else []
    return sources, chats, session.get(Venture, scope["venture_id"])


def _report_at(session, scope, record):
    from datetime import datetime
    from app.db.models import ChatMessage
    now = utcnow()
    sources = [session.get(ChatMessage, reference.split(":", 1)[1])
        for reference in record.evidence if reference.startswith("chat:")]
    valid = sources and all(source and (source.workspace_id, source.venture_id, source.author_person_id)
        == (scope["workspace_id"], scope["venture_id"], scope["person_id"])
        and as_utc(source.created_at) <= now for source in sources)
    at = max(as_utc(source.created_at) for source in sources) if valid else None
    if record.tombstoned:
        try:
            forgotten = datetime.fromisoformat(record.metadata["forgotten_at"])
            at = as_utc(forgotten) if forgotten.tzinfo is not None and as_utc(forgotten) <= now else None
        except (KeyError, ValueError, TypeError):
            at = None
    return at


def _selection(session, scope, rows, records, *, include_history=False):
    from app.memory.adapters.postgres import PostgresMemoryAdapter
    from app.memory.current import select_current_reports
    from app.memory.evidence import evidence_current
    from app.memory.extraction import quoted_candidates
    # Keep bounded source rows alive so the unchanged validators reuse the session identity map.
    _source_cache = _prime_sources(session, scope, rows, records)
    adapter = PostgresMemoryAdapter()
    now = utcnow()
    hits, cutoffs = [], {}
    for record in records:
        if record.venture_id != scope["venture_id"] or record.metadata.get("workspace_id") != scope["workspace_id"]:
            continue
        subjects = {candidate.subject for candidate in quoted_candidates(record.statement)}
        if record.tombstoned or not evidence_current(session, record):
            if record.valid_to is None:
                at = _report_at(session, scope, record)
                for subject in subjects:
                    cutoffs[subject] = max(cutoffs[subject], at) if subject in cutoffs and cutoffs[subject] and at else at
        elif record.valid_from <= now:
            hits.append(adapter._to_hit(record, as_of=now))
    selection = select_current_reports(session, venture_id=scope["venture_id"], legacy_hits=hits,
        automatic_rows=rows, raw_legacy=False, include_history=include_history)
    if cutoffs and not include_history:
        selection["facts"] = [row for row in selection["facts"]
            if row.subject not in cutoffs or cutoffs[row.subject] is not None and as_utc(row.valid_from) > cutoffs[row.subject]]
        selection["legacy"] = [hit for hit in selection["legacy"]
            if not any(candidate.subject in cutoffs and (cutoffs[candidate.subject] is None
                or as_utc(hit.valid_from) <= cutoffs[candidate.subject]) for candidate in quoted_candidates(hit.statement))]
        selection["blocked_count"] += sum(not any(row.subject == subject for row in selection["facts"])
            for subject in cutoffs)
    if _reports_pending(session, scope) and not include_history:
        queue_refresh(session, scope)
        selection["facts"], selection["legacy"] = [], []
        selection["blocked_count"] += 1
    return selection


def read_profile(session, *, venture_id):
    scope = _scope(session, venture_id)
    saved = session.scalar(text("""SELECT payload_json FROM memory_core_profiles
        WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id
        AND generation=refreshed_generation"""), scope)
    if saved is None:
        queue_refresh(session, scope)
        return None
    payload = json.loads(saved)
    from app.db.fact_models import VentureFact
    from app.db.models import InstitutionalMemory
    identities = payload.get("fact_ids", [])[:CORE_LIMIT]
    rows = list(session.scalars(select(VentureFact).where(VentureFact.workspace_id == scope["workspace_id"],
        VentureFact.venture_id == venture_id, VentureFact.person_id == scope["person_id"],
        VentureFact.id.in_(identities)))) if identities else []
    record_ids = payload.get("record_ids", [])[:CORE_LIMIT]
    records = []
    for row in session.scalars(select(InstitutionalMemory).where(InstitutionalMemory.workspace_id == scope["workspace_id"],
            InstitutionalMemory.venture_id == venture_id, InstitutionalMemory.id.in_(record_ids))) if record_ids else []:
        record = scoped_record(scope, row.id, row.dedupe_key, row.record_json)
        if record:
            records.append(record)
    return _selection(session, scope, rows, records)


def _answers(selection, query, subject):
    if selection is None or selection["blocked_count"] or selection["conflicts"]:
        return False
    if not query.strip():
        return bool(selection["facts"] or selection["legacy"] or
            any(authority and authority["status"] == "known" for authority in selection["authorities"].values()))
    if not subject:
        return False
    words = set(tokenize(query))
    wrappers = {"who", "what", "how", "is", "are", "my", "our", "the", "current", "saved", "your",
        "a", "an", "do", "we", "i", "for", "of", "about", "tell", "me", "remind", "target"}
    from app.memory.context import subject_for_query
    if any(word not in wrappers and subject_for_query(word) != subject for word in words):
        return False
    authority = selection["authorities"].get(subject)
    from app.memory.extraction import quoted_candidates
    return (any(row.subject == subject for row in selection["facts"]) or bool(authority and authority["status"] == "known")
        or any(candidate.subject == subject for hit in selection["legacy"] for candidate in quoted_candidates(hit.statement)))


def selection_for(session, *, venture_id, query, include_history=False):
    from app.db.fact_models import VentureFact
    from app.memory.context import subject_for_query
    scope = _scope(session, venture_id)
    profile = read_profile(session, venture_id=venture_id)
    subject = subject_for_query(query)
    if not include_history and _answers(profile, query, subject):
        return profile
    candidates = search_rows(session, venture_id=venture_id, query=query, subject=subject, history=include_history)
    identities = [row["id"] for row in candidates if row["record_type"] == "fact"]
    found = {row.id: row for row in session.scalars(select(VentureFact).where(
        VentureFact.workspace_id == scope["workspace_id"], VentureFact.venture_id == venture_id,
        VentureFact.person_id == scope["person_id"], VentureFact.id.in_(identities)))} if identities else {}
    rows = [found[identity] for identity in identities if identity in found]
    from app.memory.extraction import quoted_candidates
    ranked_records = [record for row in candidates if row["record_type"] == "memory"
        and (record := scoped_record(scope, row["id"], row["dedupe_key"], row["record_json"]))]
    core_rows = profile["facts"] if profile and not include_history else []
    core_records = profile["legacy"] if profile and not include_history else []
    wanted = list({row.subject for row in [*core_rows, *rows]} | {candidate.subject
        for record in [*ranked_records, *core_records] for candidate in quoted_candidates(record.statement)})
    latest = _latest(session, scope, subjects=wanted) if wanted else []
    rows = list({row.id: row for row in [*core_rows, *rows, *latest]}.values())
    records = list({record.mem_id: record for record in [*ranked_records, *_legacy_records(session, scope, subjects=wanted or None)]}.values())
    selection = _selection(session, scope, rows, records, include_history=include_history)
    if profile and not include_history:
        selection["blocked_count"] = max(selection["blocked_count"], profile["blocked_count"])
        selection["conflicts"] = [*profile["conflicts"], *selection["conflicts"]]
    return selection


async def embed_content(content, *, scope, config, byok=False):
    from app.credits import metered
    from app.credits.estimate import actual_llm_micro, estimate_llm_micro
    from llm_gateway import sole
    model = config.deployment_embed
    estimate = estimate_llm_micro(model, prompt_tokens=len(content.encode()), max_tokens=0)

    def actual(response):
        tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
        if type(tokens) is not int or tokens < 0:
            raise ValueError("memory_embedding_usage_unconfirmed")
        return actual_llm_micro(model, prompt_tokens=tokens, completion_tokens=0)

    response = await metered.call(lambda: sole.embed([content], purpose="memory.embed",
        workspace_id=scope["workspace_id"], config=config), purpose="memory.embed",
        workspace_id=scope["workspace_id"], venture_id=scope["venture_id"], provider=config.provider,
        model=model, request_id="memory.embed:" + model + ":" + _hash(content),
        actor_id=scope["person_id"], estimate_micro=estimate, actual_micro=actual,
        payer="founder" if byok else None, max_tokens=len(content.encode()))
    values = response.data[0].embedding
    # shortcut: HNSW is dimensioned at 1536; migrate the index before changing embedding dimensions.
    if len(values) != 1536 or any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise ValueError("memory_embedding_dimensions_invalid")
    return json.dumps(values, separators=(",", ":"))


def _claim():
    from app.state import db
    with db()() as session:
        if not enabled(session):
            return None
        job = session.execute(text("""SELECT * FROM memory_search_jobs WHERE run_after<=CURRENT_TIMESTAMP
            AND (lease_until IS NULL OR lease_until<CURRENT_TIMESTAMP)
            ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED""")).mappings().first()
        if job is None:
            return None
        job = dict(job)
        job["lease_token"] = uuid4().hex
        session.execute(text("""UPDATE memory_search_jobs SET lease_until=:lease,lease_token=:lease_token,attempts=attempts+1
            WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id
            AND request_hash=:request_hash"""), {**job, "lease": utcnow() + timedelta(seconds=120)})
        session.commit()
        return job


def _prepare(job):
    from app.state import db
    from app.memory.extraction import quoted_candidates
    scope = {key: job[key] for key in ("workspace_id", "venture_id", "person_id")}
    with db()() as session:
        _scope(session, scope["venture_id"])
        reports = session.execute(text("""SELECT id,dedupe_key,record_json FROM institutional_memories
            WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND record_json::jsonb->>'author'=:person_id
            AND NOT report_subjects_ready ORDER BY id LIMIT 32 FOR UPDATE SKIP LOCKED"""), scope).mappings().all()
        for report in reports:
            record = scoped_record(scope, report["id"], report["dedupe_key"], report["record_json"])
            subjects = sorted({record.subject, *(candidate.subject for candidate in quoted_candidates(record.statement))}) if record else []
            at = _report_at(session, scope, record) if record else None
            session.execute(text("""UPDATE institutional_memories SET report_subjects=CAST(:subjects AS text[]),
                report_at=:at,report_subjects_ready=TRUE WHERE id=:id AND record_json=:record_json
                AND workspace_id=:workspace_id AND venture_id=:venture_id"""), {**scope, **report, "subjects": subjects, "at": at})
        more_reports = bool(_reports_pending(session, scope))
        if not job["request_hash"]:
            session.execute(text("""INSERT INTO memory_core_profiles (workspace_id,venture_id,person_id)
                VALUES (:workspace_id,:venture_id,:person_id) ON CONFLICT DO NOTHING"""), scope)
            generation = session.scalar(text("""SELECT generation FROM memory_core_profiles
                WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id"""), scope)
            rows = _latest(session, scope, core=True)
            records = _legacy_records(session, scope)
            payload = json.dumps({"fact_ids": [row.id for row in rows],
                "record_ids": [record.mem_id for record in records]}, separators=(",", ":"))
            if not more_reports:
                session.execute(text("""UPDATE memory_core_profiles SET payload_json=:payload,refreshed_generation=:generation
                    WHERE workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id
                    AND generation=:generation"""), {**scope, "generation": generation, "payload": payload})
        session.commit()
        if more_reports:
            return scope, [], None, False, True
        if not vector_available(session):
            return scope, [], None, False, more_reports
        config, byok = deployment_config(scope["workspace_id"])
        pending = []
        more_work = more_reports
        if job["request_hash"]:
            pending.append((None, None, job["query_text"]))
        else:
            for table, expression, owner, active in (
                ("venture_facts", "subject || ' ' || value", "person_id", "valid_to IS NULL AND forgotten_at IS NULL"),
                ("institutional_memories", "coalesce(record_json::jsonb->>'subject','') || ' ' || "
                    "coalesce(record_json::jsonb->>'statement','') || ' ' || coalesce(record_json::jsonb->>'detail','')",
                    "record_json::jsonb->>'author'", "record_json::jsonb->>'valid_to' IS NULL "
                    "AND coalesce((record_json::jsonb->>'tombstoned')::boolean,false)=false"),
            ):
                owner_filter = f"{owner}=:person_id" if table == "venture_facts" else (
                    f"({owner}=:person_id OR record_json::jsonb->>'kind'='outcome')")
                candidates = session.execute(text(f"""
                    SELECT id,{expression} AS content FROM {table} WHERE workspace_id=:workspace_id
                    AND venture_id=:venture_id AND {owner_filter} AND {active}
                    AND (embedding_hash IS NULL OR embedding_deployment IS DISTINCT FROM :deployment)
                    ORDER BY id LIMIT 32"""), {**scope, "deployment": config.deployment_embed}).mappings().all()
                more_work = more_work or len(candidates) == 32
                for candidate in candidates:
                    if table == "venture_facts":
                        from app.db.fact_models import VentureFact
                        from app.memory.facts import source_current
                        valid = source_current(session, session.get(VentureFact, candidate["id"]))
                    else:
                        from app.db.models import InstitutionalMemory
                        from app.memory.evidence import evidence_current
                        saved = session.get(InstitutionalMemory, candidate["id"])
                        record = scoped_record(scope, saved.id, saved.dedupe_key, saved.record_json)
                        valid = record is not None and evidence_current(session, record)
                    if valid:
                        pending.append((table, candidate["id"], candidate["content"]))
                    else:
                        session.execute(text(f"""UPDATE {table} SET embedding=NULL,embedding_hash=:hash,
                            embedding_deployment=:deployment WHERE id=:id AND workspace_id=:workspace_id AND venture_id=:venture_id"""),
                            {**scope, "id": candidate["id"], "hash": _hash(candidate["content"]), "deployment": config.deployment_embed})
            session.commit()
        return scope, pending, config, byok, more_work


def _cache(scope, deployment, content):
    from app.state import db
    with db()() as session:
        return session.scalar(text("""SELECT embedding_json FROM memory_embedding_cache
            WHERE workspace_id=:workspace_id AND venture_id=:venture_id
            AND deployment=:deployment AND content_hash=:hash"""),
            {**scope, "deployment": deployment, "hash": _hash(content)})


def _save_embedding(scope, deployment, content, embedding, table, identity):
    from app.state import db
    with db()() as session:
        parameters = {**scope, "deployment": deployment, "hash": _hash(content), "embedding": embedding,
            "identity": identity, "content": content}
        session.execute(text("""INSERT INTO memory_embedding_cache
            (workspace_id,venture_id,deployment,content_hash,embedding_json)
            VALUES (:workspace_id,:venture_id,:deployment,:hash,:embedding) ON CONFLICT DO NOTHING"""), parameters)
        if table:
            if table not in {"venture_facts", "institutional_memories"}:
                raise ValueError("memory_search_table_invalid")
            expression = ("subject || ' ' || value" if table == "venture_facts" else
                "coalesce(record_json::jsonb->>'subject','') || ' ' || coalesce(record_json::jsonb->>'statement','') "
                "|| ' ' || coalesce(record_json::jsonb->>'detail','')")
            session.execute(text(f"""UPDATE {table} SET embedding=CAST(:embedding AS vector),
                embedding_hash=:hash,embedding_deployment=:deployment WHERE id=:identity
                AND workspace_id=:workspace_id AND venture_id=:venture_id AND {expression}=:content"""), parameters)
        session.commit()


def _finish(job, *, more=False, failed=False):
    from app.state import db
    with db()() as session:
        conditions = ("workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id "
            "AND request_hash=:request_hash AND lease_token=:lease_token")
        if more or failed:
            session.execute(text("UPDATE memory_search_jobs SET lease_until=NULL,lease_token='', "
                "run_after=CASE WHEN generation=:generation THEN :after ELSE CURRENT_TIMESTAMP END WHERE " + conditions),
                {**job, "after": utcnow() + timedelta(seconds=min(300, 2 ** min(job["attempts"], 8)) if failed else 0)})
        else:
            session.execute(text("DELETE FROM memory_search_jobs WHERE " + conditions + " AND generation=:generation"), job)
            session.execute(text("UPDATE memory_search_jobs SET lease_until=NULL,lease_token='',run_after=CURRENT_TIMESTAMP WHERE " + conditions), job)
        session.commit()


def _renew(job):
    from app.state import db
    with db()() as session:
        session.execute(text("""UPDATE memory_search_jobs SET lease_until=:until WHERE
            workspace_id=:workspace_id AND venture_id=:venture_id AND person_id=:person_id
            AND request_hash=:request_hash AND lease_token=:lease_token"""),
            {**job, "until": utcnow() + timedelta(seconds=120)})
        session.commit()


async def _heartbeat(job):
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(_renew, job)


async def run_one() -> bool:
    from app.tenancy import bind
    from llm_gateway.limiter import request_class
    job = await asyncio.to_thread(_claim)
    if job is None:
        return False
    with bind(workspace_id=job["workspace_id"], venture_id=job["venture_id"], actor=job["person_id"]), request_class("bulk"):
        heartbeat = asyncio.create_task(_heartbeat(job))
        try:
            scope, pending, config, byok, more_work = await asyncio.to_thread(_prepare, job)
            for table, identity, content in pending:
                embedding = await asyncio.to_thread(_cache, scope, config.deployment_embed, content)
                if embedding is None:
                    embedding = await embed_content(content, scope=scope, config=config, byok=byok)
                await asyncio.to_thread(_save_embedding, scope, config.deployment_embed, content, embedding, table, identity)
            await asyncio.to_thread(_finish, job, more=more_work and not job["request_hash"])
        except Exception as exc:
            log.warning("memory_search_job_failed", extra={"error_class": type(exc).__name__})
            await asyncio.to_thread(_finish, job, failed=True)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
    return True


async def _main(once):
    while True:
        worked = await run_one()
        if once:
            return
        if not worked:
            await asyncio.sleep(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(_main(parser.parse_args().once))
