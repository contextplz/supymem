"""Add durable paid-campaign proposals and provider campaign records.

Revision ID: s4t5u6v7w8x9
Revises: r3p4q5r6s7t8
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s4t5u6v7w8x9"
down_revision: str | None = "r3p4q5r6s7t8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "marketing_ad_proposals" not in tables:
        op.create_table(
            "marketing_ad_proposals",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("platform", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("idea", sa.Text(), nullable=False),
            sa.Column("blueprint_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("approval_id", sa.Text(), unique=True),
            sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint("platform IN ('google', 'meta')", name="ck_marketing_ad_proposals_platform"),
            sa.CheckConstraint("status IN ('draft', 'pending_approval', 'approved', 'created', 'blocked', 'rejected')", name="ck_marketing_ad_proposals_status"),
        )
        op.create_index("ix_marketing_ad_proposals_workspace_id", "marketing_ad_proposals", ["workspace_id"])
        op.create_index("ix_marketing_ad_proposals_queue", "marketing_ad_proposals", ["workspace_id", "status", "created_at"])
    if "marketing_ad_campaigns" not in tables:
        op.create_table(
            "marketing_ad_campaigns",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("proposal_id", sa.Text(), sa.ForeignKey("marketing_ad_proposals.id"), nullable=False, unique=True),
            sa.Column("platform", sa.Text(), nullable=False),
            sa.Column("external_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("daily_budget_usd", sa.Float(), nullable=False),
            sa.Column("provider_response_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("platform", "external_id", name="uq_marketing_ad_provider_id"),
            sa.CheckConstraint("platform IN ('google', 'meta')", name="ck_marketing_ad_campaigns_platform"),
            sa.CheckConstraint("status IN ('PAUSED', 'ACTIVE', 'FAILED', 'REMOVED')", name="ck_marketing_ad_campaigns_status"),
        )
        op.create_index("ix_marketing_ad_campaigns_workspace_id", "marketing_ad_campaigns", ["workspace_id"])
        op.create_index("ix_marketing_ad_campaigns_workspace", "marketing_ad_campaigns", ["workspace_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "marketing_ad_campaigns" in tables:
        op.drop_table("marketing_ad_campaigns")
    if "marketing_ad_proposals" in tables:
        op.drop_table("marketing_ad_proposals")
