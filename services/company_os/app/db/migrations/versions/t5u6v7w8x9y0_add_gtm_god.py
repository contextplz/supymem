"""AI God GTM plans, specialist tasks, and collaboration bus.

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t5u6v7w8x9y0"
down_revision: str | None = "s4t5u6v7w8x9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "gtm_god_plans" not in tables:
        op.create_table(
            "gtm_god_plans",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("venture_id", sa.Text()),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("idea", sa.Text(), nullable=False, server_default=""),
            sa.Column("problem", sa.Text(), nullable=False, server_default=""),
            sa.Column("brief_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("diagnosis_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("artifacts_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("log_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('diagnosing', 'executing', 'waiting_human', 'paused', 'done')",
                name="ck_gtm_god_plan_status",
            ),
        )
        op.create_index("ix_gtm_god_plans_ws", "gtm_god_plans", ["workspace_id", "status"])
    if "gtm_god_tasks" not in tables:
        op.create_table(
            "gtm_god_tasks",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("plan_id", sa.Text(), nullable=False),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("owner_agent", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("horizon", sa.Text(), nullable=False, server_default="now"),
            sa.Column("depends_on_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("output_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("human_instruction", sa.Text()),
            sa.Column("evidence_tag", sa.Text(), nullable=False, server_default="assumption"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('queued', 'running', 'blocked_human', 'done', 'skipped', 'failed')",
                name="ck_gtm_god_task_status",
            ),
            sa.CheckConstraint(
                "horizon IN ('now', 'next', 'later')",
                name="ck_gtm_god_task_horizon",
            ),
            sa.CheckConstraint(
                "evidence_tag IN ('assumption', 'validated')",
                name="ck_gtm_god_task_evidence",
            ),
        )
        op.create_index("ix_gtm_god_tasks_plan", "gtm_god_tasks", ["plan_id", "status"])
    if "gtm_god_messages" not in tables:
        op.create_table(
            "gtm_god_messages",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("plan_id", sa.Text(), nullable=False),
            sa.Column("task_id", sa.Text()),
            sa.Column("from_agent", sa.Text(), nullable=False),
            sa.Column("to_agent", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_gtm_god_messages_plan", "gtm_god_messages", ["plan_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "gtm_god_messages" in tables:
        op.drop_table("gtm_god_messages")
    if "gtm_god_tasks" in tables:
        op.drop_table("gtm_god_tasks")
    if "gtm_god_plans" in tables:
        op.drop_table("gtm_god_plans")
