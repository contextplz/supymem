"""Add Stage S5 money ledger and durable agent live status.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "money_ledger_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("debit_account", sa.Text(), nullable=False),
        sa.Column("credit_account", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text()),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source", sa.Text(), nullable=False, server_default="internal"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venture_id", "ref", name="uq_money_ledger_venture_ref"),
    )
    op.create_index("ix_money_ledger_venture_created", "money_ledger_entries", ["venture_id", "created_at"])
    op.create_table(
        "venture_revenue_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("earned_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_revenue_snapshot_venture", "venture_revenue_snapshots", ["venture_id", "created_at"])
    op.create_table(
        "agent_live_status",
        sa.Column("agent_id", sa.Text(), sa.ForeignKey("agent_instances.id"), primary_key=True),
        sa.Column("venture_id", sa.Text(), sa.ForeignKey("ventures.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("current_task_desc", sa.Text()),
        sa.Column("last_heartbeat", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_live_venture", "agent_live_status", ["venture_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_agent_live_venture", table_name="agent_live_status")
    op.drop_table("agent_live_status")
    op.drop_index("ix_revenue_snapshot_venture", table_name="venture_revenue_snapshots")
    op.drop_table("venture_revenue_snapshots")
    op.drop_index("ix_money_ledger_venture_created", table_name="money_ledger_entries")
    op.drop_table("money_ledger_entries")
