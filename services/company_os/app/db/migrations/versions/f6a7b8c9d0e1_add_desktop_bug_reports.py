"""Add desktop bug / crash report table.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desktop_bug_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("app_version", sa.Text(), nullable=False),
        sa.Column("os_name", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("stack_text", sa.Text()),
        sa.Column("logs_text", sa.Text()),
        sa.Column("contact", sa.Text()),
        sa.Column("account_id", sa.Text()),
        sa.Column("anonymous", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("install_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('crash', 'user')", name="ck_desktop_bug_reports_kind"),
        sa.CheckConstraint(
            "status IN ('new', 'ack', 'closed')", name="ck_desktop_bug_reports_status"
        ),
    )
    op.create_index("ix_desktop_bug_reports_kind", "desktop_bug_reports", ["kind"])
    op.create_index("ix_desktop_bug_reports_status", "desktop_bug_reports", ["status"])
    op.create_index(
        "ix_desktop_bug_reports_received_at", "desktop_bug_reports", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_bug_reports_received_at", table_name="desktop_bug_reports")
    op.drop_index("ix_desktop_bug_reports_status", table_name="desktop_bug_reports")
    op.drop_index("ix_desktop_bug_reports_kind", table_name="desktop_bug_reports")
    op.drop_table("desktop_bug_reports")
