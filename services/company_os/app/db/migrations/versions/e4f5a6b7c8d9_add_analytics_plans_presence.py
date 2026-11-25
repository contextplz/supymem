"""Analytics, plan subscriptions, and presence (doc 112).

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8

Postgres-safe (AWS RDS) and SQLite-safe. No vendor SQL.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("venture_id", sa.Text()),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("props_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_analytics_events_person_id", "analytics_events", ["person_id"])
    op.create_index("ix_analytics_events_workspace_id", "analytics_events", ["workspace_id"])
    op.create_index("ix_analytics_events_venture_id", "analytics_events", ["venture_id"])
    op.create_index("ix_analytics_events_event_name", "analytics_events", ["event_name"])
    op.create_index("ix_analytics_events_occurred_at", "analytics_events", ["occurred_at"])

    op.create_table(
        "analytics_daily_rollup",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("dau", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wau", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mau", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signups", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_ventures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_paid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "plan_subscriptions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("person_id", sa.Text()),
        sa.Column("workspace_id", sa.Text()),
        sa.Column("tier", sa.Text(), nullable=False, server_default="free"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("current_period_end", sa.DateTime()),
        sa.Column("trial_ends_at", sa.DateTime()),
        sa.Column("provider", sa.Text()),
        sa.Column("provider_customer_id", sa.Text()),
        sa.Column("provider_subscription_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("seats >= 1", name="ck_plan_subscriptions_seats"),
        sa.CheckConstraint("tier IN ('free', 'pro', 'team')", name="ck_plan_subscriptions_tier"),
        sa.CheckConstraint(
            "status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')",
            name="ck_plan_subscriptions_status",
        ),
    )
    op.create_index(
        "ix_plan_subscriptions_workspace", "plan_subscriptions", ["workspace_id"]
    )
    op.create_index("ix_plan_subscriptions_person_id", "plan_subscriptions", ["person_id"])

    op.create_table(
        "presence_heartbeats",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text()),
        sa.Column("surface", sa.Text()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "person_id", name="uq_presence_workspace_person"
        ),
    )
    op.create_index(
        "ix_presence_workspace_seen",
        "presence_heartbeats",
        ["workspace_id", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_table("presence_heartbeats")
    op.drop_table("plan_subscriptions")
    op.drop_table("analytics_daily_rollup")
    op.drop_table("analytics_events")
