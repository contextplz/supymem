"""Supymem foundation tables + job/reservation extensions (doc 05 M1–M13).

Revision ID: s001supymem
Revises: e4f5a6b7c8d9

Logical sections M1–M13 are applied in one revision so SQLite tests and PG stay
on a single head. Downgrade drops the new tables and columns.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s001supymem"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_JOB_COLS = (
    sa.Column("workspace_id", sa.Text()),
    sa.Column("dedupe_key", sa.Text()),
    sa.Column("effect_key", sa.Text()),
    sa.Column("run_after", sa.DateTime()),
    sa.Column("locked_by", sa.Text()),
    sa.Column("locked_at", sa.DateTime()),
)

_RETENTION = (
    ("build_events", 30, "archive_s3"),
    ("agent_runtime_steps", 30, "archive_s3"),
    ("site_visits", 90, "delete"),
    ("app_usage_records", 400, "delete"),
    ("security_events", 400, "delete"),
)


def _foundation_tables():
    """Freeze build schema at s001; additive s006 owns managed-run receipts.

    This historical migration originally used live ORM metadata. Copy other
    tables without changing them, but do not create future build columns or
    receipts on a fresh installation before their additive migration runs.
    """
    from app.db.models import Base
    from app.db.supymem_models import SUPYMEM_TABLE_NAMES
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        if table.name not in {"apps", "build_runs", "build_events", "build_model_calls", "build_artifacts", "qa_runs"}:
            table.to_metadata(metadata)
    # s010 owns the imported-source app scope key. Do not create future ORM
    # constraints during a fresh s001 installation.
    sa.Table("apps", metadata,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False, index=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False, index=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("name", "kind", "state", "build_mode", "deploy_target")],
        sa.Column("infra_tier_id", sa.Text(), sa.ForeignKey("infra_tiers.id")),
        sa.Column("repo_id", sa.Text()), sa.Column("primary_domain_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
    )
    sa.Table("build_runs", metadata,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("app_id", sa.Text(), sa.ForeignKey("apps.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, index=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False, index=True),
        sa.Column("plan_task_id", sa.Text()),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("trigger", "agent", "state", "spec")],
        *[sa.Column(name, sa.Text()) for name in ("sandbox_ref", "reservation_id")],
        sa.Column("credits_used", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime()), sa.Column("finished_at", sa.DateTime()),
        *[sa.Column(name, sa.Text()) for name in ("exit_summary", "error_code", "artifact_id")],
    )
    sa.Table("build_events", metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("build_run_id", sa.Text(), sa.ForeignKey("build_runs.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False), sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False), sa.Column("payload", sa.Text(), nullable=False),
        sa.Index("ix_build_events_run_seq", "build_run_id", "seq"),
    )
    # s008 owns source-scoped independent QA. Do not pull future ORM FKs or
    # columns into s001 before s006 creates the referenced build scope key.
    sa.Table("build_artifacts", metadata,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("build_run_id", sa.Text(), sa.ForeignKey("build_runs.id"), nullable=False),
        sa.Column("app_id", sa.Text(), sa.ForeignKey("apps.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, index=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("kind", "s3_key", "sha256")],
        sa.Column("image_uri", sa.Text()), sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    sa.Table("qa_runs", metadata,
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("app_id", sa.Text(), sa.ForeignKey("apps.id"), nullable=False),
        sa.Column("build_run_id", sa.Text(), sa.ForeignKey("build_runs.id"), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False, index=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False, index=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ("state", "suites")],
        sa.Column("passed", sa.Integer(), nullable=False), sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("report_s3_key", sa.Text()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
    )
    return metadata, [t for t in metadata.sorted_tables if t.name in SUPYMEM_TABLE_NAMES]


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("jobs") as batch:
        for col in _NEW_JOB_COLS:
            batch.add_column(col)
        batch.create_index("ix_jobs_dedupe_key", ["dedupe_key"], unique=True)
    with op.batch_alter_table("credit_reservations") as batch:
        batch.add_column(sa.Column("parent_id", sa.Text()))

    metadata, tables = _foundation_tables()
    metadata.create_all(bind, tables=tables)

    op.bulk_insert(
        sa.table(
            "retention_policies",
            sa.column("table_name", sa.Text),
            sa.column("days", sa.Integer),
            sa.column("action", sa.Text),
        ),
        [{"table_name": t, "days": d, "action": a} for t, d, a in _RETENTION],
    )
    op.bulk_insert(
        sa.table(
            "infra_tiers",
            sa.column("id", sa.Text),
            sa.column("name", sa.Text),
            sa.column("cpu", sa.Float),
            sa.column("memory_mb", sa.Integer),
            sa.column("min_tasks", sa.Integer),
            sa.column("max_tasks", sa.Integer),
            sa.column("db_placement", sa.Text),
            sa.column("monthly_credits", sa.BigInteger),
            sa.column("description", sa.Text),
        ),
        [
            {
                "id": "static",
                "name": "static",
                "cpu": 0,
                "memory_mb": 0,
                "min_tasks": 0,
                "max_tasks": 0,
                "db_placement": "none",
                "monthly_credits": 30,
                "description": "Landing pages and static apps",
            },
            {
                "id": "micro",
                "name": "micro",
                "cpu": 0.25,
                "memory_mb": 512,
                "min_tasks": 0,
                "max_tasks": 2,
                "db_placement": "shared_cluster",
                "monthly_credits": 900,
                "description": "First users",
            },
            {
                "id": "small",
                "name": "small",
                "cpu": 0.5,
                "memory_mb": 1024,
                "min_tasks": 1,
                "max_tasks": 3,
                "db_placement": "shared_cluster",
                "monthly_credits": 2400,
                "description": "First paying customers",
            },
            {
                "id": "medium",
                "name": "medium",
                "cpu": 1,
                "memory_mb": 2048,
                "min_tasks": 2,
                "max_tasks": 6,
                "db_placement": "dedicated_instance",
                "monthly_credits": 9000,
                "description": "Growth",
            },
        ],
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_idea_sessions_one_active
            ON idea_sessions (venture_id)
            WHERE state NOT IN ('confirmed', 'abandoned')
            """
        )
        for table in (
            "idea_revisions",
            "build_events",
            "agent_runtime_steps",
            "app_usage_records",
        ):
            op.execute(
                f"""
                CREATE OR REPLACE FUNCTION {table}_append_only() RETURNS trigger AS $$
                BEGIN
                  RAISE EXCEPTION 'append_only';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            op.execute(
                f"""
                DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {table}_append_only();
                """
            )
        for table in ("apps", "deployments", "sites"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            op.execute(
                f"""
                DROP POLICY IF EXISTS tenant_isolation ON {table};
                CREATE POLICY tenant_isolation ON {table}
                USING (workspace_id = current_setting('app.workspace_id', true))
                """
            )


def downgrade() -> None:
    bind = op.get_bind()
    from app.db.supymem_models import SUPYMEM_TABLE_NAMES
    from app.db.models import Base

    tables = [t for t in reversed(Base.metadata.sorted_tables) if t.name in SUPYMEM_TABLE_NAMES]
    Base.metadata.drop_all(bind, tables=tables)
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_dedupe_key")
        for name in (
            "workspace_id",
            "dedupe_key",
            "effect_key",
            "run_after",
            "locked_by",
            "locked_at",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("credit_reservations") as batch:
        batch.drop_column("parent_id")
