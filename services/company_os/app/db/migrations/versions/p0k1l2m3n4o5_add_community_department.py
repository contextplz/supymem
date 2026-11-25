"""Add Community Department tables (plans, servers, people, lab logs).

Revision ID: p0k1l2m3n4o5
Revises: o9j0k1l2m3n4
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p0k1l2m3n4o5"
down_revision: str | None = "o9j0k1l2m3n4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "community_plans" not in insp.get_table_names():
        op.create_table(
            "community_plans",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("venture_id", sa.Text()),
            sa.Column("research_bundle_id", sa.Text()),
            sa.Column(
                "schema_version",
                sa.Text(),
                nullable=False,
                server_default="community.plan.v1",
            ),
            sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
            sa.Column("verdict", sa.Text(), nullable=False, server_default=""),
            sa.Column("thesis_score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("thesis_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("objectives_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("identity_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("program_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("work_orders_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('draft', 'founder_review', 'approved', 'executing', "
                "'rescore_due', 'archived')",
                name="ck_community_plan_status",
            ),
        )
        op.create_index(
            "ix_community_plans_workspace",
            "community_plans",
            ["workspace_id", "status"],
        )
        op.create_index(
            "ix_community_plans_workspace_id",
            "community_plans",
            ["workspace_id"],
        )

    if "community_servers" not in insp.get_table_names():
        op.create_table(
            "community_servers",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("plan_id", sa.Text()),
            sa.Column("guild_id", sa.Text(), nullable=False),
            sa.Column("guild_name", sa.Text()),
            sa.Column("connected_account_id", sa.Text()),
            sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
            sa.Column("provision_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "workspace_id", "guild_id", name="uq_community_server_guild"
            ),
        )
        op.create_index(
            "ix_community_servers_workspace",
            "community_servers",
            ["workspace_id", "status"],
        )
        op.create_index(
            "ix_community_servers_workspace_id",
            "community_servers",
            ["workspace_id"],
        )

    if "community_people" not in insp.get_table_names():
        op.create_table(
            "community_people",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("server_id", sa.Text()),
            sa.Column("discord_user_id", sa.Text(), nullable=False),
            sa.Column("display_name", sa.Text()),
            sa.Column("stage", sa.Text(), nullable=False, server_default="member"),
            sa.Column("gtm_contact_id", sa.Text()),
            sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "workspace_id",
                "discord_user_id",
                name="uq_community_person_discord",
            ),
        )
        op.create_index(
            "ix_community_people_stage",
            "community_people",
            ["workspace_id", "stage"],
        )
        op.create_index(
            "ix_community_people_workspace_id",
            "community_people",
            ["workspace_id"],
        )

    if "community_lab_logs" not in insp.get_table_names():
        op.create_table(
            "community_lab_logs",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text()),
            sa.Column("level", sa.Text(), nullable=False, server_default="info"),
            sa.Column("action", sa.Text(), nullable=False, server_default=""),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_community_lab_logs_workspace_id",
            "community_lab_logs",
            ["workspace_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table in (
        "community_lab_logs",
        "community_people",
        "community_servers",
        "community_plans",
    ):
        if table in insp.get_table_names():
            op.drop_table(table)
