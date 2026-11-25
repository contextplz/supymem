"""Add GTM relationship engine tables (docs 115–117).

Revision ID: g1a2b3c4d5e6
Revises: f6a7b8c9d0e1
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g1a2b3c4d5e6"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gtm_companies",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text()),
        sa.Column("yc_batch", sa.Text()),
        sa.Column("accelerator", sa.Text()),
        sa.Column("stage_signal", sa.Text()),
        sa.Column("funding_usd", sa.Float()),
        sa.Column("hq_country", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("source_urls_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "domain", name="uq_gtm_company_domain"),
    )
    op.create_index("ix_gtm_companies_workspace_id", "gtm_companies", ["workspace_id"])
    op.create_index(
        "ix_gtm_companies_ws_name", "gtm_companies", ["workspace_id", "canonical_name"]
    )

    op.create_table(
        "gtm_contacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.Text(), sa.ForeignKey("gtm_companies.id")),
        sa.Column("ops_prospect_id", sa.Text(), sa.ForeignKey("ops_prospects.id")),
        sa.Column("email", sa.Text()),
        sa.Column("email_confidence", sa.Text(), nullable=False, server_default="none"),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("role", sa.Text()),
        sa.Column("linkedin_url", sa.Text()),
        sa.Column("timezone", sa.Text()),
        sa.Column("lawful_basis", sa.Text(), nullable=False, server_default="none"),
        sa.Column("lawful_basis_note", sa.Text()),
        sa.Column("lawful_basis_at", sa.DateTime()),
        sa.Column(
            "relationship_stage", sa.Text(), nullable=False, server_default="new"
        ),
        sa.Column("stage_changed_at", sa.DateTime(), nullable=False),
        sa.Column("owner_mailbox_id", sa.Text()),
        sa.Column("unsubscribed_at", sa.DateTime()),
        sa.Column("do_not_contact", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.Text()),
        sa.Column("dossier_hook", sa.Text()),
        sa.Column("dossier_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "email", name="uq_gtm_contact_email"),
        sa.CheckConstraint(
            "email_confidence IN ('safe', 'risky', 'guess', 'none')",
            name="ck_gtm_contact_email_confidence",
        ),
        sa.CheckConstraint(
            "lawful_basis IN ('legitimate_interest', 'consent', 'none')",
            name="ck_gtm_contact_lawful_basis",
        ),
    )
    op.create_index("ix_gtm_contacts_workspace_id", "gtm_contacts", ["workspace_id"])
    op.create_index(
        "ix_gtm_contacts_stage", "gtm_contacts", ["workspace_id", "relationship_stage"]
    )

    op.create_table(
        "gtm_signals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.Text(), sa.ForeignKey("gtm_companies.id")),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("evidence_url", sa.Text()),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("icp_score", sa.Float()),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "dedup_key", name="uq_gtm_signal_dedup"),
    )
    op.create_index("ix_gtm_signals_workspace_id", "gtm_signals", ["workspace_id"])
    op.create_index(
        "ix_gtm_signals_kind", "gtm_signals", ["workspace_id", "kind", "observed_at"]
    )

    op.create_table(
        "gtm_mailboxes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("sending_domain", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="smtp"),
        sa.Column("smtp_host", sa.Text()),
        sa.Column("smtp_port", sa.Integer()),
        sa.Column("imap_host", sa.Text()),
        sa.Column("imap_port", sa.Integer()),
        sa.Column("secret_ref", sa.Text()),
        sa.Column("warmup_started_at", sa.DateTime()),
        sa.Column("warmup_day_cap", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("hard_daily_cap", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("sent_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounces_7d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complaints_7d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("health", sa.Text(), nullable=False, server_default="warming"),
        sa.Column("last_imap_uid", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "address", name="uq_gtm_mailbox_address"),
        sa.CheckConstraint(
            "health IN ('warming', 'healthy', 'paused', 'burnt')",
            name="ck_gtm_mailbox_health",
        ),
        sa.CheckConstraint(
            "provider IN ('gworkspace', 'm365', 'smtp')",
            name="ck_gtm_mailbox_provider",
        ),
    )
    op.create_index("ix_gtm_mailboxes_workspace_id", "gtm_mailboxes", ["workspace_id"])

    op.create_table(
        "gtm_sending_domains",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("spf_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dkim_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dmarc_policy", sa.Text()),
        sa.Column("verified_at", sa.DateTime()),
        sa.Column("first_send_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "domain", name="uq_gtm_sending_domain"),
    )
    op.create_index(
        "ix_gtm_sending_domains_workspace_id", "gtm_sending_domains", ["workspace_id"]
    )

    op.create_table(
        "gtm_sequences",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("track", sa.Text(), nullable=False, server_default="relationship"),
        sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("armed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("armed_by", sa.Text()),
        sa.Column("armed_at", sa.DateTime()),
        sa.Column("approval_id", sa.Text()),
        sa.Column("hypothesis", sa.Text()),
        sa.Column("metric", sa.Text()),
        sa.Column("threshold", sa.Float()),
        sa.Column("window_days", sa.Integer()),
        sa.Column("product_name", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gtm_sequences_workspace_id", "gtm_sequences", ["workspace_id"])

    op.create_table(
        "gtm_enrollments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column(
            "sequence_id", sa.Text(), sa.ForeignKey("gtm_sequences.id"), nullable=False
        ),
        sa.Column("step_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_at", sa.DateTime()),
        sa.Column("mailbox_id", sa.Text(), sa.ForeignKey("gtm_mailboxes.id")),
        sa.Column("thread_root_message_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("cooldown_until", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'replied', 'converted', "
            "'cooled', 'unsubscribed', 'bounced')",
            name="ck_gtm_enrollment_status",
        ),
    )
    op.create_index(
        "ix_gtm_enrollments_next", "gtm_enrollments", ["status", "next_at"]
    )

    op.create_table(
        "gtm_touches",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("enrollment_id", sa.Text(), sa.ForeignKey("gtm_enrollments.id")),
        sa.Column(
            "contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False
        ),
        sa.Column("mailbox_id", sa.Text(), sa.ForeignKey("gtm_mailboxes.id")),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default="email"),
        sa.Column("message_id", sa.Text()),
        sa.Column("in_reply_to", sa.Text()),
        sa.Column("subject", sa.Text()),
        sa.Column("body_text", sa.Text()),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("classification", sa.Text()),
        sa.Column("approval_id", sa.Text()),
        sa.Column("ops_email_log_id", sa.Text(), sa.ForeignKey("ops_email_logs.id")),
        sa.Column("simulated", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('out', 'in')", name="ck_gtm_touch_direction"
        ),
    )
    op.create_index(
        "ix_gtm_touches_contact", "gtm_touches", ["contact_id", "sent_at"]
    )

    op.create_table(
        "gtm_attribution_links",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id")),
        sa.Column("campaign_id", sa.Text()),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_click_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("gtm_attribution_links")
    op.drop_table("gtm_touches")
    op.drop_table("gtm_enrollments")
    op.drop_table("gtm_sequences")
    op.drop_table("gtm_sending_domains")
    op.drop_table("gtm_mailboxes")
    op.drop_table("gtm_signals")
    op.drop_table("gtm_contacts")
    op.drop_table("gtm_companies")
