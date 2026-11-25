"""Add consumer account tables (Cognito identity, 14-day plan, open-app tokens).

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consumer_users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("cognito_sub", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Text()),
        sa.Column("credit_account_id", sa.Text()),
        sa.Column("plan", sa.Text(), nullable=False, server_default="trial"),
        sa.Column("plan_started_at", sa.DateTime(), nullable=False),
        sa.Column("plan_expires_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cognito_sub", name="uq_consumer_users_cognito_sub"),
        sa.CheckConstraint("plan IN ('trial', 'paid')", name="ck_consumer_users_plan"),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name="ck_consumer_users_status"
        ),
    )
    op.create_index("ix_consumer_users_email", "consumer_users", ["email"])

    op.create_table(
        "consumer_open_tokens",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_consumer_open_tokens_hash"),
    )
    op.create_index(
        "ix_consumer_open_tokens_user_id", "consumer_open_tokens", ["user_id"]
    )

    op.create_table(
        "consumer_usage_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("route", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credits_micro", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_consumer_usage_events_user_id", "consumer_usage_events", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_consumer_usage_events_user_id", table_name="consumer_usage_events")
    op.drop_table("consumer_usage_events")
    op.drop_index("ix_consumer_open_tokens_user_id", table_name="consumer_open_tokens")
    op.drop_table("consumer_open_tokens")
    op.drop_index("ix_consumer_users_email", table_name="consumer_users")
    op.drop_table("consumer_users")
