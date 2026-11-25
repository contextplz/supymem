"""Add onboarding, activity, provisioning, planning, and chat tables."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "bb8ea93e5a0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.create_table(
        "onboarding_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("phases_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("phases_total", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("paused_reason", sa.Text()),
        sa.Column("resume_token", sa.Text()),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("flags_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("research_founder", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_up_headline", sa.Text()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("venture_id", "started_at"),
    )
    op.create_index(
        "ix_onboarding_runs_venture", "onboarding_runs", ["venture_id", "started_at"]
    )

    # One active (running|paused) onboarding run per venture.
    if is_pg:
        op.execute(
            """
            CREATE UNIQUE INDEX uq_onboarding_active_per_venture
            ON onboarding_runs (venture_id)
            WHERE status IN ('running', 'paused')
            """
        )
    else:
        # SQLite partial unique index
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_onboarding_active_per_venture
            ON onboarding_runs (venture_id)
            WHERE status IN ('running', 'paused')
            """
        )

    op.create_table(
        "onboarding_steps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("onboarding_runs.id"), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text()),
        sa.Column("error_class", sa.Text()),
        sa.Column("error_detail", sa.Text()),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.UniqueConstraint("run_id", "step_id", "attempt"),
    )
    op.create_index("ix_onboarding_steps_run", "onboarding_steps", ["run_id", "started_at"])

    # activity_lines: SQLite AUTOINCREMENT vs Postgres BIGSERIAL
    if is_pg:
        op.execute(
            """
            CREATE TABLE activity_lines (
              seq BIGSERIAL PRIMARY KEY,
              venture_id TEXT NOT NULL,
              run_id TEXT,
              step_id TEXT,
              phase TEXT NOT NULL,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              text TEXT NOT NULL,
              detail_json TEXT,
              query TEXT,
              evidence_url TEXT,
              undo_json TEXT,
              cost_usd DOUBLE PRECISION,
              ts TIMESTAMP NOT NULL
            )
            """
        )
    else:
        op.execute(
            """
            CREATE TABLE activity_lines (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              venture_id TEXT NOT NULL,
              run_id TEXT,
              step_id TEXT,
              phase TEXT NOT NULL,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              text TEXT NOT NULL,
              detail_json TEXT,
              query TEXT,
              evidence_url TEXT,
              undo_json TEXT,
              cost_usd REAL,
              ts DATETIME NOT NULL
            )
            """
        )
    op.create_index("ix_activity_venture_seq", "activity_lines", ["venture_id", "seq"])

    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS activity_lines_no_update
            BEFORE UPDATE ON activity_lines BEGIN
              SELECT RAISE(ABORT, 'activity_lines is append-only');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS activity_lines_no_delete
            BEFORE DELETE ON activity_lines BEGIN
              SELECT RAISE(ABORT, 'activity_lines is append-only');
            END;
            """
        )
    elif is_pg:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION activity_lines_append_only() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'activity_lines is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER activity_lines_no_update
            BEFORE UPDATE ON activity_lines
            FOR EACH ROW EXECUTE FUNCTION activity_lines_append_only();
            """
        )
        op.execute(
            """
            CREATE TRIGGER activity_lines_no_delete
            BEFORE DELETE ON activity_lines
            FOR EACH ROW EXECUTE FUNCTION activity_lines_append_only();
            """
        )

    op.create_table(
        "venture_resources",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("simulated", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("display", sa.Text(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("detail_json", sa.Text()),
        sa.Column("secrets_json", sa.Text()),
        sa.Column("teardown_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venture_id", "resource"),
    )
    op.create_index("ix_venture_resources_venture", "venture_resources", ["venture_id"])

    op.create_table(
        "task_fingerprints",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("dismiss_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venture_id", "fingerprint"),
    )
    op.create_index("ix_task_fingerprints_venture", "task_fingerprints", ["venture_id"])

    op.create_table(
        "planned_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("why_now", sa.Text(), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("est_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("runnable", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.Text(), nullable=False, server_default="planner"),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fingerprint", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
    )
    op.create_index(
        "ix_planned_tasks_venture_status",
        "planned_tasks",
        ["venture_id", "status", "rank"],
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="text"),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("citations_json", sa.Text()),
        sa.Column("actions_json", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_chat_messages_venture", "chat_messages", ["venture_id", "created_at"]
    )

    op.create_table(
        "venture_documents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("path", sa.Text()),
        sa.Column("body_md", sa.Text()),
        sa.Column("provenance_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_venture_documents_venture", "venture_documents", ["venture_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS activity_lines_no_update")
        op.execute("DROP TRIGGER IF EXISTS activity_lines_no_delete")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS activity_lines_no_update ON activity_lines")
        op.execute("DROP TRIGGER IF EXISTS activity_lines_no_delete ON activity_lines")
        op.execute("DROP FUNCTION IF EXISTS activity_lines_append_only()")

    op.drop_index("ix_venture_documents_venture", table_name="venture_documents")
    op.drop_table("venture_documents")
    op.drop_index("ix_chat_messages_venture", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_planned_tasks_venture_status", table_name="planned_tasks")
    op.drop_table("planned_tasks")
    op.drop_index("ix_task_fingerprints_venture", table_name="task_fingerprints")
    op.drop_table("task_fingerprints")
    op.drop_index("ix_venture_resources_venture", table_name="venture_resources")
    op.drop_table("venture_resources")
    op.drop_index("ix_activity_venture_seq", table_name="activity_lines")
    op.drop_table("activity_lines")
    op.drop_index("ix_onboarding_steps_run", table_name="onboarding_steps")
    op.drop_table("onboarding_steps")
    op.execute("DROP INDEX IF EXISTS uq_onboarding_active_per_venture")
    op.drop_index("ix_onboarding_runs_venture", table_name="onboarding_runs")
    op.drop_table("onboarding_runs")
