"""Additive, opt-in memory search and transaction-local profile invalidation."""
import logging

import sqlalchemy as sa
from alembic import op

revision = "s058memsearch"
down_revision = "s057inboxbrief"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.execute(sa.text("""CREATE TABLE IF NOT EXISTS venture_facts (
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        venture_id TEXT NOT NULL REFERENCES ventures(id), person_id TEXT NOT NULL,
        subject TEXT NOT NULL, predicate TEXT NOT NULL, value TEXT NOT NULL,
        value_json TEXT NOT NULL, source_json TEXT NOT NULL, source_digest TEXT NOT NULL,
        dedupe_key TEXT NOT NULL, confidence FLOAT NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        valid_from TIMESTAMP NOT NULL, valid_to TIMESTAMP, recorded_at TIMESTAMP NOT NULL,
        superseded_at TIMESTAMP, forgotten_at TIMESTAMP,
        CONSTRAINT uq_venture_fact_source UNIQUE (workspace_id, venture_id, person_id, dedupe_key),
        CHECK (valid_to IS NULL OR valid_to >= valid_from),
        CHECK (superseded_at IS NULL OR superseded_at >= recorded_at),
        CHECK ((valid_to IS NULL) = (superseded_at IS NULL)))"""))
    connection.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS uq_venture_fact_current ON venture_facts "
        "(workspace_id,venture_id,person_id,subject,predicate) WHERE valid_to IS NULL AND forgotten_at IS NULL"))
    connection.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_venture_fact_history ON venture_facts "
        "(workspace_id,venture_id,person_id,subject,predicate,valid_from)"))
    connection.execute(sa.text("""CREATE TABLE IF NOT EXISTS memory_core_profiles (
        workspace_id TEXT NOT NULL, venture_id TEXT NOT NULL, person_id TEXT NOT NULL,
        generation BIGINT NOT NULL DEFAULT 1, refreshed_generation BIGINT NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (workspace_id,venture_id,person_id))"""))
    connection.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_memory_profile_venture ON memory_core_profiles (venture_id)"))
    connection.execute(sa.text("""CREATE TABLE IF NOT EXISTS memory_search_jobs (
        workspace_id TEXT NOT NULL, venture_id TEXT NOT NULL, person_id TEXT NOT NULL,
        request_hash TEXT NOT NULL DEFAULT '', query_text TEXT NOT NULL DEFAULT '',
        generation BIGINT NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0,
        run_after TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        lease_until TIMESTAMP, lease_token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace_id,venture_id,person_id,request_hash))"""))
    connection.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_memory_search_jobs_due ON memory_search_jobs (run_after)"))
    connection.execute(sa.text("""CREATE TABLE IF NOT EXISTS memory_embedding_cache (
        workspace_id TEXT NOT NULL, venture_id TEXT NOT NULL, deployment TEXT NOT NULL,
        content_hash TEXT NOT NULL, embedding_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id,venture_id,deployment,content_hash))"""))
    if connection.dialect.name != "postgresql":
        return
    vector = False
    try:
        with connection.begin_nested():
            connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        vector = True
    except sa.exc.DBAPIError:
        logging.getLogger(__name__).warning("memory_search_vector_unavailable_fulltext_only")
    indexes = []
    for table, expression in (
        ("venture_facts", "subject || ' ' || value"),
        ("institutional_memories", "coalesce(record_json::jsonb->>'subject','') || ' ' || "
            "coalesce(record_json::jsonb->>'statement','') || ' ' || coalesce(record_json::jsonb->>'detail','')"),
    ):
        if table == "venture_facts":
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS search_document tsvector "
                f"GENERATED ALWAYS AS (to_tsvector('english'::regconfig, {expression})) STORED"))
            indexes.append(f"ix_{table}_search ON {table} USING gin(search_document)")
        else:
            indexes.append(f"ix_memory_record_fulltext ON {table} "
                f"USING gin(to_tsvector('english'::regconfig, {expression}))")
        if vector:
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector(1536)"))
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding_hash TEXT"))
            connection.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding_deployment TEXT"))
            indexes.append(f"ix_{table}_embedding ON {table} "
                "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL")
    indexes.extend(("ix_memory_fact_recent ON venture_facts "
        "(workspace_id,venture_id,person_id,valid_from DESC,id) WHERE valid_to IS NULL AND forgotten_at IS NULL",
        "ix_memory_fact_latest ON venture_facts "
        "(workspace_id,venture_id,person_id,subject,predicate,valid_from DESC,recorded_at DESC,id DESC)",
        "ix_memory_record_scope ON institutional_memories "
        "(workspace_id,venture_id,((record_json::jsonb)->>'author'),id)"))
    connection.execute(sa.text("ALTER TABLE institutional_memories ADD COLUMN IF NOT EXISTS report_subjects TEXT[] NOT NULL DEFAULT '{}'"))
    connection.execute(sa.text("ALTER TABLE institutional_memories ADD COLUMN IF NOT EXISTS report_at TIMESTAMPTZ"))
    connection.execute(sa.text("ALTER TABLE institutional_memories ADD COLUMN IF NOT EXISTS report_subjects_ready BOOLEAN NOT NULL DEFAULT FALSE"))
    indexes.extend(("ix_memory_report_subjects ON institutional_memories USING gin(report_subjects)",
        "ix_memory_reports_pending ON institutional_memories "
        "(workspace_id,venture_id,((record_json::jsonb)->>'author'),id) WHERE NOT report_subjects_ready"))
    reset_embedding = "NEW.embedding := NULL; NEW.embedding_hash := NULL; NEW.embedding_deployment := NULL;" if vector else ""
    connection.execute(sa.text(f"""CREATE OR REPLACE FUNCTION memory_search_reset() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_TABLE_NAME='institutional_memories' THEN NEW.report_subjects_ready := FALSE; END IF;
          {reset_embedding}
          RETURN NEW;
        END $$"""))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS trg_memory_search_reset ON institutional_memories"))
    connection.execute(sa.text("CREATE TRIGGER trg_memory_search_reset BEFORE UPDATE OF record_json ON institutional_memories "
        "FOR EACH ROW EXECUTE FUNCTION memory_search_reset()"))
    connection.execute(sa.text("DROP TRIGGER IF EXISTS trg_memory_search_reset ON venture_facts"))
    connection.execute(sa.text("CREATE TRIGGER trg_memory_search_reset BEFORE UPDATE OF value,subject,source_json ON venture_facts "
        "FOR EACH ROW EXECUTE FUNCTION memory_search_reset()"))
    connection.execute(sa.text("""CREATE OR REPLACE FUNCTION memory_search_invalidate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE workspace TEXT; venture TEXT; person TEXT; active BOOLEAN;
        BEGIN
          active := EXISTS (SELECT 1 FROM credit_policies WHERE key='feature_flags'
              AND value_json::jsonb->'memory_search_v2' = 'true'::jsonb);
          IF TG_TABLE_NAME = 'venture_facts' THEN
            IF TG_OP = 'DELETE' THEN
              workspace := OLD.workspace_id; venture := OLD.venture_id; person := OLD.person_id;
            ELSE
              workspace := NEW.workspace_id; venture := NEW.venture_id; person := NEW.person_id;
            END IF;
          ELSIF TG_TABLE_NAME = 'institutional_memories' THEN
            IF TG_OP = 'DELETE' THEN
              workspace := OLD.workspace_id; venture := OLD.venture_id; person := OLD.record_json::jsonb->>'author';
            ELSE
              workspace := NEW.workspace_id; venture := NEW.venture_id; person := NEW.record_json::jsonb->>'author';
            END IF;
            IF (CASE WHEN TG_OP='DELETE' THEN OLD.record_json ELSE NEW.record_json END)::jsonb->>'kind'='outcome' THEN
              SELECT owner_id INTO person FROM workspaces WHERE id=workspace;
            END IF;
          ELSE
            IF TG_OP = 'DELETE' THEN venture := OLD.venture_id; ELSE venture := NEW.venture_id; END IF;
            UPDATE memory_core_profiles SET generation=generation+1 WHERE venture_id=venture;
            IF active THEN
              INSERT INTO memory_search_jobs (workspace_id,venture_id,person_id)
                SELECT workspace_id,venture_id,person_id FROM memory_core_profiles WHERE venture_id=venture
                ON CONFLICT (workspace_id,venture_id,person_id,request_hash)
                DO UPDATE SET generation=memory_search_jobs.generation+1,run_after=CURRENT_TIMESTAMP,attempts=0;
            END IF;
            RETURN NULL;
          END IF;
          IF workspace IS NOT NULL AND venture IS NOT NULL AND person IS NOT NULL THEN
            IF NOT active THEN
              UPDATE memory_core_profiles SET generation=generation+1
                WHERE workspace_id=workspace AND venture_id=venture AND person_id=person;
              RETURN NULL;
            END IF;
            INSERT INTO memory_core_profiles (workspace_id,venture_id,person_id) VALUES (workspace,venture,person)
              ON CONFLICT (workspace_id,venture_id,person_id) DO UPDATE SET generation=memory_core_profiles.generation+1;
            INSERT INTO memory_search_jobs (workspace_id,venture_id,person_id) VALUES (workspace,venture,person)
              ON CONFLICT (workspace_id,venture_id,person_id,request_hash)
              DO UPDATE SET generation=memory_search_jobs.generation+1,run_after=CURRENT_TIMESTAMP,attempts=0;
          END IF;
          RETURN NULL;
        END $$"""))
    for table, events in (("venture_facts", "INSERT OR UPDATE OF value,subject,predicate,valid_to,forgotten_at,source_json OR DELETE"),
                          ("institutional_memories", "INSERT OR UPDATE OF record_json OR DELETE"),
                          ("chat_messages", "UPDATE OR DELETE"), ("chat_conversations", "UPDATE OR DELETE"),
                          ("idea_revisions", "INSERT OR UPDATE OR DELETE"),
                          ("clarifier_turns", "INSERT OR UPDATE OR DELETE")):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_memory_search ON {table}"))
        connection.execute(sa.text(f"CREATE TRIGGER trg_memory_search AFTER {events} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION memory_search_invalidate()"))
    with op.get_context().autocommit_block():
        for index in indexes:
            name = index.split()[0]
            if connection.scalar(sa.text("SELECT NOT indisvalid FROM pg_index "
                    "WHERE indexrelid=to_regclass(:name)"), {"name": name}):
                connection.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            connection.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index}"))


def downgrade():
    connection = op.get_bind()
    tables = ("memory_core_profiles", "memory_search_jobs", "memory_embedding_cache", "venture_facts")
    sources = ("institutional_memories", "chat_messages", "chat_conversations", "idea_revisions", "clarifier_turns")
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE " + ", ".join((*tables, *sources)) + " IN ACCESS EXCLUSIVE MODE"))
    if any(connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")) for table in tables):
        raise RuntimeError("memory_search_history_prevents_downgrade")
    if connection.dialect.name == "postgresql":
        for table in ("venture_facts", *sources):
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_memory_search ON {table}"))
        connection.execute(sa.text("DROP TRIGGER IF EXISTS trg_memory_search_reset ON institutional_memories"))
        connection.execute(sa.text("DROP TRIGGER IF EXISTS trg_memory_search_reset ON venture_facts"))
        connection.execute(sa.text("DROP FUNCTION IF EXISTS memory_search_reset()"))
        connection.execute(sa.text("DROP FUNCTION IF EXISTS memory_search_invalidate()"))
        for column in ("search_document", "embedding", "embedding_hash", "embedding_deployment",
                       "report_subjects", "report_at", "report_subjects_ready"):
            connection.execute(sa.text(f"ALTER TABLE institutional_memories DROP COLUMN IF EXISTS {column}"))
        connection.execute(sa.text("DROP INDEX IF EXISTS ix_memory_record_scope"))
        connection.execute(sa.text("DROP INDEX IF EXISTS ix_memory_record_fulltext"))
    for table in tables:
        connection.execute(sa.text(f"DROP TABLE {table}"))
