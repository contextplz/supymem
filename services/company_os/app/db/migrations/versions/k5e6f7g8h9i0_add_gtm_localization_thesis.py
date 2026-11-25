"""GTM localization profiles, theses, experiments, outcomes and buying groups.

Revision ID: k5e6f7g8h9i0
Revises: j4d5e6f7g8h9
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k5e6f7g8h9i0"
down_revision: str | None = "j4d5e6f7g8h9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gtm_market_profiles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("seller_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("product_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("segment_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("constraints_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "version", name="uq_gtm_market_profile_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_gtm_market_profile_status",
        ),
    )
    op.create_index(
        "ix_gtm_market_profiles_workspace_id",
        "gtm_market_profiles",
        ["workspace_id"],
    )
    op.create_index(
        "ix_gtm_market_profiles_status",
        "gtm_market_profiles",
        ["workspace_id", "status"],
    )

    op.create_table(
        "gtm_theses",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column(
            "profile_id",
            sa.Text(),
            sa.ForeignKey("gtm_market_profiles.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("archetype_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("buying_profile_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("strategy_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("assumptions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("policy_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text()),
        sa.Column("approved_by", sa.Text()),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "profile_id", "version", name="uq_gtm_thesis_profile_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'rejected', 'superseded')",
            name="ck_gtm_thesis_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_gtm_thesis_confidence",
        ),
    )
    op.create_index("ix_gtm_theses_workspace_id", "gtm_theses", ["workspace_id"])
    op.create_index(
        "ix_gtm_theses_status", "gtm_theses", ["workspace_id", "status"]
    )

    op.create_table(
        "gtm_campaign_experiments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("thesis_id", sa.Text(), sa.ForeignKey("gtm_theses.id"), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("primary_metric", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("minimum_sample_size", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("variants_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("allocation_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("guardrails_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="planned"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("stopped_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('planned', 'running', 'paused', 'completed', 'cancelled')",
            name="ck_gtm_experiment_status",
        ),
    )
    op.create_index(
        "ix_gtm_campaign_experiments_workspace_id",
        "gtm_campaign_experiments",
        ["workspace_id"],
    )
    op.create_index(
        "ix_gtm_experiments_status",
        "gtm_campaign_experiments",
        ["workspace_id", "status"],
    )

    op.create_table(
        "gtm_outcomes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("thesis_id", sa.Text(), sa.ForeignKey("gtm_theses.id")),
        sa.Column(
            "experiment_id",
            sa.Text(),
            sa.ForeignKey("gtm_campaign_experiments.id"),
        ),
        sa.Column("contact_id", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text()),
        sa.Column("variant", sa.Text()),
        sa.Column("value", sa.Float()),
        sa.Column("currency", sa.Text()),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gtm_outcomes_workspace_id", "gtm_outcomes", ["workspace_id"])
    op.create_index(
        "ix_gtm_outcomes_experiment",
        "gtm_outcomes",
        ["workspace_id", "experiment_id", "occurred_at"],
    )

    op.create_table(
        "gtm_buying_groups",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.Text(), sa.ForeignKey("gtm_companies.id"), nullable=False),
        sa.Column("product_key", sa.Text(), nullable=False, server_default="default"),
        sa.Column("status", sa.Text(), nullable=False, server_default="forming"),
        sa.Column("completeness_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("engagement_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "company_id",
            "product_key",
            name="uq_gtm_buying_group_product",
        ),
    )
    op.create_index(
        "ix_gtm_buying_groups_workspace_id",
        "gtm_buying_groups",
        ["workspace_id"],
    )
    op.create_table(
        "gtm_buying_group_members",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "buying_group_id",
            sa.Text(),
            sa.ForeignKey("gtm_buying_groups.id"),
            nullable=False,
        ),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column("role_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "buying_group_id", "contact_id", name="uq_gtm_buying_group_member"
        ),
    )

    # Postgres production databases need the same append-only protection as create_all.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION gtm_outcomes_append_only() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'gtm_outcomes is append-only'; END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER gtm_outcomes_no_update
            BEFORE UPDATE ON gtm_outcomes
            FOR EACH ROW EXECUTE FUNCTION gtm_outcomes_append_only();
            """
        )
        op.execute(
            """
            CREATE TRIGGER gtm_outcomes_no_delete
            BEFORE DELETE ON gtm_outcomes
            FOR EACH ROW EXECUTE FUNCTION gtm_outcomes_append_only();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS gtm_outcomes_no_delete ON gtm_outcomes")
        op.execute("DROP TRIGGER IF EXISTS gtm_outcomes_no_update ON gtm_outcomes")
        op.execute("DROP FUNCTION IF EXISTS gtm_outcomes_append_only")
    op.drop_table("gtm_buying_group_members")
    op.drop_table("gtm_buying_groups")
    op.drop_table("gtm_outcomes")
    op.drop_table("gtm_campaign_experiments")
    op.drop_table("gtm_theses")
    op.drop_table("gtm_market_profiles")
