"""Add Email-Supymem outreach tables and contact enrichment columns.

Revision ID: h2b3c4d5e6f7
Revises: g1a2b3c4d5e6
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h2b3c4d5e6f7"
down_revision: str | None = "g1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gtm_contacts", sa.Column("secondary_email", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("x_handle", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("enrichment_json", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("sequence_state_json", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("gmail_thread_id", sa.Text()))
    op.add_column("gtm_contacts", sa.Column("last_emailed_at", sa.DateTime()))
    op.add_column(
        "gtm_contacts",
        sa.Column("times_emailed", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("gtm_contacts", sa.Column("replied_at", sa.DateTime()))
    op.create_index(
        "ix_gtm_contacts_linkedin",
        "gtm_contacts",
        ["workspace_id", "linkedin_url"],
    )

    op.create_table(
        "gtm_email_accounts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("app_user_id", sa.Text(), nullable=False, server_default="local-user"),
        sa.Column("provider", sa.Text(), nullable=False, server_default="gmail"),
        sa.Column("email", sa.Text()),
        sa.Column("composio_user_id", sa.Text(), nullable=False),
        sa.Column("connected_account_id", sa.Text(), nullable=False),
        sa.Column("daily_send_limit", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("sent_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count_date", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("connected_account_id", name="uq_gtm_email_account_ca"),
    )
    op.create_index(
        "ix_gtm_email_accounts_ws", "gtm_email_accounts", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_gtm_email_accounts_workspace_id", "gtm_email_accounts", ["workspace_id"]
    )

    op.create_table(
        "gtm_email_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id")),
        sa.Column("email_account_id", sa.Text(), sa.ForeignKey("gtm_email_accounts.id")),
        sa.Column("sequence_day", sa.Integer()),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), unique=True),
        sa.Column("metadata_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_gtm_email_events_contact",
        "gtm_email_events",
        ["contact_id", "created_at"],
    )

    op.create_table(
        "gtm_email_send_queue",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("contact_id", sa.Text(), sa.ForeignKey("gtm_contacts.id"), nullable=False),
        sa.Column(
            "email_account_id",
            sa.Text(),
            sa.ForeignKey("gtm_email_accounts.id"),
            nullable=False,
        ),
        sa.Column("sequence_day", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("locked_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_gtm_send_idempotency"),
        sa.UniqueConstraint("contact_id", "sequence_day", name="uq_gtm_send_contact_day"),
        sa.CheckConstraint(
            "sequence_day IN (1, 2, 4, 7)", name="ck_gtm_send_sequence_day"
        ),
    )
    op.create_index(
        "ix_gtm_send_queue_due", "gtm_email_send_queue", ["status", "scheduled_for"]
    )


def downgrade() -> None:
    op.drop_index("ix_gtm_send_queue_due", table_name="gtm_email_send_queue")
    op.drop_table("gtm_email_send_queue")
    op.drop_index("ix_gtm_email_events_contact", table_name="gtm_email_events")
    op.drop_table("gtm_email_events")
    op.drop_index("ix_gtm_email_accounts_workspace_id", table_name="gtm_email_accounts")
    op.drop_index("ix_gtm_email_accounts_ws", table_name="gtm_email_accounts")
    op.drop_table("gtm_email_accounts")
    op.drop_index("ix_gtm_contacts_linkedin", table_name="gtm_contacts")
    op.drop_column("gtm_contacts", "replied_at")
    op.drop_column("gtm_contacts", "times_emailed")
    op.drop_column("gtm_contacts", "last_emailed_at")
    op.drop_column("gtm_contacts", "gmail_thread_id")
    op.drop_column("gtm_contacts", "sequence_state_json")
    op.drop_column("gtm_contacts", "enrichment_json")
    op.drop_column("gtm_contacts", "x_handle")
    op.drop_column("gtm_contacts", "secondary_email")
