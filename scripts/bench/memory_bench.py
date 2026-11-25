"""Compare scoped retrieval on the same reviewed corpus, without provider calls.

SQLite is a lexical baseline. PostgreSQL compares lexical and pgvector hybrid
retrieval using the production SQL. Only loopback PostgreSQL is accepted, and
all benchmark tables are temporary and rolled back. Captured provider vectors
can be supplied with --corpus; equivalent fixture vectors are not provider proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

DEFAULT_CORPUS = Path(__file__).with_name("fixtures") / "memory_corpus.json"
LIMIT = 20
CANDIDATES = LIMIT * 2


def load_corpus(path=DEFAULT_CORPUS):
    return validate_corpus(json.loads(Path(path).read_text()))


def validate_corpus(corpus):
    if not isinstance(corpus, dict) or any(key not in corpus for key in ("records", "queries", "deployments")):
        raise ValueError("Supply a corpus with records, queries and deployments, then retry.")
    records, queries, deployments = (corpus[key] for key in ("records", "queries", "deployments"))
    if not isinstance(records, list) or not isinstance(queries, list) or not isinstance(deployments, dict) or not records or not queries or not deployments:
        raise ValueError("Supply records, queries and captured or equivalent vectors, then retry.")
    for items in (records, queries):
        if len({item["id"] for item in items}) != len(items):
            raise ValueError("Corpus IDs must be unique.")
        if any(not isinstance(item[key], str) or not item[key].strip() for item in items for key in ("id", "domain")):
            raise ValueError("Corpus IDs and domains must be nonempty text.")
    domains = {record["domain"] for record in records}
    if len(domains) < 2 or {query["domain"] for query in queries} != domains:
        raise ValueError("Supply queries for at least two unrelated domains, then retry.")
    for record in records:
        if any(not isinstance(record[key], str) or not record[key].strip() for key in ("subject", "value")):
            raise ValueError("Corpus facts require a subject and value.")
    for query in queries:
        owned = {record["id"] for record in records if record["domain"] == query["domain"]}
        if not isinstance(query["text"], str) or not query["text"].strip() or not query["expected_ids"] or not set(query["expected_ids"]) <= owned:
            raise ValueError("Each query must have text and expected IDs from its own domain.")
    for name, deployment in deployments.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Give each embedding deployment a name, then retry.")
        if deployment["source"] not in {"equivalent", "provider_capture"}:
            raise ValueError("Label vectors as equivalent or provider_capture.")
        dimension = deployment["dimensions"]
        if type(dimension) is not int or not 1 <= dimension <= 2000:
            raise ValueError("Use 1 to 2000 dimensions for the pgvector HNSW comparison.")
        for kind, items in (("records", records), ("queries", queries)):
            if set(deployment[kind]) != {item["id"] for item in items}:
                raise ValueError("Every deployment must embed the same records and queries.")
            for vector in deployment[kind].values():
                if not isinstance(vector, list) or len(vector) != dimension or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in vector
                ) or not any(vector):
                    raise ValueError("Vectors must be finite, nonzero and match the declared dimensions.")
        receipts = deployment.get("metered_receipt_ids")
        if deployment["source"] == "provider_capture" and (not isinstance(receipts, list) or not receipts or any(
            not isinstance(identity, str) or not identity.strip() for identity in receipts
        )):
            raise ValueError("Attach the existing metered provider receipt IDs to captured vectors.")
    return corpus


def summary(values):
    values = sorted(values)
    return {"median_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3), "samples": len(values)}


def measure(callback, repeats):
    return summary(timings(callback, repeats))


def timings(callback, repeats):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        callback()
        values.append((time.perf_counter() - start) * 1000)
    return values


# shortcut: scaling repeats reviewed notes, use independent notes before assessing production capacity.
def fact_rows(corpus, rows):
    for identity in range(rows):
        record = corpus["records"][identity % len(corpus["records"])]
        scope = f"{identity // max(5000, len(corpus['records']))}:{record['domain']}"
        yield dict(id=str(identity), corpus_id=record["id"], workspace_id="ws:" + scope,
            venture_id="v:" + scope, person_id="p:" + scope, subject=record["subject"], value=record["value"])


def parameters(query):
    scope = "0:" + query["domain"]
    return dict(workspace_id="ws:" + scope, venture_id="v:" + scope, person_id="p:" + scope,
        query=query["text"], subject=None, history=False, kinds=None, subjects=None,
        candidates=CANDIDATES, limit=LIMIT)


def profile_rows(corpus):
    for domain in sorted({record["domain"] for record in corpus["records"]}):
        yield {"venture_id": "v:0:" + domain,
            "payload": json.dumps({"facts": [record for record in corpus["records"] if record["domain"] == domain][:8]})}


def report(backend, corpus, rows, repeats, execute, profile, explain, deployment=None):
    samples, profile_samples, queries, plans = [], [], [], []
    for query in corpus["queries"]:
        selected = execute(query)
        scope = parameters(query)
        if any(any(row[key] != scope[key] for key in ("workspace_id", "venture_id", "person_id")) for row in selected):
            raise ValueError("Benchmark retrieval crossed a tenant boundary; stop and inspect the query.")
        found = {row["corpus_id"] for row in selected}
        values = timings(lambda: execute(query), repeats)
        samples.extend(values)
        profile_samples.extend(timings(lambda: profile(query), repeats))
        queries.append(dict(id=query["id"], domain=query["domain"], returned_ids=[row["id"] for row in selected],
            retrieved_corpus_ids=sorted(found), recall_at_20=len(found & set(query["expected_ids"])) / len(set(query["expected_ids"])),
            **summary(values)))
        plans.append({"query_id": query["id"], "steps": explain(query)})
    return dict(backend=backend, rows=rows, repeats=repeats, corpus_sha256=hashlib.sha256(
        json.dumps(corpus, sort_keys=True).encode()).hexdigest(), candidates=CANDIDATES, limit=LIMIT,
        corpus_records=len(corpus["records"]), expansion="Repeated corpus notes in isolated tenant partitions",
        partition_rows=max(5000, len(corpus["records"])), cache="warm",
        embedding=deployment or "not measured", profile=summary(profile_samples), search=summary(samples),
        queries=queries, plan=[step for plan in plans for step in plan["steps"]], query_plans=plans)


def validate_size(corpus, rows, repeats):
    if rows < len(corpus["records"]) or repeats < 1:
        raise ValueError("Use at least the corpus record count and one repeat, then retry.")


def sqlite_bench(rows, repeats, corpus=None):
    corpus = validate_corpus(corpus) if corpus is not None else load_corpus()
    validate_size(corpus, rows, repeats)
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript("""CREATE TABLE facts(id TEXT PRIMARY KEY,corpus_id TEXT,workspace_id TEXT,
            venture_id TEXT,person_id TEXT,subject TEXT,value TEXT);
            CREATE INDEX fact_scope ON facts(workspace_id,venture_id,person_id,subject,id);
            CREATE VIRTUAL TABLE fact_search USING fts5(subject,value,content='facts',content_rowid='rowid');
            CREATE TABLE profiles(venture_id TEXT PRIMARY KEY,payload TEXT NOT NULL);""")
        connection.executemany("INSERT INTO facts VALUES (:id,:corpus_id,:workspace_id,:venture_id,:person_id,:subject,:value)",
            fact_rows(corpus, rows))
        connection.execute("INSERT INTO fact_search(fact_search) VALUES ('rebuild')")
        connection.executemany("INSERT INTO profiles VALUES (:venture_id,:payload)", profile_rows(corpus))
        sql = """WITH lexical AS MATERIALIZED (
            SELECT facts.id,bm25(fact_search) AS relevance FROM fact_search JOIN facts ON facts.rowid=fact_search.rowid
            WHERE fact_search MATCH :query AND facts.workspace_id=:workspace_id AND facts.venture_id=:venture_id
            AND facts.person_id=:person_id ORDER BY relevance,id LIMIT :candidates), ranked AS (
            SELECT id,1.0/(60+row_number() OVER (ORDER BY relevance,id)) AS score FROM lexical)
            SELECT facts.*,score FROM ranked JOIN facts USING(id) ORDER BY score DESC,id LIMIT :limit"""
        return report("sqlite_fts5_baseline", corpus, rows, repeats,
            lambda query: connection.execute(sql, parameters(query)).fetchall(),
            lambda query: connection.execute("SELECT payload FROM profiles WHERE venture_id=:venture_id", parameters(query)).fetchone(),
            lambda query: [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters(query))])


def postgres_bench(url, rows, repeats, corpus=None):
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or parsed.host not in {"127.0.0.1", "localhost", "::1"} or parsed.query:
        raise ValueError("Use loopback PostgreSQL without connection overrides, then retry.")
    corpus = validate_corpus(corpus) if corpus is not None else load_corpus()
    validate_size(corpus, rows, repeats)
    from app.memory.fuse import hybrid_sql
    engine = create_engine(url)
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            if not connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")):
                raise ValueError("Enable pgvector in your isolated benchmark database, then retry.")
            connection.execute(text("CREATE TEMP TABLE venture_facts (id TEXT PRIMARY KEY,corpus_id TEXT,workspace_id TEXT,venture_id TEXT,"
                "person_id TEXT,subject TEXT,value TEXT,valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,valid_to TIMESTAMP,forgotten_at TIMESTAMP,"
                "embedding vector,embedding_deployment TEXT,"
                "search_document tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig,subject || ' ' || value)) STORED)"))
            insert = text("INSERT INTO venture_facts (id,corpus_id,workspace_id,venture_id,person_id,subject,value) "
                "VALUES (:id,:corpus_id,:workspace_id,:venture_id,:person_id,:subject,:value)")
            batch = []
            for row in fact_rows(corpus, rows):
                batch.append(row)
                if len(batch) == 1000:
                    connection.execute(insert, batch)
                    batch.clear()
            if batch:
                connection.execute(insert, batch)
            connection.execute(text("CREATE INDEX bench_gin ON venture_facts USING gin(search_document)"))
            connection.execute(text("CREATE INDEX bench_scope ON venture_facts (workspace_id,venture_id,person_id,subject,valid_from DESC) "
                "WHERE valid_to IS NULL AND forgotten_at IS NULL"))
            connection.execute(text("CREATE TEMP TABLE profiles(venture_id TEXT PRIMARY KEY,payload_json TEXT)"))
            connection.execute(text("INSERT INTO profiles VALUES (:venture_id,:payload)"), list(profile_rows(corpus)))
            stamp = connection.scalar(text("SELECT CURRENT_TIMESTAMP"))
            connection.execute(text("ANALYZE venture_facts"))
            comparisons = []
            for name in [None, *corpus["deployments"]]:
                deployment = corpus["deployments"].get(name)
                if deployment:
                    dimensions = deployment["dimensions"]
                    connection.execute(text("DROP INDEX IF EXISTS pg_temp.bench_hnsw"))
                    connection.execute(text(f"ALTER TABLE venture_facts ALTER COLUMN embedding TYPE vector({dimensions}) USING NULL::vector({dimensions})"))
                    connection.execute(text("UPDATE venture_facts SET embedding=CAST(:embedding AS vector),embedding_deployment=:deployment WHERE corpus_id=:corpus_id"),
                        [dict(corpus_id=identity, embedding=json.dumps(vector), deployment=name) for identity, vector in deployment["records"].items()])
                    connection.execute(text("CREATE INDEX bench_hnsw ON venture_facts USING hnsw (embedding vector_cosine_ops)"))
                    connection.execute(text("ANALYZE venture_facts"))
                sql = text(hybrid_sql(table="venture_facts", vector=deployment is not None))

                def query_parameters(query):
                    return dict(**parameters(query), at=stamp, deployment=name,
                        embedding=json.dumps(deployment["queries"][query["id"]]) if deployment else None)

                result = report("postgres_hybrid" if deployment else "postgres_fulltext", corpus, rows, repeats,
                    lambda query: connection.execute(sql, query_parameters(query)).mappings().all(),
                    lambda query: connection.execute(text("SELECT payload_json FROM profiles WHERE venture_id=:venture_id"), parameters(query)).fetchone(),
                    lambda query: connection.execute(text("EXPLAIN (ANALYZE,BUFFERS) " + sql.text), query_parameters(query)).scalars().all(),
                    dict(deployment=name, dimensions=deployment["dimensions"], source=deployment["source"],
                        metered_receipt_ids=deployment.get("metered_receipt_ids", []),
                        provider_measurements=deployment.get("provider_measurements", "not measured")) if deployment else None)
                comparisons.append(result)
            transaction.rollback()
            return {**comparisons[0], "comparisons": comparisons}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--url")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    corpus = load_corpus(arguments.corpus)
    result = (postgres_bench(arguments.url, arguments.rows, arguments.repeats, corpus) if arguments.url else
        sqlite_bench(arguments.rows, arguments.repeats, corpus))
    rendered = json.dumps(result, indent=2)
    if arguments.output:
        arguments.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
