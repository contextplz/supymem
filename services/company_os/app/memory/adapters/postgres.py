"""Transactional shared MemoryPort. Raw receipts remain the authority for outcomes."""
import asyncio
import json
import threading
from datetime import datetime

from sqlalchemy import JSON, cast, func, select
from app.db.models import InstitutionalMemory, Venture
from app.db.session import serialize_writes
from app.memory.adapters.local import LocalMemoryAdapter, _utcnow
from app.memory.fuse import bm25_rank, rrf_order
from app.memory.model import MemoryRecord, MemoryWrite, MemoryKind, MemoryHit, RecallQuery, TaskRef, ContextPack, MemoryHealth, MemoryStats
from app.state import db
from app.tenancy import current
from core.ids import new_id


def venture_scope(session, venture_id):
    row = session.get(Venture, venture_id)
    scope = current()
    if row is None or scope.venture_id != venture_id:
        raise PermissionError("memory_scope_mismatch")
    if scope.workspace_id != row.workspace_id:
        raise PermissionError("memory_scope_mismatch")
    return row


def payload(record):
    return record.model_dump_json()


def mutation_owner(record, row, *, operation):
    from app.chat.conversations import actor_id
    person = actor_id()
    if (record.mem_id != row.id or record.venture_id != row.venture_id
            or record.dedupe_key != row.dedupe_key or record.metadata.get("workspace_id") != row.workspace_id):
        raise ValueError(f"memory_{operation}_source_changed")
    if ((record.kind != MemoryKind.OUTCOME or any(ref.startswith("chat:") for ref in record.evidence))
            and (not person or record.author != person)):
        raise PermissionError("memory_scope_mismatch")


class PostgresMemoryAdapter(LocalMemoryAdapter):
    """Uses PostgreSQL, also runnable against a fresh SQLAlchemy test database.

    No process cache. The shared algorithm helpers are provider-free, and every
    query validates source workspace/venture plus the caller's bound tenancy.
    """
    name = "postgres"

    def __init__(self):
        self._lock = threading.RLock()

    def _new(self, session, m, venture, *, supersede_target=None):
        from app.memory.evidence import validate_write_evidence
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(m.model_dump_json()):
            raise ValueError("memory_credential_withheld")
        proof = validate_write_evidence(session, m)
        correction_targets = [entry.get("target_mem_id") for entry in proof if entry.get("operation") == "correct"]
        if correction_targets and (not supersede_target or any(target != supersede_target for target in correction_targets)):
            raise ValueError("memory_correction_target_mismatch")
        if not 1 <= len(m.statement.encode()) <= 16000 or len((m.detail or "").encode()) > 32000:
            raise ValueError("memory_item_too_large")
        if len(m.evidence) > 32 or not 1 <= len(m.dedupe_key) <= 512:
            raise ValueError("memory_write_invalid")
        old = session.scalar(select(InstitutionalMemory).where(
            InstitutionalMemory.venture_id == m.venture_id, InstitutionalMemory.dedupe_key == m.dedupe_key))
        if old:
            existing = MemoryRecord.model_validate_json(old.record_json)
            if (old.workspace_id != venture.workspace_id or existing.mem_id != old.id
                    or existing.venture_id != old.venture_id
                    or existing.metadata.get("workspace_id") != venture.workspace_id
                    or existing.metadata.get("source_evidence") != proof
                    or any(getattr(existing, key) != getattr(m, key) for key in ("kind", "subject", "statement", "detail", "evidence", "author"))):
                raise ValueError("memory_idempotency_conflict")
            return old
        record = MemoryRecord(mem_id=new_id("mem"), **m.model_dump(), metadata={"source_evidence": proof, "workspace_id": venture.workspace_id})
        row = InstitutionalMemory(id=record.mem_id, workspace_id=venture.workspace_id,
            venture_id=m.venture_id, dedupe_key=m.dedupe_key, record_json=payload(record), version=1)
        session.add(row)
        session.flush()
        return row

    async def remember(self, m: MemoryWrite) -> str:
        with db()() as session:
            serialize_writes(session, key="institutional-memory:" + m.venture_id)
            venture = venture_scope(session, m.venture_id)
            row = self._new(session, m, venture)
            result = row.id
            session.commit()
            return result

    async def supersede(self, mem_id: str, *, by: MemoryWrite, reason: str) -> str:
        with db()() as session:
            serialize_writes(session, key="institutional-memory:" + by.venture_id)
            venture = venture_scope(session, by.venture_id)
            old = session.get(InstitutionalMemory, mem_id)
            if old is None:
                raise KeyError(mem_id)
            if old.venture_id != by.venture_id or old.workspace_id != venture.workspace_id:
                raise PermissionError("memory_scope_mismatch")
            previous = MemoryRecord.model_validate_json(old.record_json)
            mutation_owner(previous, old, operation="correction")
            if previous.tombstoned or previous.valid_to is not None:
                raise ValueError("memory_already_retired")
            row = self._new(session, by, venture, supersede_target=mem_id)
            if any(entry.get("operation") == "correct" for entry in MemoryRecord.model_validate_json(row.record_json).metadata["source_evidence"]):
                from app.memory.evidence import evidence_current
                if (previous.mem_id != old.id or previous.venture_id != old.venture_id
                        or previous.author != current().actor or not evidence_current(session, previous)):
                    raise ValueError("memory_correction_source_changed")
            if row.id == old.id:
                raise ValueError("memory_self_supersession")
            previous.valid_to = _utcnow()
            previous.superseded_by = row.id
            previous.metadata["supersede_reason"] = reason
            old.record_json = payload(previous)
            old.version += 1
            result = row.id
            session.commit()
            return result

    def _records(self, venture_id):
        from app.memory.evidence import evidence_current
        with db()() as session:
            venture = venture_scope(session, venture_id)
            rows = session.scalars(select(InstitutionalMemory).where(
                InstitutionalMemory.venture_id == venture_id, InstitutionalMemory.workspace_id == venture.workspace_id)).all()
            result = []
            for row in rows:
                try:
                    rec = MemoryRecord.model_validate_json(row.record_json)
                except ValueError:
                    continue
                if (rec.mem_id == row.id and rec.venture_id == row.venture_id
                        and rec.dedupe_key == row.dedupe_key and not rec.tombstoned
                        and evidence_current(session, rec)):
                    result.append(rec)
            return result

    def _active_rows(self, venture_id, *, as_of, kinds=None, subjects=None, include_tombstoned=False):
        return [r for r in self._records(venture_id) if r.valid_from <= as_of
                and (r.valid_to is None or r.valid_to > as_of)
                and (not kinds or r.kind in kinds) and (not subjects or r.subject in subjects)]

    async def recall(self, q: RecallQuery):
        if await asyncio.to_thread(self._search_v2_enabled):
            return await asyncio.to_thread(self._recall_v2, q)
        at = q.as_of or _utcnow()
        records = self._active_rows(q.venture_id, as_of=at, kinds=q.kinds, subjects=q.subjects)
        limit = max(1, min(q.limit, 100))
        lexical = bm25_rank(q.text, [(r.mem_id, f"{r.subject} {r.statement} {r.detail or ''} {' '.join(r.tags)}") for r in records], limit=limit * 2)
        recent = [r.mem_id for r in sorted(records, key=lambda r: r.valid_from, reverse=True)]
        # A nonempty search never claims unrelated recency-only records answer it.
        allowed = set(lexical) if q.text.strip() else set(recent)
        ids = rrf_order([lexical, recent], limit=len(records))
        rows = {r.mem_id: r for r in records}
        return [self._to_hit(rows[mid], as_of=at, score=1 / (1 + i)) for i, mid in enumerate(ids)
                if mid in allowed and (q.include_decayed or self._decay(rows[mid], at) >= .15)][:limit]

    async def context_pack(self, *, venture_id: str, task: TaskRef, token_budget: int = 4000):
        if await asyncio.to_thread(self._search_v2_enabled):
            return await asyncio.to_thread(self._context_pack_v2, venture_id, task, token_budget)
        at = _utcnow()
        records = self._active_rows(venture_id, as_of=at)
        priorities = {MemoryKind.FOUNDER_PREFERENCE: 0, MemoryKind.STYLE_CORRECTION: 0,
                      MemoryKind.CONSTRAINT: 1, MemoryKind.DECISION: 2, MemoryKind.FAILURE: 3}
        query = " ".join([task.title, task.capability_id or "", *task.tags])
        relevance = bm25_rank(query, [(r.mem_id, r.statement + " " + (r.detail or "")) for r in records], limit=len(records))
        rank = {mid: i for i, mid in enumerate(relevance)}
        records.sort(key=lambda r: (priorities.get(r.kind, 4), rank.get(r.mem_id, len(records)), -r.valid_from.timestamp()))
        kept, omitted, tokens = [], [], 0
        for rec in records:
            hit = self._to_hit(rec, as_of=at)
            # UTF-8 bytes is a conservative text token upper bound, including metadata.
            estimate = len(hit.model_dump_json().encode())
            if tokens + estimate > max(0, token_budget):
                omitted.append(rec)
                continue
            kept.append(hit)
            tokens += estimate
        return ContextPack(venture_id=venture_id, memories=kept, token_estimate=tokens,
            truncated_kinds=sorted({r.kind.value for r in omitted}),
            omitted_count=len(omitted), omitted_memory_ids=[r.mem_id for r in omitted])

    def _search_v2_enabled(self):
        from app.memory.profile import enabled
        with db()() as session:
            return enabled(session)

    def _recall_v2(self, q):
        from app.memory.evidence import evidence_current
        from app.db.fact_models import VentureFact
        from app.memory.profile import search_rows, _scope, _selection, _legacy_records, _latest, scoped_record
        from app.memory.extraction import quoted_candidates
        at = q.as_of or _utcnow()
        limit = max(1, min(q.limit, 100))
        with db()() as session:
            venture = venture_scope(session, q.venture_id)
            rows = search_rows(session, venture_id=q.venture_id, query=q.text,
                limit=min(limit * 2, 100), kinds=q.kinds, subjects=q.subjects, at=q.as_of)
            facts = [session.get(VentureFact, row["id"]) for row in rows if row["record_type"] == "fact"]
            scope = _scope(session, q.venture_id)
            records = {}
            for row in rows:
                if row["record_type"] == "memory":
                    record = scoped_record(scope, row["id"], row["dedupe_key"], row["record_json"])
                    if record:
                        records[row["id"]] = record
            if q.as_of is None:
                wanted = list({fact.subject for fact in facts if fact is not None} | {candidate.subject
                    for record in records.values() for candidate in quoted_candidates(record.statement)})
                facts = list({fact.id: fact for fact in [*facts, *(_latest(session, scope, subjects=wanted) if wanted else [])]
                    if fact is not None}.values())
                selected_records = list({record.mem_id: record for record in
                    [*records.values(), *_legacy_records(session, scope, subjects=wanted or None)]}.values())
                selection = _selection(session, scope, facts, selected_records)
                allowed_facts = {row.id: row for row in selection["facts"]}
                allowed_records = {hit.mem_id for hit in selection["legacy"]}
            else:
                from app.memory.facts import source_current
                from core.clock import as_utc
                allowed_facts = {fact.id: fact for fact in facts if fact is not None and source_current(session, fact)
                    and fact.forgotten_at is None and as_utc(fact.valid_from) <= at
                    and (fact.valid_to is None or as_utc(fact.valid_to) > at)}
                allowed_records = {record.mem_id for record in records.values() if record.valid_from <= at
                    and (record.valid_to is None or record.valid_to > at)}
            hits = []
            for row in rows:
                if row["record_type"] == "fact":
                    fact = allowed_facts.get(row["id"])
                    if fact is None:
                        continue
                    hits.append(MemoryHit(mem_id=fact.id, venture_id=q.venture_id,
                        kind=MemoryKind.FOUNDER_PREFERENCE if fact.subject.startswith("preference.") else
                            MemoryKind.DECISION if fact.subject.startswith("decision.") else MemoryKind.INSIGHT,
                        subject=fact.subject, statement=fact.value, evidence=[json.loads(fact.source_json)["ref"]],
                        confidence=fact.confidence, decayed_confidence=fact.confidence, valid_from=fact.valid_from,
                        author=fact.person_id, score=float(row["score"])))
                    if len(hits) == limit:
                        break
                    continue
                record = records.get(row["id"])
                if record is None or record.kind != MemoryKind.OUTCOME and record.mem_id not in allowed_records:
                    continue
                if (record.mem_id != row["id"] or record.venture_id != q.venture_id
                        or record.dedupe_key != row["dedupe_key"]
                        or record.metadata.get("workspace_id") != venture.workspace_id
                        or record.tombstoned or not evidence_current(session, record)
                        or not q.include_decayed and self._decay(record, at) < .15):
                    continue
                hits.append(self._to_hit(record, as_of=at, score=float(row["score"])))
                if len(hits) == limit:
                    break
            return hits

    def _context_pack_v2(self, venture_id, task, token_budget):
        from app.memory.evidence import evidence_current
        from app.memory.profile import selection_for, scoped_record, _scope
        query = " ".join([task.title, task.capability_id or "", *task.tags])
        with db()() as session:
            venture_scope(session, venture_id)
            selection = selection_for(session, venture_id=venture_id, query=query)
            hits = [MemoryHit(mem_id=row.id, venture_id=venture_id,
                kind=MemoryKind.FOUNDER_PREFERENCE if row.subject.startswith("preference.") else
                    MemoryKind.DECISION if row.subject.startswith("decision.") else MemoryKind.INSIGHT,
                subject=row.subject, statement=row.value, evidence=[json.loads(row.source_json)["ref"]],
                confidence=row.confidence, decayed_confidence=row.confidence,
                valid_from=row.valid_from, author=row.person_id) for row in selection["facts"]]
            hits.extend(selection["legacy"])
            scope = _scope(session, venture_id)
            at = _utcnow()
            document = cast(InstitutionalMemory.record_json, JSON)
            postgres = session.get_bind().dialect.name == "postgresql"
            kind = document["kind"].as_string() if postgres else func.json_extract(InstitutionalMemory.record_json, "$.kind")
            stamp = document["valid_from"].as_string() if postgres else func.json_extract(InstitutionalMemory.record_json, "$.valid_from")
            outcomes = session.scalars(select(InstitutionalMemory).where(
                InstitutionalMemory.workspace_id == scope["workspace_id"],
                InstitutionalMemory.venture_id == venture_id,
                kind == MemoryKind.OUTCOME.value)
                .order_by(stamp.desc(), InstitutionalMemory.id).limit(100))
            for row in outcomes:
                record = scoped_record(scope, row.id, row.dedupe_key, row.record_json)
                if (record and record.kind == MemoryKind.OUTCOME and record.valid_from <= at
                        and (record.valid_to is None or record.valid_to > at) and not record.superseded_by
                        and not record.tombstoned and evidence_current(session, record)):
                    hits.append(self._to_hit(record, as_of=at))
        kept, omitted, tokens = [], [], 0
        for hit in hits:
            estimate = len(hit.model_dump_json().encode())
            if tokens + estimate > max(0, token_budget):
                omitted.append(hit)
            else:
                kept.append(hit)
                tokens += estimate
        return ContextPack(venture_id=venture_id, memories=kept, token_estimate=tokens,
            truncated_kinds=sorted({hit.kind.value for hit in omitted}), omitted_count=len(omitted),
            omitted_memory_ids=[hit.mem_id for hit in omitted])

    async def answer(self, *, venture_id: str, question: str):
        answer = await super().answer(venture_id=venture_id, question=question)
        if answer.conflicts:
            answer.abstained = True
            answer.abstain_reason = "conflicting current source-backed memories"
            answer.text = "The current saved memories conflict. Review their sources before treating either as established."
        return answer

    async def timeline(self, *, venture_id: str, subject: str, since: datetime | None = None):
        at = _utcnow()
        return [self._to_hit(r, as_of=at) for r in sorted(self._records(venture_id), key=lambda r: r.valid_from)
                if r.subject == subject and (since is None or r.valid_from >= since)]

    async def forget(self, *, venture_id: str, mem_id: str, reason: str, source_message_id: str | None = None):
        with db()() as session:
            serialize_writes(session, key="institutional-memory:" + venture_id)
            venture = venture_scope(session, venture_id)
            row = session.get(InstitutionalMemory, mem_id)
            if row is None:
                raise KeyError(mem_id)
            if row.venture_id != venture_id or row.workspace_id != venture.workspace_id:
                raise PermissionError("memory_scope_mismatch")
            rec = MemoryRecord.model_validate_json(row.record_json)
            mutation_owner(rec, row, operation="forget")
            if source_message_id is not None:
                from app.memory.evidence import validate_forget_source, evidence_current
                validate_forget_source(session, venture_id=venture_id, mem_id=mem_id, source_message_id=source_message_id)
                if (rec.mem_id != row.id or rec.venture_id != row.venture_id
                        or rec.author != current().actor or rec.tombstoned or rec.valid_to is not None
                        or rec.superseded_by is not None or not evidence_current(session, rec)):
                    raise ValueError("memory_forget_source_changed")
            if not rec.tombstoned:
                from core.clock import utcnow
                rec.metadata["forgotten_at"] = utcnow().isoformat()
            rec.tombstoned, rec.tombstone_reason = True, reason
            row.record_json = payload(rec)
            row.version += 1
            session.commit()

    async def stats(self, venture_id):
        records = self._records(venture_id)
        by_kind = {kind.value: sum(r.kind == kind for r in records) for kind in MemoryKind}
        return MemoryStats(venture_id=venture_id, total=len(records), by_kind=by_kind,
            active=sum(r.valid_to is None for r in records), superseded=sum(r.valid_to is not None for r in records))

    async def health(self):
        try:
            with db()() as session:
                session.execute(select(InstitutionalMemory.id).limit(1))
            return MemoryHealth(ok=True, adapter=self.name, detail="shared source-qualified PostgreSQL memory")
        except Exception:
            return MemoryHealth(ok=False, adapter=self.name, detail="memory storage unavailable")

    def close(self):
        pass
