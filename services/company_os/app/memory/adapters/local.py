"""Local Tier-2 memory adapter — SQLite + FTS5 + RRF + exponential decay (doc 72 §3.1)."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import data_root
from app.memory.fuse import bm25_rank, rrf_order, tokenize
from app.memory.model import (
    Citation,
    ContextPack,
    GroundedAnswer,
    MemoryHealth,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryStats,
    MemoryWrite,
    RecallQuery,
    TaskRef,
)
from app.memory.sqlite_store import MemoryDatabase
from app.tenancy import scoped


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_path() -> Path:
    override = os.environ.get("COMPANY_OS_MEMORY_PATH")
    if override:
        return Path(override)
    return data_root() / "institutional_memory.sqlite3"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _dump_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


DEFAULT_HALF_LIFE: dict[MemoryKind, float] = {
    MemoryKind.FOUNDER_PREFERENCE: 3650.0,
    MemoryKind.STYLE_CORRECTION: 3650.0,
    MemoryKind.CONSTRAINT: 365.0,
    MemoryKind.FAILURE: 180.0,
    MemoryKind.INSIGHT: 90.0,
    MemoryKind.DECISION: 120.0,
    MemoryKind.OUTCOME: 60.0,
    MemoryKind.ENTITY: 180.0,
    MemoryKind.ARTIFACT: 90.0,
    MemoryKind.SKILL_RESOLUTION: 45.0,
    MemoryKind.POCKET: 7.0,
}


class LocalMemoryAdapter:
    """First-class default — no Postgres, Redis, or API key required."""

    name = "local"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else _default_path()
        self._db = MemoryDatabase(self.path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._db.connect(initialize=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    mem_id TEXT PRIMARY KEY,
                    venture_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    detail TEXT,
                    evidence_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    confidence_source TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT,
                    superseded_by TEXT,
                    entities_json TEXT NOT NULL,
                    author TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    half_life_days REAL,
                    tombstoned INTEGER NOT NULL DEFAULT 0,
                    tombstone_reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_mem_dedupe
                    ON memories(venture_id, dedupe_key);
                CREATE INDEX IF NOT EXISTS ix_mem_venture_subject
                    ON memories(venture_id, subject);
                CREATE INDEX IF NOT EXISTS ix_mem_venture_kind
                    ON memories(venture_id, kind);

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    mem_id UNINDEXED,
                    venture_id UNINDEXED,
                    statement,
                    detail,
                    tags,
                    tokenize = 'porter'
                );
                """
            )

    def close(self) -> None:
        self._db.close()

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            mem_id=row["mem_id"],
            venture_id=row["venture_id"],
            kind=MemoryKind(row["kind"]),
            subject=row["subject"],
            statement=row["statement"],
            detail=row["detail"],
            evidence=json.loads(row["evidence_json"] or "[]"),
            confidence=float(row["confidence"]),
            confidence_source=row["confidence_source"],
            valid_from=_parse_dt(row["valid_from"]) or _utcnow(),
            valid_to=_parse_dt(row["valid_to"]),
            superseded_by=row["superseded_by"],
            entities=json.loads(row["entities_json"] or "[]"),
            author=row["author"],
            dedupe_key=row["dedupe_key"],
            tags=json.loads(row["tags_json"] or "[]"),
            half_life_days=row["half_life_days"],
            tombstoned=bool(row["tombstoned"]),
            tombstone_reason=row["tombstone_reason"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def _decay(self, rec: MemoryRecord, as_of: datetime) -> float:
        half = rec.half_life_days
        if half is None:
            half = DEFAULT_HALF_LIFE.get(rec.kind, 90.0)
        if half <= 0:
            return rec.confidence
        age_days = max(0.0, (as_of - rec.valid_from).total_seconds() / 86400.0)
        return rec.confidence * math.exp(-math.log(2) * age_days / half)

    def _to_hit(self, rec: MemoryRecord, *, as_of: datetime, score: float = 0.0) -> MemoryHit:
        return MemoryHit(
            mem_id=rec.mem_id,
            venture_id=rec.venture_id,
            kind=rec.kind,
            subject=rec.subject,
            statement=rec.statement,
            detail=rec.detail,
            evidence=rec.evidence,
            confidence=rec.confidence,
            decayed_confidence=self._decay(rec, as_of),
            valid_from=rec.valid_from,
            valid_to=rec.valid_to,
            superseded_by=rec.superseded_by,
            entities=rec.entities,
            author=rec.author,
            tags=rec.tags,
            score=score,
        )

    def _validate_write(self, m: MemoryWrite) -> None:
        with scoped(m.venture_id):
            pass
        if m.confidence_source == "llm_self":
            raise ValueError("confidence may not be LLM-self-reported")
        # Evidence ids must look like resolvable refs (non-empty strings).
        for eid in m.evidence:
            if not eid or not str(eid).strip():
                raise ValueError("evidence ids must resolve / be non-empty")

    async def remember(self, m: MemoryWrite) -> str:
        self._validate_write(m)
        with self._db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._remember(conn, m)

    def _remember(self, conn: sqlite3.Connection, m: MemoryWrite) -> str:
        existing = conn.execute(
            "SELECT mem_id FROM memories WHERE venture_id = ? AND dedupe_key = ?",
            (m.venture_id, m.dedupe_key),
        ).fetchone()
        if existing:
            return existing["mem_id"]

        mem_id = f"mem_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            INSERT INTO memories (
                mem_id, venture_id, kind, subject, statement, detail,
                evidence_json, confidence, confidence_source, valid_from, valid_to,
                superseded_by, entities_json, author, dedupe_key, tags_json,
                half_life_days, tombstoned, tombstone_reason, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, '{}')
            """,
            (
                mem_id,
                m.venture_id,
                m.kind.value,
                m.subject,
                m.statement,
                m.detail,
                json.dumps(m.evidence),
                m.confidence,
                m.confidence_source,
                _dump_dt(m.valid_from),
                json.dumps(m.entities),
                m.author,
                m.dedupe_key,
                json.dumps(m.tags),
                m.half_life_days,
            ),
        )
        conn.execute(
            """
            INSERT INTO memories_fts(mem_id, venture_id, statement, detail, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (mem_id, m.venture_id, m.statement, m.detail or "", " ".join(m.tags)),
        )
        return mem_id

    async def supersede(self, mem_id: str, *, by: MemoryWrite, reason: str) -> str:
        self._validate_write(by)
        with self._db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM memories WHERE mem_id = ?", (mem_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown memory {mem_id}")
            if row["venture_id"] != by.venture_id:
                raise PermissionError("cannot supersede across ventures")
            new_id = self._remember(conn, by)
            # remember() may have returned an existing dedupe hit; still close the old one
            conn.execute(
                """
                UPDATE memories
                SET valid_to = ?, superseded_by = ?
                WHERE mem_id = ? AND (valid_to IS NULL)
                """,
                (_dump_dt(_utcnow()), new_id, mem_id),
            )
            meta = json.loads(row["metadata_json"] or "{}")
            meta["supersede_reason"] = reason
            conn.execute(
                "UPDATE memories SET metadata_json = ? WHERE mem_id = ?",
                (json.dumps(meta), mem_id),
            )
            return new_id

    def _active_rows(
        self,
        venture_id: str,
        *,
        as_of: datetime,
        kinds: list[MemoryKind] | None = None,
        subjects: list[str] | None = None,
        include_tombstoned: bool = False,
    ) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE venture_id = ?"
        params: list[Any] = [venture_id]
        if not include_tombstoned:
            sql += " AND tombstoned = 0"
        with self._db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[MemoryRecord] = []
        for row in rows:
            rec = self._row_to_record(row)
            if rec.valid_from > as_of:
                continue
            if rec.valid_to is not None and rec.valid_to <= as_of:
                continue
            if kinds and rec.kind not in kinds:
                continue
            if subjects and rec.subject not in subjects:
                continue
            out.append(rec)
        return out

    async def recall(self, q: RecallQuery) -> list[MemoryHit]:
        with scoped(q.venture_id):
            pass
        as_of = q.as_of or _utcnow()
        records = self._active_rows(
            q.venture_id,
            as_of=as_of,
            kinds=q.kinds,
            subjects=q.subjects,
        )
        if not records:
            return []

        # FTS / BM25 lane
        bm25_ids = bm25_rank(
            q.text or " ".join(q.subjects or []),
            [(r.mem_id, f"{r.statement} {r.detail or ''} {' '.join(r.tags)}") for r in records],
            limit=q.limit * 2,
        )
        # Recency lane
        by_recency = sorted(records, key=lambda r: r.valid_from, reverse=True)
        recency_ids = [r.mem_id for r in by_recency[: q.limit * 2]]

        fused = rrf_order([bm25_ids, recency_ids], limit=q.limit)
        by_id = {r.mem_id: r for r in records}
        hits: list[MemoryHit] = []
        for i, mid in enumerate(fused):
            rec = by_id.get(mid)
            if rec is None:
                continue
            decayed = self._decay(rec, as_of)
            if not q.include_decayed and decayed < 0.15:
                continue
            hits.append(self._to_hit(rec, as_of=as_of, score=1.0 / (1 + i)))
        return hits

    async def context_pack(
        self,
        *,
        venture_id: str,
        task: TaskRef,
        token_budget: int = 4000,
    ) -> ContextPack:
        with scoped(venture_id):
            pass
        as_of = _utcnow()
        records = self._active_rows(venture_id, as_of=as_of)

        def pick(kinds: set[MemoryKind], limit: int | None = None) -> list[MemoryRecord]:
            chosen = [r for r in records if r.kind in kinds]
            chosen.sort(key=lambda r: (self._decay(r, as_of), r.valid_from), reverse=True)
            if limit is not None:
                chosen = chosen[:limit]
            return chosen

        # Priority buckets (doc 72 §6) — preferences never truncated.
        prefs = pick({MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION})
        constraints = pick({MemoryKind.CONSTRAINT}, limit=20)
        failures = pick({MemoryKind.FAILURE}, limit=5)
        insights = pick({MemoryKind.INSIGHT}, limit=10)
        decisions = pick({MemoryKind.DECISION}, limit=5)
        entities = pick({MemoryKind.ENTITY}, limit=8)
        outcomes = pick({MemoryKind.OUTCOME}, limit=3)

        # If task text is present, re-rank insights/failures via BM25.
        qtext = " ".join(filter(None, [task.title, task.capability_id or "", " ".join(task.tags)]))
        if qtext.strip():
            insight_ids = set(
                bm25_rank(
                    qtext,
                    [(r.mem_id, f"{r.statement} {r.detail or ''}") for r in insights],
                    limit=8,
                )
            )
            insights = [r for r in insights if r.mem_id in insight_ids] or insights[:5]

        ordered = prefs + constraints + failures + insights + decisions + entities + outcomes
        memories: list[MemoryHit] = []
        tokens = 0
        truncated: list[str] = []
        for rec in ordered:
            est = max(8, len(tokenize(rec.statement)) + len(tokenize(rec.detail or "")))
            # Priority-1 (prefs) never dropped
            is_pref = rec.kind in {MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION}
            if not is_pref and tokens + est > token_budget:
                truncated.append(rec.kind.value)
                continue
            memories.append(self._to_hit(rec, as_of=as_of))
            tokens += est

        return ContextPack(
            venture_id=venture_id,
            memories=memories,
            token_estimate=tokens,
            truncated_kinds=sorted(set(truncated)),
        )

    async def answer(self, *, venture_id: str, question: str) -> GroundedAnswer:
        hits = await self.recall(RecallQuery(venture_id=venture_id, text=question, limit=8))
        if not hits:
            return GroundedAnswer(
                text="I do not have a record that covers this.",
                abstained=True,
                abstain_reason="no memory covers this",
            )
        # Surface conflicts: same subject, different statements among top hits.
        by_subject: dict[str, list[MemoryHit]] = {}
        for h in hits:
            by_subject.setdefault(h.subject, []).append(h)
        conflicts: list[tuple[str, str]] = []
        for subject, group in by_subject.items():
            stmts = {g.statement for g in group}
            if len(stmts) > 1:
                ordered = sorted(group, key=lambda g: g.valid_from)
                conflicts.append((ordered[0].statement, ordered[-1].statement))

        citations = [
            Citation(
                mem_id=h.mem_id,
                statement=h.statement,
                when=h.valid_from,
                author=h.author,
            )
            for h in hits[:5]
        ]
        lines = [h.statement for h in hits[:5]]
        text = " ".join(lines)
        if conflicts:
            text += " There are conflicting memories; both sides are shown in conflicts."
        return GroundedAnswer(
            text=text,
            citations=citations,
            abstained=False,
            conflicts=conflicts,
        )

    async def timeline(
        self,
        *,
        venture_id: str,
        subject: str,
        since: datetime | None = None,
    ) -> list[MemoryHit]:
        with scoped(venture_id):
            pass
        as_of = _utcnow()
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE venture_id = ? AND subject = ? AND tombstoned = 0
                ORDER BY valid_from ASC
                """,
                (venture_id, subject),
            ).fetchall()
        hits: list[MemoryHit] = []
        for row in rows:
            rec = self._row_to_record(row)
            if since and rec.valid_from < since:
                continue
            hits.append(self._to_hit(rec, as_of=as_of))
        return hits

    async def forget(self, *, venture_id: str, mem_id: str, reason: str) -> None:
        with scoped(venture_id):
            pass
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT venture_id FROM memories WHERE mem_id = ?", (mem_id,)
            ).fetchone()
            if row is None:
                raise KeyError(mem_id)
            if row["venture_id"] != venture_id:
                raise PermissionError("cannot forget another venture's memory")
            conn.execute(
                """
                UPDATE memories SET tombstoned = 1, tombstone_reason = ?
                WHERE mem_id = ?
                """,
                (reason, mem_id),
            )

    async def health(self) -> MemoryHealth:
        try:
            with self._db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return MemoryHealth(ok=True, adapter=self.name, detail="sqlite local")
        except Exception as exc:  # noqa: BLE001
            return MemoryHealth(ok=False, adapter=self.name, detail=str(exc))

    async def stats(self, venture_id: str) -> MemoryStats:
        with scoped(venture_id):
            pass
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT kind, valid_to, tombstoned FROM memories WHERE venture_id = ?",
                (venture_id,),
            ).fetchall()
        by_kind: dict[str, int] = {}
        active = superseded = 0
        for row in rows:
            if row["tombstoned"]:
                continue
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
            if row["valid_to"]:
                superseded += 1
            else:
                active += 1
        return MemoryStats(
            venture_id=venture_id,
            total=active + superseded,
            by_kind=by_kind,
            active=active,
            superseded=superseded,
        )
