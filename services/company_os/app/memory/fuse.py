"""RRF fusion at k=60 — shared by memory recall and skill resolution (doc 72 §6)."""

from __future__ import annotations

from collections import defaultdict
from typing import Hashable, Iterable, Sequence, TypeVar

T = TypeVar("T", bound=Hashable)

DEFAULT_K = 60


def hybrid_sql(*, table: str, vector: bool = False, history: bool = False, as_of: bool = False) -> str:
    """One SQL RRF, with bounded lanes and no application-side corpus ranking."""
    if table == "all":
        facts = hybrid_sql(table="venture_facts", vector=vector, history=history, as_of=as_of)
        statements = hybrid_sql(table="institutional_memories", vector=vector, history=history, as_of=as_of)
        return f"""WITH facts AS MATERIALIZED ({facts}), statements AS MATERIALIZED ({statements})
            SELECT * FROM (
            SELECT 'fact' AS record_type,id,score,jsonb_build_object('id',id,'score',score) AS row_json FROM facts
            UNION ALL SELECT 'memory' AS record_type,id,score,
            jsonb_build_object('id',id,'score',score,'dedupe_key',dedupe_key,'record_json',record_json) AS row_json FROM statements
            ) combined ORDER BY score DESC,record_type,id LIMIT :limit"""
    if table not in {"venture_facts", "institutional_memories"}:
        raise ValueError("memory_search_table_invalid")
    fact = table == "venture_facts"
    search_document = "search_document" if fact else (
        "to_tsvector('english'::regconfig, coalesce(record_json::jsonb->>'subject','') || ' ' || "
        "coalesce(record_json::jsonb->>'statement','') || ' ' || coalesce(record_json::jsonb->>'detail',''))")
    document = "record_json::jsonb"
    subject_filter = "subject=:subject" if fact else ":subject=ANY(report_subjects)"
    person = "person_id" if fact else f"{document}->>'author'"
    owner_filter = f"{person}=:person_id" if fact else f"({person}=:person_id OR {document}->>'kind'='outcome')"
    stamp = "valid_from" if fact else f"({document}->>'valid_from')::timestamptz"
    fact_active = "(valid_to IS NULL OR valid_to>:at) AND forgotten_at IS NULL" if as_of else "valid_to IS NULL AND forgotten_at IS NULL"
    active = fact_active if fact else (
        f"coalesce(({document}->>'tombstoned')::boolean,false)=false "
        f"AND ({document}->>'valid_to' IS NULL OR ({document}->>'valid_to')::timestamptz > :at) "
        + ("" if as_of else f"AND {document}->>'superseded_by' IS NULL"))
    current_filter = "" if history else f"AND ({active}) "
    filters = (f"workspace_id=:workspace_id AND venture_id=:venture_id AND {owner_filter} "
        f"AND {stamp}<=:at {current_filter}")
    if not fact:
        filters += (f" AND (CAST(:kinds AS text[]) IS NULL OR {document}->>'kind'=ANY(CAST(:kinds AS text[])))"
                    " AND (CAST(:subjects AS text[]) IS NULL OR report_subjects && CAST(:subjects AS text[]))")
    else:
        filters += (" AND (CAST(:subjects AS text[]) IS NULL OR subject=ANY(CAST(:subjects AS text[])))"
            " AND (CAST(:kinds AS text[]) IS NULL OR (CASE WHEN subject LIKE 'preference.%' THEN 'founder_preference' "
            "WHEN subject LIKE 'decision.%' THEN 'decision' ELSE 'insight' END)=ANY(CAST(:kinds AS text[])))")
    semantic = ""
    semantic_lane = ""
    if vector:
        semantic = f""", semantic AS MATERIALIZED (
            SELECT id, embedding <=> CAST(:embedding AS vector) AS distance FROM {table}
            WHERE {filters} AND embedding IS NOT NULL AND embedding_deployment=:deployment
            ORDER BY embedding <=> CAST(:embedding AS vector) LIMIT :candidates)"""
        semantic_lane = " UNION ALL SELECT id, 1.0/(60+row_number() OVER (ORDER BY distance,id)) FROM semantic"
    # GIN BitmapAnds the scope B-tree; the literal current predicate permits partial indexes.
    # At 50M rows, HNSW orders candidates or the scope index limits exact vector sorting to one venture.
    return f"""WITH lexical AS MATERIALIZED (
        SELECT id, ts_rank_cd({search_document},websearch_to_tsquery('english',:query)) AS relevance
        FROM {table} WHERE {filters} AND {search_document} @@ websearch_to_tsquery('english',:query)
        ORDER BY relevance DESC,id LIMIT :candidates),
        exact AS MATERIALIZED (
        SELECT id,{stamp} AS stamp FROM {table} WHERE {filters}
        AND ((CAST(:subject AS text) IS NOT NULL AND {subject_filter}) OR :query='')
        ORDER BY {stamp} DESC,id LIMIT :candidates)
        {semantic}, ranked AS (
        SELECT id,1.0/(60+row_number() OVER (ORDER BY relevance DESC,id)) AS score FROM lexical
        UNION ALL SELECT id,1.0/(60+row_number() OVER (ORDER BY stamp DESC,id)) FROM exact
        {semantic_lane}), fused AS (
        SELECT id,sum(score) AS score FROM ranked GROUP BY id ORDER BY score DESC,id LIMIT :limit)
        SELECT memory.*,fused.score FROM fused JOIN {table} memory USING(id)
        ORDER BY fused.score DESC,fused.id"""


def rrf_fuse(
    ranked_lists: Sequence[Sequence[T]],
    *,
    k: int = DEFAULT_K,
    limit: int | None = None,
) -> list[tuple[T, float]]:
    """Reciprocal Rank Fusion over multiple ranked result lists.

    Returns (item, score) descending. Identical items across lists accumulate.
    """
    scores: dict[T, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] += 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if limit is not None:
        ordered = ordered[:limit]
    return ordered


def rrf_order(
    ranked_lists: Sequence[Sequence[T]],
    *,
    k: int = DEFAULT_K,
    limit: int | None = None,
) -> list[T]:
    return [item for item, _ in rrf_fuse(ranked_lists, k=k, limit=limit)]


def tokenize(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


def bm25_rank(
    query: str,
    corpus: Iterable[tuple[T, str]],
    *,
    limit: int = 50,
) -> list[T]:
    """Lightweight BM25-ish ranking without external deps.

    Good enough for single-venture memory sizes (10³–10⁴). Uses TF + IDF with
    a simple length normalisation.
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    docs: list[tuple[T, list[str]]] = [(doc_id, tokenize(text)) for doc_id, text in corpus]
    if not docs:
        return []

    n = len(docs)
    df: dict[str, int] = defaultdict(int)
    for _, toks in docs:
        for t in set(toks):
            df[t] += 1

    avgdl = sum(len(toks) for _, toks in docs) / max(n, 1)
    k1, b = 1.2, 0.75
    scored: list[tuple[T, float]] = []
    for doc_id, toks in docs:
        if not toks:
            continue
        tf: dict[str, int] = defaultdict(int)
        for t in toks:
            tf[t] += 1
        score = 0.0
        dl = len(toks)
        for qt in q_tokens:
            if qt not in tf:
                continue
            idf = max(0.0, (n - df[qt] + 0.5) / (df[qt] + 0.5))
            # log-ish compression
            import math

            idf = math.log(1.0 + idf)
            freq = tf[qt]
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scored.append((doc_id, score))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [doc_id for doc_id, _ in scored[:limit]]
