"""Phase 4 outreach tables: edges, interviews, pilots, evidence, public docs.

Revision ID: r2n3o4p5q6r7
Revises: q1l2m3n4o5p6
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r2n3o4p5q6r7"
down_revision: str | None = "q1l2m3n4o5p6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "gtm_evidence_sources" not in tables:
        op.create_table(
            "gtm_evidence_sources",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("provider", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="deferred"),
            sa.Column("connected_account_id", sa.Text()),
            sa.Column("deferred_at", sa.DateTime()),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("connected_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "provider", name="uq_gtm_evidence_provider"),
        )
        op.create_index(
            "ix_gtm_evidence_sources_workspace_id",
            "gtm_evidence_sources",
            ["workspace_id"],
        )

    if "gtm_network_edges" not in tables:
        op.create_table(
            "gtm_network_edges",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("from_contact_id", sa.Text(), nullable=False),
            sa.Column("to_contact_id", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("domain", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_gtm_network_edges_ws",
            "gtm_network_edges",
            ["workspace_id", "from_contact_id"],
        )

    if "gtm_interviews" not in tables:
        op.create_table(
            "gtm_interviews",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("contact_id", sa.Text(), nullable=False),
            sa.Column("company_id", sa.Text()),
            sa.Column("kind", sa.Text(), nullable=False, server_default="first_meeting"),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("duration_min", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("script_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("notes_md", sa.Text(), nullable=False, server_default=""),
            sa.Column("extraction_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("mom_test_ok", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_step", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_gtm_interviews_contact",
            "gtm_interviews",
            ["workspace_id", "contact_id"],
        )

    if "gtm_pilots" not in tables:
        op.create_table(
            "gtm_pilots",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("company_id", sa.Text()),
            sa.Column("contact_id", sa.Text(), nullable=False),
            sa.Column("use_case", sa.Text(), nullable=False),
            sa.Column("team", sa.Text()),
            sa.Column("baseline_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("success_criterion", sa.Text(), nullable=False),
            sa.Column("duration_days", sa.Integer(), nullable=False, server_default="14"),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("ends_at", sa.DateTime()),
            sa.Column("paid_decision_at", sa.DateTime()),
            sa.Column("permission_to_demo_at", sa.DateTime()),
            sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_gtm_pilots_ws", "gtm_pilots", ["workspace_id", "status"])

    if "gtm_public_documents" not in tables:
        op.create_table(
            "gtm_public_documents",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text()),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("title", sa.Text()),
            sa.Column("evidence_url", sa.Text()),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("body_hash", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_gtm_public_docs_kind",
            "gtm_public_documents",
            ["workspace_id", "kind"],
        )

    if "gtm_paste_bodies" not in tables:
        op.create_table(
            "gtm_paste_bodies",
            sa.Column("signal_id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("raw_text", sa.Text()),
            sa.Column("raw_text_sha256", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("nulled_at", sa.DateTime()),
        )
        op.create_index(
            "ix_gtm_paste_bodies_workspace_id",
            "gtm_paste_bodies",
            ["workspace_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    for name in (
        "gtm_paste_bodies",
        "gtm_public_documents",
        "gtm_pilots",
        "gtm_interviews",
        "gtm_network_edges",
        "gtm_evidence_sources",
    ):
        if name in tables:
            op.drop_table(name)
