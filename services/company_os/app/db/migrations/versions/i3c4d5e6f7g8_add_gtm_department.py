"""AI GTM department: research jobs, LinkedIn queue, run/stop, learning.

Revision ID: i3c4d5e6f7g8
Revises: h2b3c4d5e6f7
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i3c4d5e6f7g8"
down_revision: str | None = "h2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gtm_contacts",
        sa.Column("preferred_channel", sa.Text(), nullable=False, server_default="auto"),
    )
    op.add_column(
        "gtm_contacts",
        sa.Column("sequence_kind", sa.Text(), nullable=False, server_default="cold"),
    )
    op.add_column(
        "gtm_contacts",
        sa.Column("enrichment_level", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("gtm_contacts", sa.Column("linkedin_stage", sa.Text()))
    op.add_column(
        "gtm_contacts",
        sa.Column("times_linkedin", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("gtm_contacts", sa.Column("last_linkedin_at", sa.DateTime()))
    op.add_column("gtm_contacts", sa.Column("bounced_at", sa.DateTime()))

    op.create_table(
        "gtm_engine_state",
        sa.Column("workspace_id", sa.Text(), primary_key=True),
        sa.Column("discovery_enabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("send_enabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("icp_query", sa.Text(), nullable=False, server_default="B2B SaaS founders"),
        sa.Column("find_batch_size", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("ready_email_buffer", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("ready_linkedin_buffer", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("week_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_discovery_at", sa.DateTime()),
        sa.Column("last_learn_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "gtm_research_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="enrich"),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("sequence_day", sa.Integer()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gtm_research_due", "gtm_research_jobs", ["status", "scheduled_for"])
    op.create_index("ix_gtm_research_jobs_workspace_id", "gtm_research_jobs", ["workspace_id"])
    op.create_table(
        "gtm_research_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column("sequence_day", sa.Integer()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("richness", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_gtm_snapshot_contact", "gtm_research_snapshots", ["contact_id", "created_at"]
    )
    op.create_table(
        "gtm_linkedin_queue",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("body", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("provider_message_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("contact_id", "step", name="uq_gtm_li_contact_step"),
    )
    op.create_index("ix_gtm_li_due", "gtm_linkedin_queue", ["status", "scheduled_for"])
    op.create_index("ix_gtm_linkedin_queue_workspace_id", "gtm_linkedin_queue", ["workspace_id"])
    op.create_table(
        "gtm_learning_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("contact_id", sa.Text()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text()),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gtm_learning_events_workspace_id", "gtm_learning_events", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("gtm_learning_events")
    op.drop_table("gtm_linkedin_queue")
    op.drop_table("gtm_research_snapshots")
    op.drop_table("gtm_research_jobs")
    op.drop_table("gtm_engine_state")
    for col in (
        "bounced_at",
        "last_linkedin_at",
        "times_linkedin",
        "linkedin_stage",
        "enrichment_level",
        "sequence_kind",
        "preferred_channel",
    ):
        op.drop_column("gtm_contacts", col)
