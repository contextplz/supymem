"""Marketing plan + jobs spine; content post visual columns.

Revision ID: m7g8h9i0j1k2
Revises: l6f7g8h9i0j1
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m7g8h9i0j1k2"
down_revision: str | None = "l6f7g8h9i0j1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "marketing_plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("brief_id", sa.Text()),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("strategy_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("constraints_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source", sa.Text(), nullable=False, server_default="strategist"),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'completed', 'archived')",
            name="ck_marketing_plan_status",
        ),
    )
    op.create_index(
        "ix_marketing_plans_workspace",
        "marketing_plans",
        ["workspace_id", "status"],
    )

    op.create_table(
        "marketing_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("marketing_plans.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("depends_on_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("scheduled_for", sa.DateTime()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("executor", sa.Text()),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("proof_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("block_reason", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'done', 'failed', 'blocked', 'cancelled')",
            name="ck_marketing_job_status",
        ),
        sa.CheckConstraint(
            "kind IN ("
            "'create_content', 'create_visual', 'publish_content', "
            "'find_threads', 'draft_reply', 'publish_reply', "
            "'research_topic', 'check_result'"
            ")",
            name="ck_marketing_job_kind",
        ),
    )
    op.create_index(
        "ix_marketing_jobs_due",
        "marketing_jobs",
        ["workspace_id", "status", "scheduled_for"],
    )
    op.create_index(
        "ix_marketing_jobs_plan",
        "marketing_jobs",
        ["plan_id", "status"],
    )

    op.add_column(
        "content_posts",
        sa.Column("visual_json", sa.Text(), server_default="{}"),
    )
    op.add_column(
        "content_posts",
        sa.Column("visual_path", sa.Text()),
    )


def downgrade() -> None:
    op.drop_column("content_posts", "visual_path")
    op.drop_column("content_posts", "visual_json")
    op.drop_index("ix_marketing_jobs_plan", table_name="marketing_jobs")
    op.drop_index("ix_marketing_jobs_due", table_name="marketing_jobs")
    op.drop_table("marketing_jobs")
    op.drop_index("ix_marketing_plans_workspace", table_name="marketing_plans")
    op.drop_table("marketing_plans")
