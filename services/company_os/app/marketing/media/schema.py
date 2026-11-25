"""Idempotent marketing media schema for create_all / SQLite local DBs."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.db.models import Base

_NEW_KINDS = (
    "generate_image",
    "generate_video",
    "operate_app",
    "operate_browser",
    "create_ad",
    "enable_ad",
    "ingest_asset",
    "enqueue_outreach",
    "send_email_touch",
)

_KIND_CHECK_SQL = (
    "'create_content', 'create_visual', 'publish_content', "
    "'find_threads', 'draft_reply', 'publish_reply', "
    "'research_topic', 'check_result', "
    "'generate_image', 'generate_video', 'operate_app', "
    "'operate_browser', 'create_ad', 'enable_ad', 'ingest_asset', "
    "'enqueue_outreach', 'send_email_touch'"
)


def ensure_marketing_media_schema(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    wanted = [
        Base.metadata.tables["marketing_assets"],
        Base.metadata.tables["marketing_learning_events"],
    ]
    Base.metadata.create_all(engine, tables=wanted)

    if "marketing_jobs" not in tables:
        return
    dialect = engine.dialect.name
    if dialect != "sqlite":
        return
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='marketing_jobs'")
        ).fetchone()
        sql = (row[0] if row else "") or ""
        if all(kind in sql for kind in _NEW_KINDS):
            return
        if "ck_marketing_job_kind" not in sql and "kind IN" not in sql:
            return
        _rebuild_sqlite_jobs(conn)


def _rebuild_sqlite_jobs(conn) -> None:
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(
        text(
            f"""
            CREATE TABLE marketing_jobs_media (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                platform TEXT,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{{}}',
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 50,
                scheduled_for DATETIME,
                started_at DATETIME,
                finished_at DATETIME,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                executor TEXT,
                result_json TEXT NOT NULL DEFAULT '{{}}',
                proof_json TEXT NOT NULL DEFAULT '{{}}',
                block_reason TEXT,
                error_message TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(plan_id) REFERENCES marketing_plans(id),
                CHECK (status IN ('pending', 'ready', 'running', 'done', 'failed', 'blocked', 'cancelled')),
                CHECK (kind IN ({_KIND_CHECK_SQL}))
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO marketing_jobs_media (
                id, workspace_id, plan_id, kind, platform, title,
                payload_json, depends_on_json, status, priority, scheduled_for,
                started_at, finished_at, attempt_count, executor, result_json,
                proof_json, block_reason, error_message, created_at, updated_at
            )
            SELECT
                id, workspace_id, plan_id, kind, platform, title,
                payload_json, depends_on_json, status, priority, scheduled_for,
                started_at, finished_at, attempt_count, executor, result_json,
                proof_json, block_reason, error_message, created_at, updated_at
            FROM marketing_jobs
            """
        )
    )
    conn.execute(text("DROP TABLE marketing_jobs"))
    conn.execute(text("ALTER TABLE marketing_jobs_media RENAME TO marketing_jobs"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_marketing_jobs_due "
            "ON marketing_jobs (workspace_id, status, scheduled_for)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_marketing_jobs_plan "
            "ON marketing_jobs (plan_id, status)"
        )
    )
    conn.execute(text("PRAGMA foreign_keys=ON"))
