# Memory retrieval measurements

You can compare SQLite full-text retrieval with PostgreSQL full-text and hybrid
vector retrieval on the **same facts, queries, owner scopes and limits**. This
does not change your backend or enable production memory search.

The checked-in corpus contains equivalent B2B equipment-maintenance and consumer
music-practice notes. It includes keyword and paraphrase queries and two vector
sets with different dimensions. These vectors describe expected relevance, not
the quality, latency or price of any embedding provider. Scaling repeats these
notes in isolated tenant partitions; it does not produce independent real notes.
Use a reviewed, de-identified corpus when you need representative capacity data.

## Run without provider calls

Use your existing Python environment with SQLAlchemy and a PostgreSQL driver.
The PostgreSQL database must be loopback, with pgvector already enabled. The
script does not install extensions, create a database or connect to providers.
It only creates temporary tables and rolls them back, including on failure.
Connection query options are refused because they can override the target host.

```bash
export PYTHONPATH="$PWD/packages:$PWD/services/company_os"
python scripts/bench/memory_bench.py --rows 12 --repeats 20 --output /tmp/memory-sqlite.json
python scripts/bench/memory_bench.py --url "$ISOLATED_POSTGRES_URL" --rows 12 --repeats 20 --output /tmp/memory-postgres.json
```

Start with the smoke size. After reviewing resource limits, use identical row
and repeat counts for larger measurements. Keep both result files. Compare only
matching `corpus_sha256`, `rows`, `repeats`, `candidates` and `limit`. Search p95
is pooled across individual query timings, not the duration of a query batch.
Profile payloads contain only their own domain's notes. Queries are warm-cache
measurements after one untimed retrieval. Plans use `EXPLAIN ANALYZE, BUFFERS` on
PostgreSQL, and SQLite reports its own query plans. No index choice is forced.

## Compare your configured embedding providers

Copy `fixtures/memory_corpus.json`, preserving the record IDs, query IDs and
domain ownership. For each already configured deployment, supply embeddings for
**every** record and query under `deployments`. Set `source` to `provider_capture`,
declare `dimensions`, and attach a nonempty `metered_receipt_ids` list from the
existing metered embedding path. Use `--corpus /path/to/reviewed-corpus.json` for
both backends. Do not put credentials or private customer notes in the corpus.

Optional `provider_measurements` may contain the measured latency, tokens, cost
and failures from those receipts. They are supplied evidence, not measurements
made by this script. Missing measurements stay `not measured`. Retrieval latency
does not include embedding latency. The equivalent fixture has no provider-cost
claim. All vector sets use cosine distance and the production bounded, scoped
fusion query, with an HNSW index for each dimension. Inspect both recall and
plans before concluding that one deployment is faster or better.

## Evidence still required

No PostgreSQL or real-provider benchmark receipt is checked in as a passing
capacity result. Run the PostgreSQL regression with `TEST_POSTGRES_URL` in your
approved isolated environment, then save matching result files. Actual provider
captures require the existing consent, tenant and metering paths and separate
authorization. This script never makes those calls for you.
